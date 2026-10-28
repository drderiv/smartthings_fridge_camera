"""Small helpers that keep type checking stable across Home Assistant releases."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries

# Newer Home Assistant reloads an entry itself when its options flow saves changed options (and
# warns about update listeners); older releases lack the class and need a listener instead.
OPTIONS_FLOW_RELOADS = hasattr(config_entries, "OptionsFlowWithReload")
OptionsFlowBase = getattr(config_entries, "OptionsFlowWithReload", config_entries.OptionsFlow)


def schema(fields: dict[Any, Any]) -> Any:
    """Build a voluptuous schema.

    Typed ``Any`` because newer Home Assistant releases annotate their own schema class, while
    older ones annotate voluptuous; either accepts the object at runtime.
    """
    return vol.Schema(fields)
