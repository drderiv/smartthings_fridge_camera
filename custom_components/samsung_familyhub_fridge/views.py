"""HTTP views for Samsung FamilyHub Fridge."""
from __future__ import annotations

import hashlib
import hmac
import os
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
    requires_auth = False  # Custom auth verification: user session, compact HMAC signature, or signed token

    def __init__(self, cache_dir: str, secret: str = "") -> None:
        """Initialize the view."""
        self.cache_dir = cache_dir
        self.secret = secret

    async def get(self, request: web.Request, filename: str) -> web.Response:
        """Serve a food circle thumbnail."""
        # 1. Prevent directory traversal attacks
        if "/" in filename or "\\" in filename or ".." in filename:
            return web.Response(status=400, text="Invalid filename")

        # 2. Access control:
        # - Logged-in Home Assistant user session (Lovelace web / companion app)
        # - Valid compact HMAC signature matching the requested filename
        # - Home Assistant signed path token (authSig) for backwards compatibility
        user = request.get("hass_user")
        sig = request.query.get("sig")
        auth_sig = request.query.get("authSig")

        is_valid_sig = bool(
            (sig and self.secret and hmac.compare_digest(sig, generate_food_circle_sig(self.secret, filename)))
            or auth_sig
        )
        if not user and not is_valid_sig:
            return web.Response(status=401, text="Unauthorized")

        file_path = os.path.join(self.cache_dir, filename)
        if not os.path.isfile(file_path):
            return web.Response(status=404, text="Food circle image not found")

        return web.FileResponse(file_path)
