# Phase 2: Open Brain + Timers — Design

**Date:** 2026-07-12 (audit-review edits 2026-07-15)
**Status:** Signed off 2026-07-15 (Approach A: router-integrated scheduler + websocket
push; incorporates four Quality Playbook audit-review edits — see "Matching precision
& dispatch safety" and the STT error-frame carry-over)

Implements Phase 2 of the [project roadmap](2026-07-11-project-phases-design.md).
Everything runs on the Mac against the fake satellite; the Pi inherits it all
in Phase 4 unchanged.

## Scope decisions (settled 2026-07-12)

- **Timer family, all in v1:** countdown timers (multiple concurrent),
  one-shot alarms, reminders with spoken text, and **recurring schedules
  limited to presets** — `daily`, `weekdays`, `weekends`, `weekly:<day>`.
  No cron/RRULE grammar.
- **Firing UX: repeat until acknowledged.** Chime + announcement every 30 s,
  max 10 repeats (5 min). Ack = Enter on the fake satellite ("stop" via wake
  word arrives with Phase 4).
- **Open Brain auth: API-key header added to open-brain-web** — Phase 2
  deliberately touches a second repo. Cookie-scripting and MCP/OAuth were
  rejected (fragile / heavyweight respectively).
- **No LLM in the timer path.** Time parsing is deterministic; unparseable
  requests get a spoken clarification, never a guess.
- **Answer synthesis lives in my-HA's router**, not Open Brain — Open Brain
  has no QA endpoint and doesn't grow one for this.

## Architecture

```
                       Mac Studio
┌──────────────────────────────────────────────────────┐
│ router.py (:8200)                                    │
│  POST /route ──► match_intent                        │
│    ├─ type: timer      ──► timers.py handlers        │
│    ├─ type: open_brain ──► search + LLM synthesis ───┼──► open-brain-web
│    └─ fallback         ──► ask_llm                   │    (Azure, X-API-Key)
│                                                      │
│  timers.py                                           │
│   SQLite (timers.db) ◄── scheduler (asyncio task)    │
│                             │ fires                  │
│  ws /events ◄───────────────┘ announce/ack           │
└──────┬───────────────────────────────────────────────┘
       │ persistent websocket (reconnecting)
┌──────┴──────────────┐
│ fake_satellite.py    │  background thread: listen → chime+speak → Enter=ack
│ (satellite.py in P4) │  foreground: push-to-talk loop (unchanged)
└─────────────────────┘
```

## Component 1: Timer store & scheduler (`router/timers.py`)

New module; `router.py` stays thin and dispatches to it.

**Store:** SQLite via `aiosqlite`, file `router/timers.db` (gitignored).

```sql
CREATE TABLE timers (
  id         TEXT PRIMARY KEY,          -- uuid4
  kind       TEXT NOT NULL CHECK (kind IN ('timer','alarm','reminder')),
  text       TEXT,                      -- reminder payload / optional label
  fire_at    TEXT NOT NULL,             -- UTC ISO-8601
  recurrence TEXT NOT NULL DEFAULT 'none',
             -- 'none' | 'daily' | 'weekdays' | 'weekends' | 'weekly:<0-6>'
  duration_seconds INTEGER,             -- countdown timers only
  status     TEXT NOT NULL DEFAULT 'armed',
             -- 'armed' | 'firing' | 'done' | 'cancelled'
  created_at TEXT NOT NULL
);
```

**Scheduler:** one asyncio task, started/stopped by FastAPI lifespan.

- Sleeps until the earliest armed `fire_at`; an `asyncio.Event` wakes it
  early whenever a timer is created/cancelled (no polling).
- On fire: status → `firing`, then the announcement loop: push to satellites,
  repeat every `ANNOUNCE_INTERVAL_S = 30`, stop after
  `ANNOUNCE_MAX_REPEATS = 10` or on ack.
- On ack (or repeat exhaustion): one-shots → `done`; recurring → compute next
  occurrence, status → `armed` with new `fire_at`.
- Announcement speech: timers → "Your N-minute timer is done."; alarms →
  "It's 7 am. This is your alarm."; reminders → "Reminder: <text>."

**Timezone:** parse and speak in local time (`America/Chicago` via
`zoneinfo`); store UTC. Recurrence math happens in local time (a 7 am
weekday alarm stays 7 am across DST changes).

**Restart recovery (exit criterion 1):** on startup, re-read the DB.
Past-due armed items: fire immediately if less than `MISSED_GRACE_S = 300`
stale; older one-shots → `done` (logged); recurring → re-arm next
occurrence. Items stuck in `firing` (router died mid-announcement) are
treated as past-due armed.

## Component 2: Push channel (`ws /events`)

Router endpoint; satellites hold one connection open each, reconnecting
with capped exponential backoff (1 s → 30 s).

Protocol (JSON text frames):

| Direction | Message |
|---|---|
| router → satellite | `{"type": "announce", "event_id": str, "speech": str, "repeat_n": int}` |
| satellite → router | `{"type": "ack", "event_id": str}` |
| router → satellite | `{"type": "ping"}` (keepalive every 20 s; satellite replies `{"type": "pong"}`) |

Announcements broadcast to **all** connected satellites; an ack from any one
stops the repeat loop. Zero connected satellites is not an error — the
scheduler keeps repeating on schedule (satellites may reconnect mid-loop).

## Component 3: Fake satellite rework (`satellite/fake_satellite.py`)

- New background **thread** running its own asyncio loop: connect to
  `EVENTS_URL` (`ws://localhost:8200/events`), reconnect forever, and on
  `announce`: play a chime (`afplay /System/Library/Sounds/Glass.aiff`),
  then `say` the speech.
- A `threading.Event` (`announcement_active`) is set while an event is
  unacknowledged. The foreground loop's Enter press checks it first:
  if set → send ack (via a queue to the listener thread), skip recording;
  else → normal push-to-talk turn.
- `EOFError` from `input()` (closed/redirected stdin) is treated like
  Ctrl-C: stop the listener thread and exit cleanly. It must not fall into
  the generic per-turn handler, which would busy-loop at CPU speed.
- `speak()` gains no changes; chime is separate.
- The Pi's `satellite.py` adopts the same listener in Phase 4 (aplay chime,
  Piper speech, "stop" ack) — out of scope here, protocol designed for it.

## Component 4: Timer intents & time parsing

`intents.yaml` gains action type `timer` with a `verb` field; `router.py`
dispatches those to `timers.py`. New module `router/timeparse.py` holds the
deterministic parser (highest-bug-density code → isolated and unit-tested).

Covered spoken forms (regex patterns with named slots):

- **Set timer:** "set a timer for ten minutes", "twenty minute timer"
- **Set alarm:** "set an alarm for 7 (am)", "wake me (up) at 6:30",
  "set an alarm for 7 am every weekday"
- **Set reminder:** "remind me to <text> at 8 pm (every day)",
  "remind me to <text> in 2 hours"
- **Query:** "how long (is) left on my/the timer", "what timers/alarms
  (do I have)"
- **Cancel:** "cancel the/my timer/alarm/reminder(s)", "cancel my 7 am
  alarm", "cancel all (my) timers"

Parser accepts: digit and word numbers ("10", "ten", "an hour and a half"),
clock times ("7", "7 am", "6:30 pm", "noon", "midnight"), recurrence
phrases ("every day", "every weekday", "on weekends", "every monday").
Bare clock times with no am/pm resolve to the **next occurrence** within
12 hours. Unparseable → spoken clarification, no state change.

Ambiguity rules (explicit): "cancel the timer" with multiple armed timers
cancels the one expiring soonest and says which; "how long left" with
multiple lists all of them.

Matching precision & dispatch safety (audit review, 2026-07-15):

- Timer intents are listed **before** device intents in intents.yaml; all
  command patterns are anchored to the utterance start (allowing optional
  politeness prefixes and trailing punctuation), and the reminder `<text>`
  slot is captured after the verb phrase so embedded command n-grams
  ("…turn on the porch lights…") never re-enter matching.
- A matched intent whose `action.type` (or timer `verb`) has no dispatch
  branch is a config error, not LLM material: log a warning and speak
  "I don't know how to do that yet." At startup the router validates that
  every `action.type`/`verb` declared in intents.yaml has a registered
  handler, and fails fast on mismatch.

## Component 5: Open Brain integration

**open-brain repo (separate, small change):** `/api/search` accepts
`X-API-Key: <key>` as an alternative to the session cookie. Key stored as a
new Key Vault secret (`ob-search-api-key`) surfaced to the container app as
an env var; constant-time comparison; 401 on mismatch as today. Unit tests
follow that repo's patterns. Deploy via the repo's documented
`az acr build` + `az containerapp update` loop. No other endpoint changes.

**my-HA router:** rewrite `ask_open_brain`:

1. `POST {OPEN_BRAIN_URL}/api/search` with header `X-API-Key:
   {OPEN_BRAIN_API_KEY}`, body `{"query": <utterance>, "limit": 5}`.
2. Keep hits with `similarity >= 0.55`. None → "I didn't find anything
   about that."
3. Synthesis: one Claude call — system prompt "Answer the question in one
   or two short spoken sentences using only these notes; if the notes don't
   answer it, say so." with the hits' `content` fields as context.

Config in `.env`: `OPEN_BRAIN_URL` (the Azure Container Apps URL),
`OPEN_BRAIN_API_KEY` (from Key Vault). The knowledge_query intent patterns
in `intents.yaml` stay as-is.

## Carry-over fixes (in scope because Phase 2 touches these files)

- **Module-level async clients** in `router.py`: one `httpx.AsyncClient`
  and one `AsyncAnthropic`, created/closed by lifespan — no more per-turn
  TLS handshakes.
- **`pipeline.transcribe` receive timeout** (`asyncio.timeout(60)`), so a
  wedged STT server can't hang a satellite forever.
- **STT error frame** (pairs with the timeout): `stt_server` wraps
  post-END transcription in try/except and replies
  `{"text": "", "error": "<exception class>"}` before closing;
  `pipeline.transcribe` treats an error reply as an immediate turn
  failure instead of stalling to the deadline. (Touches
  `server/stt_server.py` — accepted scope addition, 2026-07-15.)
- **`py_compile` smoke test** for `satellite.py` in the Mac test suite.

Still deferred (unchanged from roadmap): STT buffer cap, binding to the
Tailscale IP — Phase 4 material.

## Error handling

- Timer creation/cancel/query failures → spoken error via the existing
  never-5xx `route()` contract.
- Open Brain unreachable or non-200 → log + spoken "I couldn't reach my
  notes." (distinct from "found nothing").
- Websocket drops → satellite reconnects with backoff; scheduler is
  connection-agnostic.
- SQLite is WAL-mode; single-writer (the router process) so no contention.

## Testing

- **`timeparse` unit tests:** table-driven over all supported forms +
  rejection cases.
- **Intent-matching collision tests:** the verified mis-route transcripts
  from the 2026-07-15 audit ("what did I decide about the kitchen lights
  on the porch", "don't turn on the kitchen lights", "The lights on my
  dashboard are red, what does that mean", "check my notes about office
  lights off schedule") plus "remind me to turn on the porch lights at 8"
  and "remind me to check my notes at 8 pm" all route to the intended
  intent — no device/knowledge hijacks.
- **Dispatch-guard tests:** an intent with an unknown `action.type`/`verb`
  yields the clarification speech (never `ask_llm`); startup validation
  rejects an intents.yaml declaring an unhandled type.
- **Scheduler tests:** temp DB, `ANNOUNCE_INTERVAL_S` shrunk via
  monkeypatch — fire, repeat, ack-stops-repeats, recurrence re-arm,
  restart recovery (new scheduler instance over the same DB), missed-grace
  handling.
- **Push channel tests:** TestClient websocket — announce delivery, ack
  round-trip, multi-client broadcast.
- **Open Brain tests:** respx-mocked search (real shape:
  `{"thoughts": [{"content", "similarity", ...}]}`) + mocked LLM —
  synthesis path, no-hits path, unreachable path.
- **Fake satellite tests:** listener message handling with a fake ws server
  (same pattern as `test_pipeline.py`); ack queueing.
- **STT error-frame test:** fake whisper model raising → client receives
  the `{"text": "", "error": ...}` reply and `transcribe` surfaces a turn
  failure (no 60 s stall, no bare disconnect).

## Exit criteria

1. **Timers:** "set a timer for 2 minutes" on the fake satellite → chime +
   announcement 2 minutes later, repeating until Enter — **including after
   killing and restarting the router mid-countdown**.
2. **Open Brain:** a real voice query ("what did I decide about the speaker
   driver?") returns a sensible spoken answer synthesized from actual Open
   Brain content, using the deployed open-brain-web with API-key auth.
