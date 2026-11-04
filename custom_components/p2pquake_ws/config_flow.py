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
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import render
from .const import (
    CONF_ENDPOINT,
    CONF_HOME_AREA,
    CONF_IMAGE,
    DEFAULT_IMAGE,
    DOMAIN,
    ENDPOINT_PRODUCTION,
    ENDPOINT_URLS,
)

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
    _names_failed = False  # 細分区域の名前を読めず、自宅の地域の欄を出せなかった
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # 選択肢を出せなかったときは、今の自宅の地域をそのまま残す
            user_input.setdefault(
                CONF_HOME_AREA, self.config_entry.options.get(CONF_HOME_AREA, "") if self._names_failed else ""
            )
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        current = options.get(
            CONF_ENDPOINT, self.config_entry.data.get(CONF_ENDPOINT, ENDPOINT_PRODUCTION)
        )
        # 地図データ（細分区域の名前）はファイルから読むので、イベントループの外で読む。
        # 読めなくても設定画面は開けるようにする（接続先を戻せなくなるのを防ぐ）
        try:
            names = await self.hass.async_add_executor_job(render.area_names)
        except Exception:  # noqa: BLE001
            names = []
            self._names_failed = True
        schema: dict[Any, Any] = {
            vol.Required(CONF_ENDPOINT, default=current): ENDPOINT_SELECTOR,
            vol.Required(
                CONF_IMAGE, default=options.get(CONF_IMAGE, DEFAULT_IMAGE)
            ): BooleanSelector(),
        }
        if names:
            home = options.get(CONF_HOME_AREA) or ""
            home_key = (
                vol.Optional(CONF_HOME_AREA, description={"suggested_value": home})
                if home in names
                else vol.Optional(CONF_HOME_AREA)
            )
            schema[home_key] = SelectSelector(
                SelectSelectorConfig(options=names, mode=SelectSelectorMode.DROPDOWN)
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
