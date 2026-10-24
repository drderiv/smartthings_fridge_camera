"""Small helpers that keep type checking stable across Home Assistant releases."""
from __future__ import annotations

from typing import Any

import voluptuous as vol


def schema(fields: dict[Any, Any]) -> Any:
    """Build a voluptuous schema.

    Typed ``Any`` because newer Home Assistant releases annotate their own schema class, while
    older ones annotate voluptuous; either accepts the object at runtime.
    """
    return vol.Schema(fields)
