"""HTTP views for Samsung FamilyHub Fridge."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView


def generate_food_circle_sig(secret: str, filename: str) -> str:
    """Generate a compact 16-character HMAC-SHA256 signature for a food circle image."""
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), filename.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


class FamilyHubFoodCircleView(HomeAssistantView):
    """View to serve food circle thumbnail images securely with authentication or signed token."""

    url = "/api/samsung_familyhub/food_circles/{filename}"
    name = "api:samsung_familyhub:food_circles"
    # Dashboard Markdown cards render <img src> without HA credentials, so authentication is
    # done here: a logged-in user, or an HMAC signature of the filename.
    requires_auth = False

    _FILENAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.jpg")

    def __init__(self, cache_dir: str, secret: str = "") -> None:
        """Initialize the view."""
        self.cache_dir = Path(cache_dir)
        self.secret = secret

    async def get(self, request: web.Request, filename: str) -> web.Response:
        """Serve a food circle thumbnail."""
        # 1. Allow-list the filename (blocks traversal and anything but cached crops)
        if not self._FILENAME_RE.fullmatch(filename):
            return web.Response(status=400, text="Invalid filename")

        # 2. Access control: logged-in Home Assistant user, or a valid HMAC signature
        # of the requested filename.
        user = request.get("hass_user")
        sig = request.query.get("sig")

        is_valid_sig = bool(
            sig
            and self.secret
            and hmac.compare_digest(sig.encode(), generate_food_circle_sig(self.secret, filename).encode())
        )
        if not user and not is_valid_sig:
            return web.Response(status=401, text="Unauthorized")

        file_path = self.cache_dir / filename
        if not await asyncio.to_thread(file_path.is_file):
            return web.Response(status=404, text="Food circle image not found")

        return web.FileResponse(file_path)
