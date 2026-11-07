"""P2P地震情報 JSON API（/v2/history）による予備受信（Home Assistant 非依存）.

WebSocket が遅れた・切れたときの予備。取得した情報は Ingest に渡し、WebSocket と同じ
重複除去・同じ処理を通す。古すぎる情報は発火せず「処理済み」として記録だけする。

- 緊急地震速報（556）: 10 秒ごと。速報が無ければ応答は `[]`（2 バイト）
- 地震情報（551）: 通常 60 秒ごと。新しい 551・556 を受け取ってから 15 分間は 15 秒ごと
  （観測点の一覧を含み重いので、普段は間隔を空ける）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .ingest import Ingest

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.p2pquake.net/v2/history"
JST = timezone(timedelta(hours=9))  # zoneinfo はイベントループでファイルを読むため使わない

EEW_INTERVAL = 10.0
QUAKE_INTERVAL = 60.0
QUAKE_BURST_INTERVAL = 15.0
BURST_DURATION = 15 * 60.0
EEW_LIMIT = 10  # バックオフ中に続報が何報か出ても取りこぼさない（速報が無ければ 2 バイト）
QUAKE_LIMIT = 3
REQUEST_TIMEOUT = 8.0
MAX_BODY = 1024 * 1024
BACKOFF_MAX = 60.0
RETRY_AFTER_MAX = 300.0

# setup より前の情報は発火しない。ただし HA の起動中に setup したときだけ、
# 停止中・起動中に出た緊急地震速報（この秒数以内）を発火に回す（HA 起動時の取りこぼし対策）
STARTUP_EEW_MAX_AGE = 180.0
# setup 以降でも、これより古い情報は発火しない（障害から戻ったときに古い情報を流さない）
MAX_AGE = {556: 600.0, 551: 1800.0}
CLOCK_SKEW_WARN = 120.0


def parse_time(value: Any) -> datetime | None:
    """P2P の時刻（日本時間 `YYYY/MM/DD HH:MM:SS[.fff]`）を読む。読めなければ None."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


class FetchError(Exception):
    """1 回の取得の失敗。retry_after があれば待ちを伸ばす（429・5xx）."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


@dataclass
class _Loop:
    code: int
    limit: int
    name: str
    failures: int = 0
    backoff: float = 0.0


class P2PQuakePoller:
    """556 と 551 を別々のループで取得し続ける."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ingest: Ingest,
        *,
        setup_at: datetime,
        setup_during_startup: bool,
        quake_ready: Callable[[], Awaitable[None]] | None = None,
        on_status: Callable[[], None] | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self._ingest = ingest
        self._setup_at = setup_at
        self._setup_during_startup = setup_during_startup
        self._quake_ready = quake_ready
        self._on_status = on_status
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._sleep = sleep
        self._base_url = base_url
        self._stopped = False
        self._burst_until = 0.0
        self._skew_warned = False
        self.last_success: float | None = None  # 556 の取得が最後に成功した時刻（monotonic）
        self.alive = True  # 取得タスクが動いているか（予期せず終わったら False）

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True

    def notify_activity(self) -> None:
        """新しい 551・556 を受け取った（WS・REST どちらでも）。15 分間は 551 を短い間隔で取る."""
        self._burst_until = self._monotonic() + BURST_DURATION

    def quake_interval(self) -> float:
        return QUAKE_BURST_INTERVAL if self._monotonic() < self._burst_until else QUAKE_INTERVAL

    def _status(self) -> None:
        if self._on_status:
            try:
                self._on_status()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("REST 取得状態の通知に失敗")

    async def run(self) -> None:
        """stop() されるかキャンセルされるまで取得し続ける."""
        await asyncio.gather(self._run_eew(), self._run_quake())

    async def _run_eew(self) -> None:
        await self._loop(_Loop(556, EEW_LIMIT, "緊急地震速報"), lambda: EEW_INTERVAL)

    async def _run_quake(self) -> None:
        # 起動中の 551 は発火しない方針なので、HA の起動完了を待ってから取り始める
        if self._quake_ready is not None:
            await self._quake_ready()
        await self._loop(_Loop(551, QUAKE_LIMIT, "地震情報"), self.quake_interval)

    async def _loop(self, loop: _Loop, interval: Callable[[], float]) -> None:
        while not self._stopped:
            started = self._monotonic()
            wait = interval()
            try:
                items = await self.fetch(loop.code, loop.limit)
            except FetchError as err:
                wait = self._on_failure(loop, err, wait)
            else:
                self._on_success(loop)
                self.process(loop.code, items)
            if self._stopped:
                return
            # 開始時刻を基準にした固定周期（取得にかかった時間を差し引く。前の取得とは重ならない）
            await self._sleep(max(0.0, wait - (self._monotonic() - started)))

    def _on_success(self, loop: _Loop) -> None:
        was_failing = loop.failures > 0
        if was_failing:
            _LOGGER.info("P2P地震情報 JSON API（%s）の取得が回復しました", loop.name)
        loop.failures = 0
        loop.backoff = 0.0
        if loop.code == 556:
            first = self.last_success is None
            self.last_success = self._monotonic()
            if first or was_failing:
                self._status()

    def _on_failure(self, loop: _Loop, err: FetchError, wait: float) -> float:
        loop.failures += 1
        _LOGGER.log(
            logging.WARNING if loop.failures == 1 else logging.DEBUG,
            "P2P地震情報 JSON API（%s）の取得に失敗: %s",
            loop.name,
            err,
        )
        if loop.code == 556:
            self._status()  # 状態センサーは時刻で判定する。ここでは見直しのきっかけだけ
        if err.retry_after is None:
            # タイムアウト・接続エラー・形式の誤りでは周期を保つ（安全経路を粗くしない）
            loop.backoff = 0.0
            return wait
        # 429・5xx: サーバーの負荷を下げるため待ちを伸ばす（Retry-After があれば従う）
        loop.backoff = min(max(loop.backoff * 2, wait * 2), BACKOFF_MAX)
        return max(loop.backoff, min(err.retry_after, RETRY_AFTER_MAX))

    async def fetch(self, code: int, limit: int) -> list[dict[str, Any]]:
        url = f"{self._base_url}?codes={code}&limit={limit}"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 429 or resp.status >= 500:
                    raise FetchError(
                        f"HTTP {resp.status}", _retry_after(resp.headers.get("Retry-After"))
                    )
                if resp.status != 200:
                    raise FetchError(f"HTTP {resp.status}")
                body = await resp.content.read(MAX_BODY + 1)
        except FetchError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FetchError(f"{type(err).__name__}: {err}") from err
        if len(body) > MAX_BODY:
            raise FetchError("応答が大きすぎます")
        try:
            data = json.loads(body)
        except ValueError as err:
            raise FetchError("JSON ではない応答") from err
        if not isinstance(data, list):
            raise FetchError("配列ではない応答")
        items = [x for x in data if isinstance(x, dict) and x.get("code") == code]
        if len(items) != len(data):
            raise FetchError("想定外の要素を含む応答")
        return items

    def process(self, code: int, items: list[dict[str, Any]]) -> None:
        """取得した要素を古い順に、発火するか記録だけするかを決めて Ingest に渡す."""
        now = self._now()
        for item in reversed(items):  # 応答は新しい順。古い順に処理する
            if self._ingest.known(item):
                continue
            when = parse_time(item.get("time"))
            if when is None:
                _LOGGER.debug("時刻を読めない情報は発火しません: %s", item.get("id"))
                self._ingest.mark_seen(item)
                continue
            age = (now - when).total_seconds()
            if age < -CLOCK_SKEW_WARN and not self._skew_warned:
                self._skew_warned = True
                _LOGGER.warning(
                    "P2P の時刻が HA の時計より %.0f 秒進んでいます。HA の時計を確認してください", -age
                )
            if when < self._setup_at:
                fire = (
                    code == 556
                    and self._setup_during_startup
                    and age <= STARTUP_EEW_MAX_AGE
                )
            else:
                fire = age <= MAX_AGE[code]
            if not fire:
                self._ingest.mark_seen(item)
                continue
            data = dict(item)
            if "_id" not in data and data.get("id"):
                data["_id"] = data["id"]  # WebSocket と同じ形にそろえる
            self._ingest.handle(data, "rest")


def _retry_after(value: str | None) -> float:
    """Retry-After（秒）を読む。無い・読めないときは 0（呼び出し側の待ちを使う）."""
    try:
        return max(0.0, float(value)) if value is not None else 0.0
    except ValueError:
        return 0.0
