# Web / Real-Time Tools for the Voice Fallback — Design

**Date:** 2026-08-13
**Status:** Approved (web_search built-in + Open-Meteo weather tool on the LLM fallback path)

Adds real-time answers to my-HA. Today the plain-LLM fallback (`ask_llm`) can only
answer from Claude's training knowledge — "what's the weather this weekend" gets
"I don't have access to real-time information." This design gives that fallback
path two tools so it can answer current questions (weather, news, scores, "is X
open," current facts). Scope is the my-HA router only.

## Problem

`route()` sends any utterance that matches no local intent (timer / lights /
Open Brain) to `ask_llm`, which is a single bare Claude call. It has no way to
fetch anything, so every question needing current or real-world data fails with a
generic "I can't access that." This is a real Alexa gap — weather is a daily
first-reach question.

## Approach

**Tool-enable the existing fallback path — no new intent, no regex changes.**
`ask_llm` becomes a standard Anthropic tool-use loop with two tools, and Claude
decides per-question whether to call either:

- **`web_search`** — Anthropic's **built-in server-side tool**. No provider
  integration, no API key, no client execution: Claude runs the search on
  Anthropic's servers and the results come back in the same response. Handles the
  long tail (news, scores, "is X open," current facts).
- **`get_weather`** — a **custom** tool backed by **Open-Meteo** (free, keyless).
  Claude calls it for weather/forecast questions; it returns structured forecast
  data that Claude turns into a spoken sentence.

A question needing neither tool (e.g. "capital of Japan") is answered directly —
Claude simply doesn't call a tool. Everything currently handled by the fallback
keeps working unchanged; it can now *also* reach for tools.

### Why this shape

- **web_search is built-in** → the broad-coverage half costs one tool
  declaration, not an Exa/Brave integration. (Verified against the claude-api
  skill, 2026-08-13.)
- **Open-Meteo for weather** → precise, structured forecast data (temp high/low,
  precip chance, conditions) beats scraping a weather page, and it's free/keyless
  — so "both" (per the design decision) is cheap to honor.
- **Lives in the fallback path** → the router's command intents (timer/lights/
  Open Brain) stay deterministic and first; only the already-LLM bucket gains
  tools. No routing risk to existing features.

## Components

### 1. `router/weather.py` (new — the one genuinely unit-testable unit)

Pure async function (HTTP mocked in tests, no Claude needed — same isolation
pattern as `satellite/channelpick.py`):

```
async def get_weather(location: str, when: str, http: httpx.AsyncClient) -> dict
```

- **Geocode** `location` → lat/lon via Open-Meteo's geocoding API (keyless):
  `https://geocoding-api.open-meteo.com/v1/search?name=<location>&count=1`.
- **Forecast** for the day(s) implied by `when` ("today" / "tomorrow" /
  "this weekend" / a named day) via
  `https://api.open-meteo.com/v1/forecast?latitude=…&longitude=…&daily=…`.
- **Returns** a structured dict: e.g.
  `{"location": "...", "days": [{"date","high","low","precip_chance","summary"}]}`.
- **On any failure** (unknown location, API error, timeout) returns
  `{"error": "<short reason>"}` — never raises. Claude then speaks a graceful
  "I couldn't get the weather" instead of the turn crashing.
- **Own timeout** (tighter than the router's shared 30s) so a slow Open-Meteo
  can't hang the voice turn.

The exact Open-Meteo `daily=` fields and the `when`→day-range mapping are pinned
during implementation against the test table below.

### 2. `router/router.py` — `ask_llm` becomes a tool-use loop

The current single `messages.create` call becomes the standard tool-use loop:

1. Call Claude (`FALLBACK_MODEL`) with:
   - the built-in web-search tool (server tool block, no function), carrying
     `max_uses: 3` and `user_location` derived from `HOME_LOCATION`,
   - the `get_weather` custom tool definition,
   - the existing `SYSTEM_PROMPT` (one/two short spoken sentences, no markdown) —
     plus `HOME_LOCATION` stated in the prompt so Claude fills `get_weather`'s
     `location` arg with it when no city is named.
2. If `stop_reason == "tool_use"` and the tool is `get_weather` → call
   `weather.get_weather(...)`, append the `tool_result`, loop.
3. `web_search` runs **server-side** — its result arrives in the same response as
   a content block; there is no client execution step for it.
4. When Claude stops calling tools → return the final spoken text.
5. **`max_iterations` cap (3)** prevents a runaway loop; on cap, return whatever
   text exists or a graceful fallback line.

**Pinned API facts (Anthropic docs + a live verification call, 2026-08-13):**
- **Web search tool for Haiku: `{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}`.**
  The `_20260209`/`_20260318` dynamic-filtering variants need Opus/Sonnet-tier;
  the basic `web_search_20250305` is the right one for Haiku 4.5. **VERIFIED LIVE
  2026-08-13:** a real `claude-haiku-4-5` + `web_search_20250305` call searched and
  correctly answered a current-events question (`web_search_requests: 1`,
  `stop_reason: end_turn`). If `FALLBACK_MODEL` is later moved to an Opus/Sonnet-tier
  model, `web_search_20260209+` (dynamic filtering, cheaper on token-heavy searches)
  becomes available — but then note `allowed_callers` (below).
- **`max_uses` caps searches per request** (set to ~3). A search-happy question
  otherwise balloons latency and cost — directly relevant to the latency concern.
- **`user_location` gives web_search the home city as a first-class field** — cleaner
  than relying only on the system prompt: `{"type":"approximate","city":...,"region":...,
  "country":"US","timezone":...}` derived from `HOME_LOCATION`. (Weather still routes to
  the dedicated Open-Meteo tool for precision; `user_location` just localizes generic
  web_search.)
- **web_search errors do not raise** — an error returns HTTP 200 with a
  `web_search_tool_result` block whose `content` is a single **error object**
  (`{"type":"web_search_tool_result_error","error_code":...}`), while success `content`
  is a **list** (empty list = searched, no matches — not an error). The loop branches on
  that shape; it never try/excepts a search error. Error codes: `too_many_requests`,
  `max_uses_exceeded`, `query_too_long`, `unavailable`, etc.
- **`encrypted_content` must be echoed back UNCHANGED across loop turns.** Each
  `web_search_result` carries `encrypted_content`; when the loop appends the assistant's
  content and continues, those blocks must be preserved verbatim — a missing/modified
  `encrypted_content` is a **400 validation error**. (In practice: append `response.content`
  wholesale, never reconstruct it.)
- **`pause_turn`:** a long search turn can stop with `stop_reason: "pause_turn"` — resend
  the assistant message unchanged to continue. The loop must handle this alongside
  `tool_use` (the max_iterations cap also bounds it).
- **web_search is org-gated:** if an admin disabled it in the Console, the request 400s
  ("web search is not enabled"). Degrade gracefully (Claude answers from knowledge).
- **Cost:** web_search is **$10 per 1,000 searches** + token costs; failed searches aren't
  billed. Weather (Open-Meteo) is free. `max_uses: 3` bounds worst-case search cost per turn.
- Custom `get_weather` tool result on error: set `is_error: true` on the `tool_result` so
  Claude adapts.

### 3. Config (`.env`)

Three settings; the path is otherwise self-contained:
- `FALLBACK_MODEL` — default `claude-haiku-4-5`. **Separate** from `LLM_MODEL`
  and `SYNTH_MODEL` so the tool-use path is tuned independently. (Speed-first
  choice; a one-line bump to `claude-sonnet-5` if search judgment/synthesis
  disappoints — which also unlocks the `web_search_20260209` variant.)
- `HOME_LOCATION` — the default city. Used two ways: as the `get_weather`
  `location` default (stated in the system prompt), and as web_search's
  `user_location` for localized generic search. Format TBD at implementation —
  likely `"City, Region, US"` parsed into the `user_location` fields, or a small
  structured env. Pinned during TDD.
- (No key for Open-Meteo or web_search — web_search is billed per-search, not keyed.)

## Behavior

| Utterance | Behavior |
|---|---|
| "what's the weather this weekend" | Claude calls `get_weather(location=HOME_LOCATION, when="this weekend")` → speaks the forecast. |
| "will it rain in Denver tomorrow" | Claude calls `get_weather(location="Denver", when="tomorrow")`. |
| "who won the game last night" / "is the hardware store open" | Claude uses `web_search` → speaks a short answer. |
| "what's the capital of Japan" | No tool → answered directly (unchanged fallback). |
| "set a timer for 2 minutes" / "check my notes on X" | Unchanged — matched by a local intent before the fallback. |

## Error handling (router contract: never 5xx, always return speech)

Every new failure mode degrades to a spoken answer:

- **Open-Meteo down / times out** → `get_weather` returns `{"error":...}` → Claude
  speaks "I couldn't get the weather right now."
- **Unknown location** → geocoding empty → error dict → "I couldn't find that place."
- **web_search error (server-side)** → error *block* (HTTP 200) → Claude answers
  from knowledge or says it couldn't search. No exception.
- **max_iterations hit** → stop, return current text or a graceful line.
- **`HOME_LOCATION` unset + no city said** → Claude asks "which location?"
  (require-a-location, only as a fallback).
- **Anthropic API error mid-loop** → caught by the existing `route()` try/except
  → `ERROR_SPEECH`. Safety net unchanged.

The whole tool loop stays inside `route()`'s existing `try/except`, so the
never-5xx contract holds.

## Honest tradeoff — latency

Tool-use makes the slow path slower. A weather/search answer is now **two Claude
round-trips plus an API call** (Claude decides → tool runs → Claude synthesizes),
realistically **~3–6s** before TTS. This is inherent to tool-use, not a defect —
and it's the same richer-answers-cost-latency reality already tracked for the
general LLM path. Haiku is chosen partly to blunt this. Weather/search will not be
Alexa-snappy; that is accepted.

## Testing

### `weather.py` — full unit tests (HTTP mocked with `respx`, already a dep)

| Case | Assert |
|---|---|
| Geocode a known city | correct lat/lon passed to the forecast call |
| Forecast response parsed | structured dict has temp high/low, precip, conditions |
| "today" vs "tomorrow" vs "this weekend" | selects the right forecast day(s) |
| Unknown location | returns `{"error":...}`, no exception |
| Open-Meteo error / timeout | returns `{"error":...}`, no exception |

### `router.py` tool-use dispatch — mocked Anthropic client

- Claude requests `get_weather` → loop calls `weather.get_weather` with Claude's
  args, feeds the result back, returns final text. Assert the tool was invoked
  with the right `location`/`when`.
- Claude answers with no tool call → returned directly, no tool invoked
  ("capital of Japan" case).
- `stop_reason: "pause_turn"` → loop resends the assistant message unchanged and
  continues (not treated as terminal).
- Assistant content containing `web_search_tool_result` blocks is appended
  **wholesale** on the next loop turn (guards the "echo `encrypted_content`
  unchanged" rule — assert the block is preserved verbatim, not reconstructed).
- `max_iterations` cap → loop terminates gracefully.
- `HOME_LOCATION` is present in the system prompt and in web_search's `user_location`.

### Existing behavior unchanged

- All current router tests pass (timers, Open Brain, lights, plain fallback). The
  plain-fallback tests update to the new tool-enabled call shape but keep their
  behavior (general question → spoken answer).

### Not tested (documented; needs live)

Real Open-Meteo calls and real `web_search` — validated by voice after deploy,
as with every prior feature.

## Out of scope for v1 (deliberately) + backlog

**Rejected / separate concerns:**

- **General-LLM-path latency** (Sonnet on `ask_llm`'s non-tool answers) — a
  separate, already-logged concern. This design picks Haiku for the *fallback/
  tool* path but does not re-architect the broader latency question.
- **A new intent for weather** — rejected; tool-use in the existing fallback is
  simpler and generalizes (news/scores/facts) without grammar work.
- **Streaming the spoken answer** — the satellite speaks the full reply; streaming
  TTS is a separate future latency lever.

**Backlog (not in v1, captured 2026-08-13):**

- **Pre-fetch/cache home weather → near-instant "what's the weather".** v1 does a
  *live* `get_weather` call (~3–6s, the tool-use cost). The Alexa-speed pattern is
  to pre-fetch the *home* forecast on a schedule and serve it from cache, so the
  common "what's the weather" is a cache read, not a live round-trip. **Will's
  intent: anchor at ~5am daily.** Design considerations to settle when built (do
  NOT build in v1):
  - *Staleness:* a 5am-only fetch is morning-old by afternoon — afternoon forecasts
    shift. Likely 5am **plus** a periodic refresh (every ~3–4h) rather than once/day.
  - *Cache-vs-live decision:* serve cache only for the **home** location and only
    when **fresh** (< refresh interval old); a named city or a stale cache falls
    back to the live `get_weather` path this spec builds. So the pre-fetch layers
    *on top of* v1, reusing `weather.py` — v1's live path is the fallback.
  - *Where it lives:* a small scheduler + cache. The router already runs an
    asyncio scheduler in its FastAPI lifespan (for timers) — the weather pre-fetch
    is the same shape and could ride alongside it, or be its own tiny loop.
  - *Scope:* cache today + the next few days so "this weekend" is also instant.
  - This is a clean fast-follow once v1's `weather.py` exists — it's a caching
    layer, not new weather logic.
