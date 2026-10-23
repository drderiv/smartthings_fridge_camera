"""Token/session file locations and owner-only, atomic writes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    """Where the rotator keeps its state. File names are stable across versions."""

    base: Path = BASE_DIR

    @property
    def config(self) -> Path:
        return self.base / "config.json"

    @property
    def pat(self) -> Path:
        return self.base / "smartthings_pat.txt"

    @property
    def food(self) -> Path:
        return self.base / "samsung_food_token.txt"

    @property
    def kics(self) -> Path:
        return self.base / "kics_food_token.txt"

    @property
    def kics_legacy(self) -> Path:
        """Older installs stored the KICS access token under this name."""
        return self.base / "active_bearer_token.txt"

    @property
    def kics_refresh(self) -> Path:
        return self.base / "kics_refresh_token.txt"

    @property
    def session(self) -> Path:
        return self.base / "smartthings_session.json"

    @property
    def log(self) -> Path:
        return self.base / "rotator.log"


def resolve(path: str | os.PathLike[str], base: Path = BASE_DIR) -> Path:
    """Expand ``~``; resolve relative paths against the rotator folder, not the cwd."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else base / p


def read_token(path: str | os.PathLike[str]) -> str | None:
    """Return the stripped file contents, or None if missing/empty/unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def token_mtime(path: str | os.PathLike[str]) -> float | None:
    """Modification time of a non-empty token file, else None."""
    if read_token(path) is None:
        return None
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def write_secret(path: str | os.PathLike[str], value: str) -> None:
    """Atomically write ``value`` with 0600 permissions.

    Readers (the HTTP server) never observe a truncated file.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value if value.endswith("\n") else value + "\n")
    os.replace(tmp, target)
