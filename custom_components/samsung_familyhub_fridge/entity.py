"""Shared entity helpers."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def fridge_device_info(entry: Any) -> DeviceInfo:
    """One device per config entry, so the cameras and sensors group together."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Samsung",
        model="Family Hub",
        name="Family Hub",
    )
