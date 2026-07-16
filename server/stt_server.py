"""STT server — runs on the Mac Studio. faster-whisper behind a websocket.

The satellite streams raw 16 kHz mono int16 PCM chunks, then sends the text
message "END"; the server transcribes the buffered utterance and replies with
{"text": ...}.

Usage:
    uvicorn stt_server:app --host 0.0.0.0 --port 8100
"""

import asyncio
import logging
import os
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from faster_whisper import WhisperModel

log = logging.getLogger("stt_server")

MODEL = os.environ.get("WHISPER_MODEL", "large-v3")

app = FastAPI()


@lru_cache(maxsize=1)
def get_model() -> WhisperModel:
    """Load whisper on first use, not at import — keeps tests and startup fast."""
    return WhisperModel(MODEL, device="auto", compute_type="auto")


@app.websocket("/stt")
async def stt(ws: WebSocket) -> None:
    await ws.accept()
    buf = bytearray()
    try:
        while True:
            msg = await ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                buf.extend(msg["bytes"])
            elif msg.get("text") == "END":
                break
            elif msg.get("type") == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return

    try:
        audio = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = await asyncio.to_thread(
            lambda: get_model().transcribe(audio, language="en", vad_filter=True)
        )
        text = " ".join(seg.text.strip() for seg in segments)
    except Exception as exc:
        # Spec (carry-over, 2026-07-15): never close with no reply — the client
        # pairs this with its 60 s deadline and fails the turn immediately.
        log.exception("transcription failed")
        await ws.send_json({"text": "", "error": type(exc).__name__})
        await ws.close()
        return
    await ws.send_json({"text": text})
    await ws.close()
