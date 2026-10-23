"""Token refresher: OAuth access tokens are renewed shortly before they expire."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.samsung_familyhub_fridge.api import FamilyHub
from custom_components.samsung_familyhub_fridge.coordinator import FridgeCoordinator
from tests.conftest import _HomeAssistant


def credentials(token, expires_in=3600):
    return SimpleNamespace(access_token=token, expires_in=expires_in)


@pytest.fixture
def hass():
    return _HomeAssistant()


def make_refresher(*tokens):
    calls = []
    queue = list(tokens)

    async def refresher():
        calls.append(1)
        return credentials(queue.pop(0))

    return refresher, calls


async def test_pat_hub_without_refresher_is_untouched(session):
    hub = FamilyHub(session, token="pat-token", device_id="d")
    await hub.async_ensure_fresh_token()
    assert hub.token == "pat-token"


async def test_fresh_token_is_not_refreshed(session):
    refresher, calls = make_refresher("never-used")
    hub = FamilyHub(session, token="initial", device_id="d")
    hub.set_token_refresher(refresher, expires_in=3600)
    await hub.async_ensure_fresh_token()
    assert calls == [] and hub.token == "initial"


async def test_token_about_to_expire_is_refreshed_and_header_updated(session):
    refresher, calls = make_refresher("rotated")
    hub = FamilyHub(session, token="initial", device_id="d")
    hub.set_token_refresher(refresher, expires_in=60)        # inside the 5-minute margin
    await hub.async_ensure_fresh_token()
    assert calls == [1]
    assert hub.token == "rotated"
    assert hub._headers["Authorization"] == "Bearer rotated"


async def test_expiry_is_tracked_from_the_refresh_response(session):
    refresher, calls = make_refresher("a", "b")
    hub = FamilyHub(session, token="t0", device_id="d")
    hub.set_token_refresher(refresher, expires_in=0)         # unknown lifetime -> assume 1 hour
    hub._token_expires_at = time.monotonic() - 1             # force "expired"
    await hub.async_ensure_fresh_token()
    await hub.async_ensure_fresh_token()                     # new expiry is an hour away
    assert calls == [1] and hub.token == "a"


async def test_concurrent_callers_refresh_once(session):
    import asyncio

    refresher, calls = make_refresher("only-once")

    async def slow():
        await asyncio.sleep(0.05)
        return await refresher()

    hub = FamilyHub(session, token="t0", device_id="d")
    hub.set_token_refresher(slow, expires_in=0)
    hub._token_expires_at = time.monotonic() - 1
    await asyncio.gather(*(hub.async_ensure_fresh_token() for _ in range(5)))
    assert calls == [1]


async def test_coordinator_refreshes_before_polling(hass, session):
    refresher, calls = make_refresher("rotated-1")
    hub = FamilyHub(session, token="initial", device_id="device-123")
    hub.set_token_refresher(refresher, expires_in=0)
    hub._token_expires_at = time.monotonic() - 1
    async def status():
        return {}

    hub.get_current_device_status = status
    for name in ("set_current_device_status", "extract_device_data"):
        setattr(hub, name, MagicMock())
    hub.get_file_ids = MagicMock(return_value=[])

    await FridgeCoordinator(hass, MagicMock(), hub)._async_update_data()
    assert calls == [1] and hub.token == "rotated-1"
