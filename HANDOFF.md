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
| 3 — Board bring-up + wake-word gate | 🔜 **NEXT — unblocked** (board arrived). Hands-on-Pi: confirm box contents, reflash I2S→USB firmware (safe mode — **won't enumerate as USB mic until reflashed**), run `wakeword_bench.py` 20× on **both** capture channels (left processed vs right ASR), wire winner into `satellite.py`, gate **>90% detection**. **Mac-side code prereq:** `wakeword_bench.py` opens `channels=1` (`satellite/wakeword_bench.py:43`) — can't compare the two channels yet; needs a `--capture-channel` flag + a stale-audio drain fix (Phase 1 candidate bug #6) so the >90% number is trustworthy. |
| 4+ | Phase 4 (full loop on Pi) needs Phase 3; Phases 5–7 after. See the roadmap spec. |

## What's next (Phase 2 done; choose per session)

- **Phase 3 — the go/no-go hardware gate.** Highest-leverage: proves the core "fix the mic, not the CPU" bet. Steps in the phase table above. The `wakeword_bench.py` channel-select + drain fix is Mac-side and can be done *before* touching hardware.
- **Quality Playbook resume** ("Run quality playbook phase 2") — agreed for after Phase 2 lands (now true). Audits the new scheduler/websocket/router code; would naturally sweep up Findings 1/3/4 below.

## Open findings (post-Phase-2, root-caused 2026-08-09, NOT yet fixed)

None block progress; all independent of the Open Brain auth (which is proven correct end-to-end). Full detail + fix shapes in `.superpowers/sdd/progress.md`.

1. **Cold-container ReadTimeout (my-HA, MEDIUM).** Router's flat `timeout=30` (`router.py:81`) trips when a scale-to-zero Open Brain container takes >30s on the first response after an idle period → "I couldn't reach my notes." **Recurred twice in one evening** → it's the *common* path for a burst-used assistant (first question after a quiet spell fails), not a rare edge. Warm response is ~1–2.5s. Fix: long read timeout + retry-on-timeout in `ask_open_brain`. **Gotcha:** `httpx.Timeout` needs all 4 params (connect/read/write/pool) or a default — `httpx.Timeout(connect=5, read=60)` raises ValueError.
2. **Open Brain hybrid-search zero-collapse (open-brain repo, MEDIUM — Will fixing there).** "check my notes on recipes" → 0 hits/0.000 while "notes on recipes" → 0.9. Hard zero = mechanical bug in the `match_thoughts` Postgres fn (vector + full-text AND-intersection), not fuzzy ranking. Writeup: `open-brain/.superpowers/sdd/BUG-hybrid-search-zero.md`. Agent is fixing the backend + red tests; my-HA-side query-cleanup (Finding 3) is defense-in-depth, does NOT excuse the backend fix (other consumers — MCP, CLIs — hit the same bug).
3. ✅ **ADDRESSED 2026-08-10 (knowledge-routing plan):** Router forwards its own trigger words as the query (my-HA, Low/UX). Sends "check my notes on recipes" verbatim to `/api/search`. Fix: strip the trigger prefix before forwarding (derive strip-list from `intents.yaml`; skip filter-inference in v1; add retry-with-lower-threshold on zero results). Sidesteps #2 for the voice path.
4. ✅ **ADDRESSED 2026-08-10 (knowledge-routing plan):** Knowledge-query grammar too narrow (my-HA, MEDIUM/UX). Only "what did I decide/say/conclude" and "check my notes/brain" reach Open Brain; **8 of 10 natural phrasings** ("tell me about X", "what do I have on X", "do I have anything about X"…) silently fall to the LLM and answer "I don't know" even when the note exists at 0.9. Design question (widen grammar vs an LLM router) — worth a real brainstorm, not a quick patch. **Interim:** by voice, say "check my notes on \<X\>" or "what did I decide about \<X\>".
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
