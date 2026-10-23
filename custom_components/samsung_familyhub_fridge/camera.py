"""Camera entities for the Family Hub's interior cameras."""
from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_ID, CONF_ENABLE_BOTTOM_CAMERA, DEFAULT_ENABLE_BOTTOM_CAMERA
from .coordinator import FridgeCoordinator

# (entity name, index into the fridge's image list)
CAMERAS = (
    ("family_hub_top", 0),
    ("family_hub_middle", 1),
    ("family_hub_bottom", 2),
)
BOTTOM_CAMERA_INDEX = 2


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Samsung Family Hub fridge cameras."""
    coordinator = config_entry.runtime_data.coordinator
    # Unique IDs have always been built from the configured device ID (or this fixed fallback),
    # never the discovered one; changing that would orphan existing entities.
    device_id = config_entry.data.get(CONF_DEVICE_ID) or "samsung_familyhub"
    enable_bottom = bool(
        config_entry.options.get(CONF_ENABLE_BOTTOM_CAMERA)
        if CONF_ENABLE_BOTTOM_CAMERA in config_entry.options
        else config_entry.data.get(CONF_ENABLE_BOTTOM_CAMERA, DEFAULT_ENABLE_BOTTOM_CAMERA)
    )
    # The bottom camera exists only on some models (most 4-door units have two door cameras).
    async_add_entities(
        FamilyHubCamera(coordinator, name, index, device_id)
        for name, index in CAMERAS
        if index != BOTTOM_CAMERA_INDEX or enable_bottom
    )


class FamilyHubCamera(CoordinatorEntity[FridgeCoordinator], Camera):
    """One interior camera; the image comes from the coordinator's last download."""

    def __init__(
        self,
        coordinator: FridgeCoordinator,
        name: str,
        index: int,
        device_id: str,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.content_type = "image/jpeg"
        self._index = index
        self._attr_name = name
        self._attr_unique_id = f"{device_id}_{name}"
        self._attr_entity_registry_enabled_default = index != BOTTOM_CAMERA_INDEX

    @property
    def available(self) -> bool:
        return self._image() is not None

    def _image(self) -> bytes | None:
        images = self.coordinator.hub.downloaded_images
        return images[self._index] if self._index < len(images) else None

    def camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._image()
