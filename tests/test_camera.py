"""Tests for Samsung FamilyHub fridge camera entities."""
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.samsung_familyhub_fridge.camera import (
    async_setup_entry,
    FamilyHubCamera,
)
from custom_components.samsung_familyhub_fridge.const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_ENABLE_BOTTOM_CAMERA,
)


@pytest.fixture
def mock_hass():
    hass = MagicMock()
    hass.data = {}
    return hass


@pytest.mark.asyncio
async def test_camera_setup_default_two_cameras(mock_hass):
    """Test camera setup by default only creates 2 door cameras for 4-door fridges."""
    mock_hub = MagicMock()
    mock_hub.device_id = "test_fridge_123"
    mock_hub.downloaded_images = [b"img0", b"img1", None]
    mock_hass.data[DOMAIN] = {}

    mock_entry = MagicMock()
    mock_entry.data = {CONF_DEVICE_ID: "test_fridge_123"}
    mock_entry.options = {}
    mock_entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(hub=mock_hub))

    added_entities = []

    def mock_add_entities(entities):
        added_entities.extend(entities)

    await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

    # By default, only top and middle cameras are created
    assert len(added_entities) == 2

    top_cam = added_entities[0]
    middle_cam = added_entities[1]

    # Verify unique IDs
    assert top_cam.unique_id == "test_fridge_123_family_hub_top"
    assert middle_cam.unique_id == "test_fridge_123_family_hub_middle"

    # Names come from translations ("Top" / "Middle") under the device name
    assert top_cam._attr_translation_key == "top"
    assert middle_cam._attr_translation_key == "middle"
    assert top_cam._attr_has_entity_name is True

    # Verify camera images
    assert top_cam.camera_image() == b"img0"
    assert middle_cam.camera_image() == b"img1"


@pytest.mark.asyncio
async def test_camera_setup_with_bottom_camera_enabled(mock_hass):
    """Test camera setup creates bottom camera when explicitly enabled in options."""
    mock_hub = MagicMock()
    mock_hub.device_id = "test_fridge_123"
    mock_hub.downloaded_images = [b"img0", b"img1", b"img2"]
    mock_hass.data[DOMAIN] = {}

    mock_entry = MagicMock()
    mock_entry.data = {CONF_DEVICE_ID: "test_fridge_123"}
    mock_entry.options = {CONF_ENABLE_BOTTOM_CAMERA: True}
    mock_entry.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(hub=mock_hub))

    added_entities = []

    def mock_add_entities(entities):
        added_entities.extend(entities)

    await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

    # All 3 cameras created
    assert len(added_entities) == 3

    top_cam = added_entities[0]
    middle_cam = added_entities[1]
    bottom_cam = added_entities[2]

    assert top_cam.unique_id == "test_fridge_123_family_hub_top"
    assert middle_cam.unique_id == "test_fridge_123_family_hub_middle"
    assert bottom_cam.unique_id == "test_fridge_123_family_hub_bottom"

    assert bottom_cam._attr_translation_key == "bottom"
    assert bottom_cam.camera_image() == b"img2"
