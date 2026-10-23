"""Sensors: when the camera images last changed, and the food inventory."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID
from .coordinator import FridgeCoordinator
from .food import SamsungFoodCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Samsung FamilyHub Fridge sensors."""
    runtime = config_entry.runtime_data
    device_id = config_entry.data.get(CONF_DEVICE_ID) or "samsung_familyhub"

    entities: list[SensorEntity] = [LastUpdatedAt(runtime.coordinator, device_id)]
    # The inventory sensor exists only when a food token was found, so non-AI fridges stay uncluttered.
    if runtime.coordinator.food_coordinator is not None:
        entities.append(SamsungFridgeFoodInventorySensor(runtime.coordinator.food_coordinator, device_id))
    async_add_entities(entities)


class LastUpdatedAt(CoordinatorEntity[FridgeCoordinator], SensorEntity):
    """Timestamp of the last camera snapshot the fridge uploaded."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Last Updated At"

    def __init__(
        self,
        coordinator: FridgeCoordinator,
        device_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_id or 'samsung_familyhub'}_last_updated_at"

    @property
    def native_value(self) -> datetime | None:
        timestamp = self.coordinator.last_updated_at
        if isinstance(timestamp, (int, float)) and timestamp > 0:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return None


class SamsungFridgeFoodInventorySensor(CoordinatorEntity[SamsungFoodCoordinator], SensorEntity):
    """Active items from Samsung Food / the fridge's AI Food Manager."""

    _attr_icon = "mdi:fridge-food"
    # The item list is large and changes constantly; keep it out of the recorder database.
    _unrecorded_attributes = frozenset({"items"})

    def __init__(
        self,
        coordinator: SamsungFoodCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_id}_food_inventory"
        self._attr_name = "Fridge Food Inventory"

    @property
    def available(self) -> bool:
        """Keep showing the last inventory while a refresh is failing."""
        return self.coordinator.last_update_success or self.coordinator.data is not None

    @property
    def native_value(self) -> int:
        return int((self.coordinator.data or {}).get("total_items", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "total_items": data.get("total_items", 0),
            "last_synced": data.get("last_synced"),
            "items": data.get("items", []),
        }
