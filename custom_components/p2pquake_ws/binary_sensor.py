"""接続状態（WebSocket）と取得状態（JSON API、予備の受信）."""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import P2PQuakeConfigEntry
from .const import SIGNAL_REST
from .entity import P2PQuakeEntity

# 緊急地震速報の取得（10 秒ごと）がこの秒数より長く成功していなければ off
REST_HEALTHY_WITHIN = 60.0
REST_RECHECK = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: P2PQuakeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[BinarySensorEntity] = [ConnectedSensor(entry, "connected")]
    if entry.runtime_data.poller is not None:  # 本番のときだけ（サンドボックスでは作らない）
        entities.append(RestSensor(entry, "rest"))
    async_add_entities(entities)


class ConnectedSensor(P2PQuakeEntity, BinarySensorEntity):
    _platform_domain = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self._runtime.client.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "endpoint": self._runtime.endpoint,
            "connect_count": self._runtime.client.connect_count,
            "stall_reconnect_count": self._runtime.client.stall_count,
        }


class RestSensor(P2PQuakeEntity, BinarySensorEntity):
    """JSON API（予備の受信）で緊急地震速報を取得できているか.

    時刻で判定し、30 秒ごとに見直す（取得タスクが止まっても on のまま残らない）。
    状態が変わったときだけ書き込む（10 秒ごとの取得のたびに recorder へ書かない）。
    """

    _platform_domain = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: P2PQuakeConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        self._written: bool | None = None

    async def async_added_to_hass(self) -> None:
        # SIGNAL_UPDATE（速報の受信など）では書き込まない。取得状態のシグナルとタイマーだけ
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_REST, self._recheck)
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._recheck, REST_RECHECK)
        )

    def _healthy(self) -> bool:
        poller = self._runtime.poller
        if poller is None or not poller.alive or poller.last_success is None:
            return False
        return time.monotonic() - poller.last_success < REST_HEALTHY_WITHIN

    @property
    def is_on(self) -> bool:
        return self._healthy()

    @callback
    def _recheck(self, *_args: Any) -> None:
        value = self._healthy()
        if value != self._written:
            self._written = value
            self.async_write_ha_state()

    @callback
    def async_write_ha_state(self) -> None:
        self._written = self._healthy()
        super().async_write_ha_state()
