"""PAT config flow: validation, unique ID, error mapping, and reauth routing."""
from __future__ import annotations

import aiohttp

from custom_components.samsung_familyhub_fridge.config_flow import ConfigFlow
from custom_components.samsung_familyhub_fridge.const import (
    AUTH_MODE_OAUTH,
    AUTH_MODE_PAT,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_TOKEN,
)
from tests.conftest import _HomeAssistant

DEVICE_LIST = "https://client.smartthings.com/devices/status"
DEVICES = {"items": [{"capabilityId": "samsungce.viewInside", "attributeName": "contents", "deviceId": "fridge-42"}]}


def make_flow():
    flow = ConfigFlow()
    flow.hass = _HomeAssistant()
    return flow


async def test_valid_pat_creates_entry_with_device_id_as_unique_id(mocked):
    flow = make_flow()
    mocked.get(DEVICE_LIST, payload=DEVICES)

    result = await flow.async_step_pat({CONF_TOKEN: "pat-123"})

    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_TOKEN: "pat-123", CONF_DEVICE_ID: "fridge-42", CONF_AUTH_MODE: AUTH_MODE_PAT}
    assert flow._unique_id == "fridge-42"


async def test_rejected_pat_shows_invalid_auth(mocked):
    flow = make_flow()
    mocked.get(DEVICE_LIST, status=401)

    result = await flow.async_step_pat({CONF_TOKEN: "bad"})

    assert result["type"] == "form" and result["errors"] == {"base": "invalid_auth"}


async def test_unreachable_smartthings_shows_cannot_connect(mocked):
    flow = make_flow()
    mocked.get(DEVICE_LIST, exception=aiohttp.ClientConnectionError("down"))

    result = await flow.async_step_pat({CONF_TOKEN: "pat"})

    assert result["errors"] == {"base": "cannot_connect"}


async def test_rate_limited_validation_shows_cannot_connect(mocked):
    flow = make_flow()
    mocked.get(DEVICE_LIST, status=429)

    assert (await flow.async_step_pat({CONF_TOKEN: "pat"}))["errors"] == {"base": "cannot_connect"}


async def test_reauth_of_removed_oauth_mode_asks_for_a_pat_instead_of_adding_an_entry():
    flow = make_flow()

    result = await flow.async_step_reauth({CONF_AUTH_MODE: AUTH_MODE_OAUTH})

    assert result["type"] == "form" and result["step_id"] == "reauth_confirm"
