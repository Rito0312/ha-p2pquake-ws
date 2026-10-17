"""共通エンティティ."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import P2PQuakeConfigEntry
from .const import DOMAIN, SIGNAL_UPDATE


class P2PQuakeEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    _platform_domain: str

    def __init__(self, entry: P2PQuakeConfigEntry, key: str) -> None:
        # 日本語名から読みにくい ID が生成されないよう、ID は固定する
        self.entity_id = f"{self._platform_domain}.{DOMAIN}_{key}"
        self._entry = entry
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            translation_key="service",
            manufacturer="P2P地震情報",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
