"""Unit tests for Samsung Family Hub Live Food Circles (KICS EPA) integration."""

import json
import pytest
import requests_mock
from unittest.mock import MagicMock

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


def test_validate_kics_token_helper():
    """Test standalone validate_kics_token helper."""
    with requests_mock.Mocker() as m:
        m.get(KICS_FOODLIST_ENDPOINT, status_code=200)
        assert validate_kics_token("valid_token", "loc-123") is True

    with requests_mock.Mocker() as m:
        m.get(KICS_FOODLIST_ENDPOINT, status_code=401)
        assert validate_kics_token("invalid_token", "loc-123") is False

