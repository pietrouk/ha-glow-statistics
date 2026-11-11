"""Turning Glow readings into hourly external statistics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN, TARIFF_TZ
from .tariffs import TariffResolver

HOUR = timedelta(hours=1)
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
CURRENCY = "GBP"  # Glow serves UK meters only


def floor_hour(when: datetime) -> datetime:
    """Start of the UTC hour."""
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hourly_from_slots(slots: Mapping[datetime, float]) -> dict[datetime, float]:
    """Sum half-hour readings into UTC hours (negative readings count as 0)."""
    hours: dict[datetime, float] = {}
    for slot, value in slots.items():
        hour = floor_hour(slot)
        hours[hour] = hours.get(hour, 0.0) + max(value, 0.0)
    return dict(sorted(hours.items()))


def energy_metadata(statistic_id: str, name: str) -> StatisticMetaData:
    """Metadata that makes a kWh statistic selectable in the Energy dashboard."""
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def cost_metadata(statistic_id: str, name: str) -> StatisticMetaData:
    """Metadata for a GBP running total ("entity tracking the total costs")."""
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=None,
        unit_of_measurement=CURRENCY,
    )


def day_start_hours(start: datetime, end: datetime) -> set[datetime]:
    """UTC hours in [start, end] that begin a UK day (where the standing charge goes)."""
    out: set[datetime] = set()
    day = start.astimezone(TARIFF_TZ).date()
    last = end.astimezone(TARIFF_TZ).date()
    while day <= last:
        midnight = datetime(day.year, day.month, day.day, tzinfo=TARIFF_TZ).astimezone(UTC)
        if start <= midnight <= end:
            out.add(midnight)
        day += timedelta(days=1)
    return out


def hourly_costs(
    kwh: Mapping[datetime, float], start: datetime, end: datetime, resolver: TariffResolver
) -> dict[datetime, float]:
    """Cost in GBP of every hour in [start, end]: kWh x unit rate, plus the day's standing charge."""
    midnights = day_start_hours(start, end)
    out: dict[datetime, float] = {}
    hour = start
    while hour <= end:
        prices = resolver.for_day(hour.astimezone(TARIFF_TZ).date())
        value = 0.0
        if prices is not None:
            value = kwh.get(hour, 0.0) * prices.unit_rate / 100
            if hour in midnights:
                value += prices.standing_charge / 100
        out[hour] = value
        hour += HOUR
    return out


def build_rows(
    changes: Mapping[datetime, float], start: datetime, end: datetime, base: float, digits: int
) -> list[StatisticData]:
    """One row per hour from start to end with a running total that never decreases."""
    rows: list[StatisticData] = []
    total = base
    hour = start
    while hour <= end:
        total += max(changes.get(hour, 0.0), 0.0)
        rows.append(StatisticData(start=hour, sum=round(total, digits)))
        hour += HOUR
    return rows


async def async_last_stat(hass: HomeAssistant, statistic_id: str) -> tuple[datetime, float] | None:
    """Start and sum of the newest stored hour of a statistic."""
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, False, {"sum"}
    )
    rows = result.get(statistic_id)
    if not rows:
        return None
    return datetime.fromtimestamp(rows[0]["start"], UTC), float(rows[0].get("sum") or 0.0)


async def async_stored_rows(
    hass: HomeAssistant, statistic_id: str, start: datetime
) -> list[tuple[datetime, float]]:
    """Stored (hour, sum) rows from ``start`` onwards, sorted."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {statistic_id}, "hour", None, {"sum"}
    )
    return [
        (datetime.fromtimestamp(row["start"], UTC), float(row.get("sum") or 0.0))
        for row in result.get(statistic_id, [])
        if row.get("sum") is not None
    ]


async def async_window(
    hass: HomeAssistant, statistic_id: str, start: datetime
) -> tuple[float, dict[datetime, float], datetime | None]:
    """Running total before ``start``, stored hourly changes from ``start``, first stored hour.

    The total before ``start`` is the sum of the last stored hour before it (0 if none).
    """
    rows = await async_stored_rows(hass, statistic_id, start - timedelta(days=31))
    before = [r for r in rows if r[0] < start]
    if not before and (not rows or rows[0][0] >= start):
        # Nothing in the month before: look further back (only needed after long gaps).
        older = await async_stored_rows(hass, statistic_id, EPOCH)
        before = [r for r in older if r[0] < start]
        rows = older
    base = before[-1][1] if before else 0.0
    changes: dict[datetime, float] = {}
    prev = base
    for hour, total in rows:
        if hour < start:
            continue
        changes[hour] = total - prev
        prev = total
    first = rows[0][0] if rows else None
    return base, changes, first
