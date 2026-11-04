"""client.py の単体テスト（HA 不要）。ローカルの WebSocket サーバーで検証する."""
import asyncio
import importlib.util
import json
import logging
import pathlib

import aiohttp
import pytest
from aiohttp import web

SPEC = importlib.util.spec_from_file_location(
    "p2p_client",
    pathlib.Path(__file__).parents[1] / "custom_components/p2pquake_ws/client.py",
)
client_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_mod)

EEW = {"_id": "abc", "code": 556, "cancelled": False, "areas": [{"name": "石狩地方中部"}]}


async def start_server(handler):
    app = web.Application()
    app.router.add_get("/ws", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/ws"


async def run_for(client, seconds):
    task = asyncio.create_task(client.run())
    await asyncio.sleep(seconds)
    client.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def quick_sleep(_):
    await asyncio.sleep(0.01)


def test_message_id_variants():
    assert client_mod.message_id({"id": "x"}) == "x"
    assert client_mod.message_id({"_id": "y"}) == "y"
    assert client_mod.message_id({"_id": {"$oid": "z"}}) == "z"
    assert client_mod.message_id({}) is None


def test_dedup_evicts_oldest():
    d = client_mod.Deduplicator(size=2)
    d.add("a")
    d.add("b")
    assert d.seen("a")
    d.add("c")  # b が押し出される
    assert not d.seen("b")
    assert not d.seen(None)


async def test_dedup_reconnect_and_bad_frames():
    frames = [
        [json.dumps(EEW), json.dumps(EEW), "not json", json.dumps([1, 2]),
         json.dumps({"_id": "p1", "code": 555})],
        [json.dumps(EEW), json.dumps({**EEW, "_id": "def"})],  # 再接続後の再配信と新規
    ]
    conns = {"n": 0}

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        idx = min(conns["n"], len(frames) - 1)
        conns["n"] += 1
        for f in frames[idx]:
            await ws.send_str(f)
        await asyncio.sleep(0.05)
        await ws.close()
        return ws

    runner, url = await start_server(handler)
    received, statuses = [], []
    async with aiohttp.ClientSession() as session:
        c = client_mod.P2PQuakeClient(
            session, url, received.append, statuses.append, sleep=quick_sleep
        )
        await run_for(c, 0.6)
    await runner.cleanup()

    assert [m.get("_id") for m in received] == ["abc", "p1", "def"]
    assert c.connect_count >= 2
    assert True in statuses and False in statuses
    assert c.connected is False


async def test_callback_error_does_not_drop_connection_and_is_retried():
    """callback が失敗した情報は「処理済み」にせず、再配信されたら再び処理する."""
    sent = {"n": 0}

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sent["n"] += 1
        await ws.send_str(json.dumps({"_id": "e1", "code": 556}))
        await ws.send_str(json.dumps({"_id": "e2", "code": 556}))
        await ws.send_str(json.dumps({"_id": "e1", "code": 556}))  # 再配信
        await asyncio.sleep(5)
        return ws

    runner, url = await start_server(handler)
    got = []

    def cb(m):
        got.append(m["_id"])
        if m["_id"] == "e1" and got.count("e1") == 1:
            raise RuntimeError("boom")

    async with aiohttp.ClientSession() as session:
        c = client_mod.P2PQuakeClient(session, url, cb, sleep=quick_sleep)
        await run_for(c, 0.4)
    await runner.cleanup()
    assert got == ["e1", "e2", "e1"]
    assert c.connect_count == 1  # 例外で接続は切れない


async def test_stall_detected_even_when_server_answers_pings(caplog):
    """サーバーが ping には応答するのにデータを送らない状態を検知して接続し直す.

    aiohttp の receive(timeout) は pong を受けるたびに数え直すため、
    heartbeat を stall_timeout より短くした本番相当の条件で確認する。
    """
    conns = {"n": 0}

    async def handler(request):
        ws = web.WebSocketResponse()  # aiohttp サーバーは ping に自動で pong を返す
        await ws.prepare(request)
        conns["n"] += 1
        if conns["n"] >= 2:
            await ws.send_str(json.dumps({"_id": "after", "code": 555}))
        async for _ in ws:
            pass
        return ws

    runner, url = await start_server(handler)
    got = []
    caplog.set_level(logging.DEBUG, logger="p2p_client")
    async with aiohttp.ClientSession() as session:
        c = client_mod.P2PQuakeClient(
            session, url, got.append, sleep=quick_sleep,
            heartbeat=0.1, stall_timeout=0.6,
        )
        await run_for(c, 1.5)
    await runner.cleanup()
    assert c.stall_count >= 1
    assert c.connect_count >= 2
    assert got and got[0]["_id"] == "after"
    # 無受信1回につき WARNING は1件だけ（続く「切断されました」は DEBUG）
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == c.stall_count


async def test_immediate_close_backs_off_and_logs_once(caplog):
    """接続直後に毎回切られる場合、待ち時間を伸ばし、WARNING は最初の1回だけにする."""

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    runner, url = await start_server(handler)
    waits, statuses = [], []

    async def record_sleep(s):
        waits.append(s)
        await asyncio.sleep(0)

    caplog.set_level(logging.DEBUG, logger="p2p_client")
    async with aiohttp.ClientSession() as session:
        c = client_mod.P2PQuakeClient(
            session, url, lambda m: None, statuses.append, sleep=record_sleep,
            backoff_initial=1, backoff_max=8,
        )
        task = asyncio.create_task(c.run())
        for _ in range(500):
            await asyncio.sleep(0.01)
            if len(waits) >= 6:
                break
        c.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await runner.cleanup()

    assert len(waits) >= 6
    for got, expected in zip(waits, [1, 2, 4, 8, 8, 8]):
        assert expected * 0.8 <= got <= expected * 1.2
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1


async def test_backoff_resets_after_data_received():
    """データを受け取れた接続のあとは、待ち時間を初期値に戻す."""
    conns = {"n": 0}

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        conns["n"] += 1
        if conns["n"] == 3:
            await ws.send_str(json.dumps({"_id": "ok", "code": 555}))
        await ws.close()
        return ws

    runner, url = await start_server(handler)
    waits = []

    async def record_sleep(s):
        waits.append(s)
        await asyncio.sleep(0)

    async with aiohttp.ClientSession() as session:
        c = client_mod.P2PQuakeClient(
            session, url, lambda m: None, sleep=record_sleep, backoff_initial=1, backoff_max=60,
        )
        task = asyncio.create_task(c.run())
        for _ in range(500):
            await asyncio.sleep(0.01)
            if len(waits) >= 4:
                break
        c.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await runner.cleanup()
    # 1回目(1s) 2回目(2s) 3回目はデータあり→初期値(1s)に戻る 4回目(2s)
    for got, expected in zip(waits, [1, 2, 1, 2]):
        assert expected * 0.8 <= got <= expected * 1.2


async def test_status_callback_error_does_not_kill_loop():
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    runner, url = await start_server(handler)

    def bad_status(_):
        raise RuntimeError("boom")

    async with aiohttp.ClientSession() as session:
        c = client_mod.P2PQuakeClient(
            session, url, lambda m: None, bad_status, sleep=quick_sleep,
            backoff_initial=0.01, backoff_max=0.01,
        )
        await run_for(c, 0.3)
    await runner.cleanup()
    assert c.connect_count >= 3  # 例外が出ても再接続を続けている


async def test_handshake_rejection_backoff_grows_and_caps():
    waits = []

    async def record_sleep(s):
        waits.append(s)
        if len(waits) >= 8:
            c.stop()
        await asyncio.sleep(0)

    async with aiohttp.ClientSession() as session:
        c = client_mod.P2PQuakeClient(
            session, "http://127.0.0.1:1/ws", lambda m: None, sleep=record_sleep,
            backoff_initial=1, backoff_max=8,
        )
        await asyncio.wait_for(c.run(), 10)
    for got, expected in zip(waits, [1, 2, 4, 8, 8]):
        assert expected * 0.8 <= got <= expected * 1.2
