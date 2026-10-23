"""Config flow paths: OAuth helper errors, PAT reauth, Standalone OAuth reauth, Samsung step errors."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

import custom_components.samsung_familyhub_fridge.config_flow as config_flow
from custom_components.samsung_familyhub_fridge.auth import AuthError, OAuthCredentials
from custom_components.samsung_familyhub_fridge.config_flow import ConfigFlow
from custom_components.samsung_familyhub_fridge.const import (
    AUTH_MODE_PAT,
    AUTH_MODE_STANDALONE_OAUTH,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_OAUTH_CLIENT_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_TOKEN,
)
from tests.conftest import _HomeAssistant

DEVICE_LIST = "https://client.smartthings.com/devices/status"
DEVICES = {"items": [{"capabilityId": "samsungce.viewInside", "attributeName": "contents", "deviceId": "fridge-42"}]}


def make_flow(entry=None):
    flow = ConfigFlow()
    flow.hass = _HomeAssistant()
    flow._reauth_entry = entry
    return flow


def entry(**data):
    return SimpleNamespace(data=data, entry_id="e1")


def creds(refresh="new-refresh"):
    return OAuthCredentials(access_token="a", refresh_token=refresh)


# --- helpers ------------------------------------------------------------------------------------

def test_redirect_uri_falls_back_when_home_assistant_has_no_url():
    # use the exception class the integration itself imported (the stub module can be loaded twice)
    with patch.object(config_flow.config_entry_oauth2_flow, "async_get_redirect_uri", side_effect=config_flow.HomeAssistantError("no url")):
        assert config_flow._redirect_uri(MagicMock()) == config_flow.DEFAULT_REDIRECT_URI


async def test_exchange_code_error_mapping():
    oauth = MagicMock()
    oauth.exchange_code = AsyncMock(side_effect=aiohttp.ClientConnectionError("down"))
    assert await config_flow._exchange_code(_HomeAssistant(), oauth, "abc") == (None, "code_exchange_failed")

    oauth.exchange_code = AsyncMock(return_value=creds())
    assert (await config_flow._exchange_code(_HomeAssistant(), oauth, "abc"))[1] is None

    with patch.object(config_flow.SmartThingsOAuth, "extract_code_from_redirect", side_effect=ValueError("no code")):
        assert await config_flow._exchange_code(_HomeAssistant(), oauth, "http://x") == (None, "invalid_redirect_url")


def test_credentials_input_validation():
    assert config_flow._credentials_input({CONF_OAUTH_CLIENT_ID: " id ", CONF_OAUTH_CLIENT_SECRET: "s"}) == ("id", "s", {})
    assert config_flow._credentials_input({CONF_OAUTH_CLIENT_ID: "", CONF_OAUTH_CLIENT_SECRET: "s"})[2] == {CONF_OAUTH_CLIENT_ID: "required"}
    assert config_flow._credentials_input({CONF_OAUTH_CLIENT_ID: "i", CONF_OAUTH_CLIENT_SECRET: ""})[2] == {CONF_OAUTH_CLIENT_SECRET: "required"}


# --- Standalone OAuth: first-time setup --------------------------------------------------------------

async def test_credentials_step_validates_and_starts_the_link_step():
    flow = make_flow()
    assert (await flow.async_step_standalone_oauth_credentials({CONF_OAUTH_CLIENT_ID: "", CONF_OAUTH_CLIENT_SECRET: ""}))["errors"]

    result = await flow.async_step_standalone_oauth_credentials({CONF_OAUTH_CLIENT_ID: "cid", CONF_OAUTH_CLIENT_SECRET: "sec"})
    assert result["step_id"] == "standalone_oauth_link"
    assert result["description_placeholders"]["authorization_url"].startswith("https://api.smartthings.com/oauth/authorize")


async def test_link_step_without_oauth_restarts_and_rejects_empty_input():
    flow = make_flow()
    assert (await flow.async_step_standalone_oauth_link())["step_id"] == "standalone_oauth_credentials"

    flow._standalone_oauth = MagicMock(get_authorization_url=MagicMock(return_value="https://auth"))
    result = await flow.async_step_standalone_oauth_link({"redirect_url_or_code": "  "})
    assert result["errors"] == {"redirect_url_or_code": "required"}


async def test_link_step_failed_exchange_shows_error():
    flow = make_flow()
    flow._standalone_oauth = MagicMock(exchange_code=AsyncMock(side_effect=aiohttp.ClientConnectionError("x")),
                                       get_authorization_url=MagicMock(return_value="https://auth"))
    result = await flow.async_step_standalone_oauth_link({"redirect_url_or_code": "code"})
    assert result["errors"] == {"redirect_url_or_code": "code_exchange_failed"}


async def test_samsung_step_error_mapping_and_skip():
    flow = make_flow()
    flow._standalone_client_id, flow._standalone_client_secret, flow._standalone_refresh_token = "c", "s", "r"
    flow.async_create_entry = lambda title, data: {"type": "create_entry", "data": data}
    form = {"samsung_email": "u@x.y", "samsung_password": "p"}

    with patch.object(config_flow, "SamsungAccountAuth") as auth:
        auth.return_value.login_iot = AsyncMock(side_effect=AuthError("bad"))
        assert (await flow.async_step_standalone_oauth_samsung(form))["errors"] == {"base": "samsung_login_failed"}
        auth.return_value.login_iot = AsyncMock(side_effect=aiohttp.ClientConnectionError("down"))
        assert (await flow.async_step_standalone_oauth_samsung(form))["errors"] == {"base": "cannot_connect"}

    skipped = await flow.async_step_standalone_oauth_samsung({})        # no Samsung credentials: skip IoT
    assert skipped["type"] == "create_entry" and CONF_OAUTH_REFRESH_TOKEN in skipped["data"]


# --- PAT ---------------------------------------------------------------------------------------------

async def test_pat_flow_unexpected_error_is_reported(mocked):
    flow = make_flow()
    with patch.object(config_flow, "_validate_pat", side_effect=RuntimeError("boom")):
        assert (await flow.async_step_pat({CONF_TOKEN: "t"}))["errors"] == {"base": "unknown"}


async def test_pat_reauth_updates_the_entry(mocked):
    flow = make_flow(entry(**{CONF_AUTH_MODE: AUTH_MODE_PAT, CONF_TOKEN: "old", CONF_DEVICE_ID: "fridge-42"}))
    mocked.get(DEVICE_LIST, payload=DEVICES)
    flow.async_update_reload_and_abort = lambda e, data: {"type": "abort", "data": data}

    result = await flow.async_step_reauth_confirm({CONF_TOKEN: "new"})

    assert result["data"][CONF_TOKEN] == "new" and result["data"][CONF_AUTH_MODE] == AUTH_MODE_PAT


@pytest.mark.parametrize(
    ("status", "error"), [(401, "invalid_auth"), (429, "cannot_connect")],
)
async def test_pat_reauth_error_mapping(mocked, status, error):
    flow = make_flow(entry(**{CONF_TOKEN: "old"}))
    mocked.get(DEVICE_LIST, status=status)
    assert (await flow.async_step_reauth_confirm({CONF_TOKEN: "x"}))["errors"] == {"base": error}


async def test_pat_reauth_unexpected_error():
    flow = make_flow(entry(**{CONF_TOKEN: "old"}))
    with patch.object(config_flow, "_validate_pat", side_effect=RuntimeError("boom")):
        assert (await flow.async_step_reauth_confirm({CONF_TOKEN: "x"}))["errors"] == {"base": "unknown"}


async def test_reauth_confirm_without_input_shows_the_form():
    result = await make_flow(entry()).async_step_reauth_confirm()
    assert result["type"] == "form" and result["step_id"] == "reauth_confirm"


# --- Standalone OAuth reauth ----------------------------------------------------------------------------

async def test_standalone_reauth_walks_through_credentials_and_code():
    flow = make_flow(entry(**{CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH, CONF_OAUTH_REFRESH_TOKEN: "old", "keep": "me"}))
    flow.async_update_reload_and_abort = lambda e, data: {"type": "abort", "data": data}

    assert (await flow.async_step_reauth_standalone_oauth({CONF_OAUTH_CLIENT_ID: "", CONF_OAUTH_CLIENT_SECRET: ""}))["errors"]
    result = await flow.async_step_reauth_standalone_oauth({CONF_OAUTH_CLIENT_ID: "cid", CONF_OAUTH_CLIENT_SECRET: "sec"})
    assert result["step_id"] == "reauth_standalone_oauth_link"

    assert (await flow.async_step_reauth_standalone_oauth_link({"redirect_url_or_code": ""}))["errors"] == {"redirect_url_or_code": "required"}
    flow._reauth_standalone_oauth.exchange_code = AsyncMock(side_effect=aiohttp.ClientConnectionError("x"))
    assert (await flow.async_step_reauth_standalone_oauth_link({"redirect_url_or_code": "c"}))["errors"] == {"redirect_url_or_code": "code_exchange_failed"}

    flow._reauth_standalone_oauth.exchange_code = AsyncMock(return_value=creds("fresh"))
    done = await flow.async_step_reauth_standalone_oauth_link({"redirect_url_or_code": "c"})
    assert done["data"] == {CONF_AUTH_MODE: AUTH_MODE_STANDALONE_OAUTH, CONF_OAUTH_REFRESH_TOKEN: "fresh", "keep": "me",
                            CONF_OAUTH_CLIENT_ID: "cid", CONF_OAUTH_CLIENT_SECRET: "sec"}


async def test_standalone_reauth_link_without_oauth_restarts():
    assert (await make_flow(entry()).async_step_reauth_standalone_oauth_link())["step_id"] == "reauth_standalone_oauth"
