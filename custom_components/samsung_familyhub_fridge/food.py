"""Samsung Food / KICS "food circles": client, image cache and coordinator."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
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
    """Local copies of the food circle crops, served by the signed image view."""

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

    def fetch(self, session: requests.Session, item_id: str, remote_url: str) -> tuple[str, str] | None:
        """Download (if not already cached) and return (filename, local URL)."""
        name = self.filename(item_id, remote_url)
        path = self.directory / name
        if not path.exists() or path.stat().st_size == 0:
            try:
                response = session.get(remote_url, timeout=15)
                if response.ok and len(response.content) > MIN_IMAGE_BYTES:
                    path.write_bytes(response.content)
                    _LOGGER.debug("Cached live food circle: %s", name)
            except (requests.RequestException, OSError) as err:
                _LOGGER.debug("Could not cache food circle image: %s", err)
        if path.exists() and path.stat().st_size > 0:
            return name, self.url(name)
        return None

    def prune(self, keep: set[str]) -> None:
        for path in self.directory.glob("*.jpg"):
            if path.name not in keep:
                try:
                    path.unlink()
                    _LOGGER.debug("Pruned stale food circle: %s", path.name)
                except OSError as err:
                    _LOGGER.debug("Could not prune %s: %s", path.name, err)

    def save_inventory(self, inventory: dict[str, Any]) -> None:
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
        rotator: RotatorClient,
        *,
        cache_dir: Path,
        view_secret: str = "",
        token: str | None = None,
        location_id: str | None = None,
        location_lookup: Callable[[], str | None] | None = None,
    ) -> None:
        self.hass = hass
        self._rotator = rotator
        self._images = FoodImageCache(cache_dir, view_secret)
        self._configured_token = token
        self._location_id = location_id
        self._location_lookup = location_lookup
        self._cached_inventory: dict[str, Any] | None = None
        self._session: requests.Session | None = None
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
        """Save tokens/location if they changed (runs on the event loop)."""
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

    def _get_rotator_token_sync(self) -> str | None:
        """Fetch the KICS (or, failing that, Food) token from the rotator (executor thread)."""
        now = time.time()
        if self._kics_token_valid(now):
            return self._cached_kics_token
        for path in ("/kics_token", "/food_token"):
            if found := self._rotator.fetch_token(path):
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
        return await self.hass.async_add_executor_job(self._get_rotator_token_sync)

    async def async_has_token(self) -> bool:
        return bool(await self.async_get_token())

    def _get_location_id_sync(self) -> str:
        """The fridge's SmartThings location ID (executor thread).

        Order: configured override / cached value, the SmartThings device record, then the rotator.
        """
        if self._location_id and self._location_id.strip():
            return self._location_id.strip()
        if self._location_lookup is not None:
            try:
                location = self._location_lookup()
            except (requests.RequestException, ValueError, KeyError) as err:
                _LOGGER.debug("Could not read locationId from SmartThings: %s", err)
            else:
                if location:
                    self._location_id = location.strip()
                    return self._location_id
        payload = self._rotator.get_json("/location_id") or {}
        location = str(payload.get("location_id") or "").strip()
        if location and not location.startswith("YOUR_"):
            self._location_id = location
            return location
        return ""

    # --- fetching ----------------------------------------------------------------

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _load_cached_inventory_from_disk(self) -> None:
        if (data := self._images.load_inventory()) is not None:
            self._cached_inventory = data
            _LOGGER.info("SamsungFoodClient: restored %d cached food items from disk", len(data["items"]))

    def _whisk_headers(self, token: str) -> dict[str, str]:
        auth, raw = split_bearer(token)
        return {"Authorization": auth, "x-whisk-token": raw, "Accept": "application/json"}

    def _get_whisk_stock_map(self, session: requests.Session) -> dict[str, str]:
        """Map item ID / lower-cased name to a Whisk stock photo, for items without a crop."""
        now = time.time()
        token = None
        if self._cached_whisk_token and (not self._whisk_expires_at or now < self._whisk_expires_at):
            token = self._cached_whisk_token
        elif found := self._rotator.fetch_token("/food_token"):
            token = self._cached_whisk_token = found.token
            self._whisk_expires_at = found.next_rotation_at or now + WHISK_TOKEN_TTL
        if not token or len(token) < WHISK_MIN_TOKEN_LENGTH:
            return {}

        stock: dict[str, str] = {}
        try:
            response = session.get(WHISK_FOODLIST_API, headers=self._whisk_headers(token), timeout=10)
            if response.ok:
                for item in response.json().get("items", []):
                    content = item.get("content", {})
                    photo = content.get("image_url") or content.get("photo_url")
                    if not photo:
                        continue
                    if item.get("id"):
                        stock[item["id"]] = photo
                    if name := (content.get("name") or "").lower():
                        stock[name] = photo
        except (requests.RequestException, ValueError) as err:
            _LOGGER.debug("Could not build Whisk stock photo map: %s", err)
        return stock

    def _parse_kics_items(self, items: list[dict[str, Any]], stock: dict[str, str], session: requests.Session) -> list[dict[str, Any]]:
        caching = self._images.ensure_directory()
        keep: set[str] = set()
        parsed = []
        for item in items:
            content = item.get("content", {})
            item_id = item.get("id", "")
            name = content.get("name") or "Unnamed Food Item"
            remote = content.get("imageUrl") or stock.get(item_id) or stock.get(name.lower())
            image_url = remote
            if remote and caching and (cached := self._images.fetch(session, item_id, remote)):
                keep.add(cached[0])
                image_url = cached[1]
            added = (
                content.get("addedAt") or content.get("createdAt") or content.get("added_at")
                or content.get("created_at") or content.get("createdTime")
            )
            parsed.append({
                "name": name,
                "image_url": image_url,
                "added_at": _seconds(added),
                "expiration_date": content.get("expirationDate") or content.get("expiration_date") or None,
            })
        if caching:
            self._images.prune(keep)
        return parsed

    def _result(self, items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        result = {
            "total_items": len(items),
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "items": items,
            **extra,
        }
        self._cached_inventory = result
        return result

    def _fetch_kics(self, session: requests.Session, token: str, location_id: str) -> tuple[dict[str, Any] | None, str | None]:
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
                response = session.get(KICS_FOODLIST_ENDPOINT, headers=headers, timeout=30)
            except requests.RequestException as err:
                error = str(err)
                if attempt < KICS_ATTEMPTS:
                    _LOGGER.info("KICS EPA fetch error on attempt %d (%s); retrying", attempt, err)
                    time.sleep(KICS_RETRY_DELAY)
                    continue
                _LOGGER.warning("KICS EPA fetch error: %s", err)
                break
            if response.ok:
                items = response.json().get("foodData", {}).get("items", [])
                parsed = self._parse_kics_items(items, self._get_whisk_stock_map(session), session)
                _LOGGER.info("SamsungFoodClient: retrieved %d live food circle items from KICS EPA", len(parsed))
                result = self._result(parsed, source="kics_epa_live_circles")
                self._images.save_inventory(result)
                return result, None
            error = f"HTTP {response.status_code}"
            if response.status_code in (401, 403):
                _LOGGER.warning("KICS token was rejected (%d); clearing the cached token", response.status_code)
                self._cached_kics_token = None
                self._kics_expires_at = 0.0
            elif response.status_code in KICS_RETRY_STATUSES and attempt < KICS_ATTEMPTS:
                _LOGGER.info("KICS EPA returned HTTP %d on attempt %d; retrying", response.status_code, attempt)
                time.sleep(KICS_RETRY_DELAY)
                continue
            _LOGGER.warning("KICS EPA response: %s", error)
            break
        return None, error

    def _fetch_whisk(self, session: requests.Session, token: str) -> dict[str, Any]:
        """Legacy path: page through the Whisk food list."""
        headers = self._whisk_headers(token)
        all_items: list[dict[str, Any]] = []
        cursor = ""
        error = None
        for page in range(1, 1000):
            params = {"paging.cursors.after": cursor} if cursor else {}
            try:
                response = session.get(WHISK_FOODLIST_API, headers=headers, params=params, timeout=30)
                if not response.ok:
                    _LOGGER.warning("Samsung Food API page %d returned HTTP %s", page, response.status_code)
                    error = f"HTTP {response.status_code}"
                    break
                data = response.json()
            except (requests.RequestException, ValueError) as err:
                _LOGGER.warning("Error fetching Samsung Food items on page %d (%s)", page, err)
                error = str(err)
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

    def _fetch_food_items_sync(self, token: str) -> dict[str, Any]:
        """KICS EPA live circles first; legacy Whisk only for long Whisk-style tokens."""
        if self._cached_inventory is None:
            self._load_cached_inventory_from_disk()
        session = self._get_session()

        location_id = self._get_location_id_sync()
        if not location_id:
            raise UpdateFailed(
                "KICS EPA food circles need a location ID. Set your fridge's Location ID under "
                "Settings → Devices & Services → Samsung FamilyHub → Configure."
            )
        inventory, error = self._fetch_kics(session, token, location_id)
        if inventory is not None:
            return inventory

        _auth, raw = split_bearer(token)
        if len(raw) < WHISK_MIN_TOKEN_LENGTH:  # a KICS token would just fail on Whisk with 401
            if self._cached_inventory and self._cached_inventory.get("items"):
                _LOGGER.info("SamsungFoodClient: keeping %d cached items after a KICS EPA error", self._cached_inventory.get("total_items", 0))
                return self._cached_inventory
            raise UpdateFailed(f"KICS EPA food circles fetch failed: {error or 'unknown error'}")
        return self._fetch_whisk(session, token)

    async def async_get_active_food_items(self) -> dict[str, Any]:
        token = await self.async_get_token()
        if not token:
            if self._cached_inventory is None:
                await self.hass.async_add_executor_job(self._load_cached_inventory_from_disk)
            return self._cached_inventory or dict(EMPTY_INVENTORY)
        try:
            return await self.hass.async_add_executor_job(self._fetch_food_items_sync, token)
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
        except (requests.RequestException, ValueError, KeyError) as err:
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
