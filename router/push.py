"""Websocket push channel — router endpoint side (spec Component 2).

Satellites hold one connection each. Announcements broadcast to ALL
connected satellites; an ack from any one is forwarded to the scheduler.
Zero connected satellites is not an error."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("push")

PING_INTERVAL_S = 20


class PushChannel:
    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()
        self._on_ack: Callable[[str], None] | None = None

    def set_ack_handler(self, cb: Callable[[str], None]) -> None:
        self._on_ack = cb

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        self._conns.add(ws)
        log.info("satellite connected (%d total)", len(self._conns))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ignoring non-JSON frame: %r", raw[:80])
                    continue
                if msg.get("type") == "ack" and self._on_ack is not None:
                    self._on_ack(str(msg.get("event_id", "")))
                # pongs and anything else: ignored
        except WebSocketDisconnect:
            pass
        finally:
            self._conns.discard(ws)
            log.info("satellite disconnected (%d total)", len(self._conns))

    async def broadcast_announce(self, event_id: str, speech: str, repeat_n: int) -> None:
        await self._send_all(
            json.dumps(
                {"type": "announce", "event_id": event_id, "speech": speech, "repeat_n": repeat_n}
            )
        )

    async def ping_loop(self, interval_s: float = PING_INTERVAL_S) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self._send_all(json.dumps({"type": "ping"}))

    async def _send_all(self, frame: str) -> None:
        for ws in list(self._conns):
            try:
                await ws.send_text(frame)
            except Exception:
                log.warning("dropping dead satellite connection")
                self._conns.discard(ws)
