"""poller.fetch を本物の HTTP サーバーで試す（HA 不要。test_client.py と同じく別に実行する）.

実際の通信では応答が少しずつ届く。偽のセッションで一度に全部返すと見逃す不具合
（read(n) が届いた分だけを返し、JSON が途中で切れる）を再発させないためのテスト。
"""
import asyncio
import gzip
import json
import pathlib

import aiohttp
import pytest
from aiohttp import web

from custom_components.p2pquake_ws.client import Deduplicator
from custom_components.p2pquake_ws.ingest import Ingest
from custom_components.p2pquake_ws.poller import MAX_BODY, FetchError, P2PQuakePoller

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures/history_551_ibaraki.json").read_text("utf-8")
)
RAW = json.dumps(FIXTURE, ensure_ascii=False).encode()


def _stream(body: bytes, gz: bool, piece: int = 1448):
    async def handler(request):
        data = gzip.compress(body) if gz else body
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        if gz:
            resp.headers["Content-Encoding"] = "gzip"
        await resp.prepare(request)
        for i in range(0, len(data), piece):
            await resp.write(data[i : i + piece])
            await asyncio.sleep(0.001)
        await resp.write_eof()
        return resp

    return handler


@pytest.fixture
async def server():
    app = web.Application()
    app.router.add_get("/raw/v2/history", _stream(RAW, gz=False))
    app.router.add_get("/gz/v2/history", _stream(RAW, gz=True))
    app.router.add_get("/huge/v2/history", _stream(b"[" + b" " * (MAX_BODY + 10) + b"]", gz=False, piece=65536))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


def _poller(session, base):
    from datetime import datetime, timezone

    return P2PQuakePoller(
        session,
        Ingest(Deduplicator(), lambda d, s: None),
        setup_at=datetime.now(timezone.utc),
        setup_during_startup=False,
        base_url=f"{base}/v2/history",
    )


@pytest.mark.parametrize("kind", ["raw", "gz"])
async def test_fetch_reads_whole_body_sent_in_small_pieces(server, kind):
    async with aiohttp.ClientSession() as session:
        items = await _poller(session, f"{server}/{kind}").fetch(551, 3)
    assert [x["id"] for x in items] == [x["id"] for x in FIXTURE]
    assert len(items[0]["points"]) == 558


async def test_fetch_rejects_too_large_body(server):
    async with aiohttp.ClientSession() as session:
        with pytest.raises(FetchError, match="大きすぎ"):
            await _poller(session, f"{server}/huge").fetch(551, 3)
