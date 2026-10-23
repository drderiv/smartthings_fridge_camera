"""Shapes of the JSON documents the integration reads and produces."""
from __future__ import annotations

from typing import NotRequired, TypedDict

# --- SmartThings device status ("components/main/status") -------------------------------


class ContactValue(TypedDict):
    value: str
    timestamp: str


class ContactSensor(TypedDict):
    contact: ContactValue


class FileReference(TypedDict):
    fileId: str


class ViewInsideContents(TypedDict):
    value: list[FileReference]


class ViewInside(TypedDict):
    contents: ViewInsideContents


# Capability ids contain dots, so the functional syntax is required.
DeviceStatus = TypedDict(
    "DeviceStatus",
    {"contactSensor": ContactSensor, "samsungce.viewInside": ViewInside},
    total=False,
)


# --- SmartThings device list ("devices/status") ---------------------------------------------


class DeviceListItem(TypedDict, total=False):
    capabilityId: str
    attributeName: str
    deviceId: str


class DeviceList(TypedDict, total=False):
    items: list[DeviceListItem]


# --- food inventory (what the inventory sensor exposes) ---------------------------------------


class FoodItem(TypedDict):
    name: str
    image_url: str | None
    added_at: int
    expiration_date: str | None


class Inventory(TypedDict):
    total_items: int
    last_synced: str | None
    items: list[FoodItem]
    source: NotRequired[str]
