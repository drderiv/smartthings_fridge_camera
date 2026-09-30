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
from custom_components.samsung_familyhub_fridge.api import SamsungFoodClient
from tools.pat_rotator.generate_kics_token import validate_kics_token


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.config.path.return_value = "/nonexistent/path/token.txt"
    hass.states.get.return_value = None
    return hass


def test_samsung_food_client_fetches_kics_food_circles(mock_hass):
    """Test SamsungFoodClient retrieves live circular crops from KICS EPA endpoint."""
    client = SamsungFoodClient(
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
    client = SamsungFoodClient(
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
    client = SamsungFoodClient(
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
        m.get(KICS_FOODLIST_ENDPOINT, json=kics_payload, status_code=200)
        m.get("http://127.0.0.1:8765/food_token", json={"token": "mock_whisk_token_for_stock_photos_12345"}, status_code=200)
        m.get("https://api.whisk.com/foodlist/v2", json=whisk_payload, status_code=200)

        result = client._fetch_food_items_sync("test_kics_token")

        assert result["total_items"] == 1
        assert result["items"][0]["name"] == "Greek Nonfat Yogurt"
        # image_url successfully fell back to Whisk stock photo!
        assert result["items"][0]["image_url"] == "https://whisk.com/yogurt_stock.jpg"


from tools.pat_rotator.generate_kics_token import (
    validate_kics_token,
    refresh_kics_token,
    rotate_kics_token,
    AUTH_BASE,
)


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
        acc = rotate_kics_token(
            refresh_token_file=str(ref_file),
            output_file=str(out_file),
        )
        assert acc == "new_access_token_999"
        assert out_file.read_text().strip() == "new_access_token_999"
        assert ref_file.read_text().strip() == "next_rolling_refresh_token_888"


def test_resolve_rotator_base_url_hierarchy(mock_hass):
    """Test resolution order of rotator base URL (options, helper entity, fallback)."""
    from custom_components.samsung_familyhub_fridge.const import (
        DEFAULT_ROTATOR_URL,
        ROTATOR_URL_ENTITY,
        CONF_ROTATOR_URL,
    )
    # 1. Fallback to default
    client = SamsungFoodClient(mock_hass)
    assert client._resolve_rotator_base_url() == DEFAULT_ROTATOR_URL

    # 2. Helper entity override
    mock_helper_state = MagicMock()
    mock_helper_state.state = "http://192.168.1.150:8765"
    mock_hass.states.get.side_effect = lambda ent: mock_helper_state if ent == ROTATOR_URL_ENTITY else None
    assert client._resolve_rotator_base_url() == "http://192.168.1.150:8765"

    # 3. Entry options override
    mock_entry = MagicMock()
    mock_entry.options = {CONF_ROTATOR_URL: "http://remote-host:9999/"}
    mock_entry.data = {}
    client_with_entry = SamsungFoodClient(mock_hass, config_entry=mock_entry)
    assert client_with_entry._resolve_rotator_base_url() == "http://remote-host:9999"


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


@pytest.mark.asyncio
async def test_data_coordinator_direct_rotator_pat_fetch(mock_hass):
    """Test DataCoordinator automatically fetches fresh PAT directly from rotator REST server."""
    from custom_components.samsung_familyhub_fridge.api import DataCoordinator, FamilyHub

    async def _mock_run(func, *args):
        return func(*args)
    mock_hass.async_add_executor_job = AsyncMock(side_effect=_mock_run)

    api = FamilyHub(mock_hass, token="old_token_123", device_id="test_device_id")
    api.async_ensure_fresh_token = AsyncMock()
    api.get_all_device_status = MagicMock(return_value={"components": {}})
    api.set_device_status = MagicMock()
    api.should_update = False
    api.get_file_ids = MagicMock(return_value=["file1"])
    api.download_images = MagicMock(return_value=True)

    mock_entry = MagicMock()
    mock_entry.options = {}
    mock_entry.data = {"token": "old_token_123"}
    mock_entry.entry_id = "test_entry_id"
    mock_hass.config_entries.async_entries.return_value = [mock_entry]

    coordinator = DataCoordinator(mock_hass, api)

    with requests_mock.Mocker() as m:
        m.get("http://127.0.0.1:8765/pat", json={"token": "fresh_rotator_pat_999"}, status_code=200)
        await coordinator._async_update_data()

        assert api.token == "fresh_rotator_pat_999"
        mock_hass.config_entries.async_update_entry.assert_called_with(
            mock_entry, data={"token": "fresh_rotator_pat_999"}
        )


@pytest.mark.asyncio
async def test_data_coordinator_rotator_pat_fetch_throttling(mock_hass):
    """Test DataCoordinator throttles PAT requests to avoid flooding the rotator server."""
    from custom_components.samsung_familyhub_fridge.api import DataCoordinator, FamilyHub

    async def _mock_run(func, *args):
        return func(*args)
    mock_hass.async_add_executor_job = AsyncMock(side_effect=_mock_run)

    api = FamilyHub(mock_hass, token="current_token", device_id="test_device_id")
    api.async_ensure_fresh_token = AsyncMock()
    api.get_all_device_status = MagicMock(return_value={"components": {}})
    api.set_device_status = MagicMock()
    api.should_update = False
    api.get_file_ids = MagicMock(return_value=["file1"])
    api.download_images = MagicMock(return_value=True)

    coordinator = DataCoordinator(mock_hass, api)

    with requests_mock.Mocker() as m:
        pat_mock = m.get("http://127.0.0.1:8765/pat", json={"token": "current_token"}, status_code=200)

        # 1. First update queries the rotator
        await coordinator._async_update_data()
        assert pat_mock.call_count == 1

        # 2. Immediate second update (e.g. 10s door polling) is throttled and does not query
        await coordinator._async_update_data()
        assert pat_mock.call_count == 1

        # 3. Simulate elapsed validity window / scheduled rotation reached
        coordinator._pat_expires_at = time.time() - 10
        await coordinator._async_update_data()
        assert pat_mock.call_count == 2


@pytest.mark.asyncio
async def test_samsung_food_client_token_caching_and_ttl(mock_hass):
    """Test SamsungFoodClient caches KICS token and respects next_rotation_at TTL."""
    async def _mock_run(func, *args):
        return func(*args)
    mock_hass.async_add_executor_job = AsyncMock(side_effect=_mock_run)

    client = SamsungFoodClient(mock_hass, rotator_url="http://127.0.0.1:8765")

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


