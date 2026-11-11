"""Tariff resolution and hourly cost maths (no Home Assistant needed)."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.glow_statistics.statistics import (
    build_rows,
    day_start_hours,
    hourly_costs,
    hourly_from_slots,
)
from custom_components.glow_statistics.tariffs import (
    TariffOverride,
    TariffResolver,
    fingerprint,
    overrides_for,
    record_api_tariff,
)

from .conftest import utc

ELEC_OVERRIDE = {
    "fuel": "electricity",
    "start": "2020-01-01",
    "end": "2026-09-03",
    "standing_charge": 40.446,
    "unit_rate": 23.52,
}
GAS_OVERRIDES = [
    {"fuel": "gas", "start": "2026-02-27", "end": None, "standing_charge": 25.35, "unit_rate": 5.6868},
    {"fuel": "gas", "start": "2020-01-01", "end": None, "standing_charge": 31.28475, "unit_rate": 6.04695},
]


def test_override_then_api_fallback() -> None:
    """Pietro's electricity: override until 3 Sep 2026, then Glow's tariff."""
    resolver = TariffResolver(
        overrides_for([ELEC_OVERRIDE, *GAS_OVERRIDES], "electricity"),
        [("2026-11-11", 45.0, 25.0)],
    )
    assert resolver.for_day(date(2026, 9, 3)).source == "override"
    assert resolver.for_day(date(2026, 9, 3)).unit_rate == 23.52
    after = resolver.for_day(date(2026, 9, 4))
    assert (after.source, after.standing_charge, after.unit_rate) == ("api", 45.0, 25.0)


def test_consecutive_overrides() -> None:
    """Pietro's gas: two open-ended overrides, the later one takes over on its start date."""
    resolver = TariffResolver(overrides_for(GAS_OVERRIDES, "gas"), [])
    assert resolver.for_day(date(2026, 2, 26)).unit_rate == 6.04695
    assert resolver.for_day(date(2026, 2, 27)).unit_rate == 5.6868
    assert resolver.for_day(date(2019, 12, 31)) is None


def test_api_history() -> None:
    """Days before the first recorded API tariff use the earliest one; later days the latest before them."""
    history: list = []
    assert record_api_tariff(history, date(2026, 5, 1), 50.0, 24.0)
    assert not record_api_tariff(history, date(2026, 5, 2), 50.0, 24.0)
    assert record_api_tariff(history, date(2026, 6, 1), 52.0, 26.0)
    resolver = TariffResolver([], [tuple(h) for h in history])
    assert resolver.for_day(date(2025, 1, 1)).unit_rate == 24.0
    assert resolver.for_day(date(2026, 5, 31)).unit_rate == 24.0
    assert resolver.for_day(date(2026, 6, 1)).unit_rate == 26.0


def test_fingerprint_changes_with_overrides() -> None:
    gas = overrides_for(GAS_OVERRIDES, "gas")
    assert fingerprint(gas) == fingerprint(list(reversed(gas)))
    assert fingerprint(gas) != fingerprint(gas[:1])
    assert (
        TariffOverride.from_option(ELEC_OVERRIDE).label().startswith("electricity: 2020-01-01 to 2026-09-03")
    )


def test_day_start_hours_across_clock_changes() -> None:
    """UK days start at 23:00 UTC in summer and 00:00 UTC in winter; 25 Oct 2026 has 25 hours."""
    hours = day_start_hours(utc(2026, 10, 24, 0), utc(2026, 10, 27, 0))
    assert hours == {utc(2026, 10, 24, 23), utc(2026, 10, 26, 0), utc(2026, 10, 27, 0)}
    assert utc(2026, 10, 25, 23) not in hours


def test_hourly_costs_standing_charge_in_first_hour() -> None:
    resolver = TariffResolver([TariffOverride("gas", date(2020, 1, 1), None, 30.0, 6.0)], [])
    kwh = {utc(2026, 1, 10, h): 2.0 for h in range(24)}
    costs = hourly_costs(kwh, utc(2026, 1, 10, 0), utc(2026, 1, 10, 23), resolver)
    assert costs[utc(2026, 1, 10, 0)] == pytest.approx(2 * 0.06 + 0.30)
    assert costs[utc(2026, 1, 10, 1)] == pytest.approx(0.12)
    assert sum(costs.values()) == pytest.approx(48 * 0.06 + 0.30)


def test_hourly_buckets_and_rows() -> None:
    slots = {utc(2026, 1, 1, 0): 0.2, utc(2026, 1, 1, 0, 30): 0.3, utc(2026, 1, 1, 2): -0.1}
    hours = hourly_from_slots(slots)
    assert hours == {utc(2026, 1, 1, 0): 0.5, utc(2026, 1, 1, 2): 0.0}
    rows = build_rows(hours, utc(2026, 1, 1, 0), utc(2026, 1, 1, 2), 10.0, 3)
    assert [r["sum"] for r in rows] == [10.5, 10.5, 10.5]
    assert [r["start"] for r in rows] == [utc(2026, 1, 1, h) for h in range(3)]
