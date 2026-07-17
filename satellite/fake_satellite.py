"""Fake satellite — runs on the Mac. Push-to-talk instead of wake word.

Exercises the identical record → STT → route → TTS loop the Pi runs, using
the Mac's mic and the built-in `say` command for TTS. This is the Phase 1
exit-criterion tool: speak a question at the Mac, hear a spoken answer.

Usage:
    STT_URL=ws://localhost:8100/stt ROUTER_URL=http://localhost:8200/route \
        python3 fake_satellite.py [--device N] [--silence-rms 300]

Run `python3 -c "import sounddevice; print(sounddevice.query_devices())"`
to list input devices if the default mic isn't right.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import threading

import requests
import sounddevice as sd
import websockets
from pipeline import FRAME_SAMPLES, SAMPLE_RATE, record_utterance, transcribe

STT_URL = os.environ.get("STT_URL", "ws://localhost:8100/stt")
ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8200/route")
EVENTS_URL = os.environ.get("EVENTS_URL", "ws://localhost:8200/events")
CHIME_CMD = ["afplay", "/System/Library/Sounds/Glass.aiff"]
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

log = logging.getLogger("fake_satellite")


def speak(text: str) -> None:
    """macOS built-in TTS — no Piper needed on the Mac."""
    subprocess.run(["say", text], check=False)


def ask_router(text: str) -> str:
    resp = requests.post(ROUTER_URL, json={"text": text}, timeout=60)
    resp.raise_for_status()
    return resp.json().get("speech", "")


class _StopSession(Exception):
    """Internal: unwinds the TaskGroup when stop() is requested."""


class EventListener:
    """Background push-channel listener (spec Component 3).

    Own thread running its own asyncio loop: connect to EVENTS_URL, reconnect
    forever with capped backoff, chime + speak on announce, pong on ping.
    announcement_active is set while an event is unacknowledged; the
    foreground loop calls request_ack() to acknowledge.

    Note: speak() blocks this loop for the utterance duration — acceptable for
    the fake satellite; the announce repeat cadence is router-side."""

    def __init__(self, events_url: str) -> None:
        self.events_url = events_url
        self.announcement_active = threading.Event()
        self._pending_event_id: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ack_queue: asyncio.Queue[str] | None = None
        self._stop_async: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="events-listener")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None and self._stop_async is not None:
            self._loop.call_soon_threadsafe(self._stop_async.set)
        self._thread.join(timeout=5)

    def request_ack(self) -> None:
        """Called from the foreground thread on Enter during an announcement."""
        eid = self._pending_event_id
        if eid is not None and self._loop is not None and self._ack_queue is not None:
            self._loop.call_soon_threadsafe(self._ack_queue.put_nowait, eid)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ack_queue = asyncio.Queue()
        self._stop_async = asyncio.Event()
        backoff = RECONNECT_MIN_S
        while not self._stop_async.is_set():
            stopped = False
            try:
                async with websockets.connect(self.events_url) as ws:
                    backoff = RECONNECT_MIN_S
                    try:
                        await self._session(ws)
                    except* _StopSession:
                        # `return` cannot appear directly in an except* clause
                        # (PEP 654) — set a flag and return just below instead.
                        stopped = True
            except (OSError, websockets.WebSocketException, ExceptionGroup):
                # TaskGroup wraps a reader ConnectionClosed in an ExceptionGroup;
                # either way: reconnect with backoff.
                pass
            if stopped or self._stop_async.is_set():
                return
            try:
                await asyncio.wait_for(self._stop_async.wait(), timeout=backoff)
                return
            except TimeoutError:
                backoff = min(backoff * 2, RECONNECT_MAX_S)

    async def _session(self, ws) -> None:  # noqa: ANN001 — ws class moved between versions
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._read(ws))
            tg.create_task(self._pump_acks(ws))
            tg.create_task(self._watch_stop())

    async def _watch_stop(self) -> None:
        await self._stop_async.wait()
        raise _StopSession

    async def _read(self, ws) -> None:  # noqa: ANN001
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "announce":
                self._pending_event_id = msg["event_id"]
                self.announcement_active.set()
                subprocess.run(CHIME_CMD, check=False)
                speak(msg["speech"])
            elif msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))

    async def _pump_acks(self, ws) -> None:  # noqa: ANN001
        while True:
            eid = await self._ack_queue.get()
            await ws.send(json.dumps({"type": "ack", "event_id": eid}))
            self.announcement_active.clear()
            self._pending_event_id = None


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index")
    ap.add_argument("--silence-rms", type=float, default=300, help="endpointing threshold")
    args = ap.parse_args()

    listener = EventListener(EVENTS_URL)
    listener.start()
    print(
        "fake satellite — press Enter, speak, pause; Enter during an alarm acks it; Ctrl-C to quit"
    )
    try:
        while True:
            try:
                input("⏎ ")
                if listener.announcement_active.is_set():
                    listener.request_ack()
                    print("(acknowledged)")
                    continue
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=FRAME_SAMPLES,
                    device=args.device,
                ) as stream:
                    audio = record_utterance(stream, silence_rms=args.silence_rms)
                text = asyncio.run(transcribe(audio, STT_URL))
                print(f"heard: {text!r}")
                if not text:
                    continue
                speech = ask_router(text)
                print(f"reply: {speech!r}")
                if speech:
                    speak(speech)
            except KeyboardInterrupt:
                print("\nbye")
                return
            except EOFError:
                # spec (audit edit): closed/redirected stdin exits cleanly,
                # never busy-loops through the generic handler
                print("\nstdin closed — exiting")
                return
            except Exception:
                log.exception("turn failed; listening again")
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
