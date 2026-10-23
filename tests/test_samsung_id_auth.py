"""Integration tests: HTTP 400 'No samsung id available' raises AuthenticationError.

Run with:
    python3 -m pytest tests/ -k 'samsung_id' -v
"""

import pytest
import re

from tests.fake_aiohttp import aioresponses

from tests.conftest import _HomeAssistant

from custom_components.samsung_familyhub_fridge.api import (
    AuthenticationError,
    FamilyHub,
)

FILE_LINK_BASE = "https://client.smartthings.com/udo/file_links/"


@pytest.fixture
def hass():
    return _HomeAssistant()


@pytest.fixture
def hub(session):
    hub = FamilyHub(session, token="test-token", device_id="device-123")
    hub._current_device_status = {
        "samsungce.viewInside": {
            "contents": {"value": [{"fileId": "file-abc"}]}
        }
    }
    return hub


NO_SAMSUNG_ID_BODY = {
    "error": {
        "code": "BadRequestError",
        "message": "No samsung id available",
    }
}

UNRELATED_400_BODY = {
    "error": {
        "code": "BadRequestError",
        "message": "Some unrelated bad request reason",
    }
}


class TestSamsungIdAuthError:
    """HTTP 400 with 'No samsung id available' must raise AuthenticationError."""

    async def test_no_samsung_id_400_raises_auth_error(self, hub):
        """HTTP 400 + 'No samsung id available' body raises AuthenticationError."""
        with aioresponses() as m:
            m.get(re.compile(r".*"), status=400, payload=NO_SAMSUNG_ID_BODY)
            with pytest.raises(AuthenticationError, match="No samsung id available"):
                await hub.download_images()

    async def test_unrelated_400_does_not_raise_auth_error(self, hub):
        """HTTP 400 with an unrelated body must NOT raise AuthenticationError."""
        with aioresponses() as m:
            m.get(re.compile(r".*"), status=400, payload=UNRELATED_400_BODY)
            # Must not raise AuthenticationError — warning is logged instead.
            try:
                await hub.download_images()
            except AuthenticationError:
                pytest.fail("AuthenticationError was raised for an unrelated 400 body")

    async def test_no_samsung_id_400_malformed_body_does_not_raise(self, hub):
        """HTTP 400 with non-JSON body falls through to warning, not exception."""
        with aioresponses() as m:
            m.get(re.compile(r".*"), status=400, body="not json at all")
            # Must not raise AuthenticationError — malformed bodies fall through.
            try:
                await hub.download_images()
            except AuthenticationError:
                pytest.fail("AuthenticationError was raised for a non-JSON 400 body")

    def test_check_response_direct_no_samsung_id(self, hub):
        """Direct _check_response call: 400 + no-samsung-id body raises."""
        body = b'{"error":{"code":"BadRequestError","message":"No samsung id available"}}'
        with pytest.raises(AuthenticationError, match="No samsung id available"):
            hub._check_response(400, body)

    def test_check_response_direct_unrelated_400(self, hub):
        """Direct _check_response call: 400 + unrelated body does not raise."""
        hub._check_response(400, b'{"error":{"code":"BadRequestError","message":"Something else"}}')  # no exception
