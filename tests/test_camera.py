"""Tests for Samsung FamilyHub fridge camera entities."""
import pytest
from unittest.mock import MagicMock

from custom_components.samsung_familyhub_fridge.camera import (
    async_setup_entry,
    FamilyHubCamera,
)
from custom_components.samsung_familyhub_fridge.const import DOMAIN, CONF_DEVICE_ID


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.data = {}
    return hass


@pytest.mark.asyncio
async def test_camera_setup_and_unique_ids(mock_hass):
    """Test camera setup sets unique_ids and disables bottom camera by default."""
    mock_hub = MagicMock()
    mock_hub.device_id = "test_fridge_123"
    mock_hub.downloaded_images = [b"img0", b"img1", None]
    mock_hass.data[DOMAIN] = {"hub": mock_hub}

    mock_entry = MagicMock()
    mock_entry.data = {CONF_DEVICE_ID: "test_fridge_123"}

    added_entities = []

    def mock_add_entities(entities):
        added_entities.extend(entities)

    await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

    assert len(added_entities) == 3

    top_cam = added_entities[0]
    middle_cam = added_entities[1]
    bottom_cam = added_entities[2]

    # Verify unique IDs
    assert top_cam.unique_id == "test_fridge_123_family_hub_top"
    assert middle_cam.unique_id == "test_fridge_123_family_hub_middle"
    assert bottom_cam.unique_id == "test_fridge_123_family_hub_bottom"

    # Verify names
    assert top_cam.name == "family_hub_top"
    assert middle_cam.name == "family_hub_middle"
    assert bottom_cam.name == "family_hub_bottom"

    # Top and middle should be enabled by default (default property is True)
    assert getattr(top_cam, "entity_registry_enabled_default", True) is True
    assert getattr(middle_cam, "entity_registry_enabled_default", True) is True

    # Bottom camera should be disabled by default in entity registry
    assert bottom_cam.entity_registry_enabled_default is False

    # Verify camera images
    assert top_cam.camera_image() == b"img0"
    assert middle_cam.camera_image() == b"img1"
    assert bottom_cam.camera_image() is None
