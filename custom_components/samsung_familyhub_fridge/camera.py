from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_DEVICE_ID
from .api import FamilyHub


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Samsung Family Hub fridge cameras."""
    hub = hass.data[DOMAIN]["hub"]
    device_id = (
        config_entry.data.get(CONF_DEVICE_ID)
        or getattr(hub, "device_id", None)
        or "samsung_familyhub"
    )
    async_add_entities(
        [
            FamilyHubCamera("family_hub_top", 0, hub, device_id),
            FamilyHubCamera("family_hub_middle", 1, hub, device_id),
            FamilyHubCamera("family_hub_bottom", 2, hub, device_id),
        ]
    )


class FamilyHubCamera(Camera):
    """Samsung Family Hub fridge camera entity."""

    def __init__(self, name: str, index: int, hub: FamilyHub, device_id: str) -> None:
        super().__init__()
        self.content_type = "image/jpeg"
        self.hub = hub
        self._index = index
        self._name = name
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{name}"
        self._image = None

        # Most modern 4-door Family Hub fridges (French doors + flex & freezer drawers)
        # only have 2 door cameras (top and middle). Disable the bottom camera by default
        # in the entity registry so it doesn't clutter dashboards, while still allowing
        # users with 3-camera models to easily toggle it enabled in the UI.
        self._attr_entity_registry_enabled_default = (index != 2)

    def camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return image response."""
        return self.hub.downloaded_images[self._index]

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name

    @property
    def unique_id(self) -> str | None:
        """Return the unique ID of this camera."""
        return self._attr_unique_id

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return whether this camera is enabled by default in the entity registry."""
        return self._attr_entity_registry_enabled_default

    @property
    def extra_state_attributes(self):
        """Return the camera state attributes."""
        return {}
