"""The integration inside a real Home Assistant: setup, entities, services, views, flows."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.samsung_familyhub_fridge.const import DOMAIN
from custom_components.samsung_familyhub_fridge.views import generate_food_circle_sig

from .conftest import COMMANDS_URL, DEVICE_ID, DEVICE_LIST_URL, JPEG, STATUS_URL, mock_smartthings


async def setup_entry(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# --- setup, entities, device ----------------------------------------------------------

async def test_setup_creates_entities_device_and_downloads_images(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)

    assert mock_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("camera.family_hub_top").state == "idle"
    assert hass.states.get("camera.family_hub_middle").state == "idle"
    assert hass.states.get("camera.family_hub_bottom") is None            # disabled unless enabled
    assert hass.states.get("sensor.family_hub_last_updated_at").state != "unknown"
    assert hass.states.get("sensor.family_hub_food_inventory") is None    # no food token

    # entities share one device; unique IDs keep their historical format
    registry = er.async_get(hass)
    top = registry.async_get("camera.family_hub_top")
    assert top.unique_id == f"{DEVICE_ID}_family_hub_top"
    device = dr.async_get(hass).async_get(top.device_id)
    assert (device.name, device.manufacturer) == ("Family Hub", "Samsung")
    assert registry.async_get("sensor.family_hub_last_updated_at").device_id == device.id

    # the first poll fetched the rotated PAT, read status, downloaded both images, refreshed the camera
    assert any("rotated-pat" in str(call[3]) for call in aioclient_mock.mock_calls if call[3])
    assert mock_entry.runtime_data.hub.downloaded_images[:2] == [JPEG, JPEG]
    assert any(call[1].path.endswith("/commands") for call in aioclient_mock.mock_calls)


async def test_camera_serves_the_downloaded_image(hass, hass_client, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)

    client = await hass_client()
    response = await client.get("/api/camera_proxy/camera.family_hub_top")

    assert response.status == 200
    assert await response.read() == JPEG


async def test_food_inventory_sensor_loads_with_signed_image_links(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock, kics=True)
    await setup_entry(hass, mock_entry)

    state = hass.states.get("sensor.family_hub_food_inventory")
    assert state.state == "1"
    image_url = state.attributes["items"][0]["image_url"]
    assert image_url.startswith("/api/samsung_familyhub/food_circles/") and "?sig=" in image_url


# --- unload / reload ----------------------------------------------------------------

async def test_unload_removes_entities_but_keeps_services(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("camera.family_hub_top").state == "unavailable"
    assert hass.services.has_service(DOMAIN, "refresh")


async def test_only_option_changes_reload_the_entry(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)
    first_runtime = mock_entry.runtime_data

    hass.config_entries.async_update_entry(mock_entry, data={**mock_entry.data, "token": "newer-pat"})
    await hass.async_block_till_done()
    assert mock_entry.runtime_data is first_runtime                       # token write: no reload

    hass.config_entries.async_update_entry(mock_entry, options={"poll_interval": 120})
    await hass.async_block_till_done()
    assert mock_entry.runtime_data is not first_runtime                   # option change: reloaded
    assert mock_entry.runtime_data.coordinator.update_interval.total_seconds() == 120


# --- failures ---------------------------------------------------------------------------

async def test_rejected_token_starts_reauth(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock, rotator=False)
    aioclient_mock.clear_requests()
    aioclient_mock.get("http://127.0.0.1:8765/tokens", status=503)
    aioclient_mock.get("http://127.0.0.1:8765/pat", status=503)
    aioclient_mock.get(STATUS_URL, status=401)

    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_unreachable_smartthings_retries_setup(hass, mock_entry, aioclient_mock):
    import aiohttp

    mock_smartthings(aioclient_mock, rotator=False)
    aioclient_mock.clear_requests()
    aioclient_mock.get("http://127.0.0.1:8765/tokens", status=503)
    aioclient_mock.get("http://127.0.0.1:8765/pat", status=503)
    aioclient_mock.get(STATUS_URL, exc=aiohttp.ClientConnectionError("offline"))

    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


# --- services -----------------------------------------------------------------------------

async def test_refresh_service_sends_the_camera_command(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)
    before = sum(1 for call in aioclient_mock.mock_calls if call[0] == "POST")

    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)

    assert sum(1 for call in aioclient_mock.mock_calls if call[0] == "POST") == before + 1


async def test_services_reject_unknown_entries_and_missing_food(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)

    with pytest.raises(ServiceValidationError, match="No loaded Samsung FamilyHub entry"):
        await hass.services.async_call(DOMAIN, "refresh", {"config_entry_id": "nope"}, blocking=True)
    with pytest.raises(ServiceValidationError, match="food inventory is not set up"):
        await hass.services.async_call(DOMAIN, "refresh_food_inventory", {}, blocking=True)


# --- diagnostics --------------------------------------------------------------------------

async def test_diagnostics_redact_credentials(hass, hass_client, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, mock_entry)

    assert diagnostics["entry"]["data"]["token"] == "**REDACTED**"
    assert "rotated-pat" not in str(diagnostics) and "saved-pat" not in str(diagnostics)
    assert diagnostics["images_available"][:2] == [True, True]


# --- the signed image view, through Home Assistant's real authentication ---------------------

async def test_food_circle_view_authentication(hass, hass_client, hass_client_no_auth, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)
    shared = hass.data[DOMAIN]
    name = "abcdef0123_01234567.jpg"
    (shared["cache_dir"] / name).write_bytes(JPEG)
    sig = generate_food_circle_sig(shared["view_secret"], name)
    base = f"/api/samsung_familyhub/food_circles/{name}"

    anonymous = await hass_client_no_auth()
    assert (await anonymous.get(f"{base}?sig={sig}")).status == 200          # signed link, no login
    assert (await anonymous.get(base)).status == 401                         # no signature
    assert (await anonymous.get(f"{base}?authSig=anything")).status == 401   # the old bypass
    assert (await anonymous.get(f"{base}?sig=0000000000000000")).status == 401
    assert (await anonymous.get(f"{base}?sig=éééééééééééééééé")).status == 401
    assert (await anonymous.get("/api/samsung_familyhub/food_circles/..%2f..%2fsecret")).status in (400, 404)

    logged_in = await hass_client()
    assert (await logged_in.get(base)).status == 200                         # a logged-in user needs no sig


# --- config flow ----------------------------------------------------------------------------

async def test_pat_flow_creates_entry_then_rejects_the_same_fridge(hass, aioclient_mock):
    aioclient_mock.get(DEVICE_LIST_URL, json={
        "items": [{"capabilityId": "samsungce.viewInside", "attributeName": "contents", "deviceId": DEVICE_ID}]})
    # Setting up the new entry polls the fridge and probes the rotator
    mock_smartthings(aioclient_mock, rotator=False)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "menu"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pat"})
    assert result["step_id"] == "pat"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "pat-1"})

    assert result["type"] == "create_entry"
    assert result["data"]["device_id"] == DEVICE_ID and result["result"].unique_id == DEVICE_ID
    await hass.async_block_till_done()

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    again = await hass.config_entries.flow.async_configure(again["flow_id"], {"next_step_id": "pat"})
    again = await hass.config_entries.flow.async_configure(again["flow_id"], {"token": "pat-2"})
    assert again["type"] == "abort" and again["reason"] == "already_configured"


async def test_pat_flow_reports_invalid_token(hass, aioclient_mock):
    aioclient_mock.get(DEVICE_LIST_URL, status=401)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pat"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "bad"})

    assert result["type"] == "form" and result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_shows_current_values_and_saves(hass, mock_entry, aioclient_mock):
    mock_smartthings(aioclient_mock)
    await setup_entry(hass, mock_entry)

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] == "form" and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"rotator_url": "http://127.0.0.1:8765", "rotator_api_key": "", "location_id": "",
         "food_update_interval": 600, "poll_interval": 45, "enable_bottom_camera": False},
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert mock_entry.options["poll_interval"] == 45
    assert mock_entry.runtime_data.coordinator.update_interval.total_seconds() == 45
