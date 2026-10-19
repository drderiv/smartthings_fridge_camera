"""HTTP views for Samsung FamilyHub Fridge."""
from __future__ import annotations

import os
from aiohttp import web
from homeassistant.components.http import HomeAssistantView


class FamilyHubFoodCircleView(HomeAssistantView):
    """View to serve food circle thumbnail images securely with authentication or signed URL."""

    url = "/api/samsung_familyhub/food_circles/{filename}"
    name = "api:samsung_familyhub:food_circles"
    requires_auth = True

    def __init__(self, cache_dir: str) -> None:
        """Initialize the view."""
        self.cache_dir = cache_dir

    async def get(self, request: web.Request, filename: str) -> web.Response:
        """Serve a food circle thumbnail."""
        # Prevent directory traversal attacks
        if "/" in filename or "\\" in filename or ".." in filename:
            return web.Response(status=400, text="Invalid filename")

        file_path = os.path.join(self.cache_dir, filename)
        if not os.path.isfile(file_path):
            return web.Response(status=404, text="Food circle image not found")

        return web.FileResponse(file_path)
