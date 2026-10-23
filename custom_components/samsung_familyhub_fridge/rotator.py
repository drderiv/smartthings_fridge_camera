"""Client for the companion token rotator (tools/pat_rotator)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import (
    CONF_ROTATOR_API_KEY,
    CONF_ROTATOR_URL,
    DEFAULT_ROTATOR_URL,
    ROTATOR_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RotatorToken:
    """A token served by the rotator and when it is next scheduled to change."""

    token: str
    next_rotation_at: float | None = None


def entry_value(entry: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a config entry: options first, then data."""
    return entry.options.get(key) or entry.data.get(key) or default


def resolve_rotator_url(entry: Any | None) -> str:
    """Options/data value, then the PAT_ROTATOR_URL environment variable, then the default."""
    url = entry_value(entry, CONF_ROTATOR_URL) if entry is not None else None
    url = url or os.environ.get("PAT_ROTATOR_URL") or DEFAULT_ROTATOR_URL
    return str(url).strip().rstrip("/")


class RotatorClient:
    """Reads tokens from the rotator's HTTP API."""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str | None = None) -> None:
        self._session = session
        self.url = url.rstrip("/")
        self._headers = (
            {"Authorization": f"Bearer {api_key.strip()}"} if api_key and api_key.strip() else {}
        )

    @classmethod
    def from_entry(cls, session: aiohttp.ClientSession, entry: Any) -> RotatorClient:
        return cls(session, resolve_rotator_url(entry), entry_value(entry, CONF_ROTATOR_API_KEY))

    async def get_json(self, path: str) -> dict[str, Any] | None:
        """GET ``path``; the JSON object on success, else None (logged at debug)."""
        try:
            async with self._session.get(
                f"{self.url}{path}",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=ROTATOR_TIMEOUT),
            ) as response:
                if not response.ok:
                    _LOGGER.debug("Rotator %s returned HTTP %s", path, response.status)
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Rotator %s unavailable: %s", path, err)
            return None
        return payload if isinstance(payload, dict) else None

    async def fetch_token(self, path: str) -> RotatorToken | None:
        """Fetch a single-token endpoint such as /pat, /kics_token or /food_token."""
        payload = await self.get_json(path)
        token = str((payload or {}).get("token") or "").strip()
        if not token:
            return None
        next_rotation = payload.get("next_rotation_at")
        return RotatorToken(token, float(next_rotation) if next_rotation else None)

    async def tokens(self) -> dict[str, Any]:
        """The combined /tokens document (empty if unreachable)."""
        return await self.get_json("/tokens") or {}
