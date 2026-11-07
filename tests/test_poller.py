"""poller.py・ingest.py の単体テスト（ネットワークに出ない。偽のセッションを使う）."""
import asyncio
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from custom_components.p2pquake_ws import poller as poller_mod
from custom_components.p2pquake_ws.client import Deduplicator
from custom_components.p2pquake_ws.ingest import Ingest
from custom_components.p2pquake_ws.poller import (
    JST,
    FetchError,
    P2PQuakePoller,
    parse_time,
)

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures/history_551_ibaraki.json").read_text("utf-8")
)
SETUP = datetime(2026, 11, 7, 20, 0, 0, tzinfo=JST)


def p2p_time(dt: datetime) -> str:
    return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S.%f")[:-3]


def eew(id_: str, when: datetime, serial: str = "1") -> dict:
    return {
        "id": id_,
        "code": 556,
        "time": p2p_time(when),
        "issue": {"eventId": "20261107200000", "serial": serial},
        "areas": [{"name": "東京都多摩東部"}],
    }


def quake(id_: str, when: datetime) -> dict:
    return {"id": id_, "code": 551, "time": p2p_time(when), "issue": {"type": "DetailScale"}}


class Recorder:
    def __init__(self):
        self.got: list[tuple[str, str]] = []
        self.raise_on: set[str] = set()

    def __call__(self, data, source):
        self.got.append((data.get("_id") or data.get("id"), source))
        if (data.get("_id") or data.get("id")) in self.raise_on:
            raise RuntimeError("boom")


def make(now: datetime, *, startup=False, recorder=None, session=None, **kw):
    rec = recorder or Recorder()
    ingest = Ingest(Deduplicator(), rec)
    p = P2PQuakePoller(
        session,
        ingest,
        setup_at=SETUP,
        setup_during_startup=startup,
        now=lambda: now,
        **kw,
    )
    return p, rec, ingest


# --- 時刻 ---------------------------------------------------------------


def test_parse_time_formats():
    assert parse_time("2026/11/07 20:12:22.977") == datetime(
        2026, 11, 7, 11, 12, 22, 977000, tzinfo=timezone.utc
    )
    assert parse_time("2026/11/07 20:12:22") == datetime(2026, 11, 7, 20, 12, 22, tzinfo=JST)
    assert parse_time("2026/11/07 03:57:08.21") is not None  # ミリ秒が2桁
    assert parse_time("") is None
    assert parse_time(None) is None
    assert parse_time("2026-11-07T20:12:22") is None


# --- Ingest -------------------------------------------------------------


def test_ingest_dedups_only_551_556_so_floods_do_not_evict():
    """強い地震で 561 が大量に届いても、556 の記録は押し出されない."""
    rec = Recorder()
    ing = Ingest(Deduplicator(size=1000), rec)
    assert ing.handle({"_id": "e1", "code": 556}, "ws")
    for i in range(1500):
        assert not ing.handle({"_id": f"u{i}", "code": 561}, "ws")
    assert not ing.handle({"id": "e1", "code": 556}, "rest")  # REST の id は WS の _id と同じ値
    assert rec.got == [("e1", "ws")]


def test_ingest_records_before_callback_so_errors_do_not_refire(caplog):
    rec = Recorder()
    rec.raise_on = {"e1"}
    ing = Ingest(Deduplicator(), rec)
    assert ing.handle({"id": "e1", "code": 556}, "rest")
    assert "情報の処理に失敗" in caplog.text
    # REST は同じ要素を返し続けるが、2回目以降は渡さない
    for _ in range(5):
        assert not ing.handle({"id": "e1", "code": 556}, "rest")
    assert rec.got == [("e1", "rest")]


def test_ingest_rest_without_id_is_dropped_ws_without_id_passes():
    rec = Recorder()
    ing = Ingest(Deduplicator(), rec)
    assert not ing.handle({"code": 556}, "rest")
    assert ing.handle({"code": 556}, "ws")
    assert ing.handle({"code": 556}, "ws")  # 識別子が無いので除けない（従来どおり）
    assert len(rec.got) == 2
    assert not ing.handle([1, 2], "ws")
    assert not ing.handle({"id": "x", "code": 555}, "ws")


def test_ingest_on_new_error_does_not_block_message(caplog):
    rec = Recorder()

    def bad(_d, _s):
        raise RuntimeError("x")

    ing = Ingest(Deduplicator(), rec, bad)
    assert ing.handle({"id": "e1", "code": 556}, "ws")
    assert rec.got == [("e1", "ws")]


# --- 発火するか記録だけするか ---------------------------------------------


def test_items_after_setup_fire_by_age_and_oldest_first():
    now = SETUP + timedelta(minutes=40)
    p, rec, _ = make(now)
    items = [  # 応答は新しい順
        eew("e3", now - timedelta(seconds=5)),
        eew("e2", now - timedelta(seconds=599)),
        eew("e1", now - timedelta(seconds=601)),  # 600 秒より古い
    ]
    p.process(556, items)
    assert rec.got == [("e2", "rest"), ("e3", "rest")]
    q = [
        quake("q2", now - timedelta(seconds=1799)),
        quake("q1", now - timedelta(seconds=1801)),
    ]
    p.process(551, q)
    assert rec.got[-1] == ("q2", "rest")
    # 記録だけしたもの（e1・q1）も、後で WS から届いたら除かれる
    p.process(556, items)
    p.process(551, q)
    assert len(rec.got) == 3


def test_items_before_setup_are_recorded_only_unless_startup_eew():
    now = SETUP + timedelta(seconds=30)
    # 稼働中の setup（再読み込み・新規追加）: setup 前の情報は発火しない
    p, rec, ing = make(now, startup=False)
    p.process(556, [eew("e1", SETUP - timedelta(seconds=60))])
    p.process(551, [quake("q1", SETUP - timedelta(seconds=10))])
    assert rec.got == []
    # 見送ったものは Ingest に記録しない（WS で後から届いたら従来どおり発火する）
    assert not ing.known({"id": "e1"}) and not ing.known({"id": "q1"})
    assert ing.handle({"_id": "e1", "code": 556}, "ws")

    # HA の起動中の setup: 180 秒以内の緊急地震速報だけ発火に回す
    p, rec, _ = make(now, startup=True)
    p.process(
        556,
        [
            eew("e2", SETUP - timedelta(seconds=100)),  # 受信時点で 130 秒前
            eew("e1", SETUP - timedelta(seconds=200)),  # 230 秒前
        ],
    )
    p.process(551, [quake("q1", SETUP - timedelta(seconds=10))])
    assert rec.got == [("e2", "rest")]


def test_late_first_success_still_fires_items_after_setup():
    """起動時に回線が不調で最初の成功が遅れても、setup 以降の 551 は発火する（初回登録だけにしない）."""
    now = SETUP + timedelta(minutes=12)
    p, rec, _ = make(now)
    p.process(551, [quake("q1", SETUP + timedelta(minutes=10))])
    assert rec.got == [("q1", "rest")]


def test_unreadable_time_is_recorded_only_and_id_copied():
    now = SETUP + timedelta(minutes=1)
    p, rec, ing = make(now)
    p.process(556, [{"id": "e1", "code": 556, "time": "??"}])
    p.process(556, [{"id": "e1", "code": 556, "time": "??"}])
    assert rec.got == []
    # 時刻の書式が変わっても、WS で届いた同じ速報は発火する（REST の見送りで WS を止めない）
    assert ing.handle({"_id": "e1", "code": 556}, "ws")
    seen = []
    p._ingest = Ingest(Deduplicator(), lambda d, s: seen.append(d))
    p.process(556, [eew("e2", now)])
    assert seen[0]["_id"] == "e2" and seen[0]["id"] == "e2"


def test_real_fixture_is_processed_oldest_first():
    when = parse_time(FIXTURE[0]["time"])
    p, rec, _ = make(when + timedelta(seconds=30))
    p._setup_at = when - timedelta(hours=1)
    p.process(551, FIXTURE)
    assert [g[0] for g in rec.got] == [x["id"] for x in reversed(FIXTURE)]


def test_clock_skew_warns_once(caplog):
    now = SETUP + timedelta(minutes=1)
    p, rec, _ = make(now)
    p.process(556, [eew("e1", now + timedelta(minutes=5))])
    p.process(556, [eew("e2", now + timedelta(minutes=5))])
    assert caplog.text.count("進んでいます") == 1
    assert len(rec.got) == 2


# --- 取得 ---------------------------------------------------------------


class FakeResp:
    def __init__(self, status=200, body=b"[]", headers=None, exc=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self._exc = exc
        self.content = self

    async def iter_any(self):
        # 実際の通信と同じく、少しずつ届く
        for i in range(0, len(self._body), 1000):
            yield self._body[i : i + 1000]

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        r = self.responses.pop(0) if self.responses else FakeResp()
        return r


async def test_fetch_validates_responses():
    now = SETUP
    body = json.dumps([eew("e1", now)]).encode()
    s = FakeSession(
        [
            FakeResp(body=body),
            FakeResp(status=429, headers={"Retry-After": "30"}),
            FakeResp(status=503),
            FakeResp(status=404),
            FakeResp(body=b"{}"),
            FakeResp(body=b"not json"),
            FakeResp(body=json.dumps([quake("q", now)]).encode()),  # code 違い
            FakeResp(body=json.dumps([1]).encode()),
            FakeResp(body=b"[" + b" " * (poller_mod.MAX_BODY + 1) + b"]"),
            FakeResp(exc=TimeoutError()),
            FakeResp(exc=aiohttp.ClientConnectionError("x")),
            FakeResp(exc=ConnectionResetError()),
            FakeResp(body=b"[" * 100000 + b"]" * 100000),  # 深い入れ子（RecursionError）
        ]
    )
    p, _, _ = make(now, session=s)
    assert (await p.fetch(556, 10))[0]["id"] == "e1"
    assert s.urls[0].endswith("/v2/history?codes=556&limit=10")
    err = None
    with pytest.raises(FetchError) as err:
        await p.fetch(556, 10)
    assert err.value.retry_after == 30
    with pytest.raises(FetchError) as err:
        await p.fetch(556, 10)
    assert err.value.retry_after == 0
    for _ in range(10):
        with pytest.raises(FetchError) as err:
            await p.fetch(556, 10)
        assert err.value.retry_after is None


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.waits = []

    def mono(self):
        return self.t

    async def sleep(self, s):
        self.waits.append(round(s, 3))
        self.t += s
        await asyncio.sleep(0)


async def run_loop_with(responses, rounds, caplog=None):
    clock = Clock()
    s = FakeSession(responses)
    statuses = []
    p, _, _ = make(
        SETUP, session=s, monotonic=clock.mono, sleep=clock.sleep, on_status=lambda: statuses.append(1)
    )

    orig_sleep = clock.sleep

    async def counting_sleep(sec):
        await orig_sleep(sec)
        if len(clock.waits) >= rounds:
            p.stop()

    p._sleep = counting_sleep
    await p._loop(poller_mod._Loop(556, 10, "緊急地震速報"), lambda: poller_mod.EEW_INTERVAL)
    return p, clock, statuses


async def test_backoff_only_on_429_and_5xx(caplog):
    caplog.set_level(logging.DEBUG)
    responses = [
        FakeResp(),  # 成功 → 10
        FakeResp(exc=TimeoutError()),  # タイムアウトは周期を保つ → 10
        FakeResp(exc=aiohttp.ClientConnectionError("x")),  # → 10
        FakeResp(status=503),  # → 20
        FakeResp(status=503),  # → 40
        FakeResp(status=503),  # → 60
        FakeResp(status=503),  # → 60（上限）
        FakeResp(status=429, headers={"Retry-After": "120"}),  # Retry-After に従う → 120
        FakeResp(),  # 回復 → 10
    ]
    p, clock, statuses = await run_loop_with(responses, len(responses))
    assert clock.waits == [10, 10, 10, 20, 40, 60, 60, 120, 10]
    warnings = [r for r in caplog.records if "取得に失敗" in r.message and r.levelno == logging.WARNING]
    assert len(warnings) == 1  # 連続失敗の1回目だけ
    assert "取得が回復しました" in caplog.text
    assert p.last_success == clock.t - 10
    # 初回成功・各失敗・回復で状態の見直しを依頼
    assert len(statuses) == 1 + 7 + 1


async def test_fixed_rate_subtracts_fetch_time():
    clock = Clock()

    class SlowSession(FakeSession):
        def get(self, url, timeout=None):
            clock.t += 3  # 取得に3秒かかった
            return FakeResp()

    p, _, _ = make(SETUP, session=SlowSession([]), monotonic=clock.mono)

    async def sleep(sec):
        clock.waits.append(round(sec, 3))
        p.stop()

    p._sleep = sleep
    await p._loop(poller_mod._Loop(556, 10, "x"), lambda: 10.0)
    assert clock.waits == [7.0]


def test_quake_interval_bursts_after_activity():
    clock = Clock()
    p, _, _ = make(SETUP, monotonic=clock.mono)
    assert p.quake_interval() == 60
    p.notify_activity()
    assert p.quake_interval() == 15
    clock.t += 15 * 60 - 1
    assert p.quake_interval() == 15
    clock.t += 2
    assert p.quake_interval() == 60


async def test_quake_loop_waits_until_ready_and_stop_ends_run():
    ready = asyncio.Event()
    s = FakeSession([])
    p, _, _ = make(SETUP, session=s, quake_ready=ready.wait, sleep=lambda _s: asyncio.sleep(0))
    task = asyncio.create_task(p.run())
    for _ in range(5):
        await asyncio.sleep(0)
    assert any("codes=556" in u for u in s.urls)
    assert not any("codes=551" in u for u in s.urls)  # 起動完了まで 551 は取らない
    ready.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert any("codes=551&limit=3" in u for u in s.urls)
    p.stop()
    await asyncio.wait_for(task, 1)


# --- レビューで見つかった不具合の再発防止 ------------------------------------------


async def test_unexpected_error_keeps_loop_running(caplog):
    clock = Clock()
    calls = {"n": 0}

    class Weird(FakeSession):
        def get(self, url, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("session closed")
            return FakeResp()

    p, _, _ = make(SETUP, session=Weird([]), monotonic=clock.mono)

    async def sleep(sec):
        clock.waits.append(round(sec, 3))
        if len(clock.waits) >= 2:
            p.stop()

    p._sleep = sleep
    await p._loop(poller_mod._Loop(556, 10, "緊急地震速報"), lambda: 10.0)
    assert calls["n"] == 2 and clock.waits == [10, 10]
    assert "想定外のエラー" in caplog.text
    assert p.last_success is not None


async def test_run_stops_both_loops_if_one_dies():
    p, _, _ = make(SETUP, session=FakeSession([]))
    other_cancelled = asyncio.Event()

    async def eew():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            other_cancelled.set()
            raise

    async def quake():
        raise RuntimeError("boom")

    p._run_eew = eew
    p._run_quake = quake
    with pytest.raises(BaseExceptionGroup):
        await asyncio.wait_for(p.run(), 1)
    assert other_cancelled.is_set()  # 管理外のループが残らない


def test_resumed_setup_uses_normal_age_rule():
    """再読み込み（記録を引き継いだ）なら、setup 直前の隙に出た速報も拾う."""
    now = SETUP + timedelta(seconds=30)
    p, rec, _ = make(now, resumed=True)
    p.process(556, [eew("e1", SETUP - timedelta(seconds=20)), eew("e0", SETUP - timedelta(seconds=700))])
    assert rec.got == [("e1", "rest")]


def test_older_quake_is_not_shown_after_newer_one():
    now = SETUP + timedelta(minutes=10)
    p, rec, _ = make(now)
    p.note_quake(quake("q2", SETUP + timedelta(minutes=5)))  # WS で新しい報を表示済み
    p.process(551, [quake("q3", SETUP + timedelta(minutes=8)), quake("q1", SETUP + timedelta(minutes=3))])
    assert rec.got == [("q3", "rest")]
