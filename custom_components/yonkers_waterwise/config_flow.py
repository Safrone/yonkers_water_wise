"""Config flow for the Yonkers WaterWise integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import Account, CannotConnect, InvalidAuth, YonkersWaterWiseClient
from .const import CONF_ACCOUNT_NUMBER, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


class YonkersWaterWiseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user through connecting their WaterWise account."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise transient flow state."""
        self._credentials: dict[str, str] = {}
        self._accounts: list[Account] = []

    async def _async_fetch_accounts(
        self, username: str, password: str
    ) -> list[Account]:
        """Log in and list the accounts the credentials can see."""
        session = async_create_clientsession(self.hass)
        client = YonkersWaterWiseClient(session, username, password)
        await client.async_login()
        return await client.async_get_accounts()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and verify them against the portal."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                accounts = await self._async_fetch_accounts(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error connecting to the WaterWise portal")
                errors["base"] = "unknown"
            else:
                self._credentials = dict(user_input)
                self._accounts = accounts
                if len(accounts) == 1:
                    return await self._async_create(accounts[0])
                return await self.async_step_account()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which billing account to track."""
        if user_input is not None:
            chosen = next(
                account
                for account in self._accounts
                if account.account_number == user_input[CONF_ACCOUNT_NUMBER]
            )
            return await self._async_create(chosen)

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_NUMBER): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {
                                    "value": account.account_number,
                                    "label": account.description,
                                }
                                for account in self._accounts
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def _async_create(self, account: Account) -> ConfigFlowResult:
        """Create the config entry for `account`."""
        await self.async_set_unique_id(account.account_number)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=account.description,
            data={**self._credentials, CONF_ACCOUNT_NUMBER: account.account_number},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the stored password stopped working."""
        self._credentials = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh password and verify it."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            username = entry.data[CONF_USERNAME]
            try:
                await self._async_fetch_accounts(username, user_input[CONF_PASSWORD])
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during reauthentication")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    )
                }
            ),
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )
