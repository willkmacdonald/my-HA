# Knowledge-Routing Triggers + Topic Extraction — Design

**Date:** 2026-08-10
**Status:** Approved (regex triggers + named-group topic capture; no LLM router)

Addresses Finding 3 (router forwards trigger words as the query) and Finding 4
(knowledge-query grammar too narrow) from the post-Phase-2 findings. Scope is the
my-HA router only — no Open Brain backend changes, no LLM in the routing decision.

## Problem

The voice router routes an utterance to Open Brain only if it matches one of two
narrow patterns — `what did I decide/say/conclude` or `check my/the notes/brain`.
Two problems:

1. **Neither trigger is how the user actually talks to Open Brain.** The user
   consistently addresses it by name — "look for X in my open brain", "ask open
   brain about X". None of those phrasings route today; they fall through to the
   plain-LLM fallback, which answers "I don't have your notes" even when the note
   exists at high relevance. Measured: 8 of 10 natural phrasings miss.
2. **The router forwards the entire utterance verbatim as the search query.**
   `route()` passes the raw `text` to `ask_open_brain`, so "look for the leek
   recipe in my open brain" searches that whole noisy string. The scaffolding
   words ("look for", "in my open brain") pollute Open Brain's hybrid
   (vector + full-text) search and can collapse an otherwise-strong match to zero
   hits (observed 2026-08-09: "check my notes on recipes" → 0.000, while
   "recipes" → 0.9).

## Approach

Pure **regex**, zero added latency, fully deterministic. An LLM classifier was
considered and **measured** before rejection: a routing classification call adds
~1.0 s per turn on Haiku 4.5 and ~2.4 s on Sonnet, on *every* utterance. Because
the user reliably names Open Brain when they mean it, the classifier's only
advantage — catching phrasings that don't name Open Brain — is a case the user
does not hit. The latency cost is not worth it. (The LLM-router remains a
documented future option if the user's phrasing ever drifts; see "Future
direction".)

The knowledge intents are rewritten so each pattern **recognizes the utterance
and extracts the search topic in one step**, via a named `{topic}` capture group —
exactly how the existing device intents capture `{room}`. The extracted topic —
not the raw utterance — becomes both the Open Brain search query and the
synthesis "Question:" hint.

### Triggers (final)

- **Primary — "open brain":** "look for X in my open brain", "search my open
  brain for X", "ask open brain about X", "open brain, what do I have on X",
  "check open brain for X".
- **Secondary — "check my notes/brain":** "check my notes on X".
- **Removed:** `what did I decide/say/conclude` — decision-centric framing that
  mismatched the user's actual notes (recipes, articles, references are *saved*,
  not *decided*).

## Components

### 1. `router/intents.yaml` — `knowledge_query` grammar

Rewritten so each pattern carries a `(?P<topic>...)` named group. The topic can
appear **before** the trigger ("look for **the leek recipe** in my open brain")
or **after** it ("open brain, what do I have on **recipes**"; "check my notes on
**the leek recipe**"), so the pattern set includes a few shapes. All patterns keep
the existing politeness-prefix tolerance
(`(?:please|hey|ok|okay|um|uh)[,.]?`).

The design commitment is: **named-group `{topic}` capture per pattern, both-sides
support, politeness-tolerant.** The exact regexes are finalized during
implementation against the test table below (§Testing) — natural-language
"topic before or after a phrase" is genuinely fiddly for one regex and is best
pinned by TDD, not hand-authored in the design. Starting shapes (illustrative,
not final):

```yaml
- name: knowledge_query
  patterns:
    # topic BETWEEN verb and trigger: "look for <topic> in my open brain"
    - "^<politeness>(?:look for|search|find|get)\\s+(?P<topic>.+?)\\s+(?:in|from|on)\\s+(?:my\\s+)?open brain\\b"
    # topic AFTER "for": "search my open brain for <topic>" / "check open brain for <topic>"
    - "^<politeness>(?:search|check|look in|ask)?\\s*(?:my\\s+)?open brain\\b.*?\\bfor\\s+(?P<topic>.+)"
    # topic AFTER trigger, various lead-ins: "ask open brain about <topic>", "open brain, what do I have on <topic>"
    - "^<politeness>(?:ask\\s+)?open brain\\b[,\\s]+(?:.*?\\b(?:on|about)\\b\\s*)?(?P<topic>.+)?"
    # secondary: "check my notes/brain on <topic>"
    - "^<politeness>check (?:my|the) (?:notes|brain)\\b\\s*(?:on|about|for)?\\s*(?P<topic>.+)?"
  action:
    type: open_brain
```

(`<politeness>` above is shorthand for the existing prefix group; written out in
the real file.)

### 2. `router/router.py` — dispatch

The `open_brain` branch in `route()` extracts the captured topic and passes it
(not the raw `text`) to `ask_open_brain`:

```python
if kind == "open_brain":
    topic = (match.groupdict().get("topic") or "").strip()
    if not topic:
        # trigger fired but no topic captured — clarify, don't search
        return {"speech": "What would you like me to look up?",
                "intent": "knowledge_query"}
    if OPEN_BRAIN_URL:
        return {"speech": await ask_open_brain(topic, state.http, state.anthropic),
                "intent": intent["name"]}
    # documented degradation: no Open Brain configured -> LLM fallback (unchanged)
```

### 3. `router/router.py` — `ask_open_brain`

**No change to its body.** Its signature already takes a `query`; it now receives
the clean topic instead of the raw utterance, and uses it for both the search and
the synthesis "Question:" (per the decision: search + synthesize with the cleaned
topic). The retrieval bug is fixed by cleaning the *input*, not by touching the
search/synthesis logic.

## Behavior

| Case | Behavior |
|---|---|
| Trigger + topic captured | Search the topic → synthesize answer, or the existing "I didn't find anything about that" / "I couldn't reach my notes". |
| Trigger, no topic ("open brain", "check my notes") | Speak "What would you like me to look up?" — **no** Open Brain call. Mirrors the timer path's clarify-on-unparseable, no side effect. |
| `OPEN_BRAIN_URL` unset | LLM fallback (unchanged documented degradation). |
| General question ("what's the weather") | No knowledge match → plain-LLM fallback (unchanged). |
| Existing intents ("set a timer…", "turn on the lights") | Unchanged — knowledge patterns must not steal them (verified by tests). |

## Error handling

- No new error surfaces. `ask_open_brain`'s existing distinct outcomes
  ("didn't find anything" vs "couldn't reach my notes") are preserved.
- Empty-topic clarification is a normal spoken response, not an error.
- The router's never-5xx `route()` contract is unchanged (the whole dispatch
  stays inside the existing `try/except`).

## Out of scope (deliberately)

- **Finding 1 (cold-start ReadTimeout)** — a separate fix (timeout config in
  `ask_open_brain`); bundling it would muddy this change. Stays a distinct finding.
- **Finding 2 (Open Brain hybrid-search zero-collapse)** — a backend bug being
  fixed in the open-brain repo. This design's topic-cleanup is defense-in-depth
  for the voice path but does not fix the backend for other consumers.
- **LLM classifier router** — measured and rejected for latency; see below.

## Future direction (documented, not built)

If the user's phrasing ever drifts away from naming Open Brain, an LLM classifier
router becomes worth revisiting: route with a fast model (Haiku 4.5, ~1 s) for the
non-command bucket only (regex intents like timer/device stay deterministic and
first), synthesize with Sonnet. Measured latency at design time: Haiku ~1.0 s,
Sonnet ~2.4 s added per classified turn. Rejected now because the user reliably
names Open Brain, making the added latency unjustified.

## Testing

Pure unit tests, no network, following `router/tests/test_router.py` patterns
(`match_intent` is a pure function). **The test table is the real spec** for the
fiddly patterns.

**Group A — routing + topic extraction:**

| Utterance | → intent | → topic |
|---|---|---|
| "look for the leek recipe in my open brain" | knowledge_query | "the leek recipe" |
| "search my open brain for the anthropic harness" | knowledge_query | "the anthropic harness" |
| "ask open brain about foundry rate limits" | knowledge_query | "foundry rate limits" |
| "open brain, what do I have on recipes" | knowledge_query | "recipes" |
| "check open brain for the leek recipe" | knowledge_query | "the leek recipe" |
| "check my notes on the leek recipe" | knowledge_query | "leek recipe" |
| "hey, look for the harness article in my open brain" | knowledge_query | "the harness article" |

**Group B — empty-topic clarification (no search):**
"open brain", "check my notes", "check my brain" → clarification response, no HTTP call.

**Group C — must NOT over-capture (safety net):**
"what's the weather" → llm_fallback; "set a timer for 2 minutes" → timer_set;
"turn on the kitchen lights" → lights_on; "don't turn on the lights" → llm_fallback.

**Group D — dispatch end-to-end (mocked Open Brain):**
A captured-topic utterance calls `ask_open_brain` with the **cleaned topic** —
assert the search body is `{"query": "<topic>", "limit": 5}`, not the raw
utterance. The empty-topic case returns the clarification **without** calling the
HTTP client.

**Normalization detail resolved during TDD:** whether the topic keeps a leading
article ("the leek recipe" vs "leek recipe"). Both search fine (0.8–0.9 observed),
so the table pins whatever the natural capture yields; no dedicated
article-stripping. Deterministic once the table is chosen.
