from __future__ import annotations
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_DEVICE_ID, CONF_FOOD_TOKEN
from .api import (
    DataCoordinator,
    SamsungFoodClient,
    SamsungFoodCoordinator,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung FamilyHub Fridge sensors."""
    hub = hass.data[DOMAIN]["hub"]
    coordinator = DataCoordinator(hass, hub)
    entities: list[SensorEntity] = [LastUpdatedAt(coordinator)]

    device_id = config_entry.data.get(CONF_DEVICE_ID) or "samsung_familyhub"

    # Opt-in: Check if Samsung Food (Whisk) AI Food Manager token is available
    food_token = config_entry.data.get(CONF_FOOD_TOKEN)
    food_client = SamsungFoodClient(hass, token=food_token)

    if await food_client.async_has_token():
        _LOGGER.info("Samsung Food token detected. Initializing AI Food Manager inventory sensor.")
        food_coordinator = SamsungFoodCoordinator(hass, food_client)
        
        # Link food_coordinator to camera DataCoordinator for door-close triggered syncs
        coordinator.food_coordinator = food_coordinator
        
        try:
            await food_coordinator.async_config_entry_first_refresh()
            entities.append(SamsungFridgeFoodInventorySensor(food_coordinator, device_id))
        except Exception as err:
            _LOGGER.warning("Could not perform initial Samsung Food sync: %s", err)
            entities.append(SamsungFridgeFoodInventorySensor(food_coordinator, device_id))
    else:
        _LOGGER.debug("No Samsung Food token found. Skipping Food Inventory sensor creation.")

    async_add_entities(entities)


class LastUpdatedAt(CoordinatorEntity, SensorEntity):
    """Sensor tracking the timestamp of the last camera/fridge update."""

    def __init__(self, coordinator: DataCoordinator) -> None:
        self._last_updated_at = None
        super().__init__(coordinator)

    @property
    def last_updated_at(self) -> str | None:
        return self._last_updated_at

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._last_updated_at = self.coordinator.last_updated_at
        if self._last_updated_at:
            self.async_write_ha_state()


class SamsungFridgeFoodInventorySensor(CoordinatorEntity[SamsungFoodCoordinator], SensorEntity):
    """Sensor exposing active Samsung Food AI Food Manager inventory."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fridge-food"

    def __init__(self, coordinator: SamsungFoodCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_food_inventory"
        self._attr_name = "Fridge Food Inventory"

    @property
    def available(self) -> bool:
        """Return True if coordinator update succeeded or data is present."""
        return self.coordinator.last_update_success or (self.coordinator.data is not None)

    @property
    def native_value(self) -> int:
        """Return the total number of active food items in the fridge."""
        if not self.coordinator.data:
            return 0
        return int(self.coordinator.data.get("total_items", 0))

    @property
    def extra_state_attributes(self) -> dict:
        """Return rich state attributes containing active items, locations, and thumbnail URLs."""
        if not self.coordinator.data:
            return {
                "total_items": 0,
                "last_synced": None,
                "items": [],
            }
        return {
            "total_items": self.coordinator.data.get("total_items", 0),
            "last_synced": self.coordinator.data.get("last_synced"),
            "items": self.coordinator.data.get("items", []),
        }
