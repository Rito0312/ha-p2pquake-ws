"""接続状態."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import P2PQuakeConfigEntry
from .entity import P2PQuakeEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: P2PQuakeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ConnectedSensor(entry, "connected")])


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
