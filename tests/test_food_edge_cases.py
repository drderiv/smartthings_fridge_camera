"""Food client edge cases: disk problems, retries, pagination, token/location fallbacks."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.samsung_familyhub_fridge.const import KICS_FOODLIST_ENDPOINT
from custom_components.samsung_familyhub_fridge.food import (
    FoodImageCache,
    SamsungFoodClient,
    SamsungFoodCoordinator,
    _seconds,
    split_bearer,
)
from custom_components.samsung_familyhub_fridge.rotator import RotatorClient
from tests.test_food_client import JPEG, ROTATOR_URL, WHISK_URL, hass, make_client  # noqa: F401  (fixtures)

try:  # UpdateFailed comes from whichever stub/real module the integration imported
    from custom_components.samsung_familyhub_fridge.food import UpdateFailed
except ImportError:  # pragma: no cover
    UpdateFailed = Exception


# --- helpers -------------------------------------------------------------------------

def test_split_bearer_and_timestamp_helpers():
    assert split_bearer("Bearer abc") == ("Bearer abc", "abc")
    assert split_bearer("Token abc ") == ("Bearer abc", "abc")
    assert split_bearer("abc") == ("Bearer abc", "abc")
    assert [_seconds(v) for v in (None, "x", 5, 1727400000, 1727400000000, "1727400000")] == [
        0, 0, 5, 1727400000, 1727400000, 1727400000,
    ]


# --- image cache disk behaviour -------------------------------------------------------------

def test_cache_survives_missing_directory_and_unwritable_files(tmp_path):
    cache = FoodImageCache(tmp_path / "missing")
    assert cache.cached_names() == set()                  # no directory yet
    assert cache.load_inventory() is None                 # no inventory.json
    assert cache.path("a.jpg") == "/api/samsung_familyhub/food_circles/a.jpg"
    assert cache.sign(cache.path("a.jpg")) == cache.path("a.jpg")     # no hass: plain path

    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert FoodImageCache(blocker / "sub").ensure_directory() is False


def test_cache_store_tolerates_os_errors(tmp_path):
    cache = FoodImageCache(tmp_path)
    (tmp_path / "inventory.json").mkdir()                 # a directory where the file should be
    cache.store({"new_00000000.jpg": JPEG}, set(), {"total_items": 0, "last_synced": None, "items": []})
    assert (tmp_path / "new_00000000.jpg").read_bytes() == JPEG
    (tmp_path / "bad.json").write_text("not json")
    assert FoodImageCache(tmp_path / "nowhere").load_inventory() is None


def test_cache_prune_failure_is_ignored(tmp_path, monkeypatch):
    cache = FoodImageCache(tmp_path)
    (tmp_path / "stale_00000000.jpg").write_bytes(JPEG)
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("busy")))
    cache.store({}, set(), None)                          # must not raise
    assert (tmp_path / "stale_00000000.jpg").exists()


# --- token and location fallbacks ---------------------------------------------------------------

async def test_prime_from_rotator_uses_documents_and_defaults(make_client):
    client = make_client()
    client.prime_from_rotator({"kics_token": "k1", "samsung_food_token": "w1", "location_id": "YOUR_ID"})
    assert client._cached_kics_token == "k1" and client._cached_whisk_token == "w1"
    assert client._kics_expires_at > time.time() and client._whisk_expires_at > time.time()
    assert client._location_id is None                    # placeholder ignored

    client.prime_from_rotator({"kics_token": "k2", "kics_next_rotation_at": 123, "location_id": "loc-9"})
    assert client._kics_expires_at == 123.0 and client._location_id == "loc-9"


async def test_configured_token_wins_and_missing_token_is_reported(make_client, mocked):
    assert await make_client(token=" configured ").async_get_token() == "configured"

    client = make_client()
    mocked.get(f"{ROTATOR_URL}/kics_token", status=404)
    mocked.get(f"{ROTATOR_URL}/food_token", status=404)
    assert await client.async_has_token() is False
    assert await client.async_get_active_food_items() == {"total_items": 0, "last_synced": None, "items": []}


async def test_no_token_returns_cached_inventory(make_client, mocked):
    client = make_client()
    mocked.get(f"{ROTATOR_URL}/kics_token", status=404)
    mocked.get(f"{ROTATOR_URL}/food_token", status=404)
    client._cached_inventory = {"total_items": 1, "last_synced": "t", "items": [{"name": "Cached"}]}
    assert (await client.async_get_active_food_items())["items"] == [{"name": "Cached"}]


async def test_missing_location_id_is_a_clear_failure(make_client, mocked):
    client = make_client(token="test_kics_token")
    mocked.get(f"{ROTATOR_URL}/location_id", status=404)
    with pytest.raises(UpdateFailed, match="Location ID"):
        await client._async_fetch_food_items("test_kics_token")


async def test_location_lookup_errors_fall_through_to_rotator(make_client, mocked):
    async def failing():
        raise aiohttp.ClientConnectionError("offline")

    client = make_client(location_lookup=failing)
    mocked.get(f"{ROTATOR_URL}/location_id", payload={"location_id": "loc-r"})
    assert await client._async_location_id() == "loc-r"


async def test_cached_inventory_is_restored_from_disk(make_client, tmp_path):
    client = make_client()
    (tmp_path / "circles").mkdir()
    (tmp_path / "circles" / "inventory.json").write_text('{"total_items": 1, "last_synced": "t", "items": [{"name": "Disk"}]}')
    await client._async_load_cached_inventory()
    assert client._cached_inventory["items"] == [{"name": "Disk"}]


# --- KICS retries ----------------------------------------------------------------------------------

async def test_kics_retries_gateway_errors_then_succeeds(make_client, mocked, monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = make_client(token="test_kics_token", location_id="loc")
    mocked.get(KICS_FOODLIST_ENDPOINT, status=503, repeat=False)
    mocked.get(KICS_FOODLIST_ENDPOINT, exception=aiohttp.ClientConnectionError("reset"), repeat=False)
    mocked.get(KICS_FOODLIST_ENDPOINT, payload={"foodData": {"items": []}}, repeat=False)
    mocked.get(f"{ROTATOR_URL}/food_token", status=404)

    result = await client._async_fetch_food_items("test_kics_token")

    assert result["total_items"] == 0 and sleeps == [3, 3]


async def test_kics_gives_up_after_repeated_failures(make_client, mocked, monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = make_client(token="test_kics_token", location_id="loc")
    mocked.get(KICS_FOODLIST_ENDPOINT, exception=aiohttp.ClientConnectionError("down"))

    with pytest.raises(UpdateFailed, match="KICS EPA food circles fetch failed"):
        await client._async_fetch_food_items("test_kics_token")

    client._cached_inventory = {"total_items": 1, "last_synced": "t", "items": [{"name": "Kept"}]}
    assert (await client._async_fetch_food_items("test_kics_token"))["items"] == [{"name": "Kept"}]


async def test_kics_404_is_not_retried(make_client, mocked):
    client = make_client(token="test_kics_token", location_id="loc")
    mocked.get(KICS_FOODLIST_ENDPOINT, status=404)
    with pytest.raises(UpdateFailed):
        await client._async_fetch_food_items("test_kics_token")
    assert mocked.count("foodlist") == 1


# --- Whisk (legacy) ---------------------------------------------------------------------------------

LONG = "w" * 50


async def test_whisk_pages_are_combined_and_consumed_items_dropped(make_client, mocked):
    client = make_client(token=LONG, location_id="loc")
    mocked.get(KICS_FOODLIST_ENDPOINT, status=500)
    page = lambda names, cursor: {"items": [{"content": {"name": n, "presence_status": "PRESENCE_STATUS_EXISTING"}} for n in names],
                                  "paging": {"cursors": {"after": cursor}}}
    mocked.get(WHISK_URL, payload=page(["A", "B"], "c2"), repeat=False)
    mocked.get(WHISK_URL, payload={"items": [
        {"content": {"name": "Gone", "consumed_at": "x", "presence_status": "PRESENCE_STATUS_CONSUMED"}},
        {"content": {"name": "C", "presence_status": "PRESENCE_STATUS_PRESENT"}}], "paging": {}}, repeat=False)

    result = await client._async_fetch_food_items(LONG)

    assert [i["name"] for i in result["items"]] == ["A", "B", "C"]


async def test_whisk_errors_keep_cache_or_raise(make_client, mocked):
    client = make_client(token=LONG, location_id="loc")
    mocked.get(KICS_FOODLIST_ENDPOINT, status=500)
    mocked.get(WHISK_URL, status=500)
    with pytest.raises(UpdateFailed, match="Failed to fetch Samsung Food items"):
        await client._async_fetch_food_items(LONG)

    client._cached_inventory = {"total_items": 1, "last_synced": "t", "items": [{"name": "Cached"}]}
    assert (await client._async_fetch_food_items(LONG))["items"] == [{"name": "Cached"}]

    mocked.get(WHISK_URL, exception=aiohttp.ClientConnectionError("down"))
    client._cached_inventory = None
    with pytest.raises(UpdateFailed):
        await client._async_fetch_food_items(LONG)


async def test_stock_photo_lookup_tolerates_bad_payloads(make_client, mocked):
    client = make_client(token="test_kics_token", location_id="loc")
    client._cached_whisk_token = LONG
    mocked.get(WHISK_URL, payload=["not", "a", "dict"])
    assert await client._async_whisk_stock_map() == {}
    mocked.get(WHISK_URL, exception=aiohttp.ClientConnectionError("down"))
    assert await client._async_whisk_stock_map() == {}
    client._cached_whisk_token = "short"
    assert await client._async_whisk_stock_map() == {}


# --- image downloads ----------------------------------------------------------------------------------

async def test_bad_or_failing_image_downloads_are_skipped(make_client, mocked):
    client = make_client()
    mocked.get("https://s3.test/ok.jpg", body=JPEG)
    mocked.get("https://s3.test/tiny.jpg", body=b"x")
    mocked.get("https://s3.test/err.jpg", status=500)
    mocked.get("https://s3.test/down.jpg", exception=aiohttp.ClientConnectionError("down"))
    result = await client._async_download_images({
        "ok.jpg": "https://s3.test/ok.jpg", "tiny.jpg": "https://s3.test/tiny.jpg",
        "err.jpg": "https://s3.test/err.jpg", "down.jpg": "https://s3.test/down.jpg"})
    assert result == {"ok.jpg": JPEG}


async def test_unusable_cache_directory_keeps_remote_urls(make_client, mocked, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    client = SamsungFoodClient(
        MagicMock(), make_client()._session, RotatorClient(make_client()._session, ROTATOR_URL),
        cache_dir=blocker / "sub", token="test_kics_token", location_id="loc",
    )

    async def run(func, *args):
        return func(*args)

    client.hass.async_add_executor_job = run
    mocked.get(KICS_FOODLIST_ENDPOINT, payload={"foodData": {"items": [
        {"id": "i", "content": {"name": "Milk", "imageUrl": "https://s3.test/milk.jpg"}}]}})
    mocked.get(f"{ROTATOR_URL}/food_token", status=404)

    result = await client._async_fetch_food_items("test_kics_token")

    assert result["items"][0]["image_url"] == "https://s3.test/milk.jpg"


# --- coordinator ------------------------------------------------------------------------------------------

async def test_food_coordinator_maps_network_errors_and_debounces_door_close(hass, make_client, monkeypatch):
    client = make_client(token="t", location_id="l")

    async def boom():
        raise aiohttp.ClientConnectionError("down")

    client.async_get_active_food_items = boom
    coordinator = SamsungFoodCoordinator(hass, MagicMock(), client, 600)
    with pytest.raises(UpdateFailed, match="Failed to fetch Samsung Food inventory"):
        await coordinator._async_update_data()

    created = []
    hass.async_create_task = lambda coro: (created.append(coro), MagicMock(done=lambda: False, cancel=MagicMock()))[1]
    coordinator.async_trigger_door_close_sync(1)
    first_task = coordinator._delay_task
    coordinator.async_trigger_door_close_sync(1)           # a newer door close cancels the pending refresh
    first_task.cancel.assert_called_once()
    for coro in created:
        coro.close()
    await coordinator.async_shutdown()


# --- signed image links ---------------------------------------------------------------------

def _signing_cache(monkeypatch, tmp_path):
    from datetime import timedelta
    from custom_components.samsung_familyhub_fridge import food

    calls = []

    def fake_sign(hass, path, expiration, **kwargs):
        calls.append((path, expiration, kwargs))
        return f"{path}?authSig=sig{len(calls)}"

    now = {"t": 1_000_000.0}
    monkeypatch.setattr(food, "async_sign_path", fake_sign)
    monkeypatch.setattr(food.time, "time", lambda: now["t"])
    return FoodImageCache(tmp_path, object()), calls, now, timedelta


def test_links_are_signed_for_24_hours_and_resigned_after_12(monkeypatch, tmp_path):
    cache, calls, now, timedelta = _signing_cache(monkeypatch, tmp_path)
    path = cache.path("a.jpg")

    first = cache.sign(path)
    assert calls == [(path, timedelta(hours=24), {"use_content_user": True})]
    now["t"] += 11 * 3600
    assert cache.sign(path) == first and len(calls) == 1          # stable while fresh: no state churn
    now["t"] += 2 * 3600                                          # 13 h old
    assert cache.sign(path) != first and len(calls) == 2


def test_sign_inventory_signs_a_copy_and_leaves_other_urls_alone(monkeypatch, tmp_path):
    cache, calls, _, _ = _signing_cache(monkeypatch, tmp_path)
    stored = {"total_items": 3, "last_synced": "t", "items": [
        {"name": "Milk", "image_url": cache.path("a.jpg")},
        {"name": "Old", "image_url": cache.path("b.jpg") + "?sig=0123456789abcdef"},   # v2.0.0 link on disk
        {"name": "Apples", "image_url": "https://whisk.test/apple.jpg"},
        {"name": "Plain", "image_url": None},
    ]}

    signed = cache.sign_inventory(stored)

    assert [i["image_url"] for i in signed["items"]] == [
        cache.path("a.jpg") + "?authSig=sig1", cache.path("b.jpg") + "?authSig=sig2",
        "https://whisk.test/apple.jpg", None,
    ]
    assert stored["items"][0]["image_url"] == cache.path("a.jpg")  # the stored inventory is untouched
    assert signed["total_items"] == 3


def test_signed_links_of_removed_items_are_forgotten(monkeypatch, tmp_path):
    cache, calls, _, _ = _signing_cache(monkeypatch, tmp_path)
    cache.sign_inventory({"items": [{"image_url": cache.path("a.jpg")}, {"image_url": cache.path("b.jpg")}]})
    cache.sign_inventory({"items": [{"image_url": cache.path("a.jpg")}]})
    assert set(cache._signed) == {cache.path("a.jpg")}
