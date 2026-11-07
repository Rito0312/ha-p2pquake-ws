"""P2P地震情報 WebSocket 受信.

緊急地震速報（警報, code 556）を受信したら、受信した JSON をそのまま
`p2pquake_eew` イベントとして発火する。地震情報（code 551）は `p2pquake_quake` イベント。
判定や機器の操作はしない。

受信経路は WebSocket（client.py）と、その予備の JSON API（poller.py、本番のみ）。
どちらで届いても ingest.py で重複を除き、同じ処理を通す（イベントの `_source` で区別できる）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from . import render
from .client import Deduplicator, P2PQuakeClient, message_id
from .ingest import SOURCE_WS, Ingest
from .poller import P2PQuakePoller, parse_time
from .const import (
    CODE_EEW,
    CODE_QUAKE,
    CONF_ENDPOINT,
    CONF_HOME_AREA,
    CONF_IMAGE,
    DEFAULT_IMAGE,
    DOMAIN,
    ENDPOINT_PRODUCTION,
    ENDPOINT_SANDBOX,
    ENDPOINT_URLS,
    EVENT_EEW,
    EVENT_QUAKE,
    SIGNAL_REST,
    SIGNAL_UPDATE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.IMAGE, Platform.SENSOR]

# HA の起動中（オートメーションがまだトリガーを取り付けていない間）に受信した速報は保留し、
# 起動完了の少し後に発火し直す。古すぎるものは捨てる。
STARTUP_FLUSH_DELAY = 10.0
STARTUP_MAX_AGE = 180.0
ISSUE_SANDBOX = "sandbox_endpoint"
ISSUE_ASSETS = "image_assets"
# 画像の機能のエンティティ（画像をオフにしたら登録から外す）
IMAGE_ENTITY_KEYS = (("image", "eew_map"), ("image", "quake_map"), ("button", "test_image"))


# 緊急地震速報の eventId ごとに覚える最大の serial（古い報が後から届いたら画像などを戻さない）
EEW_SERIALS_MAX = 20


@dataclass
class RuntimeData:
    client: P2PQuakeClient
    endpoint: str
    poller: P2PQuakePoller | None = None
    eew_serials: dict[str, int] = field(default_factory=dict)
    last_eew_at: datetime | None = None
    last_eew: dict[str, Any] | None = None
    pending: list[tuple[datetime, dict[str, Any]]] = field(default_factory=list)
    image_option: bool = DEFAULT_IMAGE  # オプションの設定値
    image_enabled: bool = DEFAULT_IMAGE  # 実際に画像を作るか（地図などのファイルを読めなければ False）
    assets_ok: bool = False
    home_area: str = ""
    image: bytes | None = None
    image_at: datetime | None = None
    image_seq: int = 0
    quake_image: bytes | None = None
    quake_image_at: datetime | None = None
    quake_seq: int = 0
    render_eew: Callable[[dict[str, Any]], None] | None = None


type P2PQuakeConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> bool:
    endpoint = entry.options.get(
        CONF_ENDPOINT, entry.data.get(CONF_ENDPOINT, ENDPOINT_PRODUCTION)
    )
    url = ENDPOINT_URLS[endpoint]

    @callback
    def _fire(event_data: dict[str, Any]) -> None:
        hass.bus.async_fire(EVENT_EEW, event_data)

    def _base_event_data(data: dict[str, Any], source: str, now: datetime) -> dict[str, Any]:
        event_data = dict(data)
        event_data["_received_at"] = now.isoformat()
        event_data["_message_id"] = message_id(data)
        event_data["_endpoint"] = endpoint
        event_data["_source"] = source
        # P2P が情報を受け付けた時刻から HA が受け取るまでの秒数（遅れの確認用）
        if (p2p_time := parse_time(data.get("time"))) is not None:
            event_data["_p2p_delay"] = round((now - p2p_time).total_seconds(), 1)
        return event_data

    def _eew_is_stale(data: dict[str, Any]) -> bool:
        """同じ地震の、既に受け取った報より古い報か（WS と REST で順番が入れ替わった場合）."""
        issue = data.get("issue")
        if not isinstance(issue, dict) or not issue.get("eventId"):
            return False
        try:
            serial = int(str(issue.get("serial")))
        except ValueError:
            return False
        key = str(issue["eventId"])
        seen = runtime.eew_serials.get(key)
        if seen is not None and serial < seen:
            return True
        runtime.eew_serials.pop(key, None)
        runtime.eew_serials[key] = serial if seen is None else max(seen, serial)
        while len(runtime.eew_serials) > EEW_SERIALS_MAX:
            runtime.eew_serials.pop(next(iter(runtime.eew_serials)))
        return False

    @callback
    def _on_message(data: dict[str, Any], source: str) -> None:
        code = data.get("code")
        if code == CODE_QUAKE:
            _on_quake(data, source)
            return
        # 556・551 以外（ピア分布・揺れた報告など）はエンティティを更新しない
        # （HA のデータベースに書き込まない）
        if code != CODE_EEW:
            return
        now = dt_util.utcnow()
        stale = _eew_is_stale(data)
        event_data = _base_event_data(data, source, now)
        if hass.state is CoreState.running:
            _fire(event_data)
        else:
            _LOGGER.warning("HA の起動完了前に緊急地震速報を受信したため、起動後に発火します")
            runtime.pending.append((now, event_data))
        if stale:
            # 発火はする（オートメーションは eventId で1回だけ動く）が、画像と最終速報は新しい報のまま
            _LOGGER.debug("古い報が後から届きました（%s）: %s", source, event_data["_message_id"])
            return
        runtime.last_eew_at = now
        runtime.last_eew = data
        async_dispatcher_send(hass, SIGNAL_UPDATE)
        # 画像はイベントを発火した後で作る（作成に失敗・遅延してもイベントには影響しない）
        _render_eew(data)

    @callback
    def _on_quake(data: dict[str, Any], source: str) -> None:
        # 地震情報は起動中に受信しても保留しない（警報ではないため）
        if hass.state is not CoreState.running:
            return
        event_data = _base_event_data(data, source, dt_util.utcnow())
        # 観測点を細分区域ごとにまとめた最大震度（オートメーションの条件に使える）
        # 対応表を読めていないときは空にする（イベントループでファイルを読まない・例外で止めない）
        scales: dict[str, int] = {}
        if runtime.assets_ok:
            try:
                scales = render.area_scales(data)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("地震情報の観測点を区域にまとめられませんでした")
        event_data["_area_scales"] = scales
        hass.bus.async_fire(EVENT_QUAKE, event_data)
        if runtime.image_enabled:
            runtime.quake_seq += 1
            entry.async_create_background_task(
                hass, _async_render_quake(runtime.quake_seq, data), f"{entry.domain}_render"
            )

    @callback
    def _render_eew(data: dict[str, Any]) -> None:
        if runtime.image_enabled:
            runtime.image_seq += 1
            entry.async_create_background_task(
                hass, _async_render(runtime.image_seq, data), f"{entry.domain}_render"
            )

    async def _async_render(seq: int, data: dict[str, Any]) -> None:
        try:
            png = await hass.async_add_executor_job(render.render, data, runtime.home_area)
        except Exception:  # noqa: BLE001 - 画像の失敗で受信を止めない
            _LOGGER.exception("警告画像の作成に失敗しました")
            return
        if seq != runtime.image_seq:
            return  # 作っている間に新しい速報が届いた。新しい方の画像を待つ
        runtime.image = png
        runtime.image_at = dt_util.utcnow()
        async_dispatcher_send(hass, SIGNAL_UPDATE)

    async def _async_render_quake(seq: int, data: dict[str, Any]) -> None:
        try:
            png = await hass.async_add_executor_job(render.render_quake, data, runtime.home_area)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("地震情報の画像の作成に失敗しました")
            return
        if seq != runtime.quake_seq:
            return
        runtime.quake_image = png
        runtime.quake_image_at = dt_util.utcnow()
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

    started = asyncio.Event()

    @callback
    def _on_started(_hass: HomeAssistant) -> None:
        started.set()
        # オートメーションのトリガー取り付けも起動完了時に行われるため、少し待ってから発火する
        entry.async_on_unload(async_call_later(hass, STARTUP_FLUSH_DELAY, _flush_pending))

    @callback
    def _on_status(_connected: bool) -> None:
        async_dispatcher_send(hass, SIGNAL_UPDATE)

    @callback
    def _on_new(_data: dict[str, Any], _source: str) -> None:
        if runtime.poller is not None:
            runtime.poller.notify_activity()

    # 重複除去の記録は再読み込み（オプション変更）をまたいで残す（同じ速報を二重に発火しない）
    domain_data = hass.data.setdefault(DOMAIN, {})
    dedup = domain_data.setdefault("dedup", Deduplicator())
    ingest = Ingest(dedup, _on_message, _on_new)

    session = async_get_clientsession(hass)
    client = P2PQuakeClient(
        session, url, lambda data: ingest.handle(data, SOURCE_WS), _on_status
    )
    image_option = bool(entry.options.get(CONF_IMAGE, DEFAULT_IMAGE))
    runtime = RuntimeData(
        client=client,
        endpoint=endpoint,
        image_option=image_option,
        image_enabled=image_option,
        home_area=str(entry.options.get(CONF_HOME_AREA) or ""),
    )
    runtime.render_eew = _render_eew
    if endpoint == ENDPOINT_PRODUCTION:
        # サンドボックスには JSON API の過去データが無いので、本番のときだけ動かす

        async def _quake_ready() -> None:
            await started.wait()

        @callback
        def _on_rest_status() -> None:
            async_dispatcher_send(hass, SIGNAL_REST)

        runtime.poller = P2PQuakePoller(
            session,
            ingest,
            setup_at=dt_util.utcnow(),
            setup_during_startup=hass.state is not CoreState.running,
            quake_ready=_quake_ready,
            on_status=_on_rest_status,
        )
    entry.runtime_data = runtime

    # 地図・対応表・フォントのファイルはイベントループの外で読んでおく
    # （地震情報のイベントに付ける区域ごとの震度にも対応表を使う）。
    # 読めなくても（ファイルの欠け・破損）画像の機能を止めるだけにし、緊急地震速報の受信は必ず続ける。
    try:
        await hass.async_add_executor_job(render.preload)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("画像用のファイルを読み込めませんでした。画像は作りません（緊急地震速報の受信は続けます）")
        runtime.image_enabled = False
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_ASSETS,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_ASSETS,
        )
    else:
        runtime.assets_ok = True
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ASSETS)

    if not image_option:
        # 画像をオフにしたら、画像とテストボタンのエンティティを登録から外す（使えないまま残さない）
        registry = er.async_get(hass)
        for platform, key in IMAGE_ENTITY_KEYS:
            entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
            if entity_id:
                registry.async_remove(entity_id)

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

    if (poller := runtime.poller) is not None:
        poller_task = entry.async_create_background_task(
            hass, poller.run(), f"{entry.domain}_rest"
        )

        @callback
        def _on_poller_done(t: asyncio.Task[None]) -> None:
            if t.cancelled() or poller.stopped:
                return
            _LOGGER.error(
                "JSON API の取得タスクが予期せず終了しました。予備の受信ができません: %r",
                t.exception(),
            )
            poller.alive = False
            async_dispatcher_send(hass, SIGNAL_REST)

        poller_task.add_done_callback(_on_poller_done)
        entry.async_on_unload(poller.stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> None:
    """オプションの変更を反映する.

    自宅の地域だけの変更なら読み込み直さない（WebSocket を切らない。切っている間の速報を落とさない）。
    """
    runtime = entry.runtime_data
    endpoint = entry.options.get(
        CONF_ENDPOINT, entry.data.get(CONF_ENDPOINT, ENDPOINT_PRODUCTION)
    )
    image_option = bool(entry.options.get(CONF_IMAGE, DEFAULT_IMAGE))
    if endpoint == runtime.endpoint and image_option == runtime.image_option:
        runtime.home_area = str(entry.options.get(CONF_HOME_AREA) or "")
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> bool:
    # 無効化・削除・再読み込みのどれでも警告を消す（再読み込みなら setup で作り直される）
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_ASSETS)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: P2PQuakeConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_ASSETS)
