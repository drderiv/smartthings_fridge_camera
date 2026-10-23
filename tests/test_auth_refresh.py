"""Unit tests for auth refresh, error detection, and defensive error handling."""

import pytest
import re

from tests.fake_aiohttp import aioresponses

# conftest installs HA stubs before any component imports
from tests.conftest import _HomeAssistant, _ConfigEntryAuthFailed

from unittest.mock import MagicMock

from custom_components.samsung_familyhub_fridge.api import AuthenticationError, FamilyHub
from custom_components.samsung_familyhub_fridge.coordinator import FridgeCoordinator


def make_coordinator(hass, hub):
    return FridgeCoordinator(hass, MagicMock(), hub)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hass():
    return _HomeAssistant()


@pytest.fixture
def hub(session):
    return FamilyHub(session, token="valid-token", device_id="device-123")


def last_headers(mocked_requests):
    """Headers of the most recent request seen by aioresponses."""
    calls = [call for group in mocked_requests.requests.values() for call in group]
    return calls[-1].kwargs["headers"]


def call_count(mocked_requests):
    return sum(len(group) for group in mocked_requests.requests.values())


DEVICE_STATUS_URL = "https://client.smartthings.com/devices/status"
CURRENT_STATUS_URL = (
    "https://api.smartthings.com/v1/devices/device-123/components/main/status"
)
COMMANDS_URL = "https://api.smartthings.com/v1/devices/device-123/commands"
FILE_LINK_URL_RE = r"https://client\.smartthings\.com/udo/file_links/.+"


# ---------------------------------------------------------------------------
# 1. _check_response: auth error detection
# ---------------------------------------------------------------------------

class TestCheckResponse:
    """Verify _check_response raises AuthenticationError on 401/403."""

    async def test_401_raises_auth_error(self, hub):
        with aioresponses() as m:
            m.get(DEVICE_STATUS_URL, status=401, payload={"error": "Unauthorized"})
            with pytest.raises(AuthenticationError, match="401"):
                await hub.get_all_device_status()

    async def test_403_raises_auth_error(self, hub):
        with aioresponses() as m:
            m.get(DEVICE_STATUS_URL, status=403, payload={"error": "Forbidden"})
            with pytest.raises(AuthenticationError, match="403"):
                await hub.get_all_device_status()

    async def test_200_does_not_raise(self, hub):
        with aioresponses() as m:
            m.get(DEVICE_STATUS_URL, payload={"items": []})
            result = await hub.get_all_device_status()
            assert result == {"items": []}

    def test_500_does_not_raise_auth_error(self, hub):
        """Non-auth HTTP errors should not raise AuthenticationError."""
        with aioresponses() as m:
            m.get(DEVICE_STATUS_URL, status=500, body="Internal Server Error")
            # Should not raise AuthenticationError (it logs a warning but returns)
            # However the .json() call will likely fail, so let's test _check_response directly
            hub._check_response(500, b"Internal Server Error")  # no exception

    async def test_current_device_status_401(self, hub):
        with aioresponses() as m:
            m.get(CURRENT_STATUS_URL, status=401)
            with pytest.raises(AuthenticationError):
                await hub.get_current_device_status()

    async def test_update_camera_401(self, hub):
        with aioresponses() as m:
            m.post(COMMANDS_URL, status=401)
            with pytest.raises(AuthenticationError):
                await hub.update_camera()

    async def test_download_images_403(self, hub):
        hub._current_device_status = {
            "samsungce.viewInside": {
                "contents": {"value": [{"fileId": "file-1"}]}
            }
        }
        with aioresponses() as m:
            m.get(re.compile(r".*"), status=403)
            with pytest.raises(AuthenticationError):
                await hub.download_images()


# ---------------------------------------------------------------------------
# 2. Defensive KeyError handling
# ---------------------------------------------------------------------------

class TestDefensiveKeyAccess:
    """Verify methods handle missing keys gracefully instead of crashing."""

    def test_extract_device_data_missing_contact_sensor(self, hub):
        """Issue #7: missing contactSensor key should not crash."""
        hub._current_device_status = {"someOtherCapability": {}}
        # Should return gracefully, not raise KeyError
        hub.extract_device_data()
        assert hub.should_update is False

    def test_extract_device_data_none_status(self, hub):
        hub._current_device_status = None
        hub.extract_device_data()
        assert hub.should_update is False

    def test_get_file_ids_missing_view_inside(self, hub):
        """Issue #7: missing samsungce.viewInside should return empty list."""
        hub._current_device_status = {"someOtherCapability": {}}
        assert hub.get_file_ids() == []

    def test_get_file_ids_missing_contents(self, hub):
        hub._current_device_status = {"samsungce.viewInside": {}}
        assert hub.get_file_ids() == []

    def test_get_file_ids_none_status(self, hub):
        hub._current_device_status = None
        assert hub.get_file_ids() == []

    def test_get_file_ids_valid_data(self, hub):
        hub._current_device_status = {
            "samsungce.viewInside": {
                "contents": {
                    "value": [
                        {"fileId": "abc123"},
                        {"fileId": "def456"},
                        {"fileId": "ghi789"},
                    ]
                }
            }
        }
        assert hub.get_file_ids() == ["abc123", "def456", "ghi789"]

    def test_set_device_id_missing_items(self, hub):
        """Issue #5: API returns error object without 'items' key."""
        hub._device_id = None
        hub._device_status = {
            "error": {
                "code": "UnexpectedError",
                "message": "A non-recoverable error condition occurred.",
            }
        }
        hub.set_device_id()
        assert hub._device_id is None  # should not crash

    def test_set_device_id_none_status(self, hub):
        hub._device_id = None
        hub._device_status = None
        hub.set_device_id()
        assert hub._device_id is None

    def test_set_device_id_valid_data(self, hub):
        hub._device_id = None
        hub._device_status = {
            "items": [
                {
                    "capabilityId": "samsungce.viewInside",
                    "attributeName": "contents",
                    "deviceId": "my-fridge-id",
                },
            ]
        }
        hub.set_device_id()
        assert hub._device_id == "my-fridge-id"

    def test_extract_device_data_triggers_update_on_door_close(self, hub):
        hub._current_device_status = {
            "contactSensor": {
                "contact": {"value": "closed", "timestamp": "2025-01-01T00:00:00Z"}
            }
        }
        hub.last_closed = None
        hub.extract_device_data()
        assert hub.should_update is True
        assert hub.last_closed == "2025-01-01T00:00:00Z"

    def test_extract_device_data_no_update_on_same_timestamp(self, hub):
        hub._current_device_status = {
            "contactSensor": {
                "contact": {"value": "closed", "timestamp": "2025-01-01T00:00:00Z"}
            }
        }
        hub.last_closed = "2025-01-01T00:00:00Z"
        hub.should_update = False
        hub.extract_device_data()
        assert hub.should_update is False


# ---------------------------------------------------------------------------
# 3. Token update
# ---------------------------------------------------------------------------

class TestTokenUpdate:
    """Verify update_token refreshes the authorization header."""

    def test_update_token_changes_header(self, hub):
        assert hub._headers["Authorization"] == "Bearer valid-token"
        hub.update_token("new-token-abc")
        assert hub.token == "new-token-abc"
        assert hub._headers["Authorization"] == "Bearer new-token-abc"

    async def test_update_token_used_in_api_calls(self, hub):
        hub.update_token("refreshed-token")
        with aioresponses() as m:
            m.get(DEVICE_STATUS_URL, payload={"items": []})
            await hub.get_all_device_status()
            assert last_headers(m)["Authorization"] == "Bearer refreshed-token"


# ---------------------------------------------------------------------------
# 4. API error response handling
# ---------------------------------------------------------------------------

class TestApiErrorResponses:
    """Verify API error responses are logged but don't crash."""

    async def test_get_all_device_status_with_error_body(self, hub):
        error_resp = {
            "error": {
                "code": "UnexpectedError",
                "message": "A non-recoverable error condition occurred.",
            }
        }
        with aioresponses() as m:
            m.get(DEVICE_STATUS_URL, payload=error_resp)
            result = await hub.get_all_device_status()
            assert "error" in result

    async def test_get_current_device_status_with_error_body(self, hub):
        error_resp = {
            "error": {
                "code": "UnexpectedError",
                "message": "Something went wrong.",
            }
        }
        with aioresponses() as m:
            m.get(CURRENT_STATUS_URL, payload=error_resp)
            result = await hub.get_current_device_status()
            assert "error" in result


# ---------------------------------------------------------------------------
# 5. DataCoordinator auth error propagation
# ---------------------------------------------------------------------------

class TestCoordinatorAuthPropagation:
    """Verify coordinator converts AuthenticationError to ConfigEntryAuthFailed."""

    @pytest.mark.asyncio
    async def test_coordinator_raises_config_entry_auth_failed(self, hass, hub):
        coordinator = make_coordinator(hass, hub)
        with aioresponses() as m:
            m.get(CURRENT_STATUS_URL, status=401)
            # Pre-set state so coordinator takes the "else" path (get_current_device_status)
            hub._device_id = "device-123"
            hub.should_update = False
            hub._current_device_status = {
                "samsungce.viewInside": {
                    "contents": {"value": [{"fileId": "f1"}]}
                }
            }
            coordinator.last_file_ids = ["f1"]  # same file IDs -> triggers status fetch

            with pytest.raises(_ConfigEntryAuthFailed):
                await coordinator._async_update_data()

    @pytest.mark.asyncio
    async def test_coordinator_raises_on_update_camera_auth_error(self, hass, hub):
        coordinator = make_coordinator(hass, hub)
        hub._device_id = "device-123"
        hub.should_update = True
        with aioresponses() as m:
            m.get(CURRENT_STATUS_URL, payload={"contactSensor": {"contact": {"value": "open", "timestamp": "t"}}})
            m.post(COMMANDS_URL, status=403)
            with pytest.raises(_ConfigEntryAuthFailed):
                await coordinator._async_update_data()

    @pytest.mark.asyncio
    async def test_coordinator_normal_flow_no_error(self, hass, hub):
        coordinator = make_coordinator(hass, hub)
        hub._device_id = "device-123"
        hub.should_update = False
        hub._current_device_status = {
            "samsungce.viewInside": {
                "contents": {"value": [{"fileId": "f1"}]}
            }
        }
        coordinator.last_file_ids = ["f1"]

        normal_status = {
            "contactSensor": {
                "contact": {"value": "open", "timestamp": "2025-01-01"}
            },
            "samsungce.viewInside": {
                "contents": {"value": [{"fileId": "f1"}]}
            },
        }
        with aioresponses() as m:
            m.get(CURRENT_STATUS_URL, payload=normal_status)
            # Should complete without raising
            await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# 6. Full auth-expire-and-recover integration scenario
# ---------------------------------------------------------------------------

class TestFullAuthRefreshScenario:
    """End-to-end: token expires -> detected -> token updated -> works again."""

    async def test_expire_detect_refresh_recover(self, hub):
        with aioresponses() as m:
            # 1. Initial call works
            m.get(DEVICE_STATUS_URL, payload={"items": []})
            result = await hub.get_all_device_status()
            assert result == {"items": []}

        with aioresponses() as m:
            # 2. Token expires -> 401
            m.get(DEVICE_STATUS_URL, status=401)
            with pytest.raises(AuthenticationError):
                await hub.get_all_device_status()

        # 3. User re-authenticates, token is updated
        hub.update_token("brand-new-token")

        with aioresponses() as m:
            # 4. Subsequent call with new token works
            m.get(DEVICE_STATUS_URL, payload={"items": [{"deviceId": "fridge-1"}]})
            result = await hub.get_all_device_status()
            assert result["items"][0]["deviceId"] == "fridge-1"
            assert last_headers(m)["Authorization"] == "Bearer brand-new-token"

    @pytest.mark.asyncio
    async def test_coordinator_recovers_after_token_update(self, hass, session):
        hub = FamilyHub(session, token="old-token", device_id="device-123")
        coordinator = make_coordinator(hass, hub)
        hub.should_update = False
        hub._current_device_status = {
            "samsungce.viewInside": {
                "contents": {"value": [{"fileId": "f1"}]}
            }
        }
        coordinator.last_file_ids = ["f1"]

        # Step 1: Auth fails
        with aioresponses() as m:
            m.get(CURRENT_STATUS_URL, status=401)
            with pytest.raises(_ConfigEntryAuthFailed):
                await coordinator._async_update_data()

        # Step 2: Token refreshed
        hub.update_token("new-token")

        # Step 3: Next poll succeeds
        normal_status = {
            "contactSensor": {
                "contact": {"value": "open", "timestamp": "2025-01-01"}
            },
            "samsungce.viewInside": {
                "contents": {"value": [{"fileId": "f1"}]}
            },
        }
        with aioresponses() as m:
            m.get(CURRENT_STATUS_URL, payload=normal_status)
            await coordinator._async_update_data()  # no exception


# ---------------------------------------------------------------------------
# 6. Status Error Handling & Coordinator Deadlock Prevention
# ---------------------------------------------------------------------------

class TestStatusErrorHandlingAndCoordinatorResilience:
    """Verify error responses do not corrupt status or deadlock coordinator."""

    def test_set_current_device_status_ignores_error_body(self, hub):
        valid_status = {
            "samsungce.viewInside": {
                "contents": {"value": [{"fileId": "valid_id"}]}
            }
        }
        hub.set_current_device_status(valid_status)
        assert hub._current_device_status == valid_status

        # Passing error dictionary must be ignored
        hub.set_current_device_status({"error": {"code": "UnexpectedError"}})
        assert hub._current_device_status == valid_status

        # Passing None or non-dict must be ignored
        hub.set_current_device_status(None)
        assert hub._current_device_status == valid_status
        hub.set_current_device_status("not a dict")
        assert hub._current_device_status == valid_status

    def test_set_device_status_ignores_error_body(self, hub):
        valid_devices = {"items": [{"deviceId": "d1", "capabilityId": "samsungce.viewInside", "attributeName": "contents"}]}
        hub.set_device_status(valid_devices)
        assert hub._device_status == valid_devices

        hub.set_device_status({"error": {"code": "UnexpectedError"}})
        assert hub._device_status == valid_devices

    async def test_download_images_returns_false_on_empty_file_ids(self, hub):
        hub._current_device_status = {"other": "data"}
        assert hub.get_file_ids() == []
        assert await hub.download_images() is False

    @pytest.mark.asyncio
    async def test_coordinator_continues_polling_on_repeated_download_failures(self, hass, hub):
        import re
        coordinator = make_coordinator(hass, hub)
        hub.should_update = False
        hub._current_device_status = {
            "samsungce.viewInside": {
                "contents": {"value": [{"fileId": "changed_id"}]}
            }
        }
        coordinator.last_file_ids = ["old_id"]

        with aioresponses() as m:
            # Mock image download failure (500 error)
            m.get(re.compile(r"^https://client\.smartthings\.com/udo/file_links/.*"), status=500)
            # Mock status polling with door open so should_update is not triggered
            m.get(CURRENT_STATUS_URL, payload={
                "contactSensor": {"contact": {"value": "open", "timestamp": "2025-01-01"}},
                "samsungce.viewInside": {"contents": {"value": [{"fileId": "changed_id"}]}}
            })

            # Poll 1-3: download fails, consecutive failures incremented
            for i in range(1, 4):
                await coordinator._async_update_data()
                assert coordinator._download_failures == i

            # Status is polled every cycle, so polling never stalls on a bad file ID
            await coordinator._async_update_data()
            assert coordinator._download_failures == 4
            assert call_count(m) >= 5

