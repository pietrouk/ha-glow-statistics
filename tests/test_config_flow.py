"""Config, re-auth and options flows."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glow_statistics.const import DOMAIN

from .conftest import FakeGlow

CREDS = {"username": "me@example.com", "password": "secret"}


@pytest.fixture(autouse=True)
def no_setup():
    """Do not set the entry up after the flow; these tests are about the forms."""
    from unittest.mock import patch

    with patch("custom_components.glow_statistics.async_setup_entry", return_value=True):
        yield


async def test_user_flow(hass: HomeAssistant, fake_glow: FakeGlow) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == CREDS
    assert result["result"].unique_id == "acc-1"


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda f: setattr(f, "auth_error", True), "invalid_auth"),
        (lambda f: setattr(f, "connection_error", True), "cannot_connect"),
        (lambda f: setattr(f, "resources", []), "no_meters"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, fake_glow: FakeGlow, setup, error) -> None:
    setup(fake_glow)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    # Recovers once the problem is gone.
    fake_glow.__init__()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_single_instance(
    hass: HomeAssistant, fake_glow: FakeGlow, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth(hass: HomeAssistant, fake_glow: FakeGlow, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    fake_glow.auth_error = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    fake_glow.auth_error = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["password"] == "new"


async def test_reauth_other_account(
    hass: HomeAssistant, fake_glow: FakeGlow, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    fake_glow.account_id = "SOMEONE-ELSE"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "x"})
    assert result["reason"] == "wrong_account"


async def _menu(hass: HomeAssistant, entry: MockConfigEntry, step: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})


async def test_options_settings(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await _menu(hass, config_entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 30, "lookback_days": 14, "backfill_days": 400}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {"scan_interval": 30, "lookback_days": 14, "backfill_days": 400}


async def test_options_overrides(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await _menu(hass, config_entry, "add_override")
    bad = {
        "fuel": "gas",
        "start": "2026-02-27",
        "end": "2026-01-01",
        "standing_charge": 25.35,
        "unit_rate": 5.6868,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"end": "end_before_start"}
    good = {k: v for k, v in bad.items() if k != "end"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], good)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["tariff_overrides"] == [
        {"fuel": "gas", "start": "2026-02-27", "end": None, "standing_charge": 25.35, "unit_rate": 5.6868}
    ]

    result = await _menu(hass, config_entry, "add_override")
    result = await hass.config_entries.options.async_configure(result["flow_id"], good)
    assert result["errors"] == {"start": "duplicate_start"}

    result = await _menu(hass, config_entry, "remove_override")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"remove": ["0"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["tariff_overrides"] == []
