"""WebSocket と JSON API（REST）の受け口（Home Assistant 非依存）.

どちらの経路で届いても、同じ情報（同じ `id` / `_id`）は1回だけ callback に渡す。
対象は緊急地震速報（556）と地震情報（551）だけ。ピア分布（555）や揺れた報告（561）は
重複除去の枠に入れない（強い地震で 561 が大量に届いても、556・551 の記録が押し出されない）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .client import Deduplicator, message_id

_LOGGER = logging.getLogger(__name__)

SOURCE_WS = "ws"
SOURCE_REST = "rest"
CODES = (551, 556)

IngestCallback = Callable[[dict[str, Any], str], None]


class Ingest:
    """重複を除いて callback に渡す.

    「処理済み」の記録は callback を呼ぶ直前に行う（多くても1回）。callback の後ろの処理で
    例外が出ても、REST が同じ情報を返し続けるたびに発火し直すことはない。
    記録の確認から登録までの間に await を入れないこと（同じ情報を2回通さないため）。
    """

    def __init__(
        self,
        dedup: Deduplicator,
        on_message: IngestCallback,
        on_new: Callable[[dict[str, Any], str], None] | None = None,
    ) -> None:
        self._dedup = dedup
        self._on_message = on_message
        self._on_new = on_new

    def known(self, data: dict[str, Any]) -> bool:
        return self._dedup.seen(message_id(data))

    def mark_seen(self, data: dict[str, Any]) -> None:
        """発火せずに「処理済み」としてだけ記録する（古い情報など）."""
        if data.get("code") in CODES:
            self._dedup.add(message_id(data))

    def handle(self, data: Any, source: str) -> bool:
        """新しい 551・556 なら callback に渡して True を返す."""
        if not isinstance(data, dict) or data.get("code") not in CODES:
            return False
        key = message_id(data)
        if key is None:
            if source == SOURCE_REST:
                # REST は同じ要素を何度も返すので、識別子が無いと毎回発火してしまう
                _LOGGER.debug("識別子の無い情報を捨てました（REST）")
                return False
            # WebSocket は従来どおり通す（重複除去はできない）
        elif self._dedup.seen(key):
            return False
        self._dedup.add(key)
        if self._on_new is not None:
            try:
                self._on_new(data, source)
            except Exception:  # noqa: BLE001 - 周期の切り替えの失敗で受信を止めない
                _LOGGER.exception("新しい情報の通知に失敗")
        try:
            self._on_message(data, source)
        except Exception:  # noqa: BLE001 - 1件の失敗で受信（WS・REST）を止めない
            _LOGGER.exception("情報の処理に失敗（%s）: %s", source, key)
        return True
