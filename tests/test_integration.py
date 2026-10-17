"""HA と組み合わせた動作のテスト（pytest-homeassistant-custom-component）."""
import asyncio
import json
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.p2pquake_ws.const import (
    CONF_ENDPOINT,
    DOMAIN,
    ENDPOINT_PRODUCTION,
    ENDPOINT_SANDBOX,
    EVENT_EEW,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

EEW = {
    "_id": "6ad19c93e88ee598246bf3ed",
    "code": 556,
    "cancelled": False,
    "earthquake": {"originTime": "2026/10/16 12:36:27", "hypocenter": {"name": "千島列島"}},
    "issue": {"eventId": "20261016123810", "serial": "1"},
    "areas": [{"name": "石狩地方中部", "pref": "北海道道央", "scaleFrom": 0, "scaleTo": 30, "kindCode": "10"}],
}


async def _never(self):
    await asyncio.Event().wait()


async def test_config_flow_and_single_instance(hass: HomeAssistant):
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ENDPOINT: ENDPOINT_SANDBOX}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.ABORT


async def test_setup_fires_event_and_updates_entities(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        client = entry.runtime_data.client
        assert client._url == "wss://api.p2pquake.net/v2/ws"
        assert hass.states.get("binary_sensor.p2pquake_ws_connected").state == "off"

        client._set_connected(True)
        await hass.async_block_till_done()

        # 556 以外（ピア分布・揺れた報告）は何件届いてもエンティティを更新しない
        writes = []
        hass.bus.async_listen(
            "state_changed",
            lambda e: writes.append(e.data["entity_id"])
            if e.data["entity_id"].startswith(("sensor.p2pquake_ws", "binary_sensor.p2pquake_ws"))
            else None,
        )
        client.handle_raw(json.dumps({"_id": "p", "code": 555}))
        for i in range(20):
            client.handle_raw(json.dumps({"_id": f"u{i}", "code": 561}))
        await hass.async_block_till_done()
        assert writes == []
        assert events == []

        client.handle_raw(json.dumps(EEW))
        client.handle_raw(json.dumps(EEW))  # 重複
        await hass.async_block_till_done()

        assert len(events) == 1
        # 速報1件（重複込み）で書き込まれるのは last_eew センサーの1回だけ
        assert writes == ["sensor.p2pquake_ws_last_eew"]
        data = events[0].data
        assert data["areas"][0]["name"] == "石狩地方中部"
        assert data["_message_id"] == EEW["_id"]
        assert data["_endpoint"] == ENDPOINT_PRODUCTION
        assert "_received_at" in data

        assert hass.states.get("binary_sensor.p2pquake_ws_connected").state == "on"
        assert hass.states.get("sensor.p2pquake_ws_last_message") is None
        last_eew = hass.states.get("sensor.p2pquake_ws_last_eew")
        assert last_eew.attributes["event_id"] == "20261016123810"
        assert last_eew.attributes["origin_time"] == "2026/10/16 12:36:27"

        # オプションでサンドボックスへ切り替え → 再読み込みされ URL が変わる
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENDPOINT: ENDPOINT_SANDBOX}
        )
        await hass.async_block_till_done()
        assert client._stopped  # 古い接続は止まっている
        new_client = entry.runtime_data.client
        assert new_client is not client
        assert new_client._url.startswith("wss://api-realtime-sandbox")

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert new_client._stopped


async def test_eew_received_during_startup_is_fired_after_started(hass: HomeAssistant):
    """HA の起動完了前に受信した速報は、起動完了の後に発火し直す（オートメーションが取りこぼさない）."""
    from datetime import timedelta

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data.client.handle_raw(json.dumps(EEW))
        await hass.async_block_till_done()
        assert events == []  # 起動中は保留
        assert hass.states.get("sensor.p2pquake_ws_last_eew").attributes["event_id"] == "20261016123810"

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert events == []  # オートメーションの準備を待つため少し遅らせる
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert len(events) == 1
        assert events[0].data["_message_id"] == EEW["_id"]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sandbox_shows_repair_issue(hass: HomeAssistant):
    """サンドボックス接続中は「修理」に警告を出し、本番に戻すと消える."""
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_SANDBOX})
    entry.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        reg = ir.async_get(hass)
        assert reg.async_get_issue(DOMAIN, "sandbox_endpoint") is not None

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENDPOINT: ENDPOINT_PRODUCTION}
        )
        await hass.async_block_till_done()
        assert reg.async_get_issue(DOMAIN, "sandbox_endpoint") is None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_last_eew_restored_after_restart(hass: HomeAssistant):
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    mock_restore_cache(hass, [State(
        "sensor.p2pquake_ws_last_eew", "2026-10-16T03:40:10+00:00",
        {"event_id": "20261016123810", "origin_time": "2026/10/16 12:36:27"},
    )])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        st = hass.states.get("sensor.p2pquake_ws_last_eew")
        assert st.state == "2026-10-16T03:40:10+00:00"
        assert st.attributes["event_id"] == "20261016123810"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cancel_message_fires_event_and_sensor_handles_nulls(hass: HomeAssistant):
    """取消報（earthquake・areas が null）もそのまま発火し、センサーが例外を出さない."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    events = async_capture_events(hass, EVENT_EEW)
    cancel = {
        "_id": "cancel1", "code": 556, "cancelled": True, "earthquake": None, "areas": None,
        "issue": {"eventId": "20261016123810", "serial": "2"},
    }
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data.client.handle_raw(json.dumps(cancel))
        # 想定外の型が来てもセンサーの更新が失敗しない
        entry.runtime_data.client.handle_raw(json.dumps(
            {"_id": "odd", "code": 556, "issue": "x", "earthquake": [1]}
        ))
        await hass.async_block_till_done()
        assert [e.data["_message_id"] for e in events] == ["cancel1", "odd"]
        assert events[0].data["cancelled"] is True
        st = hass.states.get("sensor.p2pquake_ws_last_eew")
        assert st.attributes["event_id"] is None and st.attributes["hypocenter"] is None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_pending_is_discarded_and_unload_cancels_flush(hass: HomeAssistant, freezer):
    """起動中に保留した速報は3分を超えたら捨てる。保留中に無効化したら発火しない."""
    from datetime import timedelta

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data.client.handle_raw(json.dumps(EEW))
        freezer.tick(timedelta(seconds=200))  # 起動に時間がかかった
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert events == []  # 古すぎるので捨てた
        assert await hass.config_entries.async_unload(entry.entry_id)

    hass.set_state(CoreState.starting)
    entry2 = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry2.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry2.entry_id)
        await hass.async_block_till_done()
        entry2.runtime_data.client.handle_raw(json.dumps({**EEW, "_id": "x2"}))
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry2.entry_id)  # 保留中に無効化
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert events == []


async def test_unload_cancels_task_and_clears_sandbox_issue(hass: HomeAssistant):
    """無効化すると受信タスクが止まり、サンドボックスの警告も消える."""
    from homeassistant.helpers import issue_registry as ir

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _run(self):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_SANDBOX})
    entry.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _run):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.wait_for(started.wait(), 1)
        reg = ir.async_get(hass)
        assert reg.async_get_issue(DOMAIN, "sandbox_endpoint") is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert reg.async_get_issue(DOMAIN, "sandbox_endpoint") is None


async def test_restore_invalid_state_and_options_default(hass: HomeAssistant):
    """復元値が日時でなくても壊れない。オプション画面の初期値は今の接続先."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    mock_restore_cache(hass, [State("sensor.p2pquake_ws_last_eew", "unknown", {})])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_SANDBOX})
    entry.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.p2pquake_ws_last_eew").state == "unknown"
        result = await hass.config_entries.options.async_init(entry.entry_id)
        schema = result["data_schema"].schema
        key = next(k for k in schema if k == CONF_ENDPOINT)
        assert key.default() == ENDPOINT_SANDBOX
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unexpected_task_exit_marks_disconnected(hass: HomeAssistant, caplog):
    """受信タスクが予期せず終了したら ERROR を記録し、接続状態を off にする."""

    async def _crash(self):
        self._set_connected(True)
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _crash):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.p2pquake_ws_connected").state == "off"
    assert "受信タスクが予期せず終了しました" in caplog.text
