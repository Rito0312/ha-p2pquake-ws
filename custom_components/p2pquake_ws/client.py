"""P2P地震情報 WebSocket クライアント（Home Assistant 非依存）.

受信・再接続・重複除去だけを担当し、判定は呼び出し側（HAのオートメーション）に任せる。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
BACKOFF_JITTER = 0.2  # 待ち時間を ±20% ばらつかせる
HEARTBEAT = 30.0
# 本番ではピア分布(555)が数分おきに必ず届く。これだけ「データ」が届かなければ
# 「つながっているのに配信が止まっている」とみなして接続し直す。
# ping/pong は数えない（サーバーが生きていても配信だけ止まる場合を検知するため）。
STALL_TIMEOUT = 20 * 60.0
# この時間以上つながっていたか、データを1件でも受け取れたら「安定した接続」とみなし、
# 再接続の待ち時間を初期値に戻す。接続直後に切られ続ける場合は待ち時間を伸ばす。
STABLE_AFTER = 60.0
HANDSHAKE_TIMEOUT = 30.0
DEDUP_SIZE = 1000

MessageCallback = Callable[[dict[str, Any]], None]
StatusCallback = Callable[[bool], None]


def message_id(data: dict[str, Any]) -> str | None:
    """情報の識別子を返す.

    公式仕様では `id` だが、WebSocket の実データでは `_id` で届く。両方を見る。
    """
    value = data.get("id") or data.get("_id")
    if isinstance(value, dict):  # 念のため {"$oid": "..."} 形式にも対応
        value = value.get("$oid")
    return str(value) if value else None


class Deduplicator:
    """直近に処理した識別子を覚えておき、同じ情報の再配信を捨てる."""

    def __init__(self, size: int = DEDUP_SIZE) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def seen(self, key: str | None) -> bool:
        if key is None:
            return False  # 識別子が無いものは捨てずに通す
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        return False

    def add(self, key: str | None) -> None:
        if key is None:
            return
        self._seen[key] = None
        self._seen.move_to_end(key)
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)


class P2PQuakeClient:
    """WebSocket に常時接続し、受信した JSON を callback に渡す."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        on_message: MessageCallback,
        on_status: StatusCallback | None = None,
        *,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
        heartbeat: float = HEARTBEAT,
        stall_timeout: float = STALL_TIMEOUT,
        stable_after: float = STABLE_AFTER,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._url = url
        self._on_message = on_message
        self._on_status = on_status
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._heartbeat = heartbeat
        self._stall_timeout = stall_timeout
        self._stable_after = stable_after
        self._handshake_timeout = handshake_timeout
        self._sleep = sleep
        self._dedup = Deduplicator()
        self._stopped = False
        self.connected = False
        self.connect_count = 0
        self.stall_count = 0

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True

    def _set_connected(self, value: bool) -> None:
        if self.connected == value:
            return
        self.connected = value
        if self._on_status:
            try:
                self._on_status(value)
            except Exception:  # noqa: BLE001 - 状態通知の失敗で受信を止めない
                _LOGGER.exception("接続状態の通知に失敗")

    def handle_raw(self, raw: str) -> dict[str, Any] | None:
        """1 フレーム分の文字列を処理する。新しい情報なら callback を呼び dict を返す.

        callback が例外を投げた場合は「処理済み」にしないので、再配信されれば再び処理される。
        """
        try:
            data = json.loads(raw)
        except ValueError:
            _LOGGER.debug("JSON ではないフレームを無視: %.100s", raw)
            return None
        if not isinstance(data, dict):
            return None
        key = message_id(data)
        if self._dedup.seen(key):
            return None
        self._on_message(data)
        self._dedup.add(key)
        return data

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        """切断・無受信になるまで受信する。データを1件でも受け取れたら True を返す."""
        loop = asyncio.get_running_loop()
        received = False
        last_data = loop.time()
        while True:
            # aiohttp の receive(timeout) は内部で pong を受けるたびにタイマーを作り直すため
            # （heartbeat が有効だと永遠に時間切れにならない）、締め切りは aiohttp の外の
            # asyncio.timeout で管理する
            remaining = self._stall_timeout - (loop.time() - last_data)
            if remaining <= 0:
                self.stall_count += 1
                _LOGGER.warning(
                    "%.0f 分間データを受信していないため接続し直します",
                    self._stall_timeout / 60,
                )
                return received
            try:
                async with asyncio.timeout(remaining):
                    msg = await ws.receive()
            except TimeoutError:
                continue  # 次の周で remaining <= 0 として扱う
            if msg.type == aiohttp.WSMsgType.TEXT:
                last_data = loop.time()
                received = True
                try:
                    self.handle_raw(msg.data)
                except Exception:  # noqa: BLE001 - 1件の失敗で接続を落とさない
                    _LOGGER.exception("メッセージ処理に失敗")
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                return received
            # PING / PONG / BINARY は無視（データとして数えない）

    async def run(self) -> None:
        """切断されても止めない。stop() されるかキャンセルされるまで再接続し続ける."""
        loop = asyncio.get_running_loop()
        backoff = self._backoff_initial
        failures = 0  # 安定した接続ができないまま続いている回数
        while not self._stopped:
            stable = False
            stall_before = self.stall_count
            try:
                async with asyncio.timeout(self._handshake_timeout):
                    ws = await self._session.ws_connect(
                        self._url,
                        heartbeat=self._heartbeat,
                        timeout=aiohttp.ClientWSTimeout(ws_close=10),
                    )
                async with ws:
                    self.connect_count += 1
                    connected_at = loop.time()
                    _LOGGER.log(
                        logging.INFO if failures == 0 else logging.DEBUG,
                        "P2P地震情報 WebSocket に接続: %s",
                        self._url,
                    )
                    self._set_connected(True)
                    received = await self._receive_loop(ws)
                    stable = received or (
                        loop.time() - connected_at >= self._stable_after
                    )
                    reason = f"close_code={ws.close_code}"
            except asyncio.CancelledError:
                self._set_connected(False)
                raise
            except Exception as err:  # noqa: BLE001 - どんな失敗でも再接続を続ける
                reason = f"{type(err).__name__}: {err!r}"

            try:
                self._set_connected(False)
                if self._stopped:
                    return
                if stable:
                    backoff = self._backoff_initial
                    failures = 0
                failures += 1
                # 連続失敗の1回目だけ WARNING、以降は DEBUG（障害中にログを溢れさせない）。
                # 無受信で自分から切った場合は、その WARNING を出し済みなので DEBUG にする
                stalled = self.stall_count != stall_before
                _LOGGER.log(
                    logging.WARNING if failures == 1 and not stalled else logging.DEBUG,
                    "P2P地震情報 WebSocket から切断されました (%s)。再接続します",
                    reason,
                )
                wait = backoff * random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
                backoff = min(backoff * 2, self._backoff_max)
            except Exception:  # noqa: BLE001 - ここでも受信タスクを止めない
                _LOGGER.exception("再接続の準備中にエラー")
                wait = self._backoff_max
            await self._sleep(wait)
