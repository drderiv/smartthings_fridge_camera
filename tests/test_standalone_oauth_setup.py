"""Tests for AUTH_MODE_STANDALONE_OAUTH wired into async_setup_entry and reauth.

Integration tests: construct a config entry with auth_mode=standalone_oauth,
mock the SmartThings / Samsung IoT refresh endpoints, call async_setup_entry,
assert the hub has the expected tokens.

Unit test: async_step_reauth routes standalone OAuth entries to
async_step_reauth_standalone_oauth (not async_step_reauth_confirm).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import _ConfigEntry, _HomeAssistant, _ServiceRegistry

from custom_components.samsung_familyhub_fridge.auth import (
    SAMSUNG_IOT_TOKEN_URL,
    SAMSUNG_IOT_AUTHORIZE_URL,
    TOKEN_URL,
)
from custom_components.samsung_familyhub_fridge.const import (
    AUTH_MODE_STANDALONE_OAUTH,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_OAUTH_CLIENT_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_SAMSUNG_IOT_AUTH_SERVER,
    CONF_SAMSUNG_IOT_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.samsung_familyhub_fridge.config_flow import ConfigFlow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hass():
    hass = _HomeAssistant()
    hass.data = {}
    hass.services = _ServiceRegistry()
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _stub_network(mocked, device_id):
    """Setup now polls SmartThings once and probes the rotator; give both quiet answers."""
    import re

    mocked.get(re.compile(r"http://127\.0\.0\.1:8765/.*"), status=404)
    mocked.get(
        f"https://api.smartthings.com/v1/devices/{device_id}/components/main/status", payload={}
    )


def _make_flow(hass) -> ConfigFlow:
    flow = ConfigFlow()
    flow.hass = hass
    return flow


# ---------------------------------------------------------------------------
# Integration test 1: standalone OAuth setup gets refreshed access token
# ---------------------------------------------------------------------------


async def test_setup_entry_standalone_oauth_refreshes_token(mocked):
    """async_setup_entry with standalone_oauth uses the refreshed access token."""
    hass = _make_hass()
    entry = _ConfigEntry(
        entry_id="se-standalone-1",
        data={
            CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH,
            CONF_OAUTH_CLIENT_ID: "client-id-1",
            CONF_OAUTH_CLIENT_SECRET: "client-secret-1",
            CONF_OAUTH_REFRESH_TOKEN: "old-refresh-token",
            CONF_DEVICE_ID: "device-abc",
        },
    )

    _stub_network(mocked, "device-abc")
    mocked.post(
        TOKEN_URL,
        payload={
            "access_token": "fresh-access-token",
            "refresh_token": "old-refresh-token",
            "token_type": "bearer",
            "expires_in": 86400,
        },
    )

    from custom_components.samsung_familyhub_fridge import async_setup_entry

    result = await async_setup_entry(hass, entry)

    assert result is True
    hub = entry.runtime_data.hub
    assert hub.token == "fresh-access-token", (
        f"Expected hub.token='fresh-access-token', got {hub.token!r}"
    )
    assert hub._samsung_iot_headers is None


# ---------------------------------------------------------------------------
# Integration test 2: standalone OAuth setup with Samsung IoT token
# ---------------------------------------------------------------------------


async def test_setup_entry_standalone_oauth_with_samsung_iot(mocked):
    """async_setup_entry with standalone_oauth + IoT token sets samsung_iot_headers."""
    hass = _make_hass()
    entry = _ConfigEntry(
        entry_id="se-standalone-2",
        data={
            CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH,
            CONF_OAUTH_CLIENT_ID: "client-id-2",
            CONF_OAUTH_CLIENT_SECRET: "client-secret-2",
            CONF_OAUTH_REFRESH_TOKEN: "st-refresh-token",
            CONF_DEVICE_ID: "device-xyz",
            CONF_SAMSUNG_IOT_REFRESH_TOKEN: "iot-refresh-token",
            CONF_SAMSUNG_IOT_AUTH_SERVER: "https://us-auth2.samsungosp.com",
        },
    )

    _stub_network(mocked, "device-xyz")
    # Mock SmartThings token refresh
    mocked.post(
        TOKEN_URL,
        payload={
            "access_token": "fresh-st-access-token",
            "refresh_token": "st-refresh-token",
            "token_type": "bearer",
            "expires_in": 86400,
        },
    )

    # Mock Samsung IoT token refresh
    mocked.post(
        "https://us-auth2.samsungosp.com/auth/oauth2/token",
        payload={
            "access_token": "fresh-iot-access-token",
            "refresh_token": "iot-refresh-token",
        },
    )

    from custom_components.samsung_familyhub_fridge import async_setup_entry

    result = await async_setup_entry(hass, entry)

    assert result is True
    hub = entry.runtime_data.hub
    assert hub.token == "fresh-st-access-token"
    assert hub._samsung_iot_headers is not None, (
        "Expected hub._samsung_iot_headers to be set after IoT token refresh"
    )
    assert hub._samsung_iot_headers["Authorization"] == "Bearer fresh-iot-access-token"


# ---------------------------------------------------------------------------
# Integration test 3: missing credentials → ConfigEntryNotReady
# ---------------------------------------------------------------------------


async def test_setup_entry_standalone_oauth_missing_creds():
    """async_setup_entry raises ConfigEntryNotReady when credentials are absent."""
    from homeassistant.exceptions import ConfigEntryNotReady

    hass = _make_hass()
    entry = _ConfigEntry(
        entry_id="se-standalone-3",
        data={
            CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH,
            CONF_DEVICE_ID: "device-123",
            # oauth credentials deliberately omitted
        },
    )

    from custom_components.samsung_familyhub_fridge import async_setup_entry

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


# ---------------------------------------------------------------------------
# Unit test: reauth routing
# ---------------------------------------------------------------------------


async def test_reauth_standalone_oauth_routes_to_correct_step():
    """async_step_reauth for a standalone_oauth entry routes to reauth_standalone_oauth."""
    hass = _make_hass()
    hass.config_entries.async_entries = MagicMock(return_value=[])
    flow = _make_flow(hass)

    result = await flow.async_step_reauth(
        {
            CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH,
            CONF_OAUTH_CLIENT_ID: "client-id",
            CONF_OAUTH_CLIENT_SECRET: "client-secret",
            CONF_OAUTH_REFRESH_TOKEN: "refresh-token",
        }
    )

    assert result["type"] == "form", f"Expected form, got: {result}"
    assert result["step_id"] == "reauth_standalone_oauth", (
        f"Expected step 'reauth_standalone_oauth', got '{result['step_id']}'"
    )


async def test_reauth_pat_routes_to_reauth_confirm(mocked):
    """async_step_reauth for a PAT entry routes to reauth_confirm (which asks the rotator first)."""
    hass = _make_hass()
    hass.config_entries.async_entries = MagicMock(return_value=[])
    flow = _make_flow(hass)
    flow._reauth_entry = SimpleNamespace(data={"token": "old-pat"}, options={}, entry_id="e")
    mocked.get("http://127.0.0.1:8765/pat", status=404)

    result = await flow.async_step_reauth({"auth_mode": "pat", "token": "old-pat"})

    assert result["step_id"] == "reauth_confirm"


async def test_standalone_oauth_refresher_persists_rotated_refresh_token(mocked):
    """The hub's refresher exchanges the saved refresh token and stores the rotated one."""
    hass = _make_hass()
    entry = _ConfigEntry(
        entry_id="se-standalone-4",
        data={
            CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH,
            CONF_OAUTH_CLIENT_ID: "cid",
            CONF_OAUTH_CLIENT_SECRET: "csecret",
            CONF_OAUTH_REFRESH_TOKEN: "refresh-1",
            CONF_DEVICE_ID: "device-abc",
        },
    )
    _stub_network(mocked, "device-abc")
    mocked.post(
        TOKEN_URL,
        payload={"access_token": "access-1", "refresh_token": "refresh-2", "expires_in": 86400},
        repeat=False,
    )
    mocked.post(
        TOKEN_URL,
        payload={"access_token": "access-2", "refresh_token": "refresh-3", "expires_in": 86400},
        repeat=False,
    )

    from custom_components.samsung_familyhub_fridge import async_setup_entry

    assert await async_setup_entry(hass, entry) is True
    hub = entry.runtime_data.hub
    assert hub.token == "access-1"

    # Simulate the config entry applying the first update, then force the access token to expire.
    entry.data = {**entry.data, CONF_OAUTH_REFRESH_TOKEN: "refresh-2"}
    hub._token_expires_at = 0
    await hub.async_ensure_fresh_token()

    assert hub.token == "access-2"
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["data"][CONF_OAUTH_REFRESH_TOKEN] == "refresh-3"
