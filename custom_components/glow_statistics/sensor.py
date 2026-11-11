"""Sensors: today's totals, current prices and the latest reading time.

The Energy dashboard should use the ``glow_statistics:*`` external statistics, which
place late readings in the hour they belong to. These sensors are for cards and
automations; their state classes are valid so Home Assistant records them as they are.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME, TARIFF_TZ
from .coordinator import GlowConfigEntry, GlowCoordinator, Stream, StreamData
from .statistics import CURRENCY

PARALLEL_UPDATES = 0


def _today_start() -> datetime:
    today = dt_util.now(TARIFF_TZ).date()
    return datetime.combine(today, datetime.min.time(), TARIFF_TZ).astimezone(UTC)


@dataclass(frozen=True, kw_only=True)
class GlowSensorDescription(SensorEntityDescription):
    """Describes a Glow Statistics sensor."""

    value_fn: Callable[[StreamData], float | datetime | None]
    resets_daily: bool = False
    cost_only: bool = False


SENSORS: tuple[GlowSensorDescription, ...] = (
    GlowSensorDescription(
        key="consumption_today",
        translation_key="consumption_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda d: d.today_kwh,
        resets_daily=True,
    ),
    GlowSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: d.today_cost,
        resets_daily=True,
        cost_only=True,
    ),
    GlowSensorDescription(
        key="unit_rate",
        translation_key="unit_rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=f"{CURRENCY}/{UnitOfEnergy.KILO_WATT_HOUR}",
        suggested_display_precision=4,
        value_fn=lambda d: round(d.prices.unit_rate / 100, 6) if d.prices else None,
        cost_only=True,
    ),
    GlowSensorDescription(
        key="standing_charge",
        translation_key="standing_charge",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=f"{CURRENCY}/d",
        suggested_display_precision=4,
        value_fn=lambda d: round(d.prices.standing_charge / 100, 6) if d.prices else None,
        cost_only=True,
    ),
    GlowSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_reading,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every stream."""
    coordinator = entry.runtime_data
    async_add_entities(
        GlowSensor(coordinator, stream, description)
        for stream in coordinator.streams
        for description in SENSORS
        if stream.cost_id or not description.cost_only
    )


class GlowSensor(CoordinatorEntity[GlowCoordinator], SensorEntity):
    """A value from the latest import."""

    _attr_has_entity_name = True
    entity_description: GlowSensorDescription

    def __init__(
        self, coordinator: GlowCoordinator, stream: Stream, description: GlowSensorDescription
    ) -> None:
        super().__init__(coordinator)
        if stream.fuel.tariff_fuel is None and description.key == "consumption_today":
            description = replace(description, translation_key="export_today")
        self.entity_description = description
        self._stream = stream
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{stream.key}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}_{stream.key}")},
            name=f"{NAME} {stream.fuel.name.split()[0].capitalize()}"
            + (" export" if stream.fuel.tariff_fuel is None else "")
            + stream.label,
            manufacturer="Hildebrand",
            model=f"Glow DCC {stream.fuel.classifier}",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _data(self) -> StreamData | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.streams.get(self._stream.key)

    @property
    def available(self) -> bool:
        """Available once a run has produced data for this stream."""
        return super().available and self._data is not None

    @property
    def native_value(self) -> float | datetime | None:
        """Value from the latest run."""
        data = self._data
        return self.entity_description.value_fn(data) if data else None

    @property
    def last_reset(self) -> datetime | None:
        """Daily totals restart at UK midnight."""
        if self.entity_description.resets_daily:
            return _today_start()
        return None
