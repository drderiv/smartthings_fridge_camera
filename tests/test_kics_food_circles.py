"""Unit tests for Samsung Family Hub Live Food Circles (KICS EPA) integration."""

import json
import time
import pytest
import requests_mock
from unittest.mock import MagicMock, AsyncMock

from custom_components.samsung_familyhub_fridge.const import (
    KICS_FOODLIST_ENDPOINT,
    KICS_CLIENT_ID,
    KICS_SA_AUTH_URL,
)
from custom_components.samsung_familyhub_fridge.food import SamsungFoodClient
from custom_components.samsung_familyhub_fridge.rotator import RotatorClient, resolve_rotator_url


ROTATOR_URL = "http://127.0.0.1:8765"


def make_client(hass, tmp_path=None, **kwargs):
    """A food client with a local cache dir and a rotator at the default URL."""
    import tempfile
    from pathlib import Path

    cache_dir = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    return SamsungFoodClient(hass, RotatorClient(ROTATOR_URL), cache_dir=cache_dir, **kwargs)


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.config.path.return_value = "/nonexistent/path/token.txt"
    hass.states.get.return_value = None
    return hass


def test_samsung_food_client_fetches_kics_food_circles(mock_hass):
    """Test SamsungFoodClient retrieves live circular crops from KICS EPA endpoint."""
    client = make_client(
        mock_hass,
        token="test_26_char_kics_token_abc",
        location_id="test-loc-1234",
    )

    kics_payload = {
        "foodData": {
            "items": [
                {
                    "content": {
                        "name": "Milk",
                        "imageUrl": "https://s3.amazonaws.com/samsung/milk_crop.jpg",
                        "createdTime": 1727400000,
                        "expirationDate": "2026-10-05",
                    }
                },
                {
                    "content": {
                        "name": "Eggs",
                        "imageUrl": "https://s3.amazonaws.com/samsung/eggs_crop.jpg",
                        "createdTime": 1727401000,
                        "expirationDate": "2026-10-12",
                    }
                },
            ]
        }
    }

    with requests_mock.Mocker() as m:
        m.get(requests_mock.ANY, status_code=404)  # image downloads / rotator: unavailable
        m.get(KICS_FOODLIST_ENDPOINT, json=kics_payload, status_code=200)

        result = client._fetch_food_items_sync("test_26_char_kics_token_abc")

        assert result["total_items"] == 2
        assert result["source"] == "kics_epa_live_circles"
        assert result["items"][0]["name"] == "Milk"
        assert result["items"][0]["image_url"] == "https://s3.amazonaws.com/samsung/milk_crop.jpg"
        assert result["items"][1]["name"] == "Eggs"
        assert result["items"][1]["image_url"] == "https://s3.amazonaws.com/samsung/eggs_crop.jpg"

        # Verify request headers
        req = next(r for r in m.request_history if KICS_FOODLIST_ENDPOINT in r.url)
        assert req.headers["kics-sa-auth-url"] == KICS_SA_AUTH_URL
        assert req.headers["kics-sa-app-id"] == KICS_CLIENT_ID
        assert req.headers["kics-location"] == "test-loc-1234"
        assert "Bearer test_26_char_kics_token_abc" in req.headers["authorization"]


def test_samsung_food_client_falls_back_to_whisk_on_kics_failure(mock_hass):
    """Test that client falls back to Whisk API if KICS EPA returns an error."""
    long_whisk_token = "test_whisk_token_long_enough_to_be_whisk_api_token"
    client = make_client(
        mock_hass,
        token=long_whisk_token,
        location_id="test-loc-1234",
    )

    whisk_payload = {
        "items": [
            {
                "content": {
                    "name": "Apples",
                    "presence_status": "PRESENCE_STATUS_EXISTING",
                    "photo_url": "https://whisk.com/apple.jpg",
                }
            }
        ],
        "paging": {},
    }

    with requests_mock.Mocker() as m:
        # KICS returns 401
        m.get(KICS_FOODLIST_ENDPOINT, status_code=401)
        # Whisk succeeds
        m.get("https://api.whisk.com/foodlist/v2", json=whisk_payload, status_code=200)

        result = client._fetch_food_items_sync(long_whisk_token)

        assert result["total_items"] == 1
        assert result["items"][0]["name"] == "Apples"
        assert result["items"][0]["image_url"] == "https://whisk.com/apple.jpg"


def test_samsung_food_client_falls_back_to_stock_photo_when_circle_missing(mock_hass):
    """Test that client falls back to Whisk stock photo if live camera circle is missing."""
    client = make_client(
        mock_hass,
        token="test_kics_token",
        location_id="test-loc-1234",
    )

    kics_payload = {
        "foodData": {
            "items": [
                {
                    "id": "food@item-yogurt-123",
                    "content": {
                        "name": "Greek Nonfat Yogurt",
                        "imageUrl": None,  # Camera circle missing/failed
                    }
                }
            ]
        }
    }

    whisk_payload = {
        "items": [
            {
                "id": "food@item-yogurt-123",
                "content": {
                    "name": "Greek Nonfat Yogurt",
                    "photo_url": "https://whisk.com/yogurt_stock.jpg",
                }
            }
        ]
    }

    with requests_mock.Mocker() as m:
        m.get("https://whisk.com/yogurt_stock.jpg", status_code=404)  # cache download fails -> keep remote URL
        m.get(KICS_FOODLIST_ENDPOINT, json=kics_payload, status_code=200)
        m.get("http://127.0.0.1:8765/food_token", json={"token": "mock_whisk_token_for_stock_photos_12345"}, status_code=200)
        m.get("https://api.whisk.com/foodlist/v2", json=whisk_payload, status_code=200)

        result = client._fetch_food_items_sync("test_kics_token")

        assert result["total_items"] == 1
        assert result["items"][0]["name"] == "Greek Nonfat Yogurt"
        # image_url successfully fell back to Whisk stock photo!
        assert result["items"][0]["image_url"] == "https://whisk.com/yogurt_stock.jpg"


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

    flow = SamsungFamilyHubOptionsFlow(entry)

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


def _pat_coordinator(mock_hass, token):
    from custom_components.samsung_familyhub_fridge.api import FamilyHub
    from custom_components.samsung_familyhub_fridge.coordinator import FridgeCoordinator

    async def _mock_run(func, *args):
        return func(*args)

    mock_hass.async_add_executor_job = AsyncMock(side_effect=_mock_run)
    api = FamilyHub(token=token, device_id="test_device_id")
    api.async_ensure_fresh_token = AsyncMock()
    api.get_current_device_status = MagicMock(return_value={})
    api.set_current_device_status = MagicMock()
    api.extract_device_data = MagicMock()
    api.get_file_ids = MagicMock(return_value=[])
    entry = MagicMock()
    entry.options = {}
    entry.data = {"token": token}
    coordinator = FridgeCoordinator(mock_hass, entry, api, rotator=RotatorClient(ROTATOR_URL))
    return coordinator, api, entry


@pytest.mark.asyncio
async def test_coordinator_picks_up_rotated_pat_from_rotator(mock_hass):
    """A new PAT from the rotator replaces the old one and is saved in the config entry."""
    coordinator, api, entry = _pat_coordinator(mock_hass, "old_token_123")

    with requests_mock.Mocker() as m:
        m.get(f"{ROTATOR_URL}/pat", json={"token": "fresh_rotator_pat_999"}, status_code=200)
        await coordinator._async_update_data()

    assert api.token == "fresh_rotator_pat_999"
    mock_hass.config_entries.async_update_entry.assert_called_with(
        entry, data={"token": "fresh_rotator_pat_999"}
    )


@pytest.mark.asyncio
async def test_coordinator_rotator_pat_fetch_throttling(mock_hass):
    """The rotator is only asked again when the scheduled rotation time is reached."""
    coordinator, api, _entry = _pat_coordinator(mock_hass, "current_token")

    with requests_mock.Mocker() as m:
        pat_mock = m.get(f"{ROTATOR_URL}/pat", json={"token": "current_token"}, status_code=200)

        await coordinator._async_update_data()          # first update queries the rotator
        assert pat_mock.call_count == 1

        await coordinator._async_update_data()          # immediately after: throttled
        assert pat_mock.call_count == 1

        coordinator._pat_expires_at = time.time() - 10  # scheduled rotation reached
        await coordinator._async_update_data()
        assert pat_mock.call_count == 2


@pytest.mark.asyncio
async def test_coordinator_retries_with_newer_pat_after_auth_error(mock_hass):
    """On 401 the coordinator asks the rotator once for a newer PAT and retries."""
    from custom_components.samsung_familyhub_fridge.api import AuthenticationError

    coordinator, api, _entry = _pat_coordinator(mock_hass, "stale_token")
    coordinator._pat_expires_at = time.time() + 3600       # rotator not due: only the recovery asks
    calls = {"n": 0}

    def status():
        calls["n"] += 1
        if api.token == "stale_token":
            raise AuthenticationError("401")
        return {}

    api.get_current_device_status = MagicMock(side_effect=status)
    with requests_mock.Mocker() as m:
        m.get(f"{ROTATOR_URL}/pat", json={"token": "new_token"}, status_code=200)
        await coordinator._async_update_data()

    assert api.token == "new_token" and calls["n"] == 2


@pytest.mark.asyncio
async def test_samsung_food_client_token_caching_and_ttl(mock_hass):
    """Test SamsungFoodClient caches KICS token and respects next_rotation_at TTL."""
    async def _mock_run(func, *args):
        return func(*args)
    mock_hass.async_add_executor_job = AsyncMock(side_effect=_mock_run)

    client = make_client(mock_hass)

    with requests_mock.Mocker() as m:
        future_ts = int(time.time() + 82800)
        kics_mock = m.get(
            "http://127.0.0.1:8765/kics_token",
            json={"token": "kics_tok_abc", "next_rotation_at": future_ts},
            status_code=200,
        )

        # 1. First retrieval fetches from rotator and sets expiration
        tok1 = await client.async_get_token()
        assert tok1 == "kics_tok_abc"
        assert kics_mock.call_count == 1
        assert client._kics_expires_at == float(future_ts)

        # 2. Subsequent call uses cached token directly without HTTP request
        tok2 = await client.async_get_token()
        assert tok2 == "kics_tok_abc"
        assert kics_mock.call_count == 1

        # 3. After expiration, queries rotator again
        client._kics_expires_at = time.time() - 10
        tok3 = await client.async_get_token()
        assert tok3 == "kics_tok_abc"
        assert kics_mock.call_count == 2


