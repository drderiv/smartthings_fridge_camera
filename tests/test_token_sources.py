"""Token/location sources: rotator-only, persisted cache, no file or helper-entity scanning."""
import time
from unittest.mock import MagicMock

import pytest

from custom_components.samsung_familyhub_fridge.food import SamsungFoodClient
from custom_components.samsung_familyhub_fridge.rotator import RotatorClient

ROTATOR = "http://rotator.test:8765"


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.config.path.return_value = "/nonexistent/path/token.txt"
    hass.states.get.return_value = None
    return hass


@pytest.fixture
def client(mock_hass, session, tmp_path):
    return SamsungFoodClient(
        mock_hass, session, RotatorClient(session, ROTATOR, "secret-key"), cache_dir=tmp_path / "cache"
    )


async def test_rotator_token_sends_api_key(client, mocked):
    mocked.get(f"{ROTATOR}/kics_token", payload={"token": "kics-abc", "next_rotation_at": int(time.time()) + 3600})
    assert await client._async_rotator_token() == "kics-abc"
    assert mocked.calls[-1].kwargs["headers"]["Authorization"] == "Bearer secret-key"


async def test_no_token_files_or_helpers_are_read(client, tmp_path, monkeypatch, mock_hass, mocked):
    """A token file in /tmp or an input_text helper must never be picked up."""
    (tmp_path / "kics_food_token.txt").write_text("planted-token")
    monkeypatch.chdir(tmp_path)
    mock_hass.states.get.side_effect = lambda ent: MagicMock(state="helper-token")
    mocked.get(f"{ROTATOR}/kics_token", status=503)
    mocked.get(f"{ROTATOR}/food_token", status=503)
    assert await client._async_rotator_token() is None


async def test_location_id_order_smartthings_then_rotator(client, mocked):
    async def from_smartthings():
        return "loc-from-smartthings"

    async def nothing():
        return None

    mocked.get(f"{ROTATOR}/location_id", payload={"location_id": "loc-from-rotator"})
    client._location_lookup = from_smartthings
    assert await client._async_location_id() == "loc-from-smartthings"

    client._location_id = None
    client._location_lookup = nothing
    assert await client._async_location_id() == "loc-from-rotator"


async def test_token_cache_round_trip(client, mock_hass, session):
    client._cached_kics_token = "kics-abc"
    client._kics_expires_at = time.time() + 3600
    client._location_id = "loc-1"
    await client._async_persist_cache()

    restored = SamsungFoodClient(
        mock_hass, session, RotatorClient(session, ROTATOR), cache_dir=client._images.directory
    )
    restored._store = client._store
    await restored.async_restore_cache()
    assert restored._cached_kics_token == "kics-abc"
    assert restored._location_id == "loc-1"


async def test_expired_cached_token_not_restored(client, mock_hass, session):
    client._cached_kics_token = "stale"
    client._kics_expires_at = time.time() - 10
    await client._async_persist_cache()

    restored = SamsungFoodClient(
        mock_hass, session, RotatorClient(session, ROTATOR), cache_dir=client._images.directory
    )
    restored._store = client._store
    await restored.async_restore_cache()
    assert restored._cached_kics_token is None


# --- polling interval / rate limiting -----------------------------------------

def test_default_poll_interval_is_one_minute():
    from custom_components.samsung_familyhub_fridge.const import DEFAULT_POLL_INTERVAL
    from datetime import timedelta
    from custom_components.samsung_familyhub_fridge.coordinator import FridgeCoordinator

    coordinator = FridgeCoordinator(MagicMock(), MagicMock(), MagicMock())
    assert DEFAULT_POLL_INTERVAL == 60
    assert coordinator.update_interval == timedelta(seconds=60)
    assert FridgeCoordinator(MagicMock(), MagicMock(), MagicMock(), poll_interval=30).update_interval == timedelta(seconds=30)


@pytest.mark.asyncio
async def test_rate_limit_skips_poll_without_error_and_recovers(caplog):
    import logging
    from custom_components.samsung_familyhub_fridge.api import RateLimitError
    from custom_components.samsung_familyhub_fridge.coordinator import FridgeCoordinator

    hass = MagicMock()
    api = MagicMock()
    api.token = "t"
    api.device_id = "dev"
    api.should_update = False
    api.get_file_ids.return_value = []

    async def ensure():
        return None

    calls = {"n": 0}

    async def status():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RateLimitError("429")
        return {}

    api.async_ensure_fresh_token = ensure
    api.get_current_device_status = status
    coordinator = FridgeCoordinator(hass, MagicMock(), api)
    coordinator._pat_expires_at = 10**12            # skip the rotator probe
    with caplog.at_level(logging.INFO):
        await coordinator._async_update_data()      # rate limited: skipped quietly (no exception)
        await coordinator._async_update_data()
        await coordinator._async_update_data()      # recovered
    warnings = [r for r in caplog.records if "rate limiting" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == 1                       # logged once per episode
    assert any("no longer rate limiting" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
