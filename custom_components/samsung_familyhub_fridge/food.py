"""Samsung Food / KICS "food circles": client, image cache and coordinator."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_FOOD_UPDATE_INTERVAL,
    DOMAIN,
    KICS_CLIENT_ID,
    KICS_FOODLIST_ENDPOINT,
    KICS_SA_AUTH_URL,
    WHISK_FOODLIST_API,
)
from .rotator import RotatorClient
from .views import generate_food_circle_sig

_LOGGER = logging.getLogger(__name__)

KICS_TOKEN_TTL = 22 * 3600
WHISK_TOKEN_TTL = 27 * 86400
KICS_ATTEMPTS = 3
KICS_RETRY_DELAY = 3
KICS_RETRY_STATUSES = (502, 503, 504, 524)
KICS_TIMEOUT = aiohttp.ClientTimeout(total=30)
WHISK_TIMEOUT = aiohttp.ClientTimeout(total=10)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=15)
IMAGE_CONCURRENCY = 4
# KICS tokens are ~26-32 characters; legacy Whisk tokens are longer.
WHISK_MIN_TOKEN_LENGTH = 35
MIN_IMAGE_BYTES = 100
TIMESTAMP_MS_THRESHOLD = 10_000_000_000
IMAGE_URL_PATH = "/api/samsung_familyhub/food_circles"
EMPTY_INVENTORY: dict[str, Any] = {"total_items": 0, "last_synced": None, "items": []}


def split_bearer(token: str) -> tuple[str, str]:
    """(Authorization header value, raw token) for a token with or without a prefix."""
    raw = token.strip().removeprefix("Bearer ").removeprefix("Token ").strip()
    return f"Bearer {raw}", raw


def _seconds(raw: Any) -> int:
    """Epoch seconds from an epoch-seconds or epoch-milliseconds value (0 if unknown)."""
    text = str(raw or 0)
    if not text.isdigit():
        return 0
    value = int(text)
    return value // 1000 if value > TIMESTAMP_MS_THRESHOLD else value


class FoodImageCache:
    """Local copies of the food circle crops, served by the signed image view.

    The methods touch the file system, so call them from an executor job.
    """

    def __init__(self, directory: Path, view_secret: str) -> None:
        self.directory = directory
        self._secret = view_secret

    @staticmethod
    def filename(item_id: str, remote_url: str) -> str:
        id_hash = hashlib.md5(item_id.encode(), usedforsecurity=False).hexdigest()[:10]
        url_hash = hashlib.md5(remote_url.split("?")[0].encode(), usedforsecurity=False).hexdigest()[:8]
        return f"{id_hash}_{url_hash}.jpg"

    def url(self, filename: str) -> str:
        sig = generate_food_circle_sig(self._secret, filename) if self._secret else ""
        return f"{IMAGE_URL_PATH}/{filename}" + (f"?sig={sig}" if sig else "")

    def ensure_directory(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.debug("Could not create the food circles cache directory: %s", err)
            return False
        return True

    def cached_names(self) -> set[str]:
        """Names of the non-empty cached images."""
        try:
            return {p.name for p in self.directory.glob("*.jpg") if p.stat().st_size > 0}
        except OSError:
            return set()

    def store(self, downloaded: dict[str, bytes], keep: set[str], inventory: dict[str, Any] | None) -> None:
        """Write new images, delete images no longer in the inventory, save the inventory."""
        for name, content in downloaded.items():
            try:
                (self.directory / name).write_bytes(content)
                _LOGGER.debug("Cached live food circle: %s", name)
            except OSError as err:
                _LOGGER.debug("Could not cache %s: %s", name, err)
        for path in self.directory.glob("*.jpg"):
            if path.name not in keep and path.name not in downloaded:
                try:
                    path.unlink()
                    _LOGGER.debug("Pruned stale food circle: %s", path.name)
                except OSError as err:
                    _LOGGER.debug("Could not prune %s: %s", path.name, err)
        if inventory is not None:
            try:
                (self.directory / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
            except OSError as err:
                _LOGGER.debug("Could not persist inventory to disk: %s", err)

    def load_inventory(self) -> dict[str, Any] | None:
        try:
            data = json.loads((self.directory / "inventory.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("items") else None


class SamsungFoodClient:
    """Fetches the fridge's food inventory from KICS EPA (or legacy Whisk)."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        rotator: RotatorClient,
        *,
        cache_dir: Path,
        view_secret: str = "",
        token: str | None = None,
        location_id: str | None = None,
        location_lookup: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self.hass = hass
        self._session = session
        self._rotator = rotator
        self._images = FoodImageCache(cache_dir, view_secret)
        self._configured_token = token
        self._location_id = location_id
        self._location_lookup = location_lookup
        self._cached_inventory: dict[str, Any] | None = None
        self._cached_kics_token: str | None = None
        self._kics_expires_at = 0.0
        self._cached_whisk_token: str | None = None
        self._whisk_expires_at = 0.0
        # Last-known tokens/location survive restarts, so a briefly unreachable rotator
        # doesn't leave the sensor without credentials.
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.token_cache")
        self._persisted: dict[str, Any] = {}

    # --- token / location cache ------------------------------------------------

    def prime_from_rotator(self, startup: dict[str, Any]) -> None:
        """Seed tokens from the rotator's /tokens document fetched at startup."""
        now = time.time()
        if kics := str(startup.get("kics_token") or "").strip():
            self._cached_kics_token = kics
            self._kics_expires_at = float(startup.get("kics_next_rotation_at") or now + KICS_TOKEN_TTL)
        if whisk := str(startup.get("samsung_food_token") or "").strip():
            self._cached_whisk_token = whisk
            self._whisk_expires_at = float(startup.get("food_next_rotation_at") or now + WHISK_TOKEN_TTL)
        location = str(startup.get("location_id") or "").strip()
        if location and not location.startswith("YOUR_") and not self._location_id:
            self._location_id = location

    async def async_restore_cache(self) -> None:
        """Restore unexpired tokens and the location ID saved by a previous run."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        self._persisted = dict(data)
        now = time.time()
        for token_key, expiry_key, token_attr, expiry_attr in (
            ("kics_token", "kics_expires_at", "_cached_kics_token", "_kics_expires_at"),
            ("whisk_token", "whisk_expires_at", "_cached_whisk_token", "_whisk_expires_at"),
        ):
            expires = float(data.get(expiry_key) or 0.0)
            if data.get(token_key) and not getattr(self, token_attr) and (not expires or expires > now):
                setattr(self, token_attr, data[token_key])
                setattr(self, expiry_attr, expires)
        if data.get("location_id") and not self._location_id:
            self._location_id = data["location_id"]

    async def _async_persist_cache(self) -> None:
        """Save tokens/location if they changed."""
        snapshot = {
            "kics_token": self._cached_kics_token,
            "kics_expires_at": self._kics_expires_at,
            "whisk_token": self._cached_whisk_token,
            "whisk_expires_at": self._whisk_expires_at,
            "location_id": self._location_id,
        }
        if snapshot != self._persisted:
            await self._store.async_save(snapshot)
            self._persisted = snapshot

    def _kics_token_valid(self, now: float) -> bool:
        return bool(self._cached_kics_token) and (not self._kics_expires_at or now < self._kics_expires_at)

    async def _async_rotator_token(self) -> str | None:
        """Fetch the KICS (or, failing that, Food) token from the rotator."""
        now = time.time()
        for path in ("/kics_token", "/food_token"):
            if found := await self._rotator.fetch_token(path):
                _LOGGER.info("SamsungFoodClient: loaded active token from the rotator (%s)", path)
                self._cached_kics_token = found.token
                self._kics_expires_at = found.next_rotation_at or now + KICS_TOKEN_TTL
                return found.token
        return None

    async def async_get_token(self) -> str | None:
        """The configured token, a cached rotator token, or a fresh one from the rotator."""
        if self._configured_token and self._configured_token.strip():
            return self._configured_token.strip()
        if self._kics_token_valid(time.time()):
            return self._cached_kics_token
        return await self._async_rotator_token()

    async def async_has_token(self) -> bool:
        return bool(await self.async_get_token())

    async def _async_location_id(self) -> str:
        """The fridge's SmartThings location ID.

        Order: configured override / cached value, the SmartThings device record, then the rotator.
        """
        if self._location_id and self._location_id.strip():
            return self._location_id.strip()
        if self._location_lookup is not None:
            try:
                location = await self._location_lookup()
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
                _LOGGER.debug("Could not read locationId from SmartThings: %s", err)
            else:
                if location:
                    self._location_id = location.strip()
                    return self._location_id
        payload = await self._rotator.get_json("/location_id") or {}
        location = str(payload.get("location_id") or "").strip()
        if location and not location.startswith("YOUR_"):
            self._location_id = location
            return location
        return ""

    # --- fetching ----------------------------------------------------------------

    async def _async_load_cached_inventory(self) -> None:
        if (data := await self.hass.async_add_executor_job(self._images.load_inventory)) is not None:
            self._cached_inventory = data
            _LOGGER.info("SamsungFoodClient: restored %d cached food items from disk", len(data["items"]))

    @staticmethod
    def _whisk_headers(token: str) -> dict[str, str]:
        auth, raw = split_bearer(token)
        return {"Authorization": auth, "x-whisk-token": raw, "Accept": "application/json"}

    async def _async_whisk_stock_map(self) -> dict[str, str]:
        """Map item ID / lower-cased name to a Whisk stock photo, for items without a crop."""
        now = time.time()
        token = None
        if self._cached_whisk_token and (not self._whisk_expires_at or now < self._whisk_expires_at):
            token = self._cached_whisk_token
        elif found := await self._rotator.fetch_token("/food_token"):
            token = self._cached_whisk_token = found.token
            self._whisk_expires_at = found.next_rotation_at or now + WHISK_TOKEN_TTL
        if not token or len(token) < WHISK_MIN_TOKEN_LENGTH:
            return {}

        stock: dict[str, str] = {}
        try:
            async with self._session.get(
                WHISK_FOODLIST_API, headers=self._whisk_headers(token), timeout=WHISK_TIMEOUT
            ) as response:
                if response.ok:
                    payload = await response.json(content_type=None)
                    for item in payload.get("items", []):
                        content = item.get("content", {})
                        photo = content.get("image_url") or content.get("photo_url")
                        if not photo:
                            continue
                        if item.get("id"):
                            stock[item["id"]] = photo
                        if name := (content.get("name") or "").lower():
                            stock[name] = photo
        except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as err:
            _LOGGER.debug("Could not build Whisk stock photo map: %s", err)
        return stock

    async def _async_download_images(self, wanted: dict[str, str]) -> dict[str, bytes]:
        """Download crops (filename -> remote URL) a few at a time."""
        semaphore = asyncio.Semaphore(IMAGE_CONCURRENCY)

        async def fetch(name: str, url: str) -> tuple[str, bytes | None]:
            async with semaphore:
                try:
                    async with self._session.get(url, timeout=IMAGE_TIMEOUT) as response:
                        content = await response.read() if response.ok else b""
                except (aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.debug("Could not cache food circle image: %s", err)
                    return name, None
            return name, content if len(content) > MIN_IMAGE_BYTES else None

        results = await asyncio.gather(*(fetch(name, url) for name, url in wanted.items()))
        return {name: content for name, content in results if content}

    async def _async_parse_kics_items(
        self, items: list[dict[str, Any]], stock: dict[str, str]
    ) -> tuple[list[dict[str, Any]], dict[str, bytes], set[str]]:
        """Turn KICS items into inventory entries, caching crops locally and signing their URLs.

        Returns (entries, newly downloaded images, filenames to keep). The last two are empty
        when the cache directory is unusable, in which case entries keep their remote URLs.
        """
        run = self.hass.async_add_executor_job
        caching = await run(self._images.ensure_directory)
        existing = await run(self._images.cached_names) if caching else set()

        entries: list[tuple[dict[str, Any], str | None]] = []
        to_download: dict[str, str] = {}
        keep: set[str] = set()
        for item in items:
            content = item.get("content", {})
            item_id = item.get("id", "")
            name = content.get("name") or "Unnamed Food Item"
            remote = content.get("imageUrl") or stock.get(item_id) or stock.get(name.lower())
            filename = FoodImageCache.filename(item_id, remote) if remote and caching else None
            if filename:
                keep.add(filename)
                if filename not in existing:
                    to_download[filename] = remote
            added = (
                content.get("addedAt") or content.get("createdAt") or content.get("added_at")
                or content.get("created_at") or content.get("createdTime")
            )
            entries.append((
                {
                    "name": name,
                    "image_url": remote,
                    "added_at": _seconds(added),
                    "expiration_date": content.get("expirationDate") or content.get("expiration_date") or None,
                },
                filename,
            ))

        downloaded = await self._async_download_images(to_download) if to_download else {}
        available = existing | set(downloaded)
        parsed = []
        for entry, filename in entries:
            if filename and filename in available:
                entry["image_url"] = self._images.url(filename)
            parsed.append(entry)
        return parsed, (downloaded if caching else {}), (keep & available if caching else set())

    def _result(self, items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        result = {
            "total_items": len(items),
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "items": items,
            **extra,
        }
        self._cached_inventory = result
        return result

    async def _async_fetch_kics(self, token: str, location_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """(inventory, error) from the KICS EPA food list, with retries for gateway errors."""
        auth, _raw = split_bearer(token)
        headers = {
            "authorization": auth,
            "kics-sa-auth-url": KICS_SA_AUTH_URL,
            "kics-sa-app-id": KICS_CLIENT_ID,
            "kics-location": location_id,
            "kics-client-platform": "android",
            "kics-client-version": "KS.ST.2.11-20",
            "accept": "application/json, text/plain, */*",
            "user-agent": "Mozilla/5.0 (Linux; Android 17; sdk_gphone16k_x86_64) AppleWebKit/537.36",
        }
        error = None
        for attempt in range(1, KICS_ATTEMPTS + 1):
            try:
                async with self._session.get(KICS_FOODLIST_ENDPOINT, headers=headers, timeout=KICS_TIMEOUT) as response:
                    status = response.status
                    payload = await response.json(content_type=None) if response.ok else None
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                error = str(err) or type(err).__name__
                if attempt < KICS_ATTEMPTS:
                    _LOGGER.info("KICS EPA fetch error on attempt %d (%s); retrying", attempt, error)
                    await asyncio.sleep(KICS_RETRY_DELAY)
                    continue
                _LOGGER.warning("KICS EPA fetch error: %s", error)
                break
            if payload is not None:
                items = payload.get("foodData", {}).get("items", [])
                parsed, downloaded, keep = await self._async_parse_kics_items(
                    items, await self._async_whisk_stock_map()
                )
                _LOGGER.info("SamsungFoodClient: retrieved %d live food circle items from KICS EPA", len(parsed))
                result = self._result(parsed, source="kics_epa_live_circles")
                await self.hass.async_add_executor_job(self._images.store, downloaded, keep, result)
                return result, None
            error = f"HTTP {status}"
            if status in (401, 403):
                _LOGGER.warning("KICS token was rejected (%d); clearing the cached token", status)
                self._cached_kics_token = None
                self._kics_expires_at = 0.0
            elif status in KICS_RETRY_STATUSES and attempt < KICS_ATTEMPTS:
                _LOGGER.info("KICS EPA returned HTTP %d on attempt %d; retrying", status, attempt)
                await asyncio.sleep(KICS_RETRY_DELAY)
                continue
            _LOGGER.warning("KICS EPA response: %s", error)
            break
        return None, error

    async def _async_fetch_whisk(self, token: str) -> dict[str, Any]:
        """Legacy path: page through the Whisk food list."""
        headers = self._whisk_headers(token)
        all_items: list[dict[str, Any]] = []
        cursor = ""
        error = None
        for page in range(1, 1000):
            params = {"paging.cursors.after": cursor} if cursor else {}
            try:
                async with self._session.get(
                    WHISK_FOODLIST_API, headers=headers, params=params, timeout=KICS_TIMEOUT
                ) as response:
                    if not response.ok:
                        _LOGGER.warning("Samsung Food API page %d returned HTTP %s", page, response.status)
                        error = f"HTTP {response.status}"
                        break
                    data = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.warning("Error fetching Samsung Food items on page %d (%s)", page, err)
                error = str(err) or type(err).__name__
                break
            items = data.get("items", [])
            all_items.extend(items)
            next_cursor = data.get("paging", {}).get("cursors", {}).get("after")
            if not (next_cursor and next_cursor != cursor and items):
                break
            cursor = next_cursor

        if error:
            if self._cached_inventory and self._cached_inventory.get("items"):
                _LOGGER.info("SamsungFoodClient: transient error (%s); keeping the cached inventory", error)
                return self._cached_inventory
            if not all_items:
                raise UpdateFailed(f"Failed to fetch Samsung Food items: {error}")

        active = []
        for item in all_items:
            content = item.get("content", {})
            status = content.get("presence_status", "")
            consumed = bool(content.get("consumed_at")) or bool(content.get("deleted_at")) or status == "PRESENCE_STATUS_CONSUMED"
            if not consumed or status in ("PRESENCE_STATUS_EXISTING", "PRESENCE_STATUS_PRESENT"):
                active.append({
                    "name": content.get("name") or "Unnamed Food Item",
                    "added_at": content.get("added_at") or 0,
                    "expiration_date": content.get("expiration_date") or content.get("days_to_expire") or None,
                    "image_url": content.get("image_url") or content.get("photo_url") or None,
                })
        _LOGGER.info("SamsungFoodClient: %d active items (of %d in the account)", len(active), len(all_items))
        return self._result(active)

    async def _async_fetch_food_items(self, token: str) -> dict[str, Any]:
        """KICS EPA live circles first; legacy Whisk only for long Whisk-style tokens."""
        if self._cached_inventory is None:
            await self._async_load_cached_inventory()

        location_id = await self._async_location_id()
        if not location_id:
            raise UpdateFailed(
                "KICS EPA food circles need a location ID. Set your fridge's Location ID under "
                "Settings → Devices & Services → Samsung FamilyHub → Configure."
            )
        inventory, error = await self._async_fetch_kics(token, location_id)
        if inventory is not None:
            return inventory

        _auth, raw = split_bearer(token)
        if len(raw) < WHISK_MIN_TOKEN_LENGTH:  # a KICS token would just fail on Whisk with 401
            if self._cached_inventory and self._cached_inventory.get("items"):
                _LOGGER.info("SamsungFoodClient: keeping %d cached items after a KICS EPA error", self._cached_inventory.get("total_items", 0))
                return self._cached_inventory
            raise UpdateFailed(f"KICS EPA food circles fetch failed: {error or 'unknown error'}")
        return await self._async_fetch_whisk(token)

    async def async_get_active_food_items(self) -> dict[str, Any]:
        token = await self.async_get_token()
        if not token:
            if self._cached_inventory is None:
                await self._async_load_cached_inventory()
            return self._cached_inventory or dict(EMPTY_INVENTORY)
        try:
            return await self._async_fetch_food_items(token)
        finally:
            await self._async_persist_cache()


class SamsungFoodCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the food inventory and refreshes shortly after the door closes."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: Any,
        client: SamsungFoodClient,
        update_interval_seconds: int = DEFAULT_FOOD_UPDATE_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Samsung Food inventory",
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self.client = client
        self._delay_task: asyncio.Task[None] | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.async_get_active_food_items()
        except UpdateFailed:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
            raise UpdateFailed(f"Failed to fetch Samsung Food inventory: {err}") from err

    def async_trigger_door_close_sync(self, delay_seconds: int = 12) -> None:
        """Refresh after a short delay so the fridge's AI vision can commit its results."""
        if self._delay_task and not self._delay_task.done():
            self._delay_task.cancel()

        async def _delayed_refresh() -> None:
            _LOGGER.debug("Door close detected; waiting %ds for AI vision to commit", delay_seconds)
            await asyncio.sleep(delay_seconds)
            await self.async_request_refresh()

        self._delay_task = self.hass.async_create_task(_delayed_refresh())

    async def async_shutdown(self) -> None:
        if self._delay_task and not self._delay_task.done():
            self._delay_task.cancel()
        await super().async_shutdown()
