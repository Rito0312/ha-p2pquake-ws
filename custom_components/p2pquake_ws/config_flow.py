"""設定フロー（1つだけ追加でき、接続先を本番／サンドボックスから選ぶ）."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import CONF_ENDPOINT, DOMAIN, ENDPOINT_PRODUCTION, ENDPOINT_URLS

ENDPOINT_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=list(ENDPOINT_URLS), translation_key=CONF_ENDPOINT)
)


class P2PQuakeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # 2つ目の追加は manifest の single_config_entry により HA 側で中断される
        if user_input is not None:
            return self.async_create_entry(title="P2P地震情報 WebSocket", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ENDPOINT, default=ENDPOINT_PRODUCTION): ENDPOINT_SELECTOR}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return P2PQuakeOptionsFlow()


class P2PQuakeOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_ENDPOINT, self.config_entry.data.get(CONF_ENDPOINT, ENDPOINT_PRODUCTION)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_ENDPOINT, default=current): ENDPOINT_SELECTOR}
            ),
        )
