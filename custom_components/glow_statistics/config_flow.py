"""Config and options flows for Glow Statistics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import GlowAuthError, GlowClient, GlowError
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_LOOKBACK_DAYS,
    CONF_OVERRIDES,
    CONF_SCAN_INTERVAL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FUELS,
    NAME,
    TARIFF_FUELS,
)
from .tariffs import OV_END, OV_FUEL, OV_RATE, OV_STANDING, OV_START, TariffOverride

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


async def _validate(hass, username: str, password: str) -> tuple[str, dict[str, str]]:
    """Log in and check the account has meters; returns (unique id, errors)."""
    client = GlowClient(async_get_clientsession(hass), username, password)
    try:
        await client.authenticate()
        resources = await client.get_resources()
    except GlowAuthError:
        return "", {"base": "invalid_auth"}
    except GlowError:
        return "", {"base": "cannot_connect"}
    if not any(r.classifier in {f.classifier for f in FUELS} for r in resources):
        return "", {"base": "no_meters"}
    return (client.account_id or username).lower(), {}


class GlowStatisticsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Glow / Bright app login."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Credentials step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            unique_id, errors = await _validate(
                self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"{NAME} ({user_input[CONF_USERNAME]})", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(CREDENTIALS_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            unique_id, errors = await _validate(
                self.hass, entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    )
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GlowStatisticsOptionsFlow:
        """Options flow."""
        return GlowStatisticsOptionsFlow()


def _number(minimum: float, maximum: float, step: float | str, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
        )
    )


class GlowStatisticsOptionsFlow(OptionsFlowWithReload):
    """Refresh interval, look-back window and tariff overrides."""

    def _overrides(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(CONF_OVERRIDES, []))

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu."""
        menu = ["settings", "add_override"]
        if self._overrides():
            menu.append("remove_override")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu,
            description_placeholders={
                "overrides": "\n".join(
                    f"- {TariffOverride.from_option(o).label()}" for o in self._overrides()
                )
                or "- none"
            },
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Refresh interval, look-back and backfill limit."""
        if user_input is not None:
            return self._save(**{k: int(v) for k, v in user_input.items()})
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): _number(30, 720, 1, "min"),
                vol.Required(
                    CONF_LOOKBACK_DAYS, default=options.get(CONF_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS)
                ): _number(1, 60, 1, "d"),
                vol.Required(
                    CONF_BACKFILL_DAYS, default=options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
                ): _number(10, 3660, 1, "d"),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_add_override(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add dated prices for one fuel."""
        errors: dict[str, str] = {}
        if user_input is not None:
            start = date.fromisoformat(user_input[OV_START])
            end = user_input.get(OV_END)
            if end and date.fromisoformat(end) < start:
                errors[OV_END] = "end_before_start"
            elif any(
                o[OV_FUEL] == user_input[OV_FUEL] and o[OV_START] == user_input[OV_START]
                for o in self._overrides()
            ):
                errors[OV_START] = "duplicate_start"
            else:
                override = {
                    OV_FUEL: user_input[OV_FUEL],
                    OV_START: start.isoformat(),
                    OV_END: end or None,
                    OV_STANDING: float(user_input[OV_STANDING]),
                    OV_RATE: float(user_input[OV_RATE]),
                }
                overrides = sorted([*self._overrides(), override], key=lambda o: (o[OV_FUEL], o[OV_START]))
                return self._save(**{CONF_OVERRIDES: overrides})
        schema = vol.Schema(
            {
                vol.Required(OV_FUEL): SelectSelector(
                    SelectSelectorConfig(
                        options=list(TARIFF_FUELS),
                        translation_key="fuel",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(OV_START): DateSelector(),
                vol.Optional(OV_END): DateSelector(),
                vol.Required(OV_STANDING): _number(0, 1000, "any", "p/day"),
                vol.Required(OV_RATE): _number(0, 1000, "any", "p/kWh"),
            }
        )
        return self.async_show_form(
            step_id="add_override",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_remove_override(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Remove overrides."""
        overrides = self._overrides()
        if user_input is not None:
            remove = {int(i) for i in user_input.get("remove", [])}
            return self._save(**{CONF_OVERRIDES: [o for i, o in enumerate(overrides) if i not in remove]})
        schema = vol.Schema(
            {
                vol.Optional("remove", default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=str(i), label=TariffOverride.from_option(o).label())
                            for i, o in enumerate(overrides)
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_override", data_schema=schema)
