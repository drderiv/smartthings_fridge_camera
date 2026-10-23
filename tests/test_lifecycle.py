"""Setup/unload lifecycle: options-only reload, services, diagnostics, PAT-mode setup."""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.samsung_familyhub_fridge import (
    FridgeRuntimeData,
    _async_options_updated,
    _target_entries,
    async_setup,
    async_setup_entry,
)
from custom_components.samsung_familyhub_fridge.const import (
    AUTH_MODE_PAT,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_ROTATOR_API_KEY,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.samsung_familyhub_fridge.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from tests.conftest import _ConfigEntry, _HomeAssistant, _ServiceRegistry

# The stub exception classes can be replaced when conftest is imported twice; always use the
# ones the integration itself raises.
import custom_components.samsung_familyhub_fridge as integration

ServiceValidationError = integration.ServiceValidationError
ConfigEntryNotReady = integration.ConfigEntryNotReady

ROTATOR = "http://127.0.0.1:8765"


def make_hass():
    hass = _HomeAssistant()
    hass.data = {}
    hass.services = _ServiceRegistry()
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


# --- reload only on option changes -------------------------------------------------

async def test_token_update_does_not_reload_but_option_change_does():
    hass = make_hass()
    entry = _ConfigEntry(entry_id="e1", data={CONF_TOKEN: "t"}, options={"poll_interval": 60})
    entry.runtime_data = FridgeRuntimeData(hub=MagicMock(), coordinator=MagicMock(), loaded_options={"poll_interval": 60})

    entry.data = {CONF_TOKEN: "rotated"}                 # token rotation rewrites entry data
    await _async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_not_called()

    entry.options = {"poll_interval": 120}               # the user changed an option
    await _async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


# --- PAT-mode setup -----------------------------------------------------------------

async def test_pat_setup_uses_rotator_token_and_polls_before_loading_platforms(mocked):
    hass = make_hass()
    entry = _ConfigEntry(
        entry_id="pat1",
        data={CONF_AUTH_MODE: AUTH_MODE_PAT, CONF_TOKEN: "stale-saved-pat", CONF_DEVICE_ID: "dev-1"},
        options={CONF_ROTATOR_API_KEY: "k"},
    )
    mocked.get(f"{ROTATOR}/tokens", payload={"smartthings_pat": "fresh-pat", "pat_next_rotation_at": 4102444800})
    mocked.get(re.compile(rf"{ROTATOR}/(kics_token|food_token|location_id)"), status=404)
    mocked.get("https://api.smartthings.com/v1/devices/dev-1/components/main/status", payload={})

    assert await async_setup_entry(hass, entry) is True

    runtime = entry.runtime_data
    assert runtime.hub.token == "fresh-pat"
    assert runtime.coordinator.food_coordinator is None          # no food token -> no inventory
    assert mocked.count("components/main/status") == 1          # first refresh happened during setup
    assert mocked.calls[0].kwargs["headers"]["Authorization"] == "Bearer k"   # rotator API key is sent
    hass.config_entries.async_update_entry.assert_called()      # new PAT saved to the entry
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()


async def test_pat_setup_without_any_token_is_not_ready(mocked):
    hass = make_hass()
    entry = _ConfigEntry(entry_id="pat2", data={CONF_AUTH_MODE: AUTH_MODE_PAT, CONF_DEVICE_ID: "d"})
    mocked.get(re.compile(rf"{ROTATOR}/.*"), status=503)
    with pytest.raises(ConfigEntryNotReady) as err:
        await async_setup_entry(hass, entry)
    assert err.value.translation_key == "no_token"


# --- services -------------------------------------------------------------------------

def loaded_entry(entry_id, *, food=None):
    entry = SimpleNamespace(entry_id=entry_id)
    hub = MagicMock()
    hub.async_ensure_fresh_token = AsyncMock()
    entry.runtime_data = SimpleNamespace(hub=hub, coordinator=SimpleNamespace(food_coordinator=food))
    return entry


def test_target_entries_filters_by_config_entry_id():
    hass = make_hass()
    a, b = loaded_entry("a"), loaded_entry("b")
    unloaded = SimpleNamespace(entry_id="c")                   # no runtime_data -> not loaded
    hass.config_entries.async_entries = MagicMock(return_value=[a, b, unloaded])

    assert _target_entries(hass, SimpleNamespace(data={})) == [a, b]
    assert _target_entries(hass, SimpleNamespace(data={"config_entry_id": "b"})) == [b]
    with pytest.raises(ServiceValidationError):
        _target_entries(hass, SimpleNamespace(data={"config_entry_id": "missing"}))


async def test_services_registered_in_async_setup_and_raise_translated_errors(tmp_path):
    from custom_components.samsung_familyhub_fridge.api import AuthenticationError
    hass = make_hass()
    hass.config.path = MagicMock(side_effect=lambda p: str(tmp_path / p))
    hass.http = MagicMock()
    hass.services.async_register = MagicMock()

    assert await async_setup(hass, {}) is True
    hass.http.register_view.assert_called_once()
    registered = {call.args[1]: call.args[2] for call in hass.services.async_register.call_args_list}
    assert set(registered) == {"refresh", "refresh_food_inventory"}
    assert (tmp_path / ".storage/samsung_familyhub/food_circles").is_dir()
    assert len(hass.data[DOMAIN]["view_secret"]) == 64          # random per installation

    # refresh: auth problems become a translated validation error and the flag is untouched
    entry = loaded_entry("a")
    entry.runtime_data.hub.update_camera = MagicMock(side_effect=AuthenticationError("401"))
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    with pytest.raises(ServiceValidationError) as err:
        await registered["refresh"](SimpleNamespace(data={}))
    assert err.value.translation_key == "auth_failed"

    # refresh_food_inventory without a food coordinator explains why
    with pytest.raises(ServiceValidationError) as err:
        await registered["refresh_food_inventory"](SimpleNamespace(data={}))
    assert err.value.translation_key == "no_food_inventory"

    # ...and refreshes it when present
    food = SimpleNamespace(async_request_refresh=AsyncMock())
    hass.config_entries.async_entries = MagicMock(return_value=[loaded_entry("b", food=food)])
    await registered["refresh_food_inventory"](SimpleNamespace(data={}))
    food.async_request_refresh.assert_awaited_once()


# --- diagnostics ----------------------------------------------------------------------

async def test_diagnostics_redacts_every_credential():
    entry = loaded_entry("d")
    secrets_in_entry = {
        CONF_TOKEN: "pat-secret",
        CONF_OAUTH_CLIENT_SECRET: "client-secret",
        CONF_OAUTH_REFRESH_TOKEN: "refresh-secret",
        CONF_ROTATOR_API_KEY: "rotator-secret",
    }
    entry.as_dict = lambda: {"data": dict(secrets_in_entry), "options": {}, "unique_id": "u-1"}
    entry.runtime_data.hub.downloaded_images = [b"x", None, None]
    entry.runtime_data.hub.last_closed = "t"
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.last_update_success = True
    entry.runtime_data.coordinator.last_file_ids = ["f1"]

    result = await async_get_config_entry_diagnostics(make_hass(), entry)

    flattened = str(result)
    assert not any(secret in flattened for secret in secrets_in_entry.values())
    assert result["images_available"] == [True, False, False]
    assert set(secrets_in_entry) <= TO_REDACT
