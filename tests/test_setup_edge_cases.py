"""Setup and polling edge cases: standalone OAuth errors, services, migration, client branches."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import custom_components.samsung_familyhub_fridge as integration
from custom_components.samsung_familyhub_fridge.api import AuthenticationError, FamilyHub, RateLimitError
from custom_components.samsung_familyhub_fridge.const import (
    AUTH_MODE_STANDALONE_OAUTH,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_OAUTH_CLIENT_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_SAMSUNG_IOT_REFRESH_TOKEN,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.samsung_familyhub_fridge.coordinator import FridgeCoordinator
from tests.conftest import _ConfigEntry, _HomeAssistant, _ServiceRegistry

TOKEN_URL = "https://api.smartthings.com/oauth/token"
IOT_URL = "https://us-auth2.samsungosp.com/auth/oauth2/token"
STATUS = "https://api.smartthings.com/v1/devices/device-abc/components/main/status"


def make_hass():
    hass = _HomeAssistant()
    hass.data = {}
    hass.services = _ServiceRegistry()
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    return hass


def standalone_entry(**extra):
    data = {
        CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH,
        CONF_OAUTH_CLIENT_ID: "cid",
        CONF_OAUTH_CLIENT_SECRET: "sec",
        CONF_OAUTH_REFRESH_TOKEN: "refresh-1",
        CONF_DEVICE_ID: "device-abc",
        **extra,
    }
    return _ConfigEntry(entry_id="s1", data=data)


# --- Standalone OAuth setup errors --------------------------------------------------------------

async def test_rejected_refresh_token_starts_reauth(mocked):
    mocked.post(TOKEN_URL, status=401)
    with pytest.raises(integration.ConfigEntryAuthFailed) as err:
        await integration.async_setup_entry(make_hass(), standalone_entry())
    assert err.value.translation_key == "auth_failed"


async def test_smartthings_outage_during_setup_retries(mocked):
    mocked.post(TOKEN_URL, exception=aiohttp.ClientConnectionError("down"))
    with pytest.raises(integration.ConfigEntryNotReady) as err:
        await integration.async_setup_entry(make_hass(), standalone_entry())
    assert err.value.translation_key == "cannot_connect"


async def test_server_error_on_refresh_propagates_as_not_ready(mocked):
    mocked.post(TOKEN_URL, status=500)
    with pytest.raises(integration.ConfigEntryNotReady):
        await integration.async_setup_entry(make_hass(), standalone_entry())


async def test_iot_refresh_failure_is_a_warning_and_rotation_is_saved(mocked, caplog):
    mocked.get(re_any(), status=404)
    mocked.get(STATUS, payload={})
    mocked.post(TOKEN_URL, payload={"access_token": "a", "refresh_token": "refresh-1", "expires_in": 3600})
    mocked.post(IOT_URL, status=401)
    entry = standalone_entry(**{CONF_SAMSUNG_IOT_REFRESH_TOKEN: "iot-1"})
    with caplog.at_level(logging.WARNING):
        assert await integration.async_setup_entry(make_hass(), entry) is True
    assert "Samsung IoT token" in caplog.text and entry.runtime_data.hub._samsung_iot_headers is None

    mocked.post(IOT_URL, payload={"access_token": "iot-a", "refresh_token": "iot-2"})
    hass = make_hass()
    assert await integration.async_setup_entry(hass, standalone_entry(**{CONF_SAMSUNG_IOT_REFRESH_TOKEN: "iot-1"})) is True
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["data"][CONF_SAMSUNG_IOT_REFRESH_TOKEN] == "iot-2"


def re_any():
    import re

    return re.compile(r"http://127\.0\.0\.1:8765/.*")


async def test_refresher_reraises_non_auth_http_errors(mocked, session):
    hub_holder = {}
    mocked.get(re_any(), status=404)
    mocked.get(STATUS, payload={})
    mocked.post(TOKEN_URL, payload={"access_token": "a", "refresh_token": "refresh-1", "expires_in": 3600})
    entry = standalone_entry()
    await integration.async_setup_entry(make_hass(), entry)
    hub_holder["hub"] = entry.runtime_data.hub

    mocked.post(TOKEN_URL, status=503)
    hub_holder["hub"]._token_expires_at = 0
    with pytest.raises(aiohttp.ClientResponseError):
        await hub_holder["hub"].async_ensure_fresh_token()


# --- PAT setup fallbacks -----------------------------------------------------------------------------

async def test_pat_setup_falls_back_to_the_pat_endpoint(mocked):
    import re

    hass = make_hass()
    entry = _ConfigEntry(entry_id="p1", data={CONF_TOKEN: "saved", CONF_DEVICE_ID: "device-abc"})
    mocked.get("http://127.0.0.1:8765/tokens", status=503)
    mocked.get("http://127.0.0.1:8765/pat", payload={"token": "from-pat-endpoint", "next_rotation_at": 4102444800})
    mocked.get(re.compile(r"http://127\.0\.0\.1:8765/(kics|food|location).*"), status=404)
    mocked.get(STATUS, payload={})

    await integration.async_setup_entry(hass, entry)

    assert entry.runtime_data.hub.token == "from-pat-endpoint"
    assert entry.runtime_data.coordinator._pat_expires_at == 4102444800.0


# --- services ----------------------------------------------------------------------------------------------

async def refresh_handler(tmp_path, hub):
    hass = make_hass()
    hass.config.path = MagicMock(side_effect=lambda p: str(tmp_path / p))
    hass.http = MagicMock()
    hass.services.async_register = MagicMock()
    await integration.async_setup(hass, {})
    handlers = {c.args[1]: c.args[2] for c in hass.services.async_register.call_args_list}
    entry = SimpleNamespace(entry_id="a", runtime_data=SimpleNamespace(hub=hub, coordinator=SimpleNamespace(food_coordinator=None)))
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    return handlers["refresh"]


@pytest.mark.parametrize(
    ("error", "key"),
    [(RateLimitError("429"), "rate_limited"), (aiohttp.ClientConnectionError("x"), "refresh_failed"), (ValueError("bad"), "refresh_failed")],
)
async def test_refresh_service_error_translation(tmp_path, error, key):
    hub = MagicMock()
    hub.async_ensure_fresh_token = AsyncMock()
    hub.update_camera = AsyncMock(side_effect=error)
    handler = await refresh_handler(tmp_path, hub)
    with pytest.raises(integration.HomeAssistantError) as err:
        await handler(SimpleNamespace(data={}))
    assert err.value.translation_key == key


async def test_refresh_service_success_clears_the_pending_flag(tmp_path):
    hub = MagicMock(should_update=True)
    hub.async_ensure_fresh_token = AsyncMock()
    hub.update_camera = AsyncMock()
    handler = await refresh_handler(tmp_path, hub)
    await handler(SimpleNamespace(data={}))
    assert hub.should_update is False


# --- migration ---------------------------------------------------------------------------------------------------

async def test_version_1_entries_are_migrated_to_pat_mode():
    hass = make_hass()
    entry = SimpleNamespace(version=1, data={CONF_TOKEN: "t"}, entry_id="m1")
    assert await integration.async_migrate_entry(hass, entry) is True
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["version"] == 2 and kwargs["data"][CONF_AUTH_MODE] == "pat"

    hass.config_entries.async_update_entry.reset_mock()
    assert await integration.async_migrate_entry(hass, SimpleNamespace(version=2, data={}, entry_id="m2")) is True
    hass.config_entries.async_update_entry.assert_not_called()


# --- FamilyHub client branches -------------------------------------------------------------------------------------

async def test_location_id_lookup_and_caching(session, mocked):
    hub = FamilyHub(session, "t", "dev")
    mocked.get("https://api.smartthings.com/v1/devices/dev", payload={"locationId": "loc-1"})
    assert await hub.get_location_id() == "loc-1"
    assert await hub.get_location_id() == "loc-1"                      # cached: no second request
    assert mocked.count("/devices/dev") == 1


async def test_location_id_failures_return_none(session, mocked):
    hub = FamilyHub(session, "t", "dev")
    mocked.get("https://api.smartthings.com/v1/devices/dev", exception=aiohttp.ClientConnectionError("x"))
    assert await hub.get_location_id() is None
    assert await FamilyHub(session, "t", None).get_location_id() is None   # no device yet
    mocked.get("https://api.smartthings.com/v1/devices/dev", status=404)
    assert await hub.get_location_id() is None


async def test_update_camera_without_device_is_a_noop(session, mocked):
    hub = FamilyHub(session, "t", None)
    await hub.update_camera()
    assert mocked.calls == []


async def test_missing_iot_refresh_token_is_an_authentication_error(session):
    hub = FamilyHub(session, "t", "dev")
    with pytest.raises(AuthenticationError):
        await hub._do_samsung_iot_refresh()


# --- coordinator branches -----------------------------------------------------------------------------------------------

def hub_with(status_error=None, device_id="dev"):
    hub = MagicMock()
    hub.token = "t"
    hub.device_id = device_id
    hub.should_update = False
    hub.get_file_ids.return_value = ["f1"]
    hub.async_ensure_fresh_token = AsyncMock()
    hub.get_all_device_status = AsyncMock(return_value={"items": []})
    hub.get_current_device_status = AsyncMock(side_effect=status_error, return_value={})
    hub.download_images = AsyncMock(return_value=False)
    return hub


async def test_unknown_device_stops_the_poll_quietly():
    hub = hub_with(device_id=None)
    await FridgeCoordinator(MagicMock(), MagicMock(), hub)._async_update_data()
    hub.get_current_device_status.assert_not_called()


async def test_download_failures_warn_three_times_then_once_more(caplog):
    coordinator = FridgeCoordinator(MagicMock(), MagicMock(), hub_with())
    with caplog.at_level(logging.WARNING):
        for _ in range(6):
            await coordinator._async_update_data()
    warnings = [r.getMessage() for r in caplog.records if "image download" in r.getMessage()]
    assert len(warnings) == 4 and "suppressing" in warnings[-1]


async def test_successful_download_triggers_a_food_refresh():
    hub = hub_with()
    hub.download_images = AsyncMock(return_value=True)
    coordinator = FridgeCoordinator(MagicMock(), MagicMock(), hub)
    coordinator.food_coordinator = MagicMock()
    await coordinator._async_update_data()
    coordinator.food_coordinator.async_trigger_door_close_sync.assert_called_once()
    assert coordinator.last_updated_at is not None and coordinator.last_file_ids == ["f1"]


async def test_auth_error_with_unchanged_pat_still_asks_for_reauth(mocked):
    hub = hub_with(status_error=AuthenticationError("401"))
    rotator = MagicMock()
    rotator.fetch_token = AsyncMock(return_value=SimpleNamespace(token="t", next_rotation_at=None))   # same PAT
    coordinator = FridgeCoordinator(MagicMock(), MagicMock(), hub, rotator=rotator)
    coordinator._pat_expires_at = 4102444800
    with pytest.raises(integration.ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_newer_pat_that_is_also_rejected_asks_for_reauth():
    hub = hub_with(status_error=AuthenticationError("401"))
    rotator = MagicMock()
    rotator.fetch_token = AsyncMock(return_value=SimpleNamespace(token="new", next_rotation_at=None))
    coordinator = FridgeCoordinator(MagicMock(), MagicMock(), hub, rotator=rotator)
    coordinator._pat_expires_at = 4102444800
    coordinator.config_entry = MagicMock(data={CONF_TOKEN: "t"})
    with pytest.raises(integration.ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    hub.update_token.assert_called_once_with("new")


async def test_rotator_outage_delays_the_next_check():
    hub = hub_with()
    rotator = MagicMock()
    rotator.fetch_token = AsyncMock(return_value=None)
    coordinator = FridgeCoordinator(MagicMock(), MagicMock(), hub, rotator=rotator)
    await coordinator._async_update_data()
    assert coordinator._pat_expires_at > 0
