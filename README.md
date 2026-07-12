# my-HA — local voice assistant (Alexa replacement)

Wake word → STT → Python router → LLM or local intents → TTS.
No Home Assistant. Full context in [docs/requirements.md](docs/requirements.md).

```
Pi 5 + ReSpeaker XVF3800          Mac Studio (over Tailscale)
┌────────────────────────┐        ┌──────────────────────────────┐
│ satellite.py           │  PCM   │ stt_server.py  (:8100)       │
│  openWakeWord          │ ─────► │  faster-whisper              │
│  record until silence  │  text  │                              │
│  Piper TTS + speaker   │ ◄───── │ router.py      (:8200)       │
└────────────────────────┘        │  intents.yaml → device API   │
                                  │  knowledge    → Open Brain   │
                                  │  fallback     → LLM          │
                                  └──────────────────────────────┘
```

## Layout

- `satellite/` — runs on the Pi: `wakeword_bench.py` (milestone 1 validation),
  `satellite.py` (the full loop)
- `server/` — runs on the Mac: faster-whisper websocket STT
- `router/` — runs on the Mac: the ~100-line router + `intents.yaml`
- `docs/requirements.md` — project context, decisions, milestones

## Milestone 1

### Setup (Mac)

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -r server/requirements.txt -r router/requirements.txt \
    -r satellite/requirements-mac.txt -r requirements-dev.txt
python3 -m pytest   # all green before touching anything
```

1. **Wake word validation** (on the Pi):

   ```bash
   cd satellite && pip install -r requirements.txt
   python wakeword_bench.py --list-devices        # find the XVF3800 index
   python wakeword_bench.py --trials 20 --device N
   ```

   Bare-mic baseline was ~5% detection. Theory validated if this reports >90%.

2. **STT server** (on the Mac):

   ```bash
   cd server && ../.venv/bin/python -m uvicorn stt_server:app --host 0.0.0.0 --port 8100
   ```

   First transcription request triggers a one-time model download (`WHISPER_MODEL=small`
   is ~460 MB, useful for fast bring-up; the default `large-v3` is ~3 GB).

3. **Router** (on the Mac). The Anthropic key lives in a gitignored `.env`
   at the repo root (`ANTHROPIC_API_KEY=…`, sourced from the `wkm-shared-kv`
   Key Vault); the router reads plain env vars, so source it at launch:

   ```bash
   cd router && set -a && source ../.env && set +a && \
       ../.venv/bin/python -m uvicorn router:app --host 0.0.0.0 --port 8200
   ```

   (`OPEN_BRAIN_URL` is unset until Phase 2 — knowledge queries fall back
   to the LLM.)

**Fake satellite (no Pi needed):** with the STT server and router running,

```bash
cd satellite && ../.venv/bin/python3 fake_satellite.py
```

press Enter, speak, hear the answer through the Mac speakers.

4. **Full loop** (on the Pi):

   ```bash
   STT_URL=ws://<mac-tailscale-name>:8100/stt \
   ROUTER_URL=http://<mac-tailscale-name>:8200/route \
   python satellite/satellite.py --device N
   ```

## Tuning notes

- `satellite.py` endpointing (`SILENCE_RMS`, `SILENCE_SECONDS`) needs
  on-device tuning — the XVF3800's processed output has a different noise
  floor than a bare mic.
- Device API URLs in `router/intents.yaml` are placeholders.
- TTS is Piper for now; swap `speak()` for Kokoro if quality beats latency.
