"""Fixtures for Glow Statistics tests: a fake Glowmarkt API with half-hourly data."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glow_statistics.api import (
    GlowAuthError,
    GlowConnectionError,
    GlowResource,
    GlowTariff,
)
from custom_components.glow_statistics.const import DOMAIN

ELEC = "res-elec"
GAS = "res-gas"
EXPORT = "res-export"
HALF = timedelta(minutes=30)


class FakeGlow:
    """In-memory stand-in for GlowClient."""

    def __init__(self) -> None:
        self.account_id = "ACC-1"
        self.resources = [
            GlowResource(ELEC, "electricity.consumption", "electricity", "kWh", "ve-1", "Home"),
            GlowResource(
                "res-elec-cost", "electricity.consumption.cost", "electricity cost", "pence", "ve-1", "Home"
            ),
            GlowResource(GAS, "gas.consumption", "gas", "kWh", "ve-1", "Home"),
            GlowResource("res-gas-cost", "gas.consumption.cost", "gas cost", "pence", "ve-1", "Home"),
        ]
        self.slots: dict[str, dict[datetime, float]] = {ELEC: {}, GAS: {}, EXPORT: {}}
        self.tariffs: dict[str, GlowTariff | None] = {
            ELEC: GlowTariff(unit_rate=24.5, standing_charge=50.0),
            GAS: GlowTariff(unit_rate=6.0, standing_charge=30.0),
        }
        self.auth_error = False
        self.connection_error = False
        self.reading_calls: list[tuple[str, datetime, datetime]] = []
        self.catchups: list[str] = []

    def fill(
        self, rid: str, start: datetime, end: datetime, value: float | Callable[[datetime], float]
    ) -> None:
        """Half-hour slots from start (inclusive) to end (exclusive)."""
        t = start
        while t < end:
            self.slots[rid][t] = value(t) if callable(value) else value
            t += HALF

    def _check(self) -> None:
        if self.auth_error:
            raise GlowAuthError("bad password")
        if self.connection_error:
            raise GlowConnectionError("down")

    async def authenticate(self) -> None:
        self._check()

    async def get_resources(self) -> list[GlowResource]:
        self._check()
        return self.resources

    async def catchup(self, resource_id: str) -> None:
        self._check()
        self.catchups.append(resource_id)

    async def get_tariff(self, resource_id: str) -> GlowTariff | None:
        self._check()
        return self.tariffs.get(resource_id)

    async def get_readings(
        self, resource_id: str, start: datetime, end: datetime, pause: float = 0
    ) -> dict[datetime, float]:
        self._check()
        self.reading_calls.append((resource_id, start, end))
        return {t: v for t, v in self.slots.get(resource_id, {}).items() if start <= t < end}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations) -> None:
    """Start the recorder (a dependency) before hass, and load custom_components/."""


@pytest.fixture(autouse=True)
def no_pause() -> Generator[None]:
    """Skip the politeness pause between API requests."""
    with patch("custom_components.glow_statistics.coordinator.REQUEST_PAUSE", 0):
        yield


@pytest.fixture
def fake_glow() -> Generator[FakeGlow]:
    """Patch GlowClient everywhere with one shared fake."""
    fake = FakeGlow()
    factory = MagicMock(return_value=fake)
    with (
        patch("custom_components.glow_statistics.GlowClient", factory),
        patch("custom_components.glow_statistics.config_flow.GlowClient", factory),
    ):
        yield fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured Glow Statistics entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Glow Statistics (me@example.com)",
        unique_id="acc-1",
        data={"username": "me@example.com", "password": "secret"},
        options={},
    )


NOW = "2026-07-15 10:20:00+00:00"


@pytest.fixture
def summer_data(fake_glow: FakeGlow, freezer) -> FakeGlow:
    """0.5 kWh/h electricity and 2 kWh/h gas from 1 June to 15 July 09:00 UTC."""
    freezer.move_to(NOW)
    fake_glow.fill(ELEC, utc(2026, 6, 1), utc(2026, 7, 15, 9), 0.25)
    fake_glow.fill(GAS, utc(2026, 6, 1), utc(2026, 7, 15, 9), 1.0)
    return fake_glow


def utc(*args: int) -> datetime:
    """Shorthand for an aware UTC datetime."""
    return datetime(*args, tzinfo=UTC)
