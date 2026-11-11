"""Statistics writing: backfill, late data, costs, Energy dashboard validity."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.energy.validate import async_validate
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.glow_statistics.const import DOMAIN, TARIFF_TZ

from .conftest import ELEC, utc

E_ID = "glow_statistics:electricity_consumption"
E_COST = "glow_statistics:electricity_cost"
G_ID = "glow_statistics:gas_consumption"
G_COST = "glow_statistics:gas_cost"


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def hourly(hass: HomeAssistant, stat_id: str) -> dict[datetime, float]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, utc(2000, 1, 1), None, {stat_id}, "hour", None, {"sum"}
    )
    return {datetime.fromtimestamp(r["start"], UTC): r["sum"] for r in result.get(stat_id, [])}


def changes(rows: dict[datetime, float]) -> dict[datetime, float]:
    out, prev = {}, 0.0
    for hour, total in rows.items():
        out[hour] = round(total - prev, 6)
        prev = total
    return out


def uk_day_total(rows: dict[datetime, float], day: date) -> float:
    start = datetime.combine(day, datetime.min.time(), TARIFF_TZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), TARIFF_TZ).astimezone(UTC)
    return round(sum(v for h, v in changes(rows).items() if start <= h < end), 6)


def assert_monotonic(rows: dict[datetime, float]) -> None:
    hours = list(rows)
    assert all(b - a == timedelta(hours=1) for a, b in zip(hours, hours[1:], strict=False)), "rows not hourly"
    sums = list(rows.values())
    assert all(b >= a for a, b in zip(sums, sums[1:], strict=False)), "sum decreased"


async def test_first_run_backfills(recorder_mock, hass: HomeAssistant, summer_data, config_entry) -> None:
    """The first run walks back until Glow has no more data and writes every hour."""
    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    for stat_id, per_hour in ((E_ID, 0.5), (G_ID, 2.0)):
        rows = await hourly(hass, stat_id)
        assert next(iter(rows)) == utc(2026, 6, 1)
        assert list(rows)[-1] == utc(2026, 7, 15, 8)
        assert_monotonic(rows)
        assert set(changes(rows).values()) == {per_hour}
        assert rows[utc(2026, 7, 15, 8)] == pytest.approx(per_hour * len(rows))

    # 10-day windows back from now; stops after three empty windows once data was seen.
    elec_calls = [c for c in summer_data.reading_calls if c[0] == ELEC]
    assert len(elec_calls) == 8
    assert all(c[2] - c[1] <= timedelta(days=10) for c in elec_calls)
    assert ELEC in summer_data.catchups


async def test_late_data_lands_in_its_hour(
    recorder_mock, hass: HomeAssistant, summer_data, freezer, config_entry
) -> None:
    """Readings that reach Glow days later are added to the hour they belong to."""
    late = [utc(2026, 7, 12, 14), utc(2026, 7, 12, 14, 30)]
    for slot in late:
        del summer_data.slots[ELEC][slot]
    await setup_integration(hass, config_entry)
    before = await hourly(hass, E_ID)
    assert changes(before)[utc(2026, 7, 12, 14)] == 0

    # Three days late, the missing hour arrives, plus a new hour.
    for slot in late:
        summer_data.slots[ELEC][slot] = 0.4
    summer_data.fill(ELEC, utc(2026, 7, 15, 9), utc(2026, 7, 15, 10), 0.25)
    freezer.move_to("2026-07-15 11:20:00+00:00")
    await config_entry.runtime_data.async_refresh()
    await settle(hass)

    after = await hourly(hass, E_ID)
    assert_monotonic(after)
    assert changes(after)[utc(2026, 7, 12, 14)] == pytest.approx(0.8)
    # Hours before the window are untouched; everything after the late hour moves up.
    assert after[utc(2026, 7, 1)] == before[utc(2026, 7, 1)]
    assert after[utc(2026, 7, 12, 13)] == before[utc(2026, 7, 12, 13)]
    assert after[utc(2026, 7, 12, 15)] == pytest.approx(before[utc(2026, 7, 12, 15)] + 0.8)
    assert after[utc(2026, 7, 15, 9)] == pytest.approx(before[utc(2026, 7, 15, 8)] + 0.8 + 0.5)

    # The second run only asked for the look-back window.
    last_call = [c for c in summer_data.reading_calls if c[0] == ELEC][-1]
    assert last_call[1] == utc(2026, 7, 5, 11)

    # The cost statistic follows: 0.8 kWh at 24.5p.
    cost = changes(await hourly(hass, E_COST))
    assert cost[utc(2026, 7, 12, 14)] == pytest.approx(0.8 * 0.245)


async def test_costs_from_api_tariff(recorder_mock, hass: HomeAssistant, summer_data, config_entry) -> None:
    """Unit rate per kWh plus the standing charge in the first hour of each UK day."""
    await setup_integration(hass, config_entry)
    cost_rows = await hourly(hass, E_COST)
    assert_monotonic(cost_rows)
    assert list(cost_rows) == list(await hourly(hass, E_ID))
    cost = changes(cost_rows)
    # 10 June starts at 23:00 UTC on 9 June (BST).
    assert cost[utc(2026, 6, 9, 23)] == pytest.approx(0.5 * 0.245 + 0.50)
    assert cost[utc(2026, 6, 10, 0)] == pytest.approx(0.5 * 0.245)
    assert uk_day_total(cost_rows, date(2026, 6, 10)) == pytest.approx(12 * 0.245 + 0.50)
    gas = await hourly(hass, G_COST)
    assert uk_day_total(gas, date(2026, 6, 10)) == pytest.approx(48 * 0.06 + 0.30)


PIETRO_OVERRIDES = [
    {"fuel": "gas", "start": "2020-01-01", "end": None, "standing_charge": 31.28475, "unit_rate": 6.04695},
    {"fuel": "gas", "start": "2026-07-01", "end": None, "standing_charge": 25.35, "unit_rate": 5.6868},
    {
        "fuel": "electricity",
        "start": "2020-01-01",
        "end": "2026-06-30",
        "standing_charge": 40.446,
        "unit_rate": 23.52,
    },
]


async def test_costs_with_overrides(recorder_mock, hass: HomeAssistant, summer_data) -> None:
    """Overridden days use the override; days after an override's end use Glow's tariff."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="acc-1",
        data={"username": "me@example.com", "password": "secret"},
        options={"tariff_overrides": PIETRO_OVERRIDES},
    )
    await setup_integration(hass, entry)
    elec = await hourly(hass, E_COST)
    gas = await hourly(hass, G_COST)
    assert uk_day_total(elec, date(2026, 6, 10)) == pytest.approx(12 * 0.2352 + 0.40446)
    assert uk_day_total(elec, date(2026, 7, 5)) == pytest.approx(12 * 0.245 + 0.50)  # API
    assert uk_day_total(gas, date(2026, 6, 10)) == pytest.approx(48 * 0.0604695 + 0.3128475)
    assert uk_day_total(gas, date(2026, 7, 5)) == pytest.approx(48 * 0.056868 + 0.2535)

    state = hass.states.get("sensor.glow_statistics_gas_unit_rate")
    assert float(state.state) == pytest.approx(0.056868)
    state = hass.states.get("sensor.glow_statistics_electricity_unit_rate")
    assert float(state.state) == pytest.approx(0.245)


async def test_override_change_rebuilds_cost_history(
    recorder_mock, hass: HomeAssistant, summer_data, config_entry
) -> None:
    """Adding an override in the options flow recalculates all past costs."""
    await setup_integration(hass, config_entry)
    energy_before = await hourly(hass, E_ID)
    assert uk_day_total(await hourly(hass, E_COST), date(2026, 6, 10)) == pytest.approx(3.44)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_override"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "fuel": "electricity",
            "start": "2026-06-01",
            "end": "2026-06-30",
            "standing_charge": 40.446,
            "unit_rate": 23.52,
        },
    )
    assert result["type"] == "create_entry"
    await settle(hass)

    cost = await hourly(hass, E_COST)
    assert_monotonic(cost)
    assert uk_day_total(cost, date(2026, 6, 10)) == pytest.approx(12 * 0.2352 + 0.40446)
    assert uk_day_total(cost, date(2026, 7, 10)) == pytest.approx(3.44)
    assert await hourly(hass, E_ID) == energy_before


async def test_energy_dashboard_accepts_statistics(
    recorder_mock, hass: HomeAssistant, summer_data, config_entry
) -> None:
    """The statistics are valid Energy dashboard sources with no helpers or YAML."""
    hass.config.currency = "GBP"
    await setup_integration(hass, config_entry)

    listed = {s["statistic_id"]: s for s in await async_list_statistic_ids(hass)}
    for stat_id in (E_ID, G_ID):
        meta = listed[stat_id]
        # What the frontend's grid and gas pickers filter on.
        assert meta["has_sum"] is True
        assert meta["unit_class"] == "energy"
        assert meta["statistics_unit_of_measurement"] == "kWh"
        assert meta["source"] == DOMAIN
        assert meta["name"].startswith("Glow ")
    for stat_id in (E_COST, G_COST):
        meta = listed[stat_id]
        # "Use an entity tracking the total costs" lists statistics in the HA currency.
        assert meta["has_sum"] is True
        assert meta["statistics_unit_of_measurement"] == hass.config.currency

    assert await async_setup_component(hass, "energy", {})
    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": E_ID,
                    "stat_energy_to": None,
                    "stat_cost": E_COST,
                    "stat_compensation": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0,
                },
                {
                    "type": "gas",
                    "stat_energy_from": G_ID,
                    "stat_cost": G_COST,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                },
            ]
        }
    )
    result = (await async_validate(hass)).as_dict()
    assert result["energy_sources"] == [[], []]


async def test_sensors(recorder_mock, hass: HomeAssistant, summer_data, config_entry) -> None:
    """Today's totals reset at UK midnight with a proper last_reset."""
    await setup_integration(hass, config_entry)
    midnight = "2026-07-14T23:00:00+00:00"

    state = hass.states.get("sensor.glow_statistics_electricity_consumption_today")
    assert float(state.state) == pytest.approx(10 * 0.5)  # 23:00-09:00 UTC
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["last_reset"] == midnight

    state = hass.states.get("sensor.glow_statistics_electricity_cost_today")
    assert float(state.state) == pytest.approx(5 * 0.245 + 0.5)
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "monetary"
    assert state.attributes["unit_of_measurement"] == "GBP"
    assert state.attributes["last_reset"] == midnight

    assert float(hass.states.get("sensor.glow_statistics_electricity_standing_charge").state) == 0.5
    assert float(hass.states.get("sensor.glow_statistics_gas_consumption_today").state) == pytest.approx(20.0)
    assert hass.states.get("sensor.glow_statistics_gas_latest_reading").state == "2026-07-15T09:00:00+00:00"


async def test_reimport_service(recorder_mock, hass: HomeAssistant, summer_data, config_entry) -> None:
    """The re-import service fetches older data and keeps the running total continuous."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, unique_id="acc-1", data=dict(config_entry.data), options={"backfill_days": 20}
    )
    await setup_integration(hass, config_entry)
    rows = await hourly(hass, E_ID)
    assert next(iter(rows)) > utc(2026, 6, 20)

    await hass.services.async_call(DOMAIN, "reimport", {"start_date": "2026-05-25"}, blocking=True)
    await settle(hass)
    rows = await hourly(hass, E_ID)
    assert_monotonic(rows)
    assert next(iter(rows)) == utc(2026, 6, 1)
    assert set(changes(rows).values()) == {0.5}
    cost = await hourly(hass, E_COST)
    assert next(iter(cost)) == utc(2026, 6, 1)
    assert_monotonic(cost)


async def test_auth_failure_starts_reauth(
    recorder_mock, hass: HomeAssistant, summer_data, config_entry
) -> None:
    """A rejected login during an update asks the user to log in again."""
    await setup_integration(hass, config_entry)
    summer_data.auth_error = True
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_api_error_marks_update_failed(
    recorder_mock, hass: HomeAssistant, summer_data, config_entry
) -> None:
    """A connection problem fails the update and keeps the statistics."""
    await setup_integration(hass, config_entry)
    summer_data.connection_error = True
    await config_entry.runtime_data.async_refresh()
    assert config_entry.runtime_data.last_update_success is False
    assert hass.states.get("sensor.glow_statistics_electricity_consumption_today").state == "unavailable"


@pytest.mark.parametrize(
    ("auth_error", "expected"),
    [(True, ConfigEntryState.SETUP_ERROR), (False, ConfigEntryState.SETUP_RETRY)],
)
async def test_setup_errors(
    recorder_mock, hass: HomeAssistant, fake_glow, config_entry, auth_error, expected
) -> None:
    """Setup reports bad credentials and outages differently."""
    fake_glow.auth_error = auth_error
    fake_glow.connection_error = not auth_error
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is expected


async def test_unload(recorder_mock, hass: HomeAssistant, summer_data, config_entry) -> None:
    """The entry unloads cleanly."""
    await setup_integration(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
