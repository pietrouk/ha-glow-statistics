"""Diagnostics for Glow Statistics."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import GlowConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, "token", "resource_id", "ve_id", "ve_name", "unique_id", "title"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GlowConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    stored = await coordinator.async_stored()
    return {
        "entry": async_redact_data(
            {"title": entry.title, "unique_id": entry.unique_id, "data": dict(entry.data)}, TO_REDACT
        ),
        "options": dict(entry.options),
        "resources": [async_redact_data(asdict(r), TO_REDACT) for r in coordinator.resources],
        "streams": [
            {"key": s.key, "classifier": s.fuel.classifier, "energy_id": s.energy_id, "cost_id": s.cost_id}
            for s in coordinator.streams
        ],
        "last_update_success": coordinator.last_update_success,
        "last_run": data.last_run.isoformat() if data and data.last_run else None,
        "results": {
            key: {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in asdict(r).items()}
            for key, r in (data.streams.items() if data else [])
        },
        "tariff_history": stored.get("tariff_history", {}),
    }
