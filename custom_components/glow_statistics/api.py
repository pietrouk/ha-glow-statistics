"""Minimal async client for the Hildebrand Glowmarkt (Bright) REST API.

Endpoints, headers and limits follow the public "Glowmarkt API Description
Individual User" document (v1.7) and the pyglowmarkt library:

* ``POST auth`` with ``{"username", "password"}`` returns ``{"valid", "token", "exp"}``;
  the token lasts 7 days and is reused until shortly before ``exp``.
* ``GET virtualentity`` and ``GET virtualentity/{veId}/resources`` list the resources;
  each has a ``classifier`` such as ``electricity.consumption`` or ``gas.consumption``.
* ``GET resource/{id}/readings?from&to&period&offset&function&nulls`` returns
  ``{"data": [[utc_epoch_seconds, value], ...], "units": "kWh"}``; PT30M allows 10 days per query.
* ``GET resource/{id}/tariff`` returns ``{"data": [{"currentRates": {"rate", "standingCharge"}}]}``
  in pence (incl. VAT).
* ``GET resource/{id}/catchup`` asks Glow to fetch outstanding data from the DCC.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .const import API_BASE, APPLICATION_ID, MAX_PT30M_SPAN

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=60)


class GlowError(Exception):
    """Base error for the Glowmarkt API."""


class GlowAuthError(GlowError):
    """The credentials were rejected."""


class GlowConnectionError(GlowError):
    """The API could not be reached or returned an unexpected response."""


class GlowRateLimitError(GlowConnectionError):
    """The API asked us to slow down (HTTP 429)."""


@dataclass(slots=True)
class GlowResource:
    """A Glowmarkt resource (one data stream of a meter)."""

    resource_id: str
    classifier: str
    name: str
    base_unit: str | None
    ve_id: str
    ve_name: str | None


@dataclass(slots=True)
class GlowTariff:
    """Current tariff of a resource, in pence incl. VAT."""

    unit_rate: float  # p/kWh
    standing_charge: float  # p/day


def _api_time(when: datetime) -> str:
    """Naive UTC ISO time as the API expects (used together with offset=0)."""
    return when.astimezone(UTC).replace(tzinfo=None, microsecond=0).isoformat()


def parse_tariff(payload: Any) -> GlowTariff | None:
    """Extract the current unit rate and standing charge from a tariff response."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        return None
    for item in reversed(data):
        rates = item.get("currentRates") or {}
        rate, standing = rates.get("rate"), rates.get("standingCharge")
        if rate is None or standing is None:
            # Older responses describe the tariff under structure[].planDetail[].
            for struct in item.get("structure") or []:
                details = struct.get("planDetail") or []
                if isinstance(details, dict):
                    details = [details]
                for detail in details:
                    rate = detail.get("rate", rate)
                    standing = detail.get("standing", standing)
        try:
            return GlowTariff(unit_rate=float(rate), standing_charge=float(standing))
        except (TypeError, ValueError):
            continue
    return None


class GlowClient:
    """Async Glowmarkt API client."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_exp: float = 0
        self.account_id: str | None = None
        self._lock = asyncio.Lock()
        # Pauses before retrying a failed GET (timeouts, HTTP 5xx; four times as long for 429).
        self.retry_delays: tuple[float, ...] = (5, 20, 60)

    async def authenticate(self) -> None:
        """Log in and keep the token."""
        try:
            async with self._session.post(
                f"{API_BASE}auth",
                json={"username": self._username, "password": self._password},
                headers={"applicationId": APPLICATION_ID},
                timeout=TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise GlowAuthError("Invalid username or password")
                if resp.status == 429:
                    raise GlowRateLimitError("Rate limited during login")
                if resp.status != 200:
                    raise GlowConnectionError(f"Login failed with HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GlowConnectionError(
                f"Cannot reach the Glowmarkt API ({type(err).__name__}: {err})"
            ) from err
        if not isinstance(body, dict) or not body.get("valid") or not body.get("token"):
            raise GlowAuthError("Invalid username or password")
        self._token = body["token"]
        self._token_exp = float(body.get("exp") or time.time() + 6 * 86400)
        self.account_id = body.get("accountId")

    async def _ensure_token(self) -> None:
        async with self._lock:
            # Renew an hour before the 7-day token expires.
            if self._token is None or time.time() > self._token_exp - 3600:
                await self.authenticate()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with re-login on 401 and retries on timeouts, 5xx and 429."""
        await self._ensure_token()
        relogged = False
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._session.get(
                    f"{API_BASE}{path}",
                    params=params,
                    headers={
                        "applicationId": APPLICATION_ID,
                        "token": self._token or "",
                        "Content-Type": "application/json",
                    },
                    timeout=TIMEOUT,
                ) as resp:
                    if resp.status == 401 and not relogged:
                        # Token revoked or expired early: log in again once.
                        relogged = True
                        self._token = None
                        await self._ensure_token()
                        attempt -= 1
                        continue
                    if resp.status in (401, 403):
                        raise GlowAuthError(f"Not authorised for {path}")
                    if resp.status == 429:
                        error: GlowConnectionError = GlowRateLimitError(f"Rate limited on {path}")
                    elif resp.status >= 500:
                        error = GlowConnectionError(f"HTTP {resp.status} on {path}")
                    elif resp.status != 200:
                        raise GlowConnectionError(f"HTTP {resp.status} on {path}")
                    else:
                        return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                error = GlowConnectionError(
                    f"Cannot reach the Glowmarkt API ({type(err).__name__}: {err}) on {path}"
                )
                error.__cause__ = err
            if attempt > len(self.retry_delays):
                raise error
            delay = self.retry_delays[attempt - 1]
            if isinstance(error, GlowRateLimitError):
                delay *= 4
            _LOGGER.debug("%s; retrying in %s s", error, delay)
            await asyncio.sleep(delay)

    async def get_resources(self) -> list[GlowResource]:
        """All resources of all virtual entities of the account."""
        entities = await self._get("virtualentity")
        if not isinstance(entities, list):
            raise GlowConnectionError("Unexpected virtual entity response")
        resources: list[GlowResource] = []
        for ve in entities:
            ve_id = ve.get("veId")
            if not ve_id:
                continue
            detail = await self._get(f"virtualentity/{ve_id}/resources")
            for res in (detail or {}).get("resources", []):
                if not res.get("resourceId") or not res.get("classifier"):
                    continue
                resources.append(
                    GlowResource(
                        resource_id=res["resourceId"],
                        classifier=res["classifier"],
                        name=res.get("name") or res["classifier"],
                        base_unit=res.get("baseUnit"),
                        ve_id=ve_id,
                        ve_name=ve.get("name"),
                    )
                )
        return resources

    async def catchup(self, resource_id: str) -> None:
        """Ask Glow to pull outstanding readings from the DCC (best effort)."""
        try:
            await self._get(f"resource/{resource_id}/catchup")
        except GlowAuthError:
            raise
        except GlowError as err:
            _LOGGER.debug("Catch-up request for %s failed: %s", resource_id, err)

    async def get_tariff(self, resource_id: str) -> GlowTariff | None:
        """The resource's current tariff, or None when Glow has none."""
        try:
            return parse_tariff(await self._get(f"resource/{resource_id}/tariff"))
        except GlowAuthError:
            raise
        except GlowError as err:
            _LOGGER.debug("No tariff for %s: %s", resource_id, err)
            return None

    async def get_readings(
        self, resource_id: str, start: datetime, end: datetime, pause: float = 0
    ) -> dict[datetime, float]:
        """Half-hourly readings between start and end, keyed by UTC slot start.

        Slots Glow has no data for yet are left out (``nulls=1`` tells them apart from 0).
        Queries are split into the API's 10-day limit.
        """
        out: dict[datetime, float] = {}
        chunk_start = start
        first = True
        while chunk_start < end:
            chunk_end = min(chunk_start + MAX_PT30M_SPAN, end)
            if not first and pause:
                await asyncio.sleep(pause)
            first = False
            out.update(await self._readings_chunk(resource_id, chunk_start, chunk_end - timedelta(seconds=1)))
            chunk_start = chunk_end
        return out

    async def _readings_chunk(
        self, resource_id: str, start: datetime, end: datetime
    ) -> dict[datetime, float]:
        body = await self._get(
            f"resource/{resource_id}/readings",
            {
                "from": _api_time(start),
                "to": _api_time(end),
                "period": "PT30M",
                "offset": 0,
                "function": "sum",
                "nulls": 1,
            },
        )
        out: dict[datetime, float] = {}
        for item in (body or {}).get("data") or []:
            try:
                ts, value = item[0], item[1]
            except (TypeError, IndexError):
                continue
            if value is None:
                continue
            slot = datetime.fromtimestamp(int(ts), UTC)
            if start <= slot <= end:
                out[slot] = float(value)
        return out
