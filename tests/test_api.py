"""The Glowmarkt client against recorded-style API responses."""

from __future__ import annotations

import re

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.glow_statistics.api import (
    GlowAuthError,
    GlowClient,
    GlowConnectionError,
    GlowRateLimitError,
    parse_tariff,
)
from custom_components.glow_statistics.const import API_BASE

from .conftest import utc

AUTH_OK = {"valid": True, "token": "tok", "exp": 4102444800, "accountId": "ACC-1"}


async def test_login_resources_readings(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API_BASE}auth", json=AUTH_OK)
    aioclient_mock.get(f"{API_BASE}virtualentity", json=[{"veId": "ve-1", "name": "Home"}])
    aioclient_mock.get(
        f"{API_BASE}virtualentity/ve-1/resources",
        json={
            "resources": [
                {
                    "resourceId": "r1",
                    "classifier": "electricity.consumption",
                    "name": "electricity",
                    "baseUnit": "kWh",
                },
                {"resourceId": "r2", "classifier": "gas.consumption", "name": "gas", "baseUnit": "kWh"},
            ]
        },
    )
    aioclient_mock.get(
        re.compile(r".*/resource/r1/readings.*"),
        json={"units": "kWh", "data": [[1767225600, 0.25], [1767227400, None], [1767229200, 0.5]]},
    )
    client = GlowClient(async_get_clientsession(hass), "me@example.com", "pw")
    await client.authenticate()
    assert client.account_id == "ACC-1"
    resources = await client.get_resources()
    assert [r.classifier for r in resources] == ["electricity.consumption", "gas.consumption"]

    readings = await client.get_readings("r1", utc(2026, 1, 1), utc(2026, 1, 1, 2))
    assert readings == {utc(2026, 1, 1, 0): 0.25, utc(2026, 1, 1, 1): 0.5}  # null slot left out
    call = aioclient_mock.mock_calls[-1]
    params = dict(call[1].query)
    assert params["from"] == "2026-01-01T00:00:00"
    assert params["to"] == "2026-01-01T01:59:59"
    assert (params["period"], params["offset"], params["function"], params["nulls"]) == (
        "PT30M",
        "0",
        "sum",
        "1",
    )
    assert call[3]["token"] == "tok"
    assert call[3]["applicationId"] == "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"


async def test_readings_split_into_ten_day_queries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{API_BASE}auth", json=AUTH_OK)
    aioclient_mock.get(re.compile(r".*/readings.*"), json={"data": []})
    client = GlowClient(async_get_clientsession(hass), "u", "p")
    await client.get_readings("r1", utc(2026, 1, 1), utc(2026, 1, 25))
    assert aioclient_mock.call_count == 4  # login + 3 windows


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, None, GlowAuthError),
        (200, {"valid": False}, GlowAuthError),
        (500, None, GlowConnectionError),
        (429, None, GlowRateLimitError),
    ],
)
async def test_login_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, body, error
) -> None:
    aioclient_mock.post(f"{API_BASE}auth", status=status, json=body)
    client = GlowClient(async_get_clientsession(hass), "u", "p")
    with pytest.raises(error):
        await client.authenticate()


async def test_tariff_and_catchup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API_BASE}auth", json=AUTH_OK)
    aioclient_mock.get(
        f"{API_BASE}resource/r1/tariff",
        json={"data": [{"name": "t", "currentRates": {"rate": 24.5, "standingCharge": 50.12}}]},
    )
    aioclient_mock.get(f"{API_BASE}resource/r2/tariff", status=404)
    aioclient_mock.get(f"{API_BASE}resource/r1/catchup", status=500)
    client = GlowClient(async_get_clientsession(hass), "u", "p")
    client.retry_delays = (0, 0, 0)
    tariff = await client.get_tariff("r1")
    assert (tariff.unit_rate, tariff.standing_charge) == (24.5, 50.12)
    assert await client.get_tariff("r2") is None
    await client.catchup("r1")  # errors are ignored


def test_parse_tariff_structure_format() -> None:
    payload = {"data": [{"structure": [{"planDetail": [{"rate": 6.1}, {"standing": 29.9}]}]}]}
    tariff = parse_tariff(payload)
    assert (tariff.unit_rate, tariff.standing_charge) == (6.1, 29.9)
    assert parse_tariff({"data": []}) is None
    assert parse_tariff({"data": [{"currentRates": {}}]}) is None


async def test_retries_timeouts_and_server_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A timeout or HTTP 5xx is retried; it fails only after every retry."""
    aioclient_mock.post(f"{API_BASE}auth", json=AUTH_OK)
    aioclient_mock.get(f"{API_BASE}virtualentity", exc=TimeoutError())
    client = GlowClient(async_get_clientsession(hass), "u", "p")
    client.retry_delays = (0, 0, 0)
    with pytest.raises(GlowConnectionError, match="TimeoutError"):
        await client.get_resources()
    assert aioclient_mock.call_count == 1 + 4  # login + first try + 3 retries

    aioclient_mock.clear_requests()
    calls = {"n": 0}

    async def flaky(method, url, data):
        calls["n"] += 1
        if calls["n"] < 3:
            return AiohttpClientMockResponse(method, url, status=502)
        return AiohttpClientMockResponse(method, url, json={"data": [[1767225600, 0.25]]})

    aioclient_mock.get(re.compile(r".*/readings.*"), side_effect=flaky)
    readings = await client.get_readings("r1", utc(2026, 1, 1), utc(2026, 1, 1, 1))
    assert readings == {utc(2026, 1, 1): 0.25}
    assert calls["n"] == 3
