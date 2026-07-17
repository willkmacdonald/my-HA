# Session Handoff — my-HA

*Written 2026-07-15. For the next Claude Code session: read this, then confirm
direction with Will before acting.*

## Project in one line

Local voice assistant (Alexa replacement): Pi 5 + ReSpeaker XVF3800 satellite →
Mac Studio backend (faster-whisper STT + Python router → Claude/Open Brain) →
Piper TTS. No Home Assistant. Roadmap:
[docs/superpowers/specs/2026-07-11-project-phases-design.md](docs/superpowers/specs/2026-07-11-project-phases-design.md).

## Status by phase

| Phase | Status |
|---|---|
| 0 — Procurement | ✅ Ordered 2026-07-11: XVF3800 w/ XIAO ESP32-S3 (case version) + Dayton ND91-4 driver. **Check if delivered** — arrival unblocks Phase 3. |
| 1 — Mac backend + fake satellite | ✅ **Complete 2026-07-12.** Both exit criteria passed (spoken Q&A loop works; real-key LLM check works). 23 tests green. |
| 2 — Open Brain + timers | 🔨 **my-HA code complete 2026-07-16** (13 tasks + final-review fixes via subagent-driven-dev; 176 tests green; commits 50b6612..2d51f84). Remaining: (a) open-brain repo companion plan (API-key auth + deploy) — [docs/superpowers/plans/2026-07-16-open-brain-search-api-key.md](docs/superpowers/plans/2026-07-16-open-brain-search-api-key.md), execute in that repo; (b) manual exit criteria with Will: 2-min timer surviving router restart + real Open Brain voice query. |
| 3+ | Blocked on hardware delivery. |

## Immediate next steps (in order)

1. ~~Confirm the Phase 2 spec~~ — **done 2026-07-15**: signed off with four
   audit-review edits (see the spec's "Matching precision & dispatch safety"
   block and the STT error-frame carry-over).
2. ~~Write the implementation plan~~ — **done 2026-07-16**: two plans in
   `docs/superpowers/plans/` — `2026-07-16-phase2-openbrain-timers.md`
   (my-HA, 13 TDD tasks) and `2026-07-16-open-brain-search-api-key.md`
   (open-brain repo: middleware key auth + operator deploy checklist).
3. Execute via `superpowers:subagent-driven-development` (worked well for
   Phase 1: haiku implementers on verbatim-code tasks, sonnet reviewers,
   fable for the final whole-branch review — that final review caught 2 real
   bugs the plan itself contained).

## Phase 2 essentials (detail in the spec)

- **Approach A approved:** timers/alarms/reminders in `router/timers.py`
  (SQLite + asyncio scheduler via lifespan), push to satellites over new
  `ws /events`, repeat announcements 30s×10 until ack. Recurrence presets
  only (daily/weekdays/weekends/weekly:<day>). Deterministic time parser in
  `router/timeparse.py` — no LLM in the timer path.
- **Open Brain integration touches TWO repos:** add `X-API-Key` auth to
  `/api/search` in `/Users/willmacdonald/Documents/Code/claude/open-brain`
  (new Key Vault secret `ob-search-api-key`; deploy via that repo's
  `az acr build` + `az containerapp update` loop — local runs are blocked
  there by a hook). Then my-HA's `ask_open_brain` → real contract:
  `POST {OPEN_BRAIN_URL}/api/search` body `{"query","limit":5}` →
  `{"thoughts":[{"content","similarity",...}]}` (NO `summary` field) →
  filter `similarity >= 0.55` → synthesize spoken answer via one Claude call.
  Production URL: `https://open-brain-web.yellowforest-4e567186.eastus2.azurecontainerapps.io`.
- **Carry-over fixes bundled into Phase 2** (also listed at the bottom of the
  roadmap doc): module-level `httpx.AsyncClient`/`AsyncAnthropic` in router,
  `asyncio.timeout(60)` in `pipeline.transcribe`, `py_compile` smoke test
  for `satellite.py`.

## Environment facts

- Repo-root `.venv` (uv). Tests: `source .venv/bin/activate && python3 -m pytest`
  → 23 passed. Lint: `ruff check .` clean.
- `.env` at repo root (gitignored) holds `ANTHROPIC_API_KEY`, pulled from
  Azure Key Vault `wkm-shared-kv` / secret `anthropic-api-key`. The router
  doesn't load `.env` itself — launch with `set -a && source ../.env && set +a`.
  Phase 2 adds `OPEN_BRAIN_URL` + `OPEN_BRAIN_API_KEY` to it.
- Run commands are in README.md (Setup (Mac) section).
- Subagent-driven-dev ledger: `.superpowers/sdd/progress.md` (gitignored) —
  full Phase 1 execution history.
- Will's security-guard hook blocks `rm -rf` and any git command string
  containing `.env` — word commit messages accordingly.

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
