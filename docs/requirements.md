# Voice Assistant — Requirements & Context

## Goal

Custom local voice assistant (Alexa replacement) with **no Home Assistant
dependency**. Pipeline: wake word → STT → custom Python router → LLM or local
intents → TTS.

## Architecture (agreed)

| Stage | Component | Where it runs |
|---|---|---|
| Capture + wake word | ReSpeaker XVF3800 USB 4-mic array + openWakeWord | Raspberry Pi 5 |
| STT | faster-whisper | Mac Studio M2 Max 96GB (over Tailscale) |
| Routing | ~100-line Python service (owned/written by Will) | Mac Studio |
| LLM fallback | Claude API / OpenRouter / local MLX | Mac Studio / cloud |
| Knowledge queries | Open Brain MCP server (FastAPI/SQLModel/Postgres) | existing |
| TTS | Piper (fast) or Kokoro (better quality) | Pi or Mac (TBD by latency) |

### Why the XVF3800

Prior wake word failures (bare mic on Pi 5: ~1 detection in 20 tries) were
caused by unprocessed far-field audio, **not** compute. The XVF3800's XMOS
chip does on-board echo cancellation + beamforming and presents clean audio
over USB. Key lesson: **audio front end matters more than processor.**

## Decisions made

- **No Home Assistant** — too much overhead for one pipeline; the routing
  logic is Python we own.
- **No custom silicon** for v1 — Tiny Tapeout shelved as a separate learning
  project. Possible later phase: custom PCB (ESP32-S3 + MEMS mic array +
  XMOS front end, KiCad) as a father-daughter hardware project with a
  woodworked enclosure. Not in scope.
- Speaker driver volume, not electronics, sets minimum enclosure size.

## Router behavior (the part to own)

```
transcript
  ├─ matches local intent (lights on/off, …) → call device API directly
  ├─ knowledge query → Open Brain ("what did I decide about Foundry rate limits")
  └─ else → LLM of choice (Claude API / OpenRouter / local MLX)
```

## Milestone 1 (current)

1. **Validate the theory:** Pi 5 + XVF3800 + openWakeWord — confirm wake
   word reliability jumps to near-100% (from ~5% with the bare mic).
2. Build the streaming path: Pi → faster-whisper on Mac over Tailscale.
3. Python router skeleton.
