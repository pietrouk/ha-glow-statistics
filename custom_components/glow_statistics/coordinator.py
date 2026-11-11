"""Coordinator: fetch Glow readings and rewrite the hourly external statistics."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GlowAuthError, GlowClient, GlowError, GlowResource
from .const import (
    BACKFILL_EMPTY_WINDOWS,
    CONF_BACKFILL_DAYS,
    CONF_LOOKBACK_DAYS,
    CONF_OVERRIDES,
    CONF_SCAN_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FUELS,
    MAX_PT30M_SPAN,
    REQUEST_PAUSE,
    TARIFF_TZ,
    Fuel,
)
from .statistics import (
    EPOCH,
    HOUR,
    async_last_stat,
    async_window,
    build_rows,
    cost_metadata,
    energy_metadata,
    floor_hour,
    hourly_costs,
    hourly_from_slots,
)
from .tariffs import Prices, TariffResolver, fingerprint, overrides_for, record_api_tariff

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1

type GlowConfigEntry = ConfigEntry[GlowCoordinator]


@dataclass(slots=True)
class Stream:
    """One meter's consumption resource and the statistics made from it."""

    fuel: Fuel
    resource: GlowResource
    suffix: str  # "" for the first meter of a fuel, "_2", "_3", ... for further ones

    @property
    def key(self) -> str:
        """Identifier used for devices and entities."""
        return f"{self.fuel.key}{self.suffix}"

    @property
    def energy_id(self) -> str:
        """Statistic ID of the kWh statistic."""
        return f"{DOMAIN}:{self.fuel.stat_key}{self.suffix}"

    @property
    def cost_id(self) -> str | None:
        """Statistic ID of the GBP statistic (None if this stream has no cost)."""
        if self.fuel.tariff_fuel is None:
            return None
        return f"{DOMAIN}:{self.fuel.key}_cost{self.suffix}"

    @property
    def label(self) -> str:
        """Suffix for names of further meters."""
        return f" ({self.suffix[1:]})" if self.suffix else ""


@dataclass(slots=True)
class StreamData:
    """What the sensors and diagnostics show for one stream after a run."""

    energy_id: str
    cost_id: str | None
    last_reading: datetime | None = None
    first_hour: datetime | None = None
    today_kwh: float | None = None
    today_cost: float | None = None
    prices: Prices | None = None
    api_tariff: tuple[float, float] | None = None
    rows_written: int = 0
    window_start: datetime | None = None


@dataclass(slots=True)
class GlowData:
    """Result of one coordinator run."""

    streams: dict[str, StreamData] = field(default_factory=dict)
    last_run: datetime | None = None


def build_streams(resources: list[GlowResource]) -> list[Stream]:
    """Map Glow resources to the streams this integration imports."""
    streams: list[Stream] = []
    for fuel in FUELS:
        matching = [r for r in resources if r.classifier == fuel.classifier]
        for index, resource in enumerate(matching):
            streams.append(Stream(fuel, resource, "" if index == 0 else f"_{index + 1}"))
    return streams


class GlowCoordinator(DataUpdateCoordinator[GlowData]):
    """Fetch readings for every stream and write its statistics."""

    config_entry: GlowConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GlowConfigEntry,
        client: GlowClient,
        resources: list[GlowResource],
    ) -> None:
        minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.resources = resources
        self.streams = build_streams(resources)
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._stored: dict[str, Any] | None = None
        self._sync_lock = asyncio.Lock()
        self.request_pause = REQUEST_PAUSE

    # ---------------------------------------------------------------- helpers

    @property
    def lookback(self) -> timedelta:
        """How far back each run rewrites the statistics."""
        return timedelta(days=self.config_entry.options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS))

    @property
    def backfill(self) -> timedelta:
        """The furthest a first run looks back."""
        return timedelta(days=self.config_entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))

    async def _async_stored(self) -> dict[str, Any]:
        if self._stored is None:
            self._stored = await self._store.async_load() or {}
            self._stored.setdefault("tariff_history", {})
            self._stored.setdefault("override_fingerprint", {})
        return self._stored

    def _resolver(self, stream: Stream, stored: dict[str, Any]) -> TariffResolver:
        overrides = overrides_for(
            self.config_entry.options.get(CONF_OVERRIDES, []), stream.fuel.tariff_fuel or ""
        )
        history = stored["tariff_history"].get(stream.cost_id, [])
        return TariffResolver(overrides, [tuple(h) for h in history])

    def _fingerprint(self, stream: Stream) -> str:
        return fingerprint(
            overrides_for(self.config_entry.options.get(CONF_OVERRIDES, []), stream.fuel.tariff_fuel or "")
        )

    # ------------------------------------------------------------------- runs

    async def _async_update_data(self) -> GlowData:
        try:
            return await self.async_sync()
        except GlowAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GlowError as err:
            raise UpdateFailed(f"Glowmarkt API error: {err}") from err

    async def async_sync(self, force_start: datetime | None = None) -> GlowData:
        """Fetch and write all streams; ``force_start`` re-imports from that time."""
        async with self._sync_lock:
            stored = await self._async_stored()
            today = dt_util.now(TARIFF_TZ).date()
            data = GlowData(last_run=dt_util.utcnow())
            for stream in self.streams:
                api_tariff = None
                if stream.cost_id:
                    tariff = await self.client.get_tariff(stream.resource.resource_id)
                    if tariff is not None:
                        api_tariff = (tariff.standing_charge, tariff.unit_rate)
                        history = stored["tariff_history"].setdefault(stream.cost_id, [])
                        record_api_tariff(history, today, tariff.standing_charge, tariff.unit_rate)
                await self.client.catchup(stream.resource.resource_id)
                result = await self._async_sync_stream(stream, stored, force_start)
                result.api_tariff = api_tariff
                data.streams[stream.key] = result
            await self._store.async_save(stored)
            return data

    async def _async_fetch_backfill(self, stream: Stream, now: datetime) -> dict[datetime, float]:
        """Walk back in 10-day windows until data runs out or the backfill limit is reached."""
        slots: dict[datetime, float] = {}
        limit = now - self.backfill
        end, empty, seen = now, 0, False
        while end > limit:
            start = max(end - MAX_PT30M_SPAN, limit)
            chunk = await self.client.get_readings(stream.resource.resource_id, start, end)
            if chunk:
                slots.update(chunk)
                seen, empty = True, 0
            else:
                empty += 1
            if empty >= (BACKFILL_EMPTY_WINDOWS if seen else 2 * BACKFILL_EMPTY_WINDOWS):
                break
            end = start
            if self.request_pause:
                await asyncio.sleep(self.request_pause)
        _LOGGER.info(
            "Backfilled %s readings for %s (%s to %s)",
            len(slots),
            stream.energy_id,
            min(slots) if slots else None,
            max(slots) if slots else None,
        )
        return slots

    async def _async_sync_stream(
        self, stream: Stream, stored: dict[str, Any], force_start: datetime | None
    ) -> StreamData:
        hass = self.hass
        now = dt_util.utcnow()
        result = StreamData(energy_id=stream.energy_id, cost_id=stream.cost_id)
        last = await async_last_stat(hass, stream.energy_id)

        # 1. Fetch readings: everything on the first run, otherwise the look-back window
        #    (extended back to the last stored hour if Home Assistant was off for longer).
        if force_start is not None:
            fetch_start = floor_hour(force_start)
            slots = await self.client.get_readings(
                stream.resource.resource_id, fetch_start, now, pause=self.request_pause
            )
        elif last is None:
            slots = await self._async_fetch_backfill(stream, now)
            fetch_start = floor_hour(min(slots)) if slots else None
        else:
            fetch_start = floor_hour(min(now - self.lookback, last[0]))
            slots = await self.client.get_readings(
                stream.resource.resource_id, fetch_start, now, pause=self.request_pause
            )
        api_hours = hourly_from_slots(slots)
        if slots:
            result.last_reading = max(slots) + timedelta(minutes=30)
        if fetch_start is None:
            _LOGGER.warning("Glow returned no readings for %s yet", stream.energy_id)
            return result

        # 2. Decide where to start rewriting. A changed set of tariff overrides (or a
        #    missing cost statistic) rewrites the whole history so all costs match.
        write_start = fetch_start
        rebuild_cost = False
        resolver = self._resolver(stream, stored) if stream.cost_id else None
        if stream.cost_id and resolver is not None and resolver.has_any and last is not None:
            saved_print = stored["override_fingerprint"].get(stream.cost_id)
            if (
                saved_print != self._fingerprint(stream)
                or await async_last_stat(hass, stream.cost_id) is None
            ):
                rebuild_cost = True
                write_start = EPOCH

        # 3. Hourly kWh: stored values, replaced by whatever Glow returned now.
        base, changes, first_stored = await async_window(hass, stream.energy_id, write_start)
        changes.update(api_hours)
        if rebuild_cost:
            write_start = min(first_stored or fetch_start, fetch_start)
        elif base == 0.0:
            # Nothing stored before: start at the first hour with data, not at an empty window.
            with_data = [h for h, v in changes.items() if h >= write_start and v > 0]
            if with_data:
                write_start = max(write_start, min(with_data))
        hours = [h for h in changes if h >= write_start]
        if not hours:
            return result
        end = max(hours)
        rows = build_rows(changes, write_start, end, base, 3)
        name = f"Glow {stream.fuel.name}{stream.label}"
        async_add_external_statistics(hass, energy_metadata(stream.energy_id, name), rows)
        result.rows_written = len(rows)
        result.window_start = write_start
        result.first_hour = min(first_stored, write_start) if first_stored else write_start

        today = dt_util.now(TARIFF_TZ).date()
        today_start = datetime.combine(today, datetime.min.time(), TARIFF_TZ).astimezone(UTC)
        result.today_kwh = round(sum(v for h, v in changes.items() if h >= today_start), 3)

        # 4. Cost: hourly kWh x unit rate, plus each UK day's standing charge in its first
        #    hour. Rows cover the same hours as the kWh statistic.
        if stream.cost_id and resolver is not None:
            result.prices = resolver.for_day(today)
            if not resolver.has_any:
                _LOGGER.warning(
                    "No tariff for %s: Glow returned none and no override is set; skipping cost",
                    stream.energy_id,
                )
                return result
            costs = hourly_costs(changes, write_start, end, resolver)
            if rebuild_cost:
                cost_base = 0.0
            else:
                cost_base, _, _ = await async_window(hass, stream.cost_id, write_start)
            cost_rows = build_rows(costs, write_start, end, cost_base, 6)
            async_add_external_statistics(
                hass,
                cost_metadata(stream.cost_id, f"Glow {stream.fuel.tariff_fuel} cost{stream.label}"),
                cost_rows,
            )
            result.today_cost = round(sum(v for h, v in costs.items() if h >= today_start), 6)
            stored["override_fingerprint"][stream.cost_id] = self._fingerprint(stream)
        return result

    async def async_reimport(self, start: date) -> None:
        """Re-import everything from a UK date onwards (service call)."""
        start_dt = datetime.combine(start, datetime.min.time(), TARIFF_TZ).astimezone(UTC)
        try:
            data = await self.async_sync(force_start=start_dt)
        except GlowAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise ConfigEntryAuthFailed(str(err)) from err
        self.async_set_updated_data(data)


__all__ = ["HOUR", "GlowConfigEntry", "GlowCoordinator", "GlowData", "Stream", "StreamData", "build_streams"]
