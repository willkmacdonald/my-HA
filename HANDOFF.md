# Session Handoff — my-HA

*Written 2026-07-15, updated 2026-08-09. For the next Claude Code session: read this,
then confirm direction with Will before acting. The roadmap-of-record is
[docs/superpowers/specs/2026-07-11-project-phases-design.md](docs/superpowers/specs/2026-07-11-project-phases-design.md);
full execution detail is in `.superpowers/sdd/progress.md`.*

## Project in one line

Local voice assistant (Alexa replacement): Pi 5 + ReSpeaker XVF3800 satellite →
Mac Studio backend (faster-whisper STT + Python router → Claude/Open Brain) →
Piper TTS. No Home Assistant. Roadmap:
[docs/superpowers/specs/2026-07-11-project-phases-design.md](docs/superpowers/specs/2026-07-11-project-phases-design.md).

## Status by phase

| Phase | Status |
|---|---|
| 0 — Procurement | ✅ Ordered 2026-07-11: XVF3800 w/ XIAO ESP32-S3 (case version) + Dayton ND91-4 driver. **Board DELIVERED 2026-08-09** — Phase 3 unblocked. |
| 1 — Mac backend + fake satellite | ✅ **Complete 2026-07-12.** Both exit criteria passed (spoken Q&A loop works; real-key LLM check works). |
| 2 — Open Brain + timers | ✅ **COMPLETE 2026-08-09.** 176 tests. open-brain X-API-Key auth built + security-reviewed + deployed to prod (open-brain `37c9f62`, via that repo's GitHub Actions Deploy workflow), key in `wkm-shared-kv`/`ob-search-api-key`, my-HA `.env` wired + leak-guarded. **Both exit criteria PASSED live via fake satellite:** (a) "check my notes on the leek recipe" → real synthesized answer from an actual note; (b) 2-min timer survived a router kill+restart mid-countdown, still fired over an auto-reconnected websocket. *(4 non-blocking follow-up findings — see "Open findings" below.)* |
| 3 — Board bring-up + wake-word gate | ✅ **GATE PASSED 2026-08-13.** Flashed I2S→USB 2-ch firmware (v2.0.10) via `dfu-util` in Safe Mode on the **XMOS port** (by the 3.5mm jack — NOT the ESP32 port; that was the gotcha). Board now enumerates as USB "reSpeaker XVF3800 4-Mic Array" (2ch@16k, device index 1 on the Mac). `wakeword_bench.py --capture-channel {left,right}` "hey jarvis": **both channels 20/20 (100%)** (median score ~0.89) vs ~5% bare-mic baseline. Channel wired into `satellite.py` + shared `pipeline.py` (`--capture-channel`, default **right**/ASR-tuned). Full flash procedure + `save_configuration` brick warning: [docs/hardware.md](docs/hardware.md). |
| 4+ | Phase 4 (full loop on Pi) needs Phase 3; Phase 5 (enclosure + a week of daily use), then **Phase 6 — Conversational paths (NEW, added 2026-08-12)**, then parked Phase 7 (music) / Phase 8 (ESP32 v2). See the roadmap spec. |

**Shipped 2026-08-12 (not in the phase table above):**
- **Knowledge-query latency halved (~4s → ~1.8s).** `ask_open_brain` synthesizes with Haiku (`SYNTH_MODEL=claude-haiku-4-5-20251001`); open-ended `ask_llm` fallback keeps Sonnet (`LLM_MODEL=claude-sonnet-5`). Both in `.env`. Measured: Haiku synth ~0.9s vs Sonnet ~2.6s, and Haiku's answer is tighter for speech. Rerank left alone (cheap, inside the ~1.2s search). **Takes effect on router restart.** Committed on `main`.
- **Render surface ("send it to my phone") split out to Second Brain.** Voice renders retrieved content as rich HTML on demand → writes to a single fixed Azure-web-app URL the user keeps as a phone tab. **Not a my-HA build item** (summary spec handed to the Second Brain repo). No Open Brain↔Second Brain data dependency. my-HA owns the *conversational* side (Phase 6).

## What's next (roadmap replanned 2026-08-12)

**Phase 4 — full loop on the Pi — is NEXT.** Will's sequencing (2026-08-12): hardware bring-up →
wake word + >90% gate (both ✅ **done 2026-08-13**) → **full loop + live with it a while (Phase 4/5)**
→ *then* conversational (Phase 6). Conversational is deliberately gated on 3–5, because how
conversation should *feel* is only knowable after daily use — do NOT start designing or building
it before Phase 5.

- **Phase 4 — full loop on the Pi.** The bench + channel wiring are validated on the **Mac** (board
  over USB, device index 1). Not yet run on the **Pi over Tailscale**. Phase 4 = `satellite.py` on
  the Pi against the real Mac backend: closed-loop audio (`APLAY_DEVICE` → the XVF3800, never the
  Pi's own out — see hardware.md), on-device endpointing tuning, barge-in, timer push on the real
  satellite, and measuring USB power draw at speaker peaks. Note `satellite.py` defaults to
  `--capture-channel right` (ASR); A/B against real Whisper transcription is a natural Phase 4 task.
  - **Pi state (2026-08-13):** it previously ran **Home Assistant** (the platform my-HA replaces) —
    abandoned, being **fresh-flashed**, not refreshed. Decision: flash **full Raspberry Pi OS (with
    desktop)**, NOT Lite. Reason (settled after Will corrected an earlier misframing): the attached
    screen is **NOT** a substitute for the portable phone visual-surface (that stays Second Brain's,
    and stays on the phone *because it's portable* — the desk-mounted Pi can't serve that need). The
    full OS is kept only for a possible **ambient status display** — stationary, voice-adjacent
    glances: timer counting down, "listening…", last transcription, now-playing (Phase 7). Voice-only
    is still built first; the desktop image just doesn't foreclose that status UI. Pi-side needs:
    Python + venv, `satellite/requirements.txt` (openwakeword, sounddevice, …), Piper TTS, Tailscale
    (reach the Mac backend). The phone/Second-Brain surface is unaffected by this — different device,
    different job.
- **Quality Playbook resume** ("Run quality playbook phase 2") — agreed for after Phase 2 lands (now true). Audits the new scheduler/websocket/router code; would naturally sweep up remaining Findings 2/5/6 below.

## Open findings (post-Phase-2, root-caused 2026-08-09, NOT yet fixed)

None block progress; all independent of the Open Brain auth (which is proven correct end-to-end). Full detail + fix shapes in `.superpowers/sdd/progress.md`.

1. ✅ **RESOLVED OPERATIONALLY 2026-08-12 (keep-warm ping).** Cold-container ReadTimeout (my-HA, was MEDIUM). Router's flat `timeout=30` (`router.py:81`) tripped when a scale-to-zero Open Brain container took >30s on the first response after an idle period → "I couldn't reach my notes." **Fix shipped is operational, not code:** a **Synology Task Scheduler cron pings `GET /health` on `open-brain-web` every 1 minute, all day** (`/health` is a public no-auth path, so the ping resets the 5-min scale-to-zero cooldown and the container never goes cold). Confirmed working: passive replica-age monitoring + a warm `/api/search` at ~1.4s. **Gotcha that cost time:** the Synology task's "Last run time" was initially `00:59` (ran only during the midnight hour) — must be **`23:59`** to run all day. **If voice queries ever start failing again with "I couldn't reach my notes," first check that Synology task is enabled and its schedule is 00:00–23:59.** A code hardening (long read timeout + retry-on-timeout in `ask_open_brain`) remains a reasonable belt-and-suspenders if the ping ever lapses, but is no longer urgent. (Gotcha for that future code: `httpx.Timeout` needs all 4 params (connect/read/write/pool) or a default — `httpx.Timeout(connect=5, read=60)` raises ValueError.)
2. **Open Brain hybrid-search zero-collapse (open-brain repo, MEDIUM — Will fixing there).** "check my notes on recipes" → 0 hits/0.000 while "notes on recipes" → 0.9. Hard zero = mechanical bug in the `match_thoughts` Postgres fn (vector + full-text AND-intersection), not fuzzy ranking. Writeup: `open-brain/.superpowers/sdd/BUG-hybrid-search-zero.md`. Agent is fixing the backend + red tests; my-HA-side query-cleanup (Finding 3) is defense-in-depth, does NOT excuse the backend fix (other consumers — MCP, CLIs — hit the same bug).
3. ✅ **ADDRESSED + VOICE-VERIFIED 2026-08-10 (knowledge-routing feature shipped):** Router forwards its own trigger words as the query (my-HA, Low/UX). Sends "check my notes on recipes" verbatim to `/api/search`. Fix: strip the trigger prefix before forwarding (derive strip-list from `intents.yaml`; skip filter-inference in v1; add retry-with-lower-threshold on zero results). Sidesteps #2 for the voice path.
4. ✅ **ADDRESSED + VOICE-VERIFIED 2026-08-10 ("open brain" is now the primary trigger; leek-recipe query answered live):** Knowledge-query grammar too narrow (my-HA, MEDIUM/UX). Only "what did I decide/say/conclude" and "check my notes/brain" reach Open Brain; **8 of 10 natural phrasings** ("tell me about X", "what do I have on X", "do I have anything about X"…) silently fall to the LLM and answer "I don't know" even when the note exists at 0.9. Design question (widen grammar vs an LLM router) — worth a real brainstorm, not a quick patch. **Interim:** by voice, say "check my notes on \<X\>" or "what did I decide about \<X\>".
5. **Search-based knowledge routing can't answer count/aggregate questions (my-HA/design, Low).** "how many recipes do I have?" → Open Brain `/api/search` returns matching documents (limit 5), not a COUNT — so an aggregate query gets a confidently-wrong answer based on top hits. Options later: an Open Brain count/list endpoint, or detect aggregate phrasing and speak "I can look things up but can't count them yet." Detail in `.superpowers/sdd/progress.md`.
6. **Trailing trigger phrasing not yet supported (my-HA, Low/UX).** The new grammar requires "open brain" in structural positions (after "look for X in", "ask ... about", etc.). A TRAILING "...in my open brain?" after an arbitrary question (e.g. "can you tell me how many recipes there are in my open brain?") does NOT match → falls to LLM. This is a real phrasing Will used live. A grammar-widening follow-up could add a trailing-trigger catch-all. Detail in `.superpowers/sdd/progress.md`.

## Phase 2 essentials (detail in the spec)

- **Approach A approved:** timers/alarms/reminders in `router/timers.py`
  (SQLite + asyncio scheduler via lifespan), push to satellites over new
  `ws /events`, repeat announcements 30s×10 until ack. Recurrence presets
  only (daily/weekdays/weekends/weekly:<day>). Deterministic time parser in
  `router/timeparse.py` — no LLM in the timer path.
- **Open Brain integration (DONE, touched two repos):** `X-API-Key` auth on
  `/api/search` in the open-brain repo (Key Vault secret `ob-search-api-key`,
  fail-fast if absent). my-HA's `ask_open_brain` calls the real contract:
  `POST {OPEN_BRAIN_URL}/api/search` header `X-API-Key`, body `{"query","limit":5}` →
  `{"thoughts":[{"content","similarity",...}]}` (NO `summary` field) →
  filter `similarity >= 0.55` → one Claude synthesis call.
  Prod URL: `https://open-brain-web.yellowforest-4e567186.eastus2.azurecontainerapps.io`.
  **DEPLOY = the open-brain repo's GitHub Actions "Deploy" workflow (target `web`),
  NOT local `az acr build`/`az containerapp update`** — the latter bypasses the prod
  gate and is against that repo's ROADMAP policy (its own plan docs say otherwise but
  are stale). Note the auth was NOT in place at the time this line was first written;
  it is now.
- **Carry-over fixes bundled into Phase 2** (also listed at the bottom of the
  roadmap doc): module-level `httpx.AsyncClient`/`AsyncAnthropic` in router,
  `asyncio.timeout(60)` in `pipeline.transcribe`, `py_compile` smoke test
  for `satellite.py`.

## Environment facts

- Repo-root `.venv` (uv). Tests: `source .venv/bin/activate && python3 -m pytest`
  → **176 passed**. Lint: `ruff check .` clean (`.claude/` excluded via pyproject).
- `.env` at repo root (gitignored) now holds **`ANTHROPIC_API_KEY`, `OPEN_BRAIN_URL`,
  `OPEN_BRAIN_API_KEY`** — all from Key Vault `wkm-shared-kv`. The router doesn't
  load `.env` itself — launch with `set -a && source ../.env && set +a`.
- Run commands are in README.md (Setup (Mac) section). Router `:8200`, STT `:8100`.
  STT default model is `large-v3` (~3 GB, lazy first-download); `WHISPER_MODEL=small`
  for fast bring-up.
- Subagent-driven-dev ledger: `.superpowers/sdd/progress.md` (gitignored) —
  full Phase 1 **and Phase 2** execution history + all findings.
- **Secrets are triple-guarded:** `.env` gitignored + never committed + a repo
  pre-commit hook (`.git/hooks/pre-commit`, blocks `.env` and credential-shaped
  strings) + Will's global security-guard hook (blocks `rm -rf` and any git command
  string containing `.env`; also blocks `git checkout --` — use `git stash`).
  **Never put `.env` in a git command.**

## Tooling notes

- `quality-playbook` plugin v1.5.8 installed from the official marketplace
  (`andrewstellman/quality-playbook`) — `/quality-playbook` works in any
  fresh session. Agreed timing: most valuable **after Phase 2 lands**
  (audit the scheduler/websocket code once it exists). Playbook Phase 1
  (exploration) already ran 2026-07-15 on this machine — findings live in
  the gitignored `quality/` tree (9 candidate bugs; 4 promoted into the
  Phase 2 spec). Resume after Phase 2 with "Run quality playbook phase 2".
- Workflow: superpowers plugin (v6.1.1) — brainstorm → spec → writing-plans →
  subagent-driven-development, specs/plans under `docs/superpowers/`.
- Solo-dev git: commit directly to `main`, push after meaningful units.

## Watch-outs discovered the hard way

- The XVF3800 XIAO board ships with **I2S firmware and won't enumerate over
  USB** until reflashed via safe mode — first hardware task in Phase 3.
- All TTS/audio out must route **through the XVF3800** (AEC reference) —
  see docs/hardware.md's closed-loop rule.
- `fake_satellite.py`'s blocking `input()` loop can't hold the Phase 2 push
  websocket — spec resolves this with a background listener thread; don't
  redesign it ad hoc.
