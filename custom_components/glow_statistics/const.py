"""Constants for the Glow Statistics integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from zoneinfo import ZoneInfo

DOMAIN: Final = "glow_statistics"
NAME: Final = "Glow Statistics"

# UK tariffs are charged per UK day, whatever time zone Home Assistant runs in.
TARIFF_TZ: Final = ZoneInfo("Europe/London")

API_BASE: Final = "https://api.glowmarkt.com/api/v0-1/"
# The public application ID of the Bright app (Glowmarkt API documentation v1.7).
APPLICATION_ID: Final = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"
# The API allows at most 10 days of PT30M readings per query.
MAX_PT30M_SPAN: Final = timedelta(days=10)
# Pause between consecutive readings requests during a backfill, to be gentle on the API.
REQUEST_PAUSE: Final = 1.0

CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_LOOKBACK_DAYS: Final = "lookback_days"
CONF_BACKFILL_DAYS: Final = "backfill_days"
CONF_OVERRIDES: Final = "tariff_overrides"

DEFAULT_SCAN_INTERVAL: Final = 60  # minutes
DEFAULT_LOOKBACK_DAYS: Final = 10
DEFAULT_BACKFILL_DAYS: Final = 1830  # about five years; the backfill stops earlier when data runs out
# A backfill stops after this many consecutive empty 10-day windows once data has been seen.
BACKFILL_EMPTY_WINDOWS: Final = 3

SERVICE_REIMPORT: Final = "reimport"
ATTR_START_DATE: Final = "start_date"


@dataclass(frozen=True, slots=True)
class Fuel:
    """A Glow consumption resource this integration turns into statistics."""

    key: str  # used in device identifiers and translation keys
    classifier: str  # Glowmarkt resource classifier
    stat_key: str  # object ID of the energy statistic (glow_statistics:<stat_key>)
    name: str  # human readable, used in statistic names
    tariff_fuel: str | None  # which tariff prices it; None means no cost statistic


FUELS: Final[tuple[Fuel, ...]] = (
    Fuel(
        "electricity",
        "electricity.consumption",
        "electricity_consumption",
        "electricity consumption",
        "electricity",
    ),
    Fuel("gas", "gas.consumption", "gas_consumption", "gas consumption", "gas"),
    Fuel(
        "electricity_export",
        "electricity.export",
        "electricity_export",
        "electricity export",
        None,
    ),
)
TARIFF_FUELS: Final = ("electricity", "gas")
