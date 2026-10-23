"""HTTP API the Home Assistant integration polls for tokens."""

from __future__ import annotations

import hmac
import json
import logging
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config, read_location_id
from .files import Paths, read_token, token_mtime

_LOGGER = logging.getLogger("rotator.server")


@dataclass(frozen=True)
class TokenSource:
    """A token file (first non-empty candidate wins) and how often it rotates."""

    kind: str
    paths: tuple[Path, ...]
    interval_s: float
    unavailable: str

    def read(self) -> tuple[str, Path] | None:
        for path in self.paths:
            token = read_token(path)
            if token:
                return token, path
        return None

    def meta(self, path: Path) -> dict[str, int]:
        mtime = token_mtime(path)
        if mtime is None:
            return {}
        next_rotation = mtime + self.interval_s
        return {
            "generated_at": int(mtime),
            "next_rotation_at": int(next_rotation),
            "ttl_seconds": int(max(0.0, next_rotation - time.time())),
        }


class TokenServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg: Config, paths: Paths) -> None:
        self.cfg = cfg
        self.paths = paths
        self.sources = {
            "pat": TokenSource(
                "smartthings_pat", (paths.pat,), cfg.pat_interval_s, "PAT not generated yet"
            ),
            "food": TokenSource(
                "samsung_food_token", (paths.food,), cfg.food_interval_s,
                "Samsung Food token not generated yet",
            ),
            "kics": TokenSource(
                "samsung_kics_food_token", (paths.kics, paths.kics_legacy), cfg.kics_interval_s,
                "KICS token not generated yet",
            ),
        }
        super().__init__((cfg.bind_address, cfg.port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: TokenServer

    # --- plumbing -------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """Silence the default per-request stderr line; we log what we serve."""

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        key = self.server.cfg.api_key
        if not key:
            return True
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(supplied.encode("utf-8"), key.encode("utf-8"))

    # --- routing --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = self.path.split("?", 1)[0].rstrip("/") or "/"

        if route == "/health":  # unauthenticated, reveals nothing about tokens
            self._send(200, {"status": "healthy"})
            return
        if not self._authorized():
            _LOGGER.warning("Rejected unauthenticated request for %s from %s", route, self.client_address[0])
            self._send(401, {"error": "unauthorized"})
            return

        handlers = {
            "/pat": lambda: self._token("pat", with_location=False),
            "/food_token": lambda: self._token("food", with_location=True),
            "/kics_token": lambda: self._token("kics", with_location=True),
            "/location_id": self._location,
            "/tokens": self._combined,
        }
        handler = handlers.get(route)
        if handler is None:
            self._send(404, {"error": "not found"})
            return
        handler()

    # --- endpoints ------------------------------------------------------

    def _location_id(self) -> str | None:
        return read_location_id(self.server.paths.config)

    def _token(self, name: str, *, with_location: bool) -> None:
        source = self.server.sources[name]
        found = source.read()
        if found is None:
            _LOGGER.warning("%s requested by %s but no token file exists", name, self.client_address[0])
            self._send(503, {"error": source.unavailable, "status": "unavailable"})
            return
        token, path = found
        payload: dict[str, Any] = {"token": token, "status": "ok", "type": source.kind, **source.meta(path)}
        if with_location:
            payload["location_id"] = self._location_id()
        _LOGGER.info("Served %s to %s", name, self.client_address[0])
        self._send(200, payload)

    def _location(self) -> None:
        location = self._location_id()
        if location:
            self._send(200, {"location_id": location, "status": "ok"})
        else:
            self._send(404, {"error": "location_id not configured in config.json", "status": "unavailable"})

    def _combined(self) -> None:
        sources = self.server.sources
        pat = sources["pat"].read()
        food = sources["food"].read()
        kics = sources["kics"].read()

        def token(found: tuple[str, Path] | None) -> str | None:
            return found[0] if found else None

        def next_rotation(name: str, found: tuple[str, Path] | None) -> int | None:
            return sources[name].meta(found[1]).get("next_rotation_at") if found else None

        self._send(200, {
            "smartthings_pat": token(pat),
            "samsung_food_token": token(food),
            "kics_token": token(kics),
            "location_id": self._location_id(),
            "status": "ok" if (pat or food or kics) else "empty",
            "pat_next_rotation_at": next_rotation("pat", pat),
            "food_next_rotation_at": next_rotation("food", food),
            "kics_next_rotation_at": next_rotation("kics", kics),
        })
        _LOGGER.info("Served combined tokens to %s", self.client_address[0])
