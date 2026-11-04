"""最終緊急地震速報."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import P2PQuakeConfigEntry
from .entity import P2PQuakeEntity

ATTR_KEYS = ("event_id", "serial", "cancelled", "test", "origin_time", "hypocenter")


def _mapping(value: Any) -> dict[str, Any]:
    """想定外の型（null・文字列・リスト）でも例外にしない."""
    return value if isinstance(value, dict) else {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: P2PQuakeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([LastEewSensor(entry, "last_eew")])


class LastEewSensor(P2PQuakeEntity, RestoreEntity, SensorEntity):
    """最後に緊急地震速報を受信した時刻（状態が変わるのは速報を受信したときだけ）.

    HA を再起動しても直前の値を復元する。
    """

    _platform_domain = "sensor"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: P2PQuakeConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        self._restored_at: datetime | None = None
        self._restored_attrs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            # unknown など日時でない値は None になる
            self._restored_at = dt_util.parse_datetime(last.state)
            self._restored_attrs = {
                k: last.attributes.get(k) for k in ATTR_KEYS if k in last.attributes
            }

    @property
    def native_value(self) -> datetime | None:
        return self._runtime.last_eew_at or self._restored_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._runtime.last_eew is None:
            return dict(self._restored_attrs)
        eew = self._runtime.last_eew
        issue = _mapping(eew.get("issue"))
        quake = _mapping(eew.get("earthquake"))
        return {
            "event_id": issue.get("eventId"),
            "serial": issue.get("serial"),
            "cancelled": eew.get("cancelled"),
            "test": eew.get("test"),
            "origin_time": quake.get("originTime"),
            "hypocenter": _mapping(quake.get("hypocenter")).get("name"),
        }
