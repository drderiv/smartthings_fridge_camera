"""Food client (KICS EPA / Whisk), image cache and PAT pickup by the polling coordinator."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from custom_components.samsung_familyhub_fridge.api import AuthenticationError, FamilyHub
from custom_components.samsung_familyhub_fridge.const import (
    KICS_CLIENT_ID,
    KICS_FOODLIST_ENDPOINT,
    KICS_SA_AUTH_URL,
)
from custom_components.samsung_familyhub_fridge.coordinator import FridgeCoordinator
from custom_components.samsung_familyhub_fridge.food import FoodImageCache, SamsungFoodClient
from custom_components.samsung_familyhub_fridge.rotator import RotatorClient

ROTATOR_URL = "http://127.0.0.1:8765"
WHISK_URL = "https://api.whisk.com/foodlist/v2"
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 200


class _Hass(MagicMock):
    """MagicMock hass whose executor jobs actually run."""


@pytest.fixture
def hass():
    hass = MagicMock()

    async def run(func, *args):
        return func(*args)

    hass.async_add_executor_job = run
    hass.config.path.return_value = "/nonexistent"
    return hass


@pytest.fixture
def make_client(hass, session, tmp_path):
    def build(**kwargs):
        return SamsungFoodClient(
            hass, session, RotatorClient(session, ROTATOR_URL), cache_dir=tmp_path / "circles", **kwargs
        )

    return build


# --- KICS ---------------------------------------------------------------------------

async def test_fetches_kics_food_circles_with_expected_headers(make_client, mocked):
    client = make_client(token="test_26_char_kics_token_abc", location_id="test-loc-1234")
    payload = {"foodData": {"items": [
        {"content": {"name": "Milk", "imageUrl": "https://s3.test/milk.jpg", "createdTime": 1727400000,
                     "expirationDate": "2026-10-05"}},
        {"content": {"name": "Eggs", "imageUrl": "https://s3.test/eggs.jpg", "createdTime": 1727401000000,
                     "expirationDate": "2026-10-12"}},
    ]}}
    mocked.get(KICS_FOODLIST_ENDPOINT, payload=payload)
    mocked.get("http://127.0.0.1:8765/food_token", status=404)
    mocked.get("https://s3.test/milk.jpg", status=404)        # crop download fails -> keep remote URL
    mocked.get("https://s3.test/eggs.jpg", status=404)

    result = await client._async_fetch_food_items("test_26_char_kics_token_abc")

    assert result["total_items"] == 2 and result["source"] == "kics_epa_live_circles"
    assert [i["name"] for i in result["items"]] == ["Milk", "Eggs"]
    assert result["items"][0]["image_url"] == "https://s3.test/milk.jpg"
    assert result["items"][0]["added_at"] == 1727400000
    assert result["items"][1]["added_at"] == 1727401000            # milliseconds are normalised
    headers = next(c.kwargs["headers"] for c in mocked.calls if c.kwargs.get("headers", {}).get("kics-location"))
    assert headers["kics-sa-auth-url"] == KICS_SA_AUTH_URL
    assert headers["kics-sa-app-id"] == KICS_CLIENT_ID
    assert headers["kics-location"] == "test-loc-1234"
    assert headers["authorization"] == "Bearer test_26_char_kics_token_abc"


async def test_crops_are_cached_locally_signed_pruned_and_inventory_saved(make_client, mocked, tmp_path):
    client = make_client(token="test_kics_token", location_id="loc")
    client._images = FoodImageCache(tmp_path / "circles", "secret-123")
    (tmp_path / "circles").mkdir()
    stale = tmp_path / "circles" / "stale_00000000.jpg"
    stale.write_bytes(JPEG)
    payload = {"foodData": {"items": [{"id": "item-1", "content": {"name": "Milk", "imageUrl": "https://s3.test/milk.jpg?X-Amz=1"}}]}}
    mocked.get(KICS_FOODLIST_ENDPOINT, payload=payload)
    mocked.get("http://127.0.0.1:8765/food_token", status=404)
    mocked.get("https://s3.test/milk.jpg", body=JPEG)

    result = await client._async_fetch_food_items("test_kics_token")

    name = FoodImageCache.filename("item-1", "https://s3.test/milk.jpg?X-Amz=1")
    url = result["items"][0]["image_url"]
    assert url.startswith(f"/api/samsung_familyhub/food_circles/{name}?sig=") and len(url.split("sig=")[1]) == 16
    assert (tmp_path / "circles" / name).read_bytes() == JPEG
    assert not stale.exists()                                   # no longer in the inventory
    assert (tmp_path / "circles" / "inventory.json").exists()   # restored after a restart

    # Second sync reuses the cached crop instead of downloading it again
    before = mocked.count("s3.test/milk.jpg")
    await client._async_fetch_food_items("test_kics_token")
    assert mocked.count("s3.test/milk.jpg") == before


async def test_falls_back_to_whisk_when_kics_rejects_a_long_token(make_client, mocked):
    token = "test_whisk_token_long_enough_to_be_whisk_api_token"
    client = make_client(token=token, location_id="loc")
    mocked.get(KICS_FOODLIST_ENDPOINT, status=401)
    mocked.get(WHISK_URL, payload={"items": [{"content": {
        "name": "Apples", "presence_status": "PRESENCE_STATUS_EXISTING", "photo_url": "https://whisk.test/apple.jpg"}}],
        "paging": {}})

    result = await client._async_fetch_food_items(token)

    assert result["total_items"] == 1 and result["items"][0]["name"] == "Apples"
    assert result["items"][0]["image_url"] == "https://whisk.test/apple.jpg"
    assert client._cached_kics_token is None                    # the rejected token was dropped


async def test_whisk_stock_photo_used_when_the_circle_is_missing(make_client, mocked):
    client = make_client(token="test_kics_token", location_id="loc")
    mocked.get(KICS_FOODLIST_ENDPOINT, payload={"foodData": {"items": [
        {"id": "food@yogurt", "content": {"name": "Greek Nonfat Yogurt", "imageUrl": None}}]}})
    mocked.get("http://127.0.0.1:8765/food_token", payload={"token": "mock_whisk_token_for_stock_photos_12345"})
    mocked.get(WHISK_URL, payload={"items": [{"id": "food@yogurt", "content": {
        "name": "Greek Nonfat Yogurt", "photo_url": "https://whisk.test/yogurt.jpg"}}]})
    mocked.get("https://whisk.test/yogurt.jpg", status=404)

    result = await client._async_fetch_food_items("test_kics_token")

    assert result["items"][0]["image_url"] == "https://whisk.test/yogurt.jpg"


async def test_kics_error_keeps_the_cached_inventory(make_client, mocked):
    client = make_client(token="test_kics_token", location_id="loc")
    client._cached_inventory = {"total_items": 1, "items": [{"name": "Cached"}], "last_synced": "t"}
    mocked.get(KICS_FOODLIST_ENDPOINT, status=500)

    result = await client._async_fetch_food_items("test_kics_token")

    assert result["items"] == [{"name": "Cached"}]


# --- token caching ----------------------------------------------------------------------

async def test_kics_token_is_cached_until_its_rotation_time(make_client, mocked):
    client = make_client()
    future = int(time.time() + 82800)
    mocked.get(f"{ROTATOR_URL}/kics_token", payload={"token": "kics_tok_abc", "next_rotation_at": future})

    assert await client.async_get_token() == "kics_tok_abc"
    assert client._kics_expires_at == float(future)
    calls = mocked.count("/kics_token")

    assert await client.async_get_token() == "kics_tok_abc"       # served from the cache
    assert mocked.count("/kics_token") == calls

    client._kics_expires_at = time.time() - 10                    # rotation time reached
    assert await client.async_get_token() == "kics_tok_abc"
    assert mocked.count("/kics_token") == calls + 1


# --- PAT pickup by the coordinator ----------------------------------------------------------

def _pat_coordinator(hass, session, token):
    api = FamilyHub(session, token=token, device_id="test_device_id")

    async def noop():
        return None

    async def status():
        return {}

    api.async_ensure_fresh_token = noop
    api.get_current_device_status = status
    api.get_file_ids = MagicMock(return_value=[])
    entry = MagicMock()
    entry.options = {}
    entry.data = {"token": token}
    coordinator = FridgeCoordinator(hass, entry, api, rotator=RotatorClient(session, ROTATOR_URL))
    return coordinator, api, entry


async def test_coordinator_picks_up_rotated_pat_from_rotator(hass, session, mocked):
    coordinator, api, entry = _pat_coordinator(hass, session, "old_token_123")
    mocked.get(f"{ROTATOR_URL}/pat", payload={"token": "fresh_rotator_pat_999"})

    await coordinator._async_update_data()

    assert api.token == "fresh_rotator_pat_999"
    hass.config_entries.async_update_entry.assert_called_with(entry, data={"token": "fresh_rotator_pat_999"})


async def test_coordinator_throttles_rotator_requests(hass, session, mocked):
    coordinator, api, _entry = _pat_coordinator(hass, session, "current_token")
    mocked.get(f"{ROTATOR_URL}/pat", payload={"token": "current_token"})

    await coordinator._async_update_data()
    assert mocked.count("/pat") == 1
    await coordinator._async_update_data()                       # immediately after: throttled
    assert mocked.count("/pat") == 1
    coordinator._pat_expires_at = time.time() - 10               # scheduled rotation reached
    await coordinator._async_update_data()
    assert mocked.count("/pat") == 2


async def test_coordinator_retries_with_newer_pat_after_auth_error(hass, session, mocked):
    coordinator, api, _entry = _pat_coordinator(hass, session, "stale_token")
    coordinator._pat_expires_at = time.time() + 3600             # not due: only the recovery asks
    calls = {"n": 0}

    async def status():
        calls["n"] += 1
        if api.token == "stale_token":
            raise AuthenticationError("401")
        return {}

    api.get_current_device_status = status
    mocked.get(f"{ROTATOR_URL}/pat", payload={"token": "new_token"})

    await coordinator._async_update_data()

    assert api.token == "new_token" and calls["n"] == 2
