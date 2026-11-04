"""緊急地震速報・地震情報の画像（オプション）.

受信するたびに作り直す。HA の画像エンティティなので、ダッシュボードや
画面付きの端末（TECHO5 の notify_picture など）から表示できる。
- image.p2pquake_ws_eew_map: 緊急地震速報（556）
- image.p2pquake_ws_quake_map: 地震情報（551）
"""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import P2PQuakeConfigEntry
from .entity import P2PQuakeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: P2PQuakeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.runtime_data.image_enabled:
        async_add_entities(
            [
                P2PQuakeImage(hass, entry, "eew_map", "image"),
                P2PQuakeImage(hass, entry, "quake_map", "quake_image"),
            ]
        )


class P2PQuakeImage(P2PQuakeEntity, ImageEntity):
    """最後に受信した情報の画像（PNG、1280×800）.

    状態（最終更新時刻）が変わるのは画像を作り直したときだけ。
    """

    _platform_domain = "image"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, entry: P2PQuakeConfigEntry, key: str, field: str) -> None:
        ImageEntity.__init__(self, hass)
        P2PQuakeEntity.__init__(self, entry, key)
        self._field = field

    @property
    def image_last_updated(self) -> datetime | None:
        return getattr(self._runtime, f"{self._field}_at")

    async def async_image(self) -> bytes | None:
        return getattr(self._runtime, self._field)
