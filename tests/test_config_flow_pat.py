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


async def test_reauth_of_removed_oauth_mode_asks_for_a_pat_instead_of_adding_an_entry(mocked):
    from types import SimpleNamespace

    flow = make_flow()
    flow._reauth_entry = SimpleNamespace(data={CONF_AUTH_MODE: AUTH_MODE_OAUTH}, options={}, entry_id="e")
    mocked.get("http://127.0.0.1:8765/pat", status=404)

    result = await flow.async_step_reauth({CONF_AUTH_MODE: AUTH_MODE_OAUTH})

    assert result["type"] == "form" and result["step_id"] == "reauth_confirm"


async def test_pat_reauth_uses_the_rotators_token_without_asking(mocked):
    from types import SimpleNamespace

    flow = make_flow()
    flow._reauth_entry = SimpleNamespace(
        data={CONF_AUTH_MODE: AUTH_MODE_PAT, CONF_TOKEN: "expired", CONF_DEVICE_ID: "fridge-42"}, options={}, entry_id="e"
    )
    mocked.get("http://127.0.0.1:8765/pat", payload={"token": "fresh-from-rotator"})
    mocked.get(DEVICE_LIST, payload=DEVICES)
    flow.async_update_reload_and_abort = lambda e, data: {"type": "abort", "data": data}

    result = await flow.async_step_reauth_confirm()

    assert result["type"] == "abort" and result["data"][CONF_TOKEN] == "fresh-from-rotator"


async def test_pat_reauth_asks_when_the_rotators_token_is_also_rejected(mocked):
    from types import SimpleNamespace

    flow = make_flow()
    flow._reauth_entry = SimpleNamespace(data={CONF_TOKEN: "expired"}, options={}, entry_id="e")
    mocked.get("http://127.0.0.1:8765/pat", payload={"token": "also-expired"})
    mocked.get(DEVICE_LIST, status=401)

    result = await flow.async_step_reauth_confirm()

    assert result["type"] == "form" and result["step_id"] == "reauth_confirm"


async def test_pat_reauth_asks_when_the_rotator_offers_the_same_token(mocked):
    from types import SimpleNamespace

    flow = make_flow()
    flow._reauth_entry = SimpleNamespace(data={CONF_TOKEN: "same"}, options={}, entry_id="e")
    mocked.get("http://127.0.0.1:8765/pat", payload={"token": "same"})

    assert (await flow.async_step_reauth_confirm())["type"] == "form"
