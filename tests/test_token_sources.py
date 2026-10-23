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


# --- polling interval / rate limiting -----------------------------------------

def test_default_poll_interval_is_one_minute():
    from custom_components.samsung_familyhub_fridge.const import DEFAULT_POLL_INTERVAL
    from datetime import timedelta
    from custom_components.samsung_familyhub_fridge.api import DataCoordinator

    coordinator = DataCoordinator(MagicMock(), MagicMock())
    assert DEFAULT_POLL_INTERVAL == 60
    assert coordinator.update_interval == timedelta(seconds=60)
    assert DataCoordinator(MagicMock(), MagicMock(), 30).update_interval == timedelta(seconds=30)


@pytest.mark.asyncio
async def test_rate_limit_skips_poll_without_error_and_recovers(caplog):
    import logging
    from custom_components.samsung_familyhub_fridge.api import DataCoordinator, RateLimitError

    hass = MagicMock()
    api = MagicMock()
    api.token = "t"
    api.device_id = "dev"
    api.should_update = False
    api.get_file_ids.return_value = []

    async def ensure():
        return None

    api.async_ensure_fresh_token = ensure
    calls = {"n": 0}

    async def run_job(func, *a):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RateLimitError("429")
        return {}

    hass.async_add_executor_job = run_job
    coordinator = DataCoordinator(hass, api)
    coordinator._pat_expires_at = 10**12            # skip the rotator probe
    coordinator.data = {"kept": True}
    with caplog.at_level(logging.INFO):
        assert await coordinator._async_update_data() == {"kept": True}
        assert await coordinator._async_update_data() == {"kept": True}
        await coordinator._async_update_data()
    warnings = [r for r in caplog.records if "rate limiting" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == 1                       # logged once per episode
    assert any("no longer rate limiting" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
