"""Token/location sources: rotator-only, persisted cache, no file or helper-entity scanning."""
import time
from unittest.mock import MagicMock

import pytest
import requests_mock

from custom_components.samsung_familyhub_fridge.api import SamsungFoodClient
from custom_components.samsung_familyhub_fridge.const import CONF_ROTATOR_API_KEY

ROTATOR = "http://rotator.test:8765"


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.config.path.return_value = "/nonexistent/path/token.txt"
    hass.states.get.return_value = None
    return hass


@pytest.fixture
def client(mock_hass):
    entry = MagicMock()
    entry.options = {CONF_ROTATOR_API_KEY: "secret-key"}
    entry.data = {}
    return SamsungFoodClient(mock_hass, rotator_url=ROTATOR, config_entry=entry)


def test_rotator_token_sends_api_key(client):
    with requests_mock.Mocker() as m:
        m.get(f"{ROTATOR}/kics_token", json={"token": "kics-abc", "next_rotation_at": int(time.time()) + 3600})
        assert client._get_rotator_token_sync() == "kics-abc"
        assert m.last_request.headers["Authorization"] == "Bearer secret-key"


def test_no_token_files_or_helpers_are_read(client, tmp_path, monkeypatch, mock_hass):
    """A token file in /tmp or an input_text helper must never be picked up."""
    (tmp_path / "kics_food_token.txt").write_text("planted-token")
    monkeypatch.chdir(tmp_path)
    mock_hass.states.get.side_effect = lambda ent: MagicMock(state="helper-token")
    with requests_mock.Mocker() as m:
        m.get(f"{ROTATOR}/kics_token", status_code=503)
        m.get(f"{ROTATOR}/food_token", status_code=503)
        assert client._get_rotator_token_sync() is None


def test_location_id_order_smartthings_then_rotator(client, mock_hass):
    hub = MagicMock()
    hub.get_location_id.return_value = "loc-from-smartthings"
    mock_hass.data = {"samsung_familyhub_fridge": {"hub": hub}}
    with requests_mock.Mocker() as m:
        m.get(f"{ROTATOR}/location_id", json={"location_id": "loc-from-rotator"})
        assert client._get_location_id_sync() == "loc-from-smartthings"

    client._location_id = None
    hub.get_location_id.return_value = None
    with requests_mock.Mocker() as m:
        m.get(f"{ROTATOR}/location_id", json={"location_id": "loc-from-rotator"})
        assert client._get_location_id_sync() == "loc-from-rotator"


@pytest.mark.asyncio
async def test_token_cache_round_trip(client, mock_hass):
    client._cached_kics_token = "kics-abc"
    client._kics_expires_at = time.time() + 3600
    client._location_id = "loc-1"
    await client._async_persist_cache()

    restored = SamsungFoodClient(mock_hass, rotator_url=ROTATOR)
    restored._store = client._store
    await restored.async_restore_cache()
    assert restored._cached_kics_token == "kics-abc"
    assert restored._location_id == "loc-1"


@pytest.mark.asyncio
async def test_expired_cached_token_not_restored(client, mock_hass):
    client._cached_kics_token = "stale"
    client._kics_expires_at = time.time() - 10
    await client._async_persist_cache()

    restored = SamsungFoodClient(mock_hass, rotator_url=ROTATOR)
    restored._store = client._store
    await restored.async_restore_cache()
    assert restored._cached_kics_token is None
