"""Diagnostics hide credentials."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.glow_statistics.diagnostics import async_get_config_entry_diagnostics

from .test_statistics import setup_integration


async def test_diagnostics(hass: HomeAssistant, summer_data, config_entry) -> None:
    await setup_integration(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(diag)
    assert "secret" not in text
    assert "me@example.com" not in text
    assert "res-elec" not in text
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
    assert {s["energy_id"] for s in diag["streams"]} == {
        "glow_statistics:electricity_consumption",
        "glow_statistics:gas_consumption",
    }
    assert diag["results"]["electricity"]["rows_written"] > 0
    assert "glow_statistics:electricity_cost" in diag["tariff_history"]
