"""The Samsung FamilyHub Fridge integration.

Auth modes (data["auth_mode"]):

- "pat"              : A SmartThings Personal Access Token. PATs expire after 24 hours, so the
                       companion token rotator (tools/pat_rotator) supplies fresh ones.
- "standalone_oauth" : Custom SmartThings app credentials (client ID/secret + refresh token); the
                       access token is renewed before it expires.
- "oauth"            : Removed (borrowed the core SmartThings integration's credentials).

Entries created before auth modes existed (``{token, device_id}``) are migrated to "pat" by
``async_migrate_entry``.
"""
from __future__ import annotations

import logging
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .api import AuthenticationError, FamilyHub, RateLimitError
from .auth import AuthError, SmartThingsOAuth, refresh_samsung_iot_token
from .const import (
    AUTH_MODE_PAT,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_FOOD_TOKEN,
    CONF_FOOD_UPDATE_INTERVAL,
    CONF_KICS_TOKEN,
    CONF_LOCATION_ID,
    CONF_OAUTH_CLIENT_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_POLL_INTERVAL,
    CONF_SAMSUNG_IOT_AUTH_SERVER,
    CONF_SAMSUNG_IOT_REFRESH_TOKEN,
    CONF_TOKEN,
    DEFAULT_FOOD_UPDATE_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    AuthMode,
)
from .coordinator import FridgeCoordinator
from .food import SamsungFoodClient, SamsungFoodCoordinator
from .rotator import RotatorClient, entry_value
from .views import FamilyHubFoodCircleView

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CAMERA, Platform.SENSOR]

SERVICE_REFRESH = "refresh"
SERVICE_REFRESH_FOOD = "refresh_food_inventory"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
SERVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): str})

CACHE_DIR = ".storage/samsung_familyhub/food_circles"
LEGACY_CACHE_DIR = "www/samsung_familyhub/food_circles"
DEFAULT_IOT_AUTH_SERVER = "https://us-auth2.samsungosp.com"


@dataclass
class FridgeRuntimeData:
    """Per-entry state shared with the platforms."""

    hub: FamilyHub
    coordinator: FridgeCoordinator
    loaded_options: dict[str, Any]


type FridgeConfigEntry = ConfigEntry[FridgeRuntimeData]


# --- integration-wide setup (services, image view) -----------------------------------


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services and the signed food-circle image view once per Home Assistant run."""
    cache_dir = Path(hass.config.path(CACHE_DIR))
    legacy_dir = Path(hass.config.path(LEGACY_CACHE_DIR))
    await hass.async_add_executor_job(_prepare_cache_dir, cache_dir, legacy_dir)

    secret_store: Store[dict[str, str]] = Store(hass, 1, f"{DOMAIN}.view_secret")
    stored = await secret_store.async_load()
    if not stored or not stored.get("secret"):
        stored = {"secret": secrets.token_hex(32)}
        await secret_store.async_save(stored)
    hass.data[DOMAIN] = {"view_secret": stored["secret"], "cache_dir": cache_dir}
    hass.http.register_view(FamilyHubFoodCircleView(str(cache_dir), secret=stored["secret"]))

    async def _async_refresh(call: ServiceCall) -> None:
        for entry in _target_entries(hass, call):
            hub = entry.runtime_data.hub
            try:
                await hub.async_ensure_fresh_token()
                await hub.update_camera()
            except AuthenticationError as err:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="auth_failed") from err
            except RateLimitError as err:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="rate_limited") from err
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="refresh_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            hub.should_update = False  # the command was just sent

    async def _async_refresh_food(call: ServiceCall) -> None:
        refreshed = False
        for entry in _target_entries(hass, call):
            if (food := entry.runtime_data.coordinator.food_coordinator) is not None:
                await food.async_request_refresh()
                refreshed = True
        if not refreshed:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_food_inventory")

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _async_refresh, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_FOOD, _async_refresh_food, schema=SERVICE_SCHEMA)
    return True


def _prepare_cache_dir(cache_dir: Path, legacy_dir: Path) -> None:
    """Create the protected image cache and move any images left in the public www/ folder."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not legacy_dir.is_dir():
        return
    for source in legacy_dir.iterdir():
        target = cache_dir / source.name
        if source.is_file() and not target.exists():
            shutil.move(source, target)
    shutil.rmtree(legacy_dir, ignore_errors=True)
    _LOGGER.info("Migrated legacy food circle images from www/ to .storage/")


def _target_entries(hass: HomeAssistant, call: ServiceCall) -> list[FridgeConfigEntry]:
    """Loaded entries addressed by a service call (all of them unless config_entry_id is given)."""
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None and (not wanted or entry.entry_id == wanted)
    ]
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_loaded_entry")
    return entries


# --- config entry setup --------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: FridgeConfigEntry) -> bool:
    """Set up Samsung FamilyHub Fridge from a config entry."""
    auth_mode = entry.data.get(CONF_AUTH_MODE, AUTH_MODE_PAT)
    device_id = entry.data.get(CONF_DEVICE_ID)
    session = async_get_clientsession(hass)
    rotator = RotatorClient.from_entry(session, entry)
    startup: dict[str, Any] = {}
    pat_expiry: float | None = None

    match auth_mode:
        case AuthMode.OAUTH:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="oauth_mode_removed")
        case AuthMode.STANDALONE_OAUTH:
            hub = await _build_standalone_hub(hass, entry, session, device_id)
            pat_rotator = None
        case _:
            hub, startup, pat_expiry = await _build_pat_hub(hass, entry, session, rotator, device_id)
            pat_rotator = rotator

    coordinator = FridgeCoordinator(
        hass,
        entry,
        hub,
        rotator=pat_rotator,
        poll_interval=int(entry_value(entry, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
    )
    coordinator.set_pat_expiry(pat_expiry)
    await coordinator.async_config_entry_first_refresh()  # ConfigEntryNotReady / reauth surface here
    entry.async_on_unload(coordinator.async_shutdown)

    coordinator.food_coordinator = await _async_setup_food(hass, entry, session, hub, rotator, startup)
    if coordinator.food_coordinator is not None:
        entry.async_on_unload(coordinator.food_coordinator.async_shutdown)

    entry.runtime_data = FridgeRuntimeData(hub=hub, coordinator=coordinator, loaded_options=dict(entry.options))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _build_pat_hub(
    hass: HomeAssistant,
    entry: FridgeConfigEntry,
    session: aiohttp.ClientSession,
    rotator: RotatorClient,
    device_id: str | None,
) -> tuple[FamilyHub, dict[str, Any], float | None]:
    """PAT mode: prefer the rotator's current PAT, fall back to the one saved in the entry."""
    startup = await rotator.tokens()
    token = str(startup.get("smartthings_pat") or "").strip()
    expiry = startup.get("pat_next_rotation_at")
    if not token and (found := await rotator.fetch_token("/pat")):
        token, expiry = found.token, found.next_rotation_at
    elif token:
        _LOGGER.info("Loaded active tokens from the rotator on startup")
    token = token or entry.data.get(CONF_TOKEN)
    if not token:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="no_token")
    if entry.data.get(CONF_TOKEN) != token:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: token})
    return FamilyHub(session, token, device_id), startup, expiry


async def _build_standalone_hub(
    hass: HomeAssistant, entry: FridgeConfigEntry, session: aiohttp.ClientSession, device_id: str | None
) -> FamilyHub:
    """Standalone OAuth: exchange the saved refresh token, and keep renewing the access token."""
    client_id = entry.data.get(CONF_OAUTH_CLIENT_ID)
    client_secret = entry.data.get(CONF_OAUTH_CLIENT_SECRET)
    if not (client_id and client_secret and entry.data.get(CONF_OAUTH_REFRESH_TOKEN)):
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="missing_credentials")
    oauth = SmartThingsOAuth(client_id=client_id, client_secret=client_secret)

    async def _refresh() -> Any:
        """Get a new access token and persist the (rotated) refresh token."""
        current = entry.data[CONF_OAUTH_REFRESH_TOKEN]
        try:
            credentials = await oauth.refresh(session, current)
        except aiohttp.ClientResponseError as err:
            if err.status in (400, 401):
                raise AuthenticationError("SmartThings refresh token rejected") from err
            raise
        if credentials.refresh_token != current:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_OAUTH_REFRESH_TOKEN: credentials.refresh_token}
            )
        return credentials

    try:
        credentials = await _refresh()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": type(err).__name__},
        ) from err

    def _persist_iot_token(refresh_token: str) -> None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SAMSUNG_IOT_REFRESH_TOKEN: refresh_token}
        )

    hub = FamilyHub(session, credentials.access_token, device_id, on_iot_refresh_token=_persist_iot_token)
    hub.set_token_refresher(_refresh, credentials.expires_in)

    if iot_refresh := entry.data.get(CONF_SAMSUNG_IOT_REFRESH_TOKEN):
        iot_server = entry.data.get(CONF_SAMSUNG_IOT_AUTH_SERVER, DEFAULT_IOT_AUTH_SERVER)
        try:
            iot = await refresh_samsung_iot_token(session, iot_refresh, iot_server)
        except (AuthError, aiohttp.ClientError, TimeoutError, KeyError, ValueError) as err:  # images only
            _LOGGER.warning(
                "Could not refresh the Samsung IoT token; image downloads will fail until resolved: %s", err
            )
        else:
            hub.set_samsung_iot_token(iot.access_token, refresh_token=iot.refresh_token, auth_server=iot_server)
            if iot.refresh_token != iot_refresh:
                _persist_iot_token(iot.refresh_token)
    return hub


async def _async_setup_food(
    hass: HomeAssistant,
    entry: FridgeConfigEntry,
    session: aiohttp.ClientSession,
    hub: FamilyHub,
    rotator: RotatorClient,
    startup: dict[str, Any],
) -> SamsungFoodCoordinator | None:
    """Create the food inventory coordinator if a KICS/Samsung Food token is available."""
    shared = hass.data.get(DOMAIN, {})
    client = SamsungFoodClient(
        hass,
        session,
        rotator,
        cache_dir=shared.get("cache_dir") or Path(hass.config.path(CACHE_DIR)),
        view_secret=shared.get("view_secret", ""),
        token=entry.data.get(CONF_FOOD_TOKEN) or entry.data.get(CONF_KICS_TOKEN),
        location_id=entry_value(entry, CONF_LOCATION_ID),
        location_lookup=hub.get_location_id,
    )
    if startup:
        client.prime_from_rotator(startup)
    await client.async_restore_cache()
    if not await client.async_has_token():
        _LOGGER.debug("No Samsung Food token found; the food inventory sensor is not created")
        return None

    _LOGGER.info("Samsung Food token detected; setting up the food inventory")
    food = SamsungFoodCoordinator(
        hass,
        entry,
        client,
        int(entry_value(entry, CONF_FOOD_UPDATE_INTERVAL, DEFAULT_FOOD_UPDATE_INTERVAL)),
    )
    await food.async_refresh()  # a failed first sync is logged by the coordinator; the sensor still loads
    return food


async def _async_options_updated(hass: HomeAssistant, entry: FridgeConfigEntry) -> None:
    """Reload only when options change (token updates also write the entry but need no reload)."""
    if dict(entry.options) != entry.runtime_data.loaded_options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: FridgeConfigEntry) -> bool:
    """Unload a config entry (listeners and coordinators are cleaned up via async_on_unload)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry data to the current schema.

    v1 -> {token, device_id} (raw PAT)
    v2 -> adds auth_mode
    """
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_AUTH_MODE: AUTH_MODE_PAT}, version=2
        )
        _LOGGER.info("Migrated entry %s from v1 to v2 (auth_mode=pat)", entry.entry_id)
    return True
