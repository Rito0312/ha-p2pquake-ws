"""テスト画像を作るボタン（オプション）.

押すと【テスト】と付いた架空の緊急地震速報の画像を作り、image.p2pquake_ws_eew_map を更新する。
p2pquake_eew イベントは発火しないので、オートメーション（照明・通知など）は動かない。
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import P2PQuakeConfigEntry, render
from .entity import P2PQuakeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: P2PQuakeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.runtime_data.image_enabled:
        async_add_entities([TestImageButton(entry, "test_image")])


# 本物の緊急地震速報を受信してからこの秒数の間は、テスト画像で本物の画像を上書きしない
PROTECT_REAL_SECONDS = 600


class TestImageButton(P2PQuakeEntity, ButtonEntity):
    _platform_domain = "button"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        runtime = self._runtime
        if runtime.last_eew_at is not None and (
            dt_util.utcnow() - runtime.last_eew_at
        ).total_seconds() < PROTECT_REAL_SECONDS:
            raise HomeAssistantError(
                "10分以内に本物の緊急地震速報を受信したため、テスト画像は作りません（本物の画像を残します）"
            )
        if runtime.render_eew is not None:
            runtime.render_eew(render.sample_eew(runtime.home_area, dt_util.now()))
