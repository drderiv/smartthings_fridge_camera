"""Diagnostics with credentials and identifiers redacted."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    CONF_FOOD_TOKEN,
    CONF_KICS_TOKEN,
    CONF_LOCATION_ID,
    CONF_OAUTH_CLIENT_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_ROTATOR_API_KEY,
    CONF_ROTATOR_URL,
    CONF_SAMSUNG_IOT_REFRESH_TOKEN,
    CONF_SAMSUNG_SIGNIN_CLIENT_SECRET,
    CONF_TOKEN,
)

TO_REDACT = {
    CONF_TOKEN,
    CONF_OAUTH_CLIENT_ID,
    CONF_OAUTH_CLIENT_SECRET,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_SAMSUNG_IOT_REFRESH_TOKEN,
    CONF_SAMSUNG_SIGNIN_CLIENT_SECRET,
    CONF_FOOD_TOKEN,
    CONF_KICS_TOKEN,
    CONF_ROTATOR_API_KEY,
    CONF_ROTATOR_URL,
    CONF_LOCATION_ID,
    CONF_DEVICE_ID,
    "unique_id",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    """Entry configuration plus coarse runtime state (no tokens, IDs or image data)."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    food = coordinator.food_coordinator
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "poll_interval_seconds": coordinator.update_interval.total_seconds() if coordinator.update_interval else None,
        "last_update_success": coordinator.last_update_success,
        "images_available": [image is not None for image in runtime.hub.downloaded_images],
        "image_file_count": len(coordinator.last_file_ids),
        "door_last_closed_known": runtime.hub.last_closed is not None,
        "food_inventory": None
        if food is None
        else {
            "last_update_success": food.last_update_success,
            "total_items": (food.data or {}).get("total_items"),
            "last_synced": (food.data or {}).get("last_synced"),
        },
    }
