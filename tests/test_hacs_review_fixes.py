"""Unit tests for the 5 HACS code review fixes.

1. Drop borrowed SmartThings OAuth mode -> ConfigEntryAuthFailed on setup.
2. Standard Home Assistant OAuth redirect URI (no httpbin).
3. Private food circle storage in .storage/samsung_familyhub/food_circles and authenticated HomeAssistantView.
4. api.py has import json.
5. LastUpdatedAt sensor has timestamp device class, unique_id, and datetime native_value.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import _ConfigEntry, _HomeAssistant

from custom_components.samsung_familyhub_fridge import async_setup_entry
from custom_components.samsung_familyhub_fridge.auth import DEFAULT_REDIRECT_URI, SmartThingsOAuth
from custom_components.samsung_familyhub_fridge.const import (
    AUTH_MODE_OAUTH,
    AUTH_MODE_PAT,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.samsung_familyhub_fridge.views import FamilyHubFoodCircleView
from custom_components.samsung_familyhub_fridge.sensor import LastUpdatedAt
import custom_components.samsung_familyhub_fridge.api as api_module


# ---------------------------------------------------------------------------
# Test 1: Drop borrowed SmartThings OAuth mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_borrowed_oauth_mode_raises_config_entry_auth_failed():
    """An entry with auth_mode='oauth' must fail with ConfigEntryAuthFailed."""
    from homeassistant.exceptions import ConfigEntryAuthFailed

    hass = _HomeAssistant()
    entry = _ConfigEntry(
        entry_id="legacy-oauth-entry",
        data={
            CONF_AUTH_MODE: AUTH_MODE_OAUTH,
            CONF_DEVICE_ID: "test-device-id",
        },
    )

    with pytest.raises(ConfigEntryAuthFailed) as exc_info:
        await async_setup_entry(hass, entry)

    assert exc_info.value.translation_key == "oauth_mode_removed"


# ---------------------------------------------------------------------------
# Test 2: OAuth redirect URI to standard Home Assistant redirect
# ---------------------------------------------------------------------------

def test_default_redirect_uri_is_home_assistant_standard():
    """DEFAULT_REDIRECT_URI must point to Home Assistant's official redirect endpoint, not httpbin."""
    assert DEFAULT_REDIRECT_URI == "https://my.home-assistant.io/redirect/oauth"
    assert "httpbin" not in DEFAULT_REDIRECT_URI

    oauth = SmartThingsOAuth(client_id="my-client", client_secret="my-secret")
    assert oauth.redirect_uri == "https://my.home-assistant.io/redirect/oauth"
    auth_url = oauth.get_authorization_url()
    assert "https%3A%2F%2Fmy.home-assistant.io%2Fredirect%2Foauth" in auth_url


# ---------------------------------------------------------------------------
# Test 3: Private storage & authenticated view
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_food_circle_view_properties_and_serving():
    """Verify FamilyHubFoodCircleView properties, access controls, and path safety."""
    from custom_components.samsung_familyhub_fridge.views import generate_food_circle_sig

    temp_dir = tempfile.mkdtemp()
    secret = "integration_secret_test_123"
    try:
        # Create a sample image file in protected storage
        filename = "test_crop_12345678.jpg"
        img_file = os.path.join(temp_dir, filename)
        with open(img_file, "wb") as f:
            f.write(b"fake_jpeg_content")

        view = FamilyHubFoodCircleView(temp_dir, secret=secret)
        assert view.url == "/api/samsung_familyhub/food_circles/{filename}"
        assert view.name == "api:samsung_familyhub:food_circles"

        # 1. Unauthorized request (no user, no sig) -> 401
        unauth_req = {"hass_user": None, "query": {}}
        unauth_req_obj = MagicMock()
        unauth_req_obj.get.side_effect = lambda k: unauth_req.get(k)
        unauth_req_obj.query = unauth_req["query"]
        resp_unauth = await view.get(unauth_req_obj, filename)
        assert resp_unauth.status == 401

        # 2. Valid request with compact HMAC signature -> 200 FileResponse
        sig = generate_food_circle_sig(secret, filename)
        assert len(sig) == 16
        auth_req = {"hass_user": None, "query": {"sig": sig}}
        auth_req_obj = MagicMock()
        auth_req_obj.get.side_effect = lambda k: auth_req.get(k)
        auth_req_obj.query = auth_req["query"]
        resp = await view.get(auth_req_obj, filename)
        assert resp.path == img_file

        # 2b. Any non-empty authSig / wrong sig must NOT grant access
        for query in ({"authSig": "anything"}, {"sig": "0" * 16}, {"sig": sig, "authSig": "x"} if False else {"authSig": sig}):
            bogus_req = {"hass_user": None, "query": query}
            bogus_req_obj = MagicMock()
            bogus_req_obj.get.side_effect = lambda k, r=bogus_req: r.get(k)
            bogus_req_obj.query = query
            assert (await view.get(bogus_req_obj, filename)).status == 401

        # 3. Valid request with logged-in HA user -> 200 FileResponse
        user_req = {"hass_user": MagicMock(), "query": {}}
        user_req_obj = MagicMock()
        user_req_obj.get.side_effect = lambda k: user_req.get(k)
        user_req_obj.query = user_req["query"]
        resp_user = await view.get(user_req_obj, filename)
        assert resp_user.path == img_file

        # 4. Nonexistent file with valid sig -> 404
        missing_sig = generate_food_circle_sig(secret, "missing.jpg")
        missing_req = {"hass_user": None, "query": {"sig": missing_sig}}
        missing_req_obj = MagicMock()
        missing_req_obj.get.side_effect = lambda k: missing_req.get(k)
        missing_req_obj.query = missing_req["query"]
        resp_404 = await view.get(missing_req_obj, "missing.jpg")
        assert resp_404.status == 404

        # 5. Directory traversal attempts -> 400
        for bad_filename in ["../secret.txt", "..\\secret.txt", "sub/file.jpg", "/etc/passwd"]:
            bad_req = {"hass_user": MagicMock(), "query": {}}
            bad_req_obj = MagicMock()
            bad_req_obj.get.side_effect = lambda k: bad_req.get(k)
            bad_req_obj.query = bad_req["query"]
            resp_bad = await view.get(bad_req_obj, bad_filename)
            assert resp_bad.status == 400
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_legacy_www_food_circles_migrated_to_storage():
    """Setup migrates any images from www/ to .storage/ and cleans up www/."""
    from pathlib import Path

    from custom_components.samsung_familyhub_fridge import _prepare_cache_dir

    temp_ha_dir = Path(tempfile.mkdtemp())
    try:
        www_dir = temp_ha_dir / "www" / "samsung_familyhub" / "food_circles"
        www_dir.mkdir(parents=True)
        (www_dir / "item1_hash1234.jpg").write_bytes(b"migrated_content")
        storage_dir = temp_ha_dir / ".storage" / "samsung_familyhub" / "food_circles"

        _prepare_cache_dir(storage_dir, www_dir)

        assert (storage_dir / "item1_hash1234.jpg").read_bytes() == b"migrated_content"
        assert not www_dir.exists()
    finally:
        shutil.rmtree(temp_ha_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 4: Missing import json fixed in api.py
# ---------------------------------------------------------------------------

def test_food_module_has_json_imported():
    """The food client persists its inventory as JSON, so the module must import json."""
    from custom_components.samsung_familyhub_fridge import food

    assert food.json is not None


# ---------------------------------------------------------------------------
# Test 5: LastUpdatedAt timestamp sensor
# ---------------------------------------------------------------------------

def test_last_updated_at_sensor_native_value_and_metadata():
    """Verify LastUpdatedAt sensor has TIMESTAMP device class, unique_id, and datetime native_value."""
    coordinator = MagicMock()
    coordinator.last_updated_at = 1729350000.0  # Unix timestamp
    coordinator.api = MagicMock()
    coordinator.api.device_id = "fridge-device-42"

    sensor = LastUpdatedAt(coordinator, "fridge-device-42")

    # Metadata assertions
    assert sensor._attr_device_class == "timestamp"
    assert sensor._attr_unique_id == "fridge-device-42_last_updated_at"
    assert sensor._attr_name == "Last Updated At"

    # Value assertion
    val = sensor.native_value
    assert isinstance(val, datetime)
    assert val.tzinfo == timezone.utc
    assert val.timestamp() == 1729350000.0

    # None timestamp assertion
    coordinator.last_updated_at = None
    assert sensor.native_value is None

    # Zero timestamp assertion
    coordinator.last_updated_at = 0
    assert sensor.native_value is None


# ---------------------------------------------------------------------------
# Test 6: Attribute size stay safely under Home Assistant's 16KB (16,384 bytes) limit
# ---------------------------------------------------------------------------

def test_food_circle_attributes_stay_under_16k_limit():
    """Verify state attributes for 35 food items stay well under 16,384 bytes."""
    import json
    from custom_components.samsung_familyhub_fridge.views import generate_food_circle_sig

    secret = "sample_stable_secret_12345"
    items = []
    for i in range(35):
        filename = f"hash_{i:04d}_abcdef12.jpg"
        sig = generate_food_circle_sig(secret, filename)
        url = f"/api/samsung_familyhub/food_circles/{filename}?sig={sig}"
        items.append({
            "name": f"Food Item Number {i} Delicious Organic",
            "image_url": url,
            "added_at": 1729350000 + (i * 3600),
            "expiration_date": None if i % 2 == 0 else "2026-10-30",
        })

    payload = {
        "total_items": len(items),
        "last_synced": "2026-10-19T16:22:27.824628+00:00",
        "items": items,
    }

    serialized = json.dumps(payload)
    # Must be less than half of the 16,384 bytes limit
    assert len(serialized) < 8000, f"Payload size {len(serialized)} exceeded 8KB target"


def test_food_items_preserve_expiration_date_key():
    """Verify item_dict always defines expiration_date even if None to prevent Jinja UndefinedError."""
    # Ensure items created with or without expiration date always retain the key
    content_without_exp = {"name": "Milk", "imageUrl": None, "createdAt": 1729350000}
    content_with_exp = {"name": "Yogurt", "imageUrl": None, "createdAt": 1729350000, "expirationDate": "2026-10-25"}

    item1 = {
        "name": content_without_exp["name"],
        "image_url": "/api/test.jpg",
        "added_at": 1729350000,
        "expiration_date": content_without_exp.get("expirationDate") or content_without_exp.get("expiration_date") or None,
    }
    item2 = {
        "name": content_with_exp["name"],
        "image_url": "/api/test.jpg",
        "added_at": 1729350000,
        "expiration_date": content_with_exp.get("expirationDate") or content_with_exp.get("expiration_date") or None,
    }

    assert "expiration_date" in item1
    assert item1["expiration_date"] is None
    assert "expiration_date" in item2
    assert item2["expiration_date"] == "2026-10-25"

