"""config.json loading and validation."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

PLACEHOLDER_PREFIX = "YOUR_"


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.startswith(PLACEHOLDER_PREFIX) else text


@dataclass(frozen=True)
class Config:
    email: str
    password: str
    two_factor_method: str = "device"
    port: int = 8765
    bind_address: str = "127.0.0.1"
    api_key: str = ""
    pat_interval_hours: float = 23
    food_interval_days: float = 28
    kics_interval_hours: float = 23
    kics_refresh_token: str = ""
    headless: bool = True
    session_keepalive_minutes: float = 0

    @property
    def pat_interval_s(self) -> float:
        return self.pat_interval_hours * 3600

    @property
    def food_interval_s(self) -> float:
        return self.food_interval_days * 86400

    @property
    def kics_interval_s(self) -> float:
        return self.kics_interval_hours * 3600

    @property
    def is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.bind_address).is_loopback
        except ValueError:
            return self.bind_address == "localhost"

    @classmethod
    def from_file(cls, path: Path) -> Config:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as err:
            raise ConfigError(
                f"{path} not found. Copy config.example.json to config.json and fill it in."
            ) from err
        except (OSError, ValueError) as err:
            raise ConfigError(f"Could not read {path}: {err}") from err
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")

        try:
            cfg = cls(
                email=_clean(raw.get("samsung_email")),
                password=_clean(raw.get("samsung_password")),
                two_factor_method=str(raw.get("two_factor_method", "device")).lower(),
                port=int(raw.get("port", 8765)),
                bind_address=str(raw.get("bind_address", "127.0.0.1")).strip(),
                api_key=str(raw.get("api_key") or "").strip(),
                pat_interval_hours=float(raw.get("rotation_interval_hours", 23)),
                food_interval_days=float(raw.get("food_rotation_interval_days", 28)),
                kics_interval_hours=float(raw.get("kics_rotation_interval_hours", 23)),
                kics_refresh_token=str(raw.get("kics_refresh_token") or "").strip(),
                headless=bool(raw.get("headless", True)),
                session_keepalive_minutes=float(raw.get("session_keepalive_minutes", 0)),
            )
        except (TypeError, ValueError) as err:
            raise ConfigError(f"Invalid value in {path}: {err}") from err

        if cfg.two_factor_method not in ("device", "push", "sms", "text"):
            raise ConfigError("two_factor_method must be 'device' or 'sms'")
        return cfg

    def require_credentials(self) -> None:
        if not self.email or not self.password:
            raise ConfigError("Set 'samsung_email' and 'samsung_password' in config.json")

    def require_safe_binding(self) -> None:
        if not self.is_loopback and not self.api_key:
            raise ConfigError(
                f"bind_address {self.bind_address} is reachable from the network but no 'api_key' "
                "is set. Generate one with: "
                'python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
            )


def read_location_id(path: Path) -> str | None:
    """The fridge's SmartThings location ID; re-read per request so edits apply live."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _clean(raw.get("location_id")) or None if isinstance(raw, dict) else None
