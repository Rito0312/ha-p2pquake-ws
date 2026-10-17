"""P2P地震情報 WebSocket 受信.

緊急地震速報（警報, code 556）を受信したら、受信した JSON をそのまま
`p2pquake_eew` イベントとして発火する。判定や機器の操作はしない。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .client import P2PQuakeClient, message_id
from .const import (
    CODE_EEW,
    CONF_ENDPOINT,
    DOMAIN,
    ENDPOINT_PRODUCTION,
    ENDPOINT_SANDBOX,
    ENDPOINT_URLS,
    EVENT_EEW,
    SIGNAL_UPDATE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

# HA の起動中（オートメーションがまだトリガーを取り付けていない間）に受信した速報は保留し、
# 起動完了の少し後に発火し直す。古すぎるものは捨てる。
STARTUP_FLUSH_DELAY = 10.0
STARTUP_MAX_AGE = 180.0
ISSUE_SANDBOX = "sandbox_endpoint"


@dataclass
class RuntimeData:
    client: P2PQuakeClient
    endpoint: str
    last_eew_at: datetime | None = None
    last_eew: dict[str, Any] | None = None
    pending: list[tuple[datetime, dict[str, Any]]] = field(default_factory=list)


type P2PQuakeConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> bool:
    endpoint = entry.options.get(
        CONF_ENDPOINT, entry.data.get(CONF_ENDPOINT, ENDPOINT_PRODUCTION)
    )
    url = ENDPOINT_URLS[endpoint]

    @callback
    def _fire(event_data: dict[str, Any]) -> None:
        hass.bus.async_fire(EVENT_EEW, event_data)

    @callback
    def _on_message(data: dict[str, Any]) -> None:
        # 556 以外（ピア分布・揺れた報告など）はエンティティを更新しない
        # （HA のデータベースに書き込まない）
        if data.get("code") != CODE_EEW:
            return
        now = dt_util.utcnow()
        runtime.last_eew_at = now
        runtime.last_eew = data
        event_data = dict(data)
        event_data["_received_at"] = now.isoformat()
        event_data["_message_id"] = message_id(data)
        event_data["_endpoint"] = endpoint
        if hass.state is CoreState.running:
            _fire(event_data)
        else:
            _LOGGER.warning("HA の起動完了前に緊急地震速報を受信したため、起動後に発火します")
            runtime.pending.append((now, event_data))
        async_dispatcher_send(hass, SIGNAL_UPDATE)

    @callback
    def _flush_pending(_now: Any = None) -> None:
        now = dt_util.utcnow()
        pending, runtime.pending = runtime.pending, []
        for received_at, event_data in pending:
            if (now - received_at).total_seconds() <= STARTUP_MAX_AGE:
                _fire(event_data)
            else:
                _LOGGER.warning("起動中に受信した古い速報を破棄: %s", event_data.get("_message_id"))

    @callback
    def _on_started(_hass: HomeAssistant) -> None:
        # オートメーションのトリガー取り付けも起動完了時に行われるため、少し待ってから発火する
        entry.async_on_unload(async_call_later(hass, STARTUP_FLUSH_DELAY, _flush_pending))

    @callback
    def _on_status(_connected: bool) -> None:
        async_dispatcher_send(hass, SIGNAL_UPDATE)

    client = P2PQuakeClient(
        async_get_clientsession(hass), url, _on_message, _on_status
    )
    runtime = RuntimeData(client=client, endpoint=endpoint)
    entry.runtime_data = runtime

    if endpoint == ENDPOINT_SANDBOX:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_SANDBOX,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SANDBOX,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(async_at_started(hass, _on_started))

    task = entry.async_create_background_task(
        hass, client.run(), f"{entry.domain}_websocket"
    )

    @callback
    def _on_task_done(t: asyncio.Task[None]) -> None:
        if t.cancelled() or client.stopped:
            return
        _LOGGER.error(
            "受信タスクが予期せず終了しました。緊急地震速報を受信できません: %r",
            t.exception(),
        )
        client.connected = False
        async_dispatcher_send(hass, SIGNAL_UPDATE)

    task.add_done_callback(_on_task_done)
    entry.async_on_unload(client.stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> bool:
    # 無効化・削除・再読み込みのどれでも警告を消す（再読み込みなら setup で作り直される）
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX)
