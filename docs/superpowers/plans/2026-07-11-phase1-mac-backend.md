# Phase 1: Mac Backend + Fake Satellite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The complete voice pipeline (record → STT → route → LLM → TTS) working on the Mac Studio alone, with tests, before the XVF3800 hardware arrives.

**Architecture:** Three processes on the Mac: `stt_server.py` (faster-whisper behind a websocket, :8100), `router.py` (intent regex → Open Brain/LLM dispatch, :8200), and a new `fake_satellite.py` (push-to-talk CLI using the Mac's mic and `say` for TTS). Shared satellite logic (`rms`, `record_utterance`, `transcribe`) is extracted to `satellite/pipeline.py` so the fake and real satellites run identical code.

**Tech Stack:** Python 3.11+, FastAPI, faster-whisper, httpx, anthropic (AsyncAnthropic), sounddevice, websockets, pytest + pytest-asyncio + respx, uv for all package management.

## Global Constraints

- Package management: `uv` only (`uv venv`, `uv pip install`); run Python as `python3`.
- Type hints on every function: parameters and return types.
- FastAPI route handlers MUST be async and use async clients for all I/O (`httpx.AsyncClient`, `AsyncAnthropic`).
- CLI scripts (satellite, fake_satellite) stay sync except where the shared async `transcribe` is awaited via `asyncio.run`.
- Errors from external calls (LLM, Open Brain, device APIs) must be caught and logged (`logging`, not `print`) and turned into spoken error text — the assistant must never answer a voice request with an HTTP 500. `print` is acceptable only for CLI user-facing output in `satellite/` scripts.
- Never commit secrets; `ANTHROPIC_API_KEY` comes from the environment.
- LLM model id default stays `claude-sonnet-5` via the `LLM_MODEL` env var.
- All work commits directly to `main` (solo-dev workflow), one commit per task.

---

### Task 1: Dev environment + test scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `requirements-dev.txt`
- Create: `satellite/requirements-mac.txt`
- Modify: `.gitignore`
- Create: `router/tests/conftest.py`, `server/tests/conftest.py`, `satellite/tests/conftest.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: a repo-root `.venv` all later tasks use; `pytest` runnable from repo root; each `tests/conftest.py` puts its component dir on `sys.path` so tests can `import router` / `import stt_server` / `import pipeline`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP"]

[tool.pytest.ini_options]
testpaths = ["router/tests", "server/tests", "satellite/tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create `requirements-dev.txt`**

```
pytest
pytest-asyncio
respx
httpx
```

(`httpx` is required by FastAPI's `TestClient` and is a runtime dep of the router anyway.)

- [ ] **Step 3: Create `satellite/requirements-mac.txt`**

The fake satellite's Mac-only dependency set — deliberately excludes
`openwakeword` (its tflite dependency is unreliable on macOS and the fake
satellite is push-to-talk) and `piper` (macOS `say` is used instead):

```
numpy
sounddevice
websockets
requests
```

- [ ] **Step 4: Append to `.gitignore`**

```
.venv/
```

- [ ] **Step 5: Create the three conftest files**

`router/tests/conftest.py`:

```python
"""Make `import router` work when pytest runs from the repo root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

`server/tests/conftest.py`:

```python
"""Make `import stt_server` work when pytest runs from the repo root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

`satellite/tests/conftest.py`:

```python
"""Make `import pipeline` / `import fake_satellite` work when pytest runs from the repo root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 6: Create the venv and install everything**

```bash
cd /Users/willmacdonald/Documents/Code/claude/my-HA
uv venv .venv
source .venv/bin/activate
uv pip install -r server/requirements.txt -r router/requirements.txt \
    -r satellite/requirements-mac.txt -r requirements-dev.txt
```

Expected: installs succeed. (`faster-whisper` is the slow one; no model
weights download at install time.)

- [ ] **Step 7: Verify pytest runs (collecting nothing yet)**

```bash
python3 -m pytest
```

Expected: `no tests ran` (exit code 5) — collection succeeds, no errors.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml requirements-dev.txt satellite/requirements-mac.txt .gitignore \
    router/tests/conftest.py server/tests/conftest.py satellite/tests/conftest.py
git commit -m "chore: dev environment — uv venv, pytest scaffolding, ruff config"
```

---

### Task 2: Router — async refactor, error handling, tests

**Files:**
- Modify: `router/router.py` (full rewrite below)
- Test: `router/tests/test_router.py`

**Interfaces:**
- Consumes: `router/intents.yaml` (existing, unchanged).
- Produces: `POST /route` accepting `{"text": str}` returning `{"speech": str, "intent": str}` — never a 5xx. Module-level names later tests/tasks rely on: `match_intent(text: str) -> tuple[dict, re.Match] | None`, `async ask_llm(text: str) -> str`, `async ask_open_brain(query: str) -> str`, `async run_device_action(intent: dict, match: re.Match) -> str`, constant `ERROR_SPEECH: str`.

- [ ] **Step 1: Write the failing tests**

`router/tests/test_router.py`:

```python
"""Router tests: intent matching, dispatch, fallbacks, error handling.

External calls (device APIs, Open Brain, Anthropic) are mocked — these tests
run offline with no API keys.
"""

from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

import router

client = TestClient(router.app)


# --- match_intent (pure function) ---


def test_match_intent_lights_on() -> None:
    matched = router.match_intent("turn on the kitchen lights")
    assert matched is not None
    intent, match = matched
    assert intent["name"] == "lights_on"
    assert match.groupdict()["room"] == "kitchen"


def test_match_intent_knowledge_query() -> None:
    matched = router.match_intent("what did I decide about Foundry rate limits")
    assert matched is not None
    intent, _ = matched
    assert intent["name"] == "knowledge_query"


def test_match_intent_no_match_returns_none() -> None:
    assert router.match_intent("what's the weather on Mars") is None


# --- /route dispatch ---


def test_unmatched_text_falls_back_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="It's red and dusty."))
    resp = client.post("/route", json={"text": "what's the weather on Mars"})
    assert resp.status_code == 200
    assert resp.json() == {"speech": "It's red and dusty.", "intent": "llm_fallback"}


def test_knowledge_intent_without_open_brain_falls_back_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "")
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="You decided X."))
    resp = client.post("/route", json={"text": "what did I decide about Foundry"})
    assert resp.json() == {"speech": "You decided X.", "intent": "llm_fallback"}


def test_knowledge_intent_with_open_brain_routes_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    monkeypatch.setattr(router, "ask_open_brain", AsyncMock(return_value="You chose Piper."))
    resp = client.post("/route", json={"text": "check my notes on TTS"})
    assert resp.json() == {"speech": "You chose Piper.", "intent": "knowledge_query"}


def test_device_intent_dispatches_to_device_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        router, "run_device_action", AsyncMock(return_value="Okay, kitchen lights on.")
    )
    resp = client.post("/route", json={"text": "turn on the kitchen lights"})
    assert resp.json() == {"speech": "Okay, kitchen lights on.", "intent": "lights_on"}


def test_llm_failure_returns_spoken_error_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "ask_llm", AsyncMock(side_effect=RuntimeError("api down")))
    resp = client.post("/route", json={"text": "hello there"})
    assert resp.status_code == 200
    assert resp.json() == {"speech": router.ERROR_SPEECH, "intent": "error"}


# --- action executors ---


@respx.mock
async def test_run_device_action_calls_url_with_slots() -> None:
    route_mock = respx.post("http://lights.local/api/kitchen/on").mock(
        return_value=Response(200)
    )
    matched = router.match_intent("turn on the kitchen lights")
    assert matched is not None
    intent, match = matched
    speech = await router.run_device_action(intent, match)
    assert route_mock.called
    assert speech == "Okay, kitchen lights on."


@respx.mock
async def test_ask_open_brain_returns_first_hit_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/search").mock(
        return_value=Response(200, json=[{"summary": "You picked the ND91-4."}])
    )
    assert await router.ask_open_brain("speaker choice") == "You picked the ND91-4."


@respx.mock
async def test_ask_open_brain_empty_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/search").mock(return_value=Response(200, json=[]))
    assert await router.ask_open_brain("nothing") == "I didn't find anything on that."
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate
python3 -m pytest router/tests -v
```

Expected: FAIL — `AttributeError: module 'router' has no attribute 'match_intent'` (and similar); the dispatch tests fail because the current sync code paths differ.

- [ ] **Step 3: Rewrite `router/router.py`**

Full replacement (keeps the module docstring's spirit, goes async, adds
`match_intent`, error handling, and the Open-Brain-unset fallback):

```python
"""The router — the ~100 lines to own.

transcript → local intent (device API) | knowledge query (Open Brain) | LLM.

Intents live in intents.yaml as regex patterns. First match wins; nothing
matches → LLM fallback. Knowledge routing is just another intent whose
action type is "open_brain". If OPEN_BRAIN_URL is unset, knowledge queries
fall back to the LLM instead of failing.

Usage:
    ANTHROPIC_API_KEY=… OPEN_BRAIN_URL=http://localhost:8000 \
        uvicorn router:app --host 0.0.0.0 --port 8200
"""

import logging
import os
import re
from pathlib import Path

import httpx
import yaml
from anthropic import AsyncAnthropic
from fastapi import FastAPI
from pydantic import BaseModel

OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = (
    "You are a home voice assistant. Answers are spoken aloud: "
    "reply in one or two short sentences, no markdown, no lists."
)

ERROR_SPEECH = "Sorry, something went wrong answering that."

log = logging.getLogger("router")

app = FastAPI()
INTENTS = yaml.safe_load(Path(__file__).with_name("intents.yaml").read_text())["intents"]


class Utterance(BaseModel):
    text: str


def match_intent(text: str) -> tuple[dict, re.Match] | None:
    """First intent whose regex matches, with its match object; None if nothing matches."""
    for intent in INTENTS:
        for pattern in intent["patterns"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return intent, match
    return None


async def run_device_action(intent: dict, match: re.Match) -> str:
    """Call a device API directly. The URL/body can use {slot} groups from the regex."""
    action = intent["action"]
    url = action["url"].format(**match.groupdict())
    async with httpx.AsyncClient(timeout=10) as client:
        await client.request(action.get("method", "POST"), url, json=action.get("json"))
    return intent.get("response", "Done.").format(**match.groupdict())


async def ask_open_brain(query: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{OPEN_BRAIN_URL}/search", json={"query": query})
        resp.raise_for_status()
    # TODO(will): shape this once the Open Brain voice-answer endpoint settles —
    # probably want a synthesized one-liner, not raw search hits.
    hits = resp.json()
    return hits[0]["summary"] if hits else "I didn't find anything on that."


async def ask_llm(text: str) -> str:
    client = AsyncAnthropic()
    msg = await client.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return msg.content[0].text


@app.post("/route")
async def route(utt: Utterance) -> dict:
    text = utt.text.strip()
    try:
        matched = match_intent(text)
        if matched:
            intent, match = matched
            kind = intent["action"]["type"]
            if kind == "device":
                return {"speech": await run_device_action(intent, match), "intent": intent["name"]}
            if kind == "open_brain" and OPEN_BRAIN_URL:
                return {"speech": await ask_open_brain(text), "intent": intent["name"]}
        return {"speech": await ask_llm(text), "intent": "llm_fallback"}
    except Exception:
        log.exception("routing failed for %r", text)
        return {"speech": ERROR_SPEECH, "intent": "error"}
```

Note the one behavior change beyond async: `open_brain` intents route to
Open Brain **only when `OPEN_BRAIN_URL` is set**; otherwise the utterance
drops through to the LLM (previously it crashed on an empty base URL).

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest router/tests -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add router/router.py router/tests/test_router.py
git commit -m "feat: async router with error handling and Open-Brain-unset LLM fallback"
```

---

### Task 3: STT server — lazy model loading + websocket protocol test

**Files:**
- Modify: `server/stt_server.py`
- Test: `server/tests/test_stt_server.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: websocket `/stt` protocol (binary int16 PCM chunks → text `"END"` → `{"text": str}` reply), unchanged from the current contract that `pipeline.transcribe` (Task 4) speaks. New module-level function `get_model() -> WhisperModel` (cached), replacing the import-time `model` global.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_stt_server.py`:

```python
"""STT websocket protocol tests with a fake whisper model — no weights download."""

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

import stt_server


def _fake_model(captured: dict[str, Any]) -> SimpleNamespace:
    def transcribe(audio: np.ndarray, **kwargs: Any) -> tuple[list[SimpleNamespace], None]:
        captured["audio"] = audio
        return [SimpleNamespace(text=" hello "), SimpleNamespace(text="world ")], None

    return SimpleNamespace(transcribe=transcribe)


def test_stt_roundtrip_joins_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(stt_server, "get_model", lambda: _fake_model(captured))
    client = TestClient(stt_server.app)
    pcm = np.zeros(1600, dtype=np.int16).tobytes()

    with client.websocket_connect("/stt") as ws:
        ws.send_bytes(pcm)
        ws.send_text("END")
        reply = ws.receive_json()

    assert reply == {"text": "hello world"}


def test_stt_converts_int16_to_normalized_float32(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(stt_server, "get_model", lambda: _fake_model(captured))
    client = TestClient(stt_server.app)
    pcm = np.array([32767, -32768, 0], dtype=np.int16).tobytes()

    with client.websocket_connect("/stt") as ws:
        ws.send_bytes(pcm)
        ws.send_text("END")
        ws.receive_json()

    audio = captured["audio"]
    assert audio.dtype == np.float32
    assert audio[0] == pytest.approx(1.0, abs=1e-4)
    assert audio[1] == pytest.approx(-1.0, abs=1e-4)
    assert audio[2] == 0.0


def test_stt_multiple_chunks_are_concatenated(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(stt_server, "get_model", lambda: _fake_model(captured))
    client = TestClient(stt_server.app)
    chunk = np.ones(100, dtype=np.int16).tobytes()

    with client.websocket_connect("/stt") as ws:
        ws.send_bytes(chunk)
        ws.send_bytes(chunk)
        ws.send_text("END")
        ws.receive_json()

    assert len(captured["audio"]) == 200
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest server/tests -v
```

Expected: FAIL — `AttributeError: module 'stt_server' has no attribute 'get_model'`. (If instead the run hangs downloading whisper weights at import, that is exactly the problem this task fixes — kill it and proceed.)

- [ ] **Step 3: Refactor `stt_server.py` to lazy model loading**

Replace the import-time `model = WhisperModel(...)` line with a cached
getter, and use it inside the handler. Full replacement of the file:

```python
"""STT server — runs on the Mac Studio. faster-whisper behind a websocket.

The satellite streams raw 16 kHz mono int16 PCM chunks, then sends the text
message "END"; the server transcribes the buffered utterance and replies with
{"text": ...}.

Usage:
    uvicorn stt_server:app --host 0.0.0.0 --port 8100
"""

import os
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from faster_whisper import WhisperModel

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

    audio = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = get_model().transcribe(audio, language="en", vad_filter=True)
    text = " ".join(seg.text.strip() for seg in segments)
    await ws.send_json({"text": text})
    await ws.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest server/tests -v
```

Expected: 3 passed, in seconds (no model download).

- [ ] **Step 5: Commit**

```bash
git add server/stt_server.py server/tests/test_stt_server.py
git commit -m "feat: lazy whisper model loading + STT websocket protocol tests"
```

---

### Task 4: Extract shared satellite pipeline

**Files:**
- Create: `satellite/pipeline.py`
- Modify: `satellite/satellite.py` (remove the moved functions, import them)
- Test: `satellite/tests/test_pipeline.py`

**Interfaces:**
- Consumes: the `/stt` websocket protocol from Task 3.
- Produces: `satellite/pipeline.py` exposing exactly:
  - `SAMPLE_RATE: int = 16000`, `FRAME_SAMPLES: int = 1280`
  - `rms(frame: np.ndarray) -> float`
  - `record_utterance(stream, *, silence_rms: float = 300, silence_seconds: float = 0.8, max_seconds: float = 12.0) -> bytes` — `stream` is anything with `read(n) -> tuple[buffer, bool]` (duck-typed so tests use a fake; real callers pass `sounddevice.InputStream`)
  - `async transcribe(audio: bytes, stt_url: str) -> str`

  Task 5's `fake_satellite.py` imports all of these.

- [ ] **Step 1: Write the failing tests**

`satellite/tests/test_pipeline.py`:

```python
"""Pipeline unit tests: rms, endpointing, and STT round-trip against a local fake server."""

import asyncio
import json

import numpy as np
import websockets

import pipeline


# --- rms ---


def test_rms_of_silence_is_zero() -> None:
    assert pipeline.rms(np.zeros(100, dtype=np.int16)) == 0.0


def test_rms_of_constant_signal() -> None:
    assert pipeline.rms(np.full(100, 1000, dtype=np.int16)) == 1000.0


# --- record_utterance endpointing ---


class FakeStream:
    """Yields loud frames, then silence forever."""

    def __init__(self, loud_frames: int, level: int = 2000) -> None:
        self.remaining_loud = loud_frames
        self.level = level

    def read(self, n: int) -> tuple[np.ndarray, bool]:
        if self.remaining_loud > 0:
            self.remaining_loud -= 1
            return np.full(n, self.level, dtype=np.int16), False
        return np.zeros(n, dtype=np.int16), False


def test_record_stops_after_sustained_silence() -> None:
    audio = pipeline.record_utterance(
        FakeStream(loud_frames=5), silence_rms=300, silence_seconds=0.8, max_seconds=12.0
    )
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    expected_frames = 5 + quiet_needed
    assert len(audio) == expected_frames * pipeline.FRAME_SAMPLES * 2  # int16 = 2 bytes


def test_record_does_not_endpoint_before_speech_starts() -> None:
    """Leading silence must not trigger the endpoint — only silence after speech."""

    class SilenceThenLoud:
        def __init__(self) -> None:
            self.frame = 0

        def read(self, n: int) -> tuple[np.ndarray, bool]:
            self.frame += 1
            if self.frame <= 30:  # 30 quiet frames > quiet_needed (10)
                return np.zeros(n, dtype=np.int16), False
            if self.frame <= 35:
                return np.full(n, 2000, dtype=np.int16), False
            return np.zeros(n, dtype=np.int16), False

    audio = pipeline.record_utterance(SilenceThenLoud(), silence_rms=300)
    quiet_needed = int(0.8 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    expected_frames = 30 + 5 + quiet_needed
    assert len(audio) == expected_frames * pipeline.FRAME_SAMPLES * 2


def test_record_respects_max_length() -> None:
    class AlwaysLoud:
        def read(self, n: int) -> tuple[np.ndarray, bool]:
            return np.full(n, 2000, dtype=np.int16), False

    audio = pipeline.record_utterance(AlwaysLoud(), max_seconds=2.0)
    max_frames = int(2.0 * pipeline.SAMPLE_RATE / pipeline.FRAME_SAMPLES)
    assert len(audio) == max_frames * pipeline.FRAME_SAMPLES * 2


# --- transcribe ---


async def test_transcribe_streams_chunks_and_reads_reply() -> None:
    received: list[bytes] = []

    # untyped ws param on purpose: the connection class moved between
    # websockets versions (WebSocketServerProtocol → ServerConnection)
    async def handler(ws) -> None:  # noqa: ANN001
        async for msg in ws:
            if msg == "END":
                await ws.send(json.dumps({"text": "  hi there  "}))
                break
            received.append(msg)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        payload = b"\x01\x02" * 30000  # bigger than one send chunk
        text = await pipeline.transcribe(payload, f"ws://127.0.0.1:{port}/stt")

    assert text == "hi there"
    assert b"".join(received) == payload
    assert len(received) > 1  # actually chunked, not one blob
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest satellite/tests -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline'`.

- [ ] **Step 3: Create `satellite/pipeline.py`**

```python
"""Shared satellite pipeline: endpointed recording + STT streaming.

Used by both satellite.py (Pi + XVF3800) and fake_satellite.py (Mac mic,
push-to-talk). Keeping this shared is the point: the fake satellite must
exercise the exact code the real one runs.
"""

import json
from typing import Protocol

import numpy as np
import websockets

SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms, openWakeWord's expected frame size


class AudioStream(Protocol):
    """The slice of sounddevice.InputStream we use (duck-typed for tests)."""

    def read(self, frames: int) -> tuple[object, bool]: ...


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


def record_utterance(
    stream: AudioStream,
    *,
    silence_rms: float = 300,
    silence_seconds: float = 0.8,
    max_seconds: float = 12.0,
) -> bytes:
    """Record from trigger until sustained silence (or max length).

    Endpointing only arms after speech is first heard, so leading silence
    doesn't cut the recording short.
    """
    frames: list[bytes] = []
    quiet_frames = 0
    quiet_needed = int(silence_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    max_frames = int(max_seconds * SAMPLE_RATE / FRAME_SAMPLES)
    started = False

    for _ in range(max_frames):
        frame, _ = stream.read(FRAME_SAMPLES)
        frames.append(bytes(frame))
        loud = rms(np.frombuffer(bytes(frame), dtype=np.int16)) >= silence_rms
        if loud:
            started = True
            quiet_frames = 0
        elif started:
            quiet_frames += 1
            if quiet_frames >= quiet_needed:
                break
    return b"".join(frames)


async def transcribe(audio: bytes, stt_url: str) -> str:
    """Stream PCM chunks to the STT server, get the transcript back."""
    async with websockets.connect(stt_url, max_size=None) as ws:
        chunk = FRAME_SAMPLES * 2 * 10  # ~0.8 s per chunk
        for i in range(0, len(audio), chunk):
            await ws.send(audio[i : i + chunk])
        await ws.send("END")
        reply = json.loads(await ws.recv())
    return reply["text"].strip()
```

(One subtle change from the original: `rms(np.frombuffer(bytes(frame), ...))`
— the original converted to `bytes` for the frames list but passed the raw
buffer to `np.frombuffer`; normalizing through `bytes()` keeps fakes simple
and behavior identical for real CFFI buffers.)

- [ ] **Step 4: Slim `satellite/satellite.py` to use the shared pipeline**

Remove `rms`, `record_utterance`, `transcribe`, and the
`SAMPLE_RATE`/`FRAME_SAMPLES`/`SILENCE_*`/`MAX_UTTERANCE_SECONDS` constants
from `satellite.py`; import from `pipeline` instead. The file becomes:

```python
"""Voice satellite — runs on the Pi 5 with the ReSpeaker XVF3800.

Loop: wait for wake word → record until silence → stream audio to the STT
server on the Mac (over Tailscale) → send transcript to the router → speak
the reply with Piper.

The XVF3800 presents as a normal USB audio device; echo cancellation and
beamforming happen on its XMOS chip, so this script just reads clean mono
16 kHz audio.

Usage:
    STT_URL=ws://mac-studio:8100/stt ROUTER_URL=http://mac-studio:8200/route \
        python satellite.py --device 1
"""

import argparse
import asyncio
import os
import subprocess

import numpy as np
import requests
import sounddevice as sd
from openwakeword.model import Model

from pipeline import FRAME_SAMPLES, SAMPLE_RATE, record_utterance, transcribe

STT_URL = os.environ.get("STT_URL", "ws://localhost:8100/stt")
ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8200/route")
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
# MUST be the XVF3800's own playback device (see docs/hardware.md): the AEC
# can only cancel audio it plays itself. e.g. APLAY_DEVICE=plughw:CARD=XVF3800
APLAY_DEVICE = os.environ.get("APLAY_DEVICE", "default")


def speak(text: str) -> None:
    piper = subprocess.Popen(
        ["piper", "--model", PIPER_VOICE, "--output-raw"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    aplay = subprocess.Popen(
        ["aplay", "-D", APLAY_DEVICE, "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
        stdin=piper.stdout,
    )
    piper.stdin.write(text.encode())
    piper.stdin.close()
    aplay.wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index (XVF3800)")
    ap.add_argument("--wake-model", default="hey_jarvis")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    oww = Model(wakeword_models=[args.wake_model])
    print(f"listening for wake word ({args.wake_model})…")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SAMPLES,
        device=args.device,
    ) as stream:
        while True:
            frame, _ = stream.read(FRAME_SAMPLES)
            scores = oww.predict(np.frombuffer(bytes(frame), dtype=np.int16))
            if max(scores.values()) < args.threshold:
                continue

            print("wake word detected, listening…")
            oww.reset()
            audio = record_utterance(stream)
            text = asyncio.run(transcribe(audio, STT_URL))
            print(f"heard: {text!r}")
            if not text:
                continue

            resp = requests.post(ROUTER_URL, json={"text": text}, timeout=60)
            speech = resp.json().get("speech", "")
            print(f"reply: {speech!r}")
            if speech:
                speak(speech)


if __name__ == "__main__":
    main()
```

(Endpointing tuning values now live as `pipeline.record_utterance` defaults;
the Phase 4 on-device tuning pass will thread overrides through as CLI args.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
python3 -m pytest satellite/tests -v
```

Expected: 6 passed.

- [ ] **Step 6: Run the full suite (regression check)**

```bash
python3 -m pytest
```

Expected: 20 passed (11 router + 3 server + 6 pipeline).

- [ ] **Step 7: Commit**

```bash
git add satellite/pipeline.py satellite/satellite.py satellite/tests/test_pipeline.py
git commit -m "refactor: extract shared satellite pipeline (rms, endpointing, transcribe)"
```

---

### Task 5: fake_satellite.py — push-to-talk Mac satellite

**Files:**
- Create: `satellite/fake_satellite.py`
- Test: `satellite/tests/test_fake_satellite.py`

**Interfaces:**
- Consumes: `pipeline.record_utterance`, `pipeline.transcribe`, `pipeline.SAMPLE_RATE`, `pipeline.FRAME_SAMPLES` (Task 4); the router `/route` contract (Task 2).
- Produces: `python3 satellite/fake_satellite.py` — the Phase 1 exit-criterion binary. Exposes `ask_router(text: str) -> str` and `speak(text: str) -> None` at module level (tested; also reusable by Phase 2's timer work).

- [ ] **Step 1: Write the failing tests**

`satellite/tests/test_fake_satellite.py`:

```python
"""fake_satellite unit tests — subprocess and HTTP are mocked."""

from typing import Any
from unittest.mock import MagicMock

import pytest

import fake_satellite


def test_speak_uses_macos_say(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        fake_satellite.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock()
    )
    fake_satellite.speak("hello world")
    assert calls == [["say", "hello world"]]


def test_ask_router_posts_text_and_returns_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: dict[str, Any] = {}

    def fake_post(url: str, json: dict, timeout: int) -> MagicMock:
        posted["url"] = url
        posted["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"speech": "It is 9 PM.", "intent": "llm_fallback"}
        return resp

    monkeypatch.setattr(fake_satellite.requests, "post", fake_post)
    speech = fake_satellite.ask_router("what time is it")
    assert speech == "It is 9 PM."
    assert posted["json"] == {"text": "what time is it"}
    assert posted["url"] == fake_satellite.ROUTER_URL
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest satellite/tests/test_fake_satellite.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'fake_satellite'`.

- [ ] **Step 3: Create `satellite/fake_satellite.py`**

```python
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
import logging
import os
import subprocess

import requests
import sounddevice as sd

from pipeline import FRAME_SAMPLES, SAMPLE_RATE, record_utterance, transcribe

STT_URL = os.environ.get("STT_URL", "ws://localhost:8100/stt")
ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8200/route")

log = logging.getLogger("fake_satellite")


def speak(text: str) -> None:
    """macOS built-in TTS — no Piper needed on the Mac."""
    subprocess.run(["say", text], check=False)


def ask_router(text: str) -> str:
    resp = requests.post(ROUTER_URL, json={"text": text}, timeout=60)
    resp.raise_for_status()
    return resp.json().get("speech", "")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None, help="sounddevice input index")
    ap.add_argument("--silence-rms", type=float, default=300, help="endpointing threshold")
    args = ap.parse_args()

    print("fake satellite — press Enter, speak, pause; Ctrl-C to quit")
    while True:
        try:
            input("⏎ ")
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
        except Exception:
            log.exception("turn failed; listening again")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest satellite/tests -v
```

Expected: 8 passed (6 pipeline + 2 fake_satellite).

- [ ] **Step 5: Commit**

```bash
git add satellite/fake_satellite.py satellite/tests/test_fake_satellite.py
git commit -m "feat: push-to-talk fake satellite for Mac (Phase 1 exit tool)"
```

---

### Task 6: End-to-end bring-up + exit criterion

**Files:**
- Modify: `README.md` (Milestone 1 section: add fake-satellite quickstart, uv venv instructions)

**Interfaces:**
- Consumes: everything above.
- Produces: verified Phase 1 exit criterion; updated README.

- [ ] **Step 1: Full test suite green**

```bash
source .venv/bin/activate
python3 -m pytest
```

Expected: 22 passed.

- [ ] **Step 2: Start the STT server (small model first for fast bring-up)**

```bash
cd server
WHISPER_MODEL=small ../.venv/bin/python -m uvicorn stt_server:app --host 0.0.0.0 --port 8100
```

Expected: uvicorn starts on :8100. First transcription request triggers the
~460 MB `small` model download; that's expected. (Production runs default to
`large-v3` — ~3 GB, downloaded once — after bring-up works.)

- [ ] **Step 3: Start the router (second terminal)**

```bash
cd router
ANTHROPIC_API_KEY=<key> ../.venv/bin/python -m uvicorn router:app --host 0.0.0.0 --port 8200
```

Expected: uvicorn on :8200.

- [ ] **Step 4: Text-level E2E check (no audio yet)**

```bash
curl -s -X POST http://localhost:8200/route \
  -H 'content-type: application/json' \
  -d '{"text": "how tall is the Eiffel Tower"}'
```

Expected: `{"speech":"<one/two sentence answer>","intent":"llm_fallback"}` —
proves router → Claude API works.

- [ ] **Step 5: Audio-level STT check (synthesized speech, no mic)**

Generate a spoken test file with `say`, convert to the raw PCM the protocol
expects, stream it to the websocket:

```bash
cd /Users/willmacdonald/Documents/Code/claude/my-HA
say -o /tmp/stt_test.wav --data-format=LEI16@16000 "testing one two three"
.venv/bin/python3 - <<'EOF'
import asyncio
import sys
import wave

sys.path.insert(0, "satellite")
from pipeline import transcribe

with wave.open("/tmp/stt_test.wav", "rb") as w:
    assert w.getframerate() == 16000 and w.getnchannels() == 1, "unexpected format"
    pcm = w.readframes(w.getnframes())

print(asyncio.run(transcribe(pcm, "ws://localhost:8100/stt")))
EOF
```

Expected output: `testing one two three` (or a near-exact transcription) —
proves the STT websocket path works end-to-end with real whisper inference.

- [ ] **Step 6: The exit criterion (human in the loop — Will speaks)**

```bash
cd satellite
../.venv/bin/python3 fake_satellite.py
```

Press Enter, ask a question out loud ("how tall is the Eiffel Tower?"),
confirm the Mac speaks a sensible answer back.

Expected: **speak a question at the Mac, hear a spoken answer** — Phase 1's
exit criterion from the design doc. If the mic never endpoints (recording
runs the full 12 s), retry with `--silence-rms` raised/lowered; note the
working value in the README.

- [ ] **Step 7: Update README.md**

In the "Milestone 1" section, replace the `pip install` instructions with the
uv venv flow and add the fake satellite between steps 3 and 4:

```markdown
## Setup (Mac)

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r server/requirements.txt -r router/requirements.txt \
    -r satellite/requirements-mac.txt -r requirements-dev.txt
python3 -m pytest   # all green before touching anything
```

**Fake satellite (no Pi needed):** with the STT server and router running,

```bash
cd satellite && ../.venv/bin/python3 fake_satellite.py
```

press Enter, speak, hear the answer through the Mac speakers.
```

Keep the existing Pi instructions; they're Phase 3/4 material.

- [ ] **Step 8: Commit and push**

```bash
git add README.md
git commit -m "docs: Phase 1 quickstart — uv venv setup and fake satellite usage"
git push
```

---

## Verification Summary

| Check | Command | Proves |
|---|---|---|
| Unit suite | `python3 -m pytest` → 22 passed | routing, protocol, endpointing logic |
| Text E2E | `curl POST /route` | router → Claude API |
| Audio E2E | `say`-generated PCM → `/stt` | whisper websocket path |
| Exit criterion | `fake_satellite.py`, spoken Q&A | the whole Phase 1 loop |
