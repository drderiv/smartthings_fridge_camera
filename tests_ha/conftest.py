"""Fixtures for tests that run against a real Home Assistant (see Dockerfile)."""
from __future__ import annotations

import re

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_familyhub_fridge.const import (
    AUTH_MODE_PAT,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_TOKEN,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"

ROTATOR = "http://127.0.0.1:8765"
DEVICE_ID = "fridge-42"
DEVICE_LIST_URL = "https://client.smartthings.com/devices/status"
STATUS_URL = f"https://api.smartthings.com/v1/devices/{DEVICE_ID}/components/main/status"
COMMANDS_URL = f"https://api.smartthings.com/v1/devices/{DEVICE_ID}/commands"
FILE_LINK_RE = re.compile(r"https://client\.smartthings\.com/udo/file_links/.*")
KICS_URL = "https://kics-epa.samsungepa.com/api/foodlist"
JPEG = b"\xff\xd8\xff\xe0" + b"\x07" * 300

DEVICES = {
    "items": [
        {"capabilityId": "samsungce.viewInside", "attributeName": "contents", "deviceId": DEVICE_ID}
    ]
}
STATUS = {
    "contactSensor": {"contact": {"value": "closed", "timestamp": "2026-01-01T00:00:00Z"}},
    "samsungce.viewInside": {"contents": {"value": [{"fileId": "file-a"}, {"fileId": "file-b"}]}},
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from /custom_components."""
    yield


@pytest.fixture
def mock_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Samsung Fridge Camera",
        version=2,
        unique_id=DEVICE_ID,
        data={CONF_AUTH_MODE: AUTH_MODE_PAT, CONF_TOKEN: "saved-pat", CONF_DEVICE_ID: DEVICE_ID},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


def mock_smartthings(aioclient_mock, *, rotator=True, kics=False):
    """Register SmartThings (and optionally rotator / KICS) responses for a healthy fridge."""
    if rotator:
        document = {"smartthings_pat": "rotated-pat", "pat_next_rotation_at": 4102444800}
        if kics:
            document |= {"kics_token": "kics-token-abc", "kics_next_rotation_at": 4102444800, "location_id": "loc-1"}
        aioclient_mock.get(f"{ROTATOR}/tokens", json=document)
    else:
        aioclient_mock.get(f"{ROTATOR}/tokens", status=503)
        aioclient_mock.get(f"{ROTATOR}/pat", status=503)
    aioclient_mock.get(DEVICE_LIST_URL, json=DEVICES)
    aioclient_mock.get(STATUS_URL, json=STATUS)
    aioclient_mock.get(FILE_LINK_RE, content=JPEG, headers={"Content-Type": "image/jpeg"})
    aioclient_mock.post(COMMANDS_URL, json={"results": [{"status": "COMPLETED"}]})
    aioclient_mock.get(f"{ROTATOR}/kics_token", status=404)
    aioclient_mock.get(f"{ROTATOR}/food_token", status=404)
    aioclient_mock.get(f"{ROTATOR}/location_id", status=404)
    if kics:
        aioclient_mock.get(f"{ROTATOR}/location_id", json={"location_id": "loc-1"})
        aioclient_mock.get(
            KICS_URL,
            json={"foodData": {"items": [
                {"id": "item-1", "content": {"name": "Milk", "imageUrl": "https://s3.test/milk.jpg",
                                             "createdTime": 1727400000, "expirationDate": "2026-10-05"}},
            ]}},
        )
        aioclient_mock.get("https://s3.test/milk.jpg", content=JPEG)
