"""KICS token tooling (tools/pat_rotator), rotator URL resolution and the options flow."""

import json
import time
import pytest
import requests_mock
from unittest.mock import MagicMock, AsyncMock

from custom_components.samsung_familyhub_fridge.const import KICS_FOODLIST_ENDPOINT
from custom_components.samsung_familyhub_fridge.rotator import resolve_rotator_url


from tools.pat_rotator.rotator.files import Paths
from tools.pat_rotator.rotator.kics import AUTH_BASE
from tools.pat_rotator.rotator.kics import refresh as refresh_kics_token
from tools.pat_rotator.rotator.kics import rotate as _rotate
from tools.pat_rotator.rotator.kics import validate_token as validate_kics_token


def test_validate_kics_token_helper():
    """Test standalone validate_kics_token helper."""
    with requests_mock.Mocker() as m:
        m.get(KICS_FOODLIST_ENDPOINT, status_code=200)
        assert validate_kics_token("valid_token", "loc-123") is True

    with requests_mock.Mocker() as m:
        m.get(KICS_FOODLIST_ENDPOINT, status_code=401)
        assert validate_kics_token("invalid_token", "loc-123") is False


def test_refresh_kics_token_success():
    """Test refresh_kics_token exchanges a refresh token for a fresh access token."""
    mock_token_resp = {
        "access_token": "fresh_access_token_xyz123",
        "expires_in": 86400,
        "refresh_token": "fresh_refresh_token_uvw456",
        "refresh_token_expires_in": 7776000,
        "token_type": "Bearer",
    }
    with requests_mock.Mocker() as m:
        m.post(f"{AUTH_BASE}/auth/oauth2/token", json=mock_token_resp, status_code=200)
        payload = refresh_kics_token("old_refresh_token_abc")
        assert payload["access_token"] == "fresh_access_token_xyz123"
        assert payload["refresh_token"] == "fresh_refresh_token_uvw456"


def test_rotate_kics_token_writes_files(tmp_path):
    """Test rotate_kics_token saves new access and rolling refresh tokens to disk."""
    ref_file = tmp_path / "kics_refresh_token.txt"
    out_file = tmp_path / "kics_food_token.txt"
    ref_file.write_text("initial_rolling_refresh_token\n")

    mock_token_resp = {
        "access_token": "new_access_token_999",
        "expires_in": 86400,
        "refresh_token": "next_rolling_refresh_token_888",
    }

    with requests_mock.Mocker() as m:
        m.post(f"{AUTH_BASE}/auth/oauth2/token", json=mock_token_resp, status_code=200)
        acc = _rotate(Paths(base=tmp_path), output=out_file, refresh_file=ref_file)
        assert acc == "new_access_token_999"
        assert out_file.read_text().strip() == "new_access_token_999"
        assert ref_file.read_text().strip() == "next_rolling_refresh_token_888"


def test_resolve_rotator_url_hierarchy(monkeypatch):
    """Entry options/data win, then PAT_ROTATOR_URL, then the default."""
    from custom_components.samsung_familyhub_fridge.const import DEFAULT_ROTATOR_URL, CONF_ROTATOR_URL

    monkeypatch.delenv("PAT_ROTATOR_URL", raising=False)
    assert resolve_rotator_url(None) == DEFAULT_ROTATOR_URL

    monkeypatch.setenv("PAT_ROTATOR_URL", "http://env-host:1111/")
    assert resolve_rotator_url(None) == "http://env-host:1111"

    entry = MagicMock()
    entry.options = {CONF_ROTATOR_URL: "http://remote-host:9999/"}
    entry.data = {}
    assert resolve_rotator_url(entry) == "http://remote-host:9999"


@pytest.mark.asyncio
async def test_options_flow_renders_and_saves():
    """Test SamsungFamilyHubOptionsFlow displays defaults and persists user choices."""
    from custom_components.samsung_familyhub_fridge.config_flow import (
        SamsungFamilyHubOptionsFlow,
    )
    from custom_components.samsung_familyhub_fridge.const import (
        CONF_ROTATOR_URL,
        CONF_LOCATION_ID,
        CONF_FOOD_UPDATE_INTERVAL,
        DEFAULT_ROTATOR_URL,
        DEFAULT_FOOD_UPDATE_INTERVAL,
    )

    entry = MagicMock()
    entry.options = {}
    entry.data = {
        CONF_ROTATOR_URL: DEFAULT_ROTATOR_URL,
        CONF_LOCATION_ID: "loc-guid-1234",
    }

    flow = SamsungFamilyHubOptionsFlow()
    flow.config_entry = entry

    # 1. Test displaying form
    result = await flow.async_step_init()
    assert result["type"] == "form"
    assert result["step_id"] == "init"

    # 2. Test saving options
    user_input = {
        CONF_ROTATOR_URL: "http://10.0.0.50:8765",
        CONF_LOCATION_ID: "loc-guid-custom",
        CONF_FOOD_UPDATE_INTERVAL: 300,
    }
    save_result = await flow.async_step_init(user_input)
    assert save_result["type"] == "create_entry"
    assert save_result["data"][CONF_ROTATOR_URL] == "http://10.0.0.50:8765"
    assert save_result["data"][CONF_LOCATION_ID] == "loc-guid-custom"
    assert save_result["data"][CONF_FOOD_UPDATE_INTERVAL] == 300
