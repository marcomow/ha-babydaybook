"""Config flow for Baby Daybook."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    BabyDaybookAuthError,
    BabyDaybookClient,
    BabyDaybookConnectionError,
    BabyDaybookError,
)
from .const import CONF_REFRESH_TOKEN, DOMAIN


class BabyDaybookConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in with e-mail and password, optionally setting a password first."""

    VERSION = 1

    _email: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user sign in, or first add a password to a Google account."""
        return self.async_show_menu(
            step_id="user", menu_options=["sign_in", "set_password"]
        )

    async def async_step_set_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """E-mail a link that adds a password to the account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            try:
                await BabyDaybookClient.send_password_email(
                    async_get_clientsession(self.hass), self._email
                )
            except BabyDaybookAuthError:
                errors["base"] = "email_not_found"
            except BabyDaybookConnectionError:
                errors["base"] = "cannot_connect"
            except BabyDaybookError:
                errors["base"] = "unknown"
            else:
                return await self.async_step_sign_in()
        return self.async_show_form(
            step_id="set_password",
            data_schema=vol.Schema({vol.Required(CONF_EMAIL): str}),
            errors=errors,
        )

    async def async_step_sign_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in; only the resulting session token is stored."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            try:
                client = await BabyDaybookClient.sign_in_with_password(
                    async_get_clientsession(self.hass),
                    self._email,
                    user_input[CONF_PASSWORD],
                )
                babies = await client.list_babies()
            except BabyDaybookAuthError:
                errors["base"] = "invalid_auth"
            except BabyDaybookConnectionError:
                errors["base"] = "cannot_connect"
            except BabyDaybookError:
                errors["base"] = "unknown"
            else:
                data = {CONF_REFRESH_TOKEN: client.refresh_token}
                await self.async_set_unique_id(client.user_uid)
                if self.source == "reauth":
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data=data
                    )
                self._abort_if_unique_id_configured()
                if not babies:
                    return self.async_abort(reason="no_babies")
                return self.async_create_entry(title="Baby Daybook", data=data)
        return self.async_show_form(
            step_id="sign_in",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=self._email): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Ask for the password again when the stored session stops working."""
        return await self.async_step_sign_in()
