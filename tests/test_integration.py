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

from custom_components.p2pquake_ws.poller import P2PQuakePoller

_REAL_POLLER_RUN = P2PQuakePoller.run  # no_rest で差し替える前の本物

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "no_rest")

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


@pytest.fixture
def no_rest():
    """JSON API の取得（予備の受信）は外へ出さない。必要なテストは process() を直接呼ぶ."""
    with patch("custom_components.p2pquake_ws.poller.P2PQuakePoller.run", _never):
        yield


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


async def _wait_render(hass: HomeAssistant) -> None:
    """画像作成のバックグラウンド処理だけ待つ（受信ループは終わらないので全体は待てない）."""
    import asyncio

    for _ in range(200):
        tasks = [t for t in hass._background_tasks if getattr(t, "get_name", lambda: "")().endswith("_render") and not t.done()]
        if not tasks:
            break
        await asyncio.wait(tasks, timeout=0.1)
    await hass.async_block_till_done()


async def test_image_is_made_after_the_event(hass: HomeAssistant, hass_client):
    """速報を受信するとイベントの後で画像を作り、画像エンティティとして取得できる."""
    from custom_components.p2pquake_ws.const import CONF_HOME_AREA, CONF_IMAGE

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENDPOINT: ENDPOINT_PRODUCTION},
        options={CONF_ENDPOINT: ENDPOINT_PRODUCTION, CONF_IMAGE: True, CONF_HOME_AREA: "神奈川県東部"},
    )
    entry.add_to_hass(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        st = hass.states.get("image.p2pquake_ws_eew_map")
        assert st is not None and st.state == "unknown"

        eew = {**EEW, "areas": EEW["areas"] + [
            {"name": "神奈川県東部", "pref": "東京", "scaleFrom": 0, "scaleTo": 40, "kindCode": "10",
             "arrivalTime": "2026/10/16 12:43:02"}]}
        entry.runtime_data.client.handle_raw(json.dumps(eew))
        assert len(events) == 1  # 画像を待たずにイベントは発火済み
        await _wait_render(hass)
        st = hass.states.get("image.p2pquake_ws_eew_map")
        assert st.state not in ("unknown", "unavailable")

        client = await hass_client()
        resp = await client.get(st.attributes["entity_picture"])
        assert resp.status == 200
        body = await resp.read()
        assert body[:8] == b"\x89PNG\r\n\x1a\n"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_image_failure_does_not_block_the_event(hass: HomeAssistant, caplog):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never), patch(
        "custom_components.p2pquake_ws.render.render", side_effect=RuntimeError("boom")
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data.client.handle_raw(json.dumps(EEW))
        await _wait_render(hass)
        assert len(events) == 1
        assert hass.states.get("image.p2pquake_ws_eew_map").state == "unknown"
        assert "警告画像の作成に失敗しました" in caplog.text
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_image_can_be_turned_off_and_home_area_set(hass: HomeAssistant):
    from custom_components.p2pquake_ws.const import CONF_HOME_AREA, CONF_IMAGE

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION})
    entry.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("image.p2pquake_ws_eew_map") is not None  # 既定では作る

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: ENDPOINT_PRODUCTION, CONF_IMAGE: True, CONF_HOME_AREA: "神奈川県東部"},
        )
        await hass.async_block_till_done()
        assert entry.options[CONF_HOME_AREA] == "神奈川県東部"
        assert entry.runtime_data.home_area == "神奈川県東部"

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENDPOINT: ENDPOINT_PRODUCTION, CONF_IMAGE: False},
        )
        await hass.async_block_till_done()
        assert entry.options[CONF_HOME_AREA] == ""
        for entity_id in ("image.p2pquake_ws_eew_map", "image.p2pquake_ws_quake_map", "button.p2pquake_ws_test_image"):
            st = hass.states.get(entity_id)
            assert st is None or st.state == "unavailable"
        entry.runtime_data.client.handle_raw(json.dumps(EEW))
        entry.runtime_data.client.handle_raw(json.dumps(QUAKE))
        await _wait_render(hass)
        assert entry.runtime_data.image_seq == 0 and entry.runtime_data.quake_seq == 0
        assert await hass.config_entries.async_unload(entry.entry_id)


QUAKE = {
    "code": 551,
    "_id": "q1",
    "issue": {"source": "気象庁", "time": "2026/10/29 00:16:00", "type": "DetailScale", "correct": "None"},
    "earthquake": {
        "time": "2026/10/29 00:07:00", "maxScale": 45, "domesticTsunami": "None",
        "hypocenter": {"name": "千葉県北東部", "latitude": 35.7, "longitude": 140.7, "depth": 50, "magnitude": 5.4},
    },
    "points": [
        {"pref": "千葉県", "addr": "旭市南堀之内", "isArea": False, "scale": 45},
        {"pref": "東京都", "addr": "横浜中区山手町", "isArea": False, "scale": 20},
    ],
}


async def test_quake_info_fires_event_and_makes_image(hass: HomeAssistant, hass_client):
    from custom_components.p2pquake_ws.const import CONF_HOME_AREA, CONF_IMAGE, EVENT_QUAKE

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENDPOINT: ENDPOINT_PRODUCTION},
        options={CONF_ENDPOINT: ENDPOINT_PRODUCTION, CONF_IMAGE: True, CONF_HOME_AREA: "神奈川県東部"},
    )
    entry.add_to_hass(hass)
    quakes = async_capture_events(hass, EVENT_QUAKE)
    eews = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        before = hass.states.get("sensor.p2pquake_ws_last_eew").state
        entry.runtime_data.client.handle_raw(json.dumps(QUAKE))
        await _wait_render(hass)
        assert len(quakes) == 1 and not eews
        data = quakes[0].data
        assert data["_area_scales"] == {"千葉県北東部": 45, "神奈川県東部": 20}
        assert data["_endpoint"] == ENDPOINT_PRODUCTION and data["_message_id"] == "q1"
        # 地震情報では緊急地震速報のセンサーと画像は変わらない
        assert hass.states.get("sensor.p2pquake_ws_last_eew").state == before
        assert hass.states.get("image.p2pquake_ws_eew_map").state == "unknown"
        st = hass.states.get("image.p2pquake_ws_quake_map")
        assert st.state not in ("unknown", "unavailable")
        client = await hass_client()
        resp = await client.get(st.attributes["entity_picture"])
        assert resp.status == 200 and (await resp.read())[:4] == b"\x89PNG"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_test_button_makes_image_without_event(hass: HomeAssistant):
    from custom_components.p2pquake_ws.const import CONF_HOME_AREA, CONF_IMAGE, EVENT_QUAKE

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ENDPOINT: ENDPOINT_PRODUCTION},
        options={CONF_ENDPOINT: ENDPOINT_PRODUCTION, CONF_IMAGE: True, CONF_HOME_AREA: "神奈川県東部"},
    )
    entry.add_to_hass(hass)
    eews = async_capture_events(hass, EVENT_EEW)
    quakes = async_capture_events(hass, EVENT_QUAKE)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.p2pquake_ws_test_image"}, blocking=True
        )
        await _wait_render(hass)
        assert not eews and not quakes
        assert hass.states.get("image.p2pquake_ws_eew_map").state not in ("unknown", "unavailable")
        assert hass.states.get("sensor.p2pquake_ws_last_eew").state == "unknown"
        assert await hass.config_entries.async_unload(entry.entry_id)


def _entry(hass, **options):
    from custom_components.p2pquake_ws.const import CONF_HOME_AREA, CONF_IMAGE

    opts = {CONF_ENDPOINT: ENDPOINT_PRODUCTION, CONF_IMAGE: True, CONF_HOME_AREA: "神奈川県東部"}
    opts.update(options)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_PRODUCTION}, options=opts)
    entry.add_to_hass(hass)
    return entry


async def test_asset_failure_keeps_eew_working(hass: HomeAssistant):
    """地図などのファイルを読めなくても、統合は起動し緊急地震速報を発火する（いちばん大事な性質）."""
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never), patch(
        "custom_components.p2pquake_ws.render.preload", side_effect=OSError("broken")
    ), patch("custom_components.p2pquake_ws.render.area_names", side_effect=OSError("broken")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert ir.async_get(hass).async_get_issue(DOMAIN, "image_assets") is not None
        assert hass.states.get("image.p2pquake_ws_eew_map") is None

        entry.runtime_data.client.handle_raw(json.dumps(EEW))
        entry.runtime_data.client.handle_raw(json.dumps(QUAKE))
        await hass.async_block_till_done()
        assert len(events) == 1
        assert entry.runtime_data.image_seq == 0  # 画像は作らない

        # 設定画面は開ける（接続先を戻せる）。自宅の地域の欄は出さず、今の値を残す
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert "home_area" not in {str(k) for k in result["data_schema"].schema}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENDPOINT: ENDPOINT_PRODUCTION, "image": True}
        )
        assert entry.options["home_area"] == "神奈川県東部"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_newest_image_wins(hass: HomeAssistant):
    import time as _time

    def fake_render(data, home):
        if data["issue"]["serial"] == "1":
            _time.sleep(0.3)  # 第1報の画像の方が遅くできる
        return data["issue"]["serial"].encode()

    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never), patch(
        "custom_components.p2pquake_ws.render.render", side_effect=fake_render
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        second = {**EEW, "_id": "x2", "issue": {**EEW["issue"], "serial": "2"}}
        entry.runtime_data.client.handle_raw(json.dumps(EEW))
        entry.runtime_data.client.handle_raw(json.dumps(second))
        await _wait_render(hass)
        await asyncio.sleep(0.4)
        await _wait_render(hass)
        assert entry.runtime_data.image == b"2"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_quake_during_startup_is_not_fired(hass: HomeAssistant):
    from homeassistant.core import CoreState
    from custom_components.p2pquake_ws.const import EVENT_QUAKE

    entry = _entry(hass)
    quakes = async_capture_events(hass, EVENT_QUAKE)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hass.set_state(CoreState.starting)
        try:
            entry.runtime_data.client.handle_raw(json.dumps(QUAKE))
            await hass.async_block_till_done()
        finally:
            hass.set_state(CoreState.running)
        assert not quakes
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_test_button_keeps_real_eew_image(hass: HomeAssistant):
    from homeassistant.exceptions import HomeAssistantError

    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data.client.handle_raw(json.dumps(EEW))
        await _wait_render(hass)
        real = entry.runtime_data.image
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "button", "press", {"entity_id": "button.p2pquake_ws_test_image"}, blocking=True
            )
        await _wait_render(hass)
        assert entry.runtime_data.image == real
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_home_area_change_does_not_reconnect(hass: HomeAssistant):
    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENDPOINT: ENDPOINT_PRODUCTION, "image": True, "home_area": "東京都２３区"}
        )
        await hass.async_block_till_done()
        assert entry.runtime_data is runtime  # 読み込み直していない（WebSocket を切っていない）
        assert runtime.home_area == "東京都２３区"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_turning_image_off_removes_its_entities(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er

    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        assert registry.async_get("button.p2pquake_ws_test_image") is not None
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENDPOINT: ENDPOINT_PRODUCTION, "image": False}
        )
        await hass.async_block_till_done()
        for entity_id in ("image.p2pquake_ws_eew_map", "image.p2pquake_ws_quake_map", "button.p2pquake_ws_test_image"):
            assert registry.async_get(entity_id) is None
            assert hass.states.get(entity_id) is None
        assert registry.async_get("binary_sensor.p2pquake_ws_connected") is not None
        assert await hass.config_entries.async_unload(entry.entry_id)


# --- JSON API（予備の受信）---------------------------------------------------


def _p2p_now(seconds_ago: float = 0.0) -> str:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.p2pquake_ws.poller import JST

    t = dt_util.utcnow() - timedelta(seconds=seconds_ago)
    return t.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]


def _rest_eew(id_: str = "6ad19c93e88ee598246bf3ed", serial: str = "1", ago: float = 2.0) -> dict:
    """JSON API の形（識別子は id、_id は無い）."""
    data = {k: v for k, v in EEW.items() if k != "_id"}
    return {**data, "id": id_, "time": _p2p_now(ago), "issue": {**EEW["issue"], "serial": serial}}


async def test_rest_eew_fires_once_with_source_and_image(hass: HomeAssistant):
    """REST で先に届いた速報も WS と同じイベント・画像になり、後から WS で届いた同じ速報は捨てる."""
    from homeassistant.util import dt as dt_util

    entry = _entry(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        assert runtime.poller is not None
        # setup より後の速報として扱う
        runtime.poller._setup_at = dt_util.utcnow() - __import__("datetime").timedelta(minutes=1)

        item = _rest_eew()
        runtime.poller.process(556, [item])
        await _wait_render(hass)
        assert len(events) == 1
        data = events[0].data
        assert data["_source"] == "rest"
        assert data["_message_id"] == item["id"] and data["_id"] == item["id"]
        assert 0 <= data["_p2p_delay"] < 10
        assert data["areas"][0]["name"] == "石狩地方中部"
        assert hass.states.get("image.p2pquake_ws_eew_map").state not in ("unknown", "unavailable")
        assert hass.states.get("sensor.p2pquake_ws_last_eew").attributes["event_id"] == "20261016123810"

        # 同じ速報が WS から遅れて届く・REST で何度も返る → 発火しない
        runtime.client.handle_raw(json.dumps({**item, "_id": item["id"]}))
        runtime.poller.process(556, [item])
        await hass.async_block_till_done()
        assert len(events) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_ws_first_then_rest_is_ignored_and_old_serial_keeps_image(hass: HomeAssistant):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    entry = _entry(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        runtime.poller._setup_at = dt_util.utcnow() - timedelta(minutes=1)

        # WS で第2報 → REST で同じ第2報（捨てる）と、遅れていた第1報（発火はするが画像は第2報のまま）
        second = _rest_eew("id2", serial="2")
        first = _rest_eew("id1", serial="1", ago=5)
        runtime.client.handle_raw(json.dumps({**second, "_id": "id2"}))
        await _wait_render(hass)
        image_at = runtime.image_at
        runtime.poller.process(556, [second, first])
        await _wait_render(hass)
        assert [e.data["_message_id"] for e in events] == ["id2", "id1"]
        assert [e.data["_source"] for e in events] == ["ws", "rest"]
        assert runtime.last_eew["issue"]["serial"] == "2"
        assert runtime.image_at == image_at
        assert hass.states.get("sensor.p2pquake_ws_last_eew").attributes["serial"] == "2"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_rest_quake_has_area_scales(hass: HomeAssistant):
    """実際の API 応答（茨城県沖、東京23区で震度2）で、REST 経路でも _area_scales が付く."""
    import pathlib
    from datetime import timedelta

    from custom_components.p2pquake_ws.const import EVENT_QUAKE
    from custom_components.p2pquake_ws.poller import parse_time

    fixture = json.loads(
        (pathlib.Path(__file__).parent / "fixtures/history_551_ibaraki.json").read_text("utf-8")
    )
    entry = _entry(hass)
    quakes = async_capture_events(hass, EVENT_QUAKE)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        poller = entry.runtime_data.poller
        newest = parse_time(fixture[0]["time"])
        poller._setup_at = newest - timedelta(hours=1)
        poller._now = lambda: newest + timedelta(seconds=30)
        poller.process(551, fixture)
        await _wait_render(hass)
        assert [q.data["issue"]["type"] for q in quakes] == ["ScalePrompt", "Destination", "DetailScale"]
        detail = quakes[-1].data
        assert detail["_source"] == "rest"
        assert detail["_area_scales"]["東京都２３区"] == 20
        assert detail["_area_scales"]["東京都多摩東部"] == 10
        assert hass.states.get("image.p2pquake_ws_quake_map").state not in ("unknown", "unavailable")
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_keeps_dedup(hass: HomeAssistant):
    """オプション変更で読み込み直しても、同じ速報を二重に発火しない."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    entry = _entry(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        item = _rest_eew()
        entry.runtime_data.client.handle_raw(json.dumps({**item, "_id": item["id"]}))
        await hass.async_block_till_done()
        assert len(events) == 1

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        poller = entry.runtime_data.poller
        poller._setup_at = dt_util.utcnow() - timedelta(minutes=1)
        poller.process(556, [item])
        entry.runtime_data.client.handle_raw(json.dumps({**item, "_id": item["id"]}))
        await hass.async_block_till_done()
        assert len(events) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_startup_rest_eew_is_fired_after_started(hass: HomeAssistant):
    """HA の停止中・起動中に出た速報（180 秒以内）を、起動時の REST 取得で拾い、起動後に発火する."""
    from datetime import timedelta

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hass.set_state(CoreState.starting)
    entry = _entry(hass)
    events = async_capture_events(hass, EVENT_EEW)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        poller = entry.runtime_data.poller
        assert poller._setup_during_startup
        poller.process(556, [_rest_eew("recent", ago=60), _rest_eew("old", ago=400)])
        await hass.async_block_till_done()
        assert events == []

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert [e.data["_message_id"] for e in events] == ["recent"]
        assert events[0].data["_source"] == "rest"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_rest_health_sensor(hass: HomeAssistant):
    """取得に成功してから 60 秒以内なら on。取得が止まれば 30 秒ごとの見直しで off になる."""
    from datetime import timedelta

    from homeassistant.helpers.dispatcher import async_dispatcher_send
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.p2pquake_ws.const import SIGNAL_REST

    clock = {"t": 5000.0}
    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never), patch(
        "custom_components.p2pquake_ws.binary_sensor.time.monotonic", lambda: clock["t"]
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.p2pquake_ws_rest").state == "off"  # まだ成功していない

        writes = []
        hass.bus.async_listen(
            "state_changed",
            lambda e: writes.append(1) if e.data["entity_id"] == "binary_sensor.p2pquake_ws_rest" else None,
        )
        poller = entry.runtime_data.poller
        poller.last_success = clock["t"]
        async_dispatcher_send(hass, SIGNAL_REST)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.p2pquake_ws_rest").state == "on"

        # 成功が続く間（状態が変わらない間）は書き込まない
        for _ in range(5):
            clock["t"] += 10
            poller.last_success = clock["t"]
            async_dispatcher_send(hass, SIGNAL_REST)
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
            await hass.async_block_till_done()
        assert len(writes) == 1

        # 取得が止まる（シグナルは来ない）→ タイマーの見直しで off
        clock["t"] += 61
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=200))
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.p2pquake_ws_rest").state == "off"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_poller_task_crash_turns_health_off(hass: HomeAssistant, caplog):
    async def _crash(self):
        raise RuntimeError("boom")

    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never), patch(
        "custom_components.p2pquake_ws.poller.P2PQuakePoller.run", _crash
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert entry.runtime_data.poller.alive is False
        assert "JSON API の取得タスクが予期せず終了しました" in caplog.text
        assert hass.states.get("binary_sensor.p2pquake_ws_rest").state == "off"
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sandbox_has_no_rest(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENDPOINT: ENDPOINT_SANDBOX})
    entry.add_to_hass(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.poller is None
        assert hass.states.get("binary_sensor.p2pquake_ws_rest") is None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_stops_poller(hass: HomeAssistant):
    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        poller = entry.runtime_data.poller
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert poller.stopped


async def test_real_poller_loop_wiring(hass: HomeAssistant):
    """実際の取得ループを動かす: 556 はすぐ、551 は HA の起動完了の後に取り始め、REST で届いた速報を発火する."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    hass.set_state(CoreState.starting)
    entry = _entry(hass)
    events = async_capture_events(hass, EVENT_EEW)
    fetched: list[int] = []
    item = _rest_eew("fresh", ago=1)

    async def fake_fetch(self, code, limit):
        fetched.append(code)
        return [item] if code == 556 else []

    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never), patch(
        "custom_components.p2pquake_ws.poller.P2PQuakePoller.run", _REAL_POLLER_RUN
    ), patch("custom_components.p2pquake_ws.poller.P2PQuakePoller.fetch", fake_fetch):
        # 各ループの1回目の取得はすぐ行われる（次の取得までの10秒・60秒はこのテストでは待たない）
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.sleep(0.05)
        assert 556 in fetched and 551 not in fetched  # 起動中は 551 を取らない

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await asyncio.sleep(0.05)
        assert 551 in fetched
        # 起動中に REST で受けた速報（setup 後のもの）は起動完了の10秒後に発火する
        from datetime import timedelta

        from homeassistant.util import dt as dt_util
        from pytest_homeassistant_custom_component.common import async_fire_time_changed

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done(wait_background_tasks=False)
        assert [e.data["_message_id"] for e in events] == ["fresh"]
        assert entry.runtime_data.poller.last_success is not None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_does_not_fire_items_skipped_at_startup(hass: HomeAssistant):
    """起動時に見送った古い速報（4分前）と地震情報が、5分後の設定変更（読み込み直し）で発火しない.

    （再レビューで見つかった誤報の経路。見送りの記録を読み込み直しで引き継ぐ）
    """
    from datetime import timedelta

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from homeassistant.util import dt as dt_util

    from custom_components.p2pquake_ws.const import EVENT_QUAKE

    hass.set_state(CoreState.starting)
    entry = _entry(hass)
    eews = async_capture_events(hass, EVENT_EEW)
    quakes = async_capture_events(hass, EVENT_QUAKE)
    old_eew = _rest_eew("e_pre", ago=240)
    old_quake = {"id": "q_pre", "code": 551, "time": _p2p_now(300), "issue": {"type": "DetailScale"},
                 "earthquake": {"maxScale": 10}, "points": []}
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        poller = entry.runtime_data.poller
        poller.process(556, [old_eew])
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        poller.process(551, [old_quake])
        assert eews == [] and quakes == []

        # 5分後に読み込み直し（P2P の時刻も5分進める）
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        poller2 = entry.runtime_data.poller
        assert poller2 is not poller and poller2._resumed_since is not None
        later = dt_util.utcnow() + timedelta(minutes=5)
        poller2._now = lambda: later
        poller2.process(556, [old_eew])
        poller2.process(551, [old_quake])
        await hass.async_block_till_done()
        assert eews == [] and quakes == []

        # 隙間に出た速報は拾う
        gap = _rest_eew("e_gap", ago=0)
        poller2._now = dt_util.utcnow
        poller2.process(556, [gap])
        await hass.async_block_till_done()
        assert [e.data["_message_id"] for e in eews] == ["e_gap"]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_late_reenable_is_not_treated_as_reload(hass: HomeAssistant, freezer):
    """無効化して何時間も後に有効化した場合は「読み込み直し」とみなさない."""
    from datetime import timedelta

    entry = _entry(hass)
    with patch("custom_components.p2pquake_ws.client.P2PQuakeClient.run", _never):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.poller._resumed_since is None  # 初回
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        freezer.tick(timedelta(hours=3))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.poller._resumed_since is None
        assert await hass.config_entries.async_unload(entry.entry_id)
