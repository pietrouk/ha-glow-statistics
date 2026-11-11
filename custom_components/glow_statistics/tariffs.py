"""Tariffs: dated per-fuel overrides from the options flow, with Glow's tariff as fallback."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

# Option keys of one override
OV_FUEL = "fuel"
OV_START = "start"
OV_END = "end"
OV_STANDING = "standing_charge"  # pence per day, incl. VAT
OV_RATE = "unit_rate"  # pence per kWh, incl. VAT


@dataclass(frozen=True, slots=True)
class Prices:
    """Prices of one day, in pence incl. VAT."""

    standing_charge: float  # p/day
    unit_rate: float  # p/kWh
    source: str  # "override" or "api"


@dataclass(frozen=True, slots=True)
class TariffOverride:
    """Prices the user entered for a fuel from ``start`` (to ``end``, both inclusive)."""

    fuel: str
    start: date
    end: date | None
    standing_charge: float
    unit_rate: float

    @classmethod
    def from_option(cls, raw: Mapping[str, Any]) -> TariffOverride:
        """Build from the stored option dict."""
        end = raw.get(OV_END)
        return cls(
            fuel=raw[OV_FUEL],
            start=date.fromisoformat(raw[OV_START]),
            end=date.fromisoformat(end) if end else None,
            standing_charge=float(raw[OV_STANDING]),
            unit_rate=float(raw[OV_RATE]),
        )

    def label(self) -> str:
        """Short description for the options flow."""
        until = f" to {self.end.isoformat()}" if self.end else " onwards"
        return (
            f"{self.fuel}: {self.start.isoformat()}{until}, "
            f"{self.standing_charge:g}p/day + {self.unit_rate:g}p/kWh"
        )


def overrides_for(options_overrides: Iterable[Mapping[str, Any]], fuel: str) -> list[TariffOverride]:
    """The overrides of one fuel, sorted by start date."""
    items = [TariffOverride.from_option(o) for o in options_overrides if o.get(OV_FUEL) == fuel]
    return sorted(items, key=lambda o: o.start)


def fingerprint(overrides: Iterable[TariffOverride]) -> str:
    """Stable hash of a fuel's overrides; a change triggers a full cost rebuild."""
    payload = [
        [o.start.isoformat(), o.end.isoformat() if o.end else None, o.standing_charge, o.unit_rate]
        for o in sorted(overrides, key=lambda o: o.start)
    ]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


class TariffResolver:
    """Prices for any UK day of one fuel.

    * The override with the latest start on or before the day applies, unless the day is
      after that override's end date.
    * Otherwise Glow's tariff applies: the API tariff seen on or before that day (the
      integration records each change), or the earliest one recorded for older days.
    * Without either, the day has no prices (no cost is added for it).
    """

    def __init__(
        self,
        overrides: list[TariffOverride],
        api_history: Iterable[tuple[str, float, float]],
    ) -> None:
        self._overrides = sorted(overrides, key=lambda o: o.start)
        self._history = sorted(
            ((date.fromisoformat(d), sc, rate) for d, sc, rate in api_history),
            key=lambda h: h[0],
        )

    @property
    def has_any(self) -> bool:
        """Whether any prices are known at all."""
        return bool(self._overrides or self._history)

    def for_day(self, day: date) -> Prices | None:
        """Prices for a UK local date."""
        current = None
        for override in self._overrides:
            if override.start <= day:
                current = override
            else:
                break
        if current is not None and (current.end is None or day <= current.end):
            return Prices(current.standing_charge, current.unit_rate, "override")
        if not self._history:
            return None
        chosen = self._history[0]
        for item in self._history:
            if item[0] <= day:
                chosen = item
            else:
                break
        return Prices(chosen[1], chosen[2], "api")


def record_api_tariff(
    history: list[list[Any]], today: date, standing_charge: float, unit_rate: float
) -> bool:
    """Append Glow's current tariff to the history when it changed; True if appended."""
    if history:
        _, last_sc, last_rate = history[-1]
        if abs(last_sc - standing_charge) < 1e-9 and abs(last_rate - unit_rate) < 1e-9:
            return False
    history.append([today.isoformat(), standing_charge, unit_rate])
    return True
