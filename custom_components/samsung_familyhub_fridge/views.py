"""HTTP view for Samsung FamilyHub Fridge."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView


class FamilyHubFoodCircleView(HomeAssistantView):
    """Serves the cached food circle crops.

    Access is Home Assistant's own: a logged-in request, or a link signed with
    ``homeassistant.components.http.auth.async_sign_path`` (``?authSig=...``, which expires). The
    signature is what lets a Markdown dashboard card show the images without credentials.
    """

    url = "/api/samsung_familyhub/food_circles/{filename}"
    name = "api:samsung_familyhub:food_circles"

    _FILENAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.jpg")

    def __init__(self, cache_dir: str) -> None:
        """Initialize the view."""
        self.cache_dir = Path(cache_dir)

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        """Serve a food circle thumbnail."""
        # Allow-list the filename (blocks traversal and anything but cached crops).
        if not self._FILENAME_RE.fullmatch(filename):
            return web.Response(status=400, text="Invalid filename")

        file_path = self.cache_dir / filename
        if not await asyncio.to_thread(file_path.is_file):
            return web.Response(status=404, text="Food circle image not found")

        return web.FileResponse(file_path)
