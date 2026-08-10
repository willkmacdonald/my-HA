# Knowledge-Routing Triggers + Topic Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route voice utterances that name "open brain" (or "check my notes/brain") to Open Brain, extracting the search *topic* from the utterance so the noisy trigger words never reach the search query.

**Architecture:** Pure regex, zero added latency. The `knowledge_query` intent in `intents.yaml` is rewritten so each pattern both recognizes the utterance and captures a `(?P<topic>...)` named group — exactly how device intents capture `{room}`. `route()`'s `open_brain` branch passes the captured topic (not the raw `text`) to `ask_open_brain`; an empty topic yields a spoken clarification with no search. `ask_open_brain`'s body is unchanged — it just receives a clean topic.

**Tech Stack:** Python 3.11, FastAPI, PyYAML, pytest + respx (existing router test stack). No new dependencies.

**Spec:** [docs/superpowers/specs/2026-08-10-knowledge-routing-design.md](../specs/2026-08-10-knowledge-routing-design.md)

## Global Constraints

Copied from the spec — every task implicitly includes these:

- **Triggers:** primary "open brain"; secondary "check my notes/brain". The `what did I decide/say/conclude` pattern is **removed**.
- **Topic capture is a named `(?P<topic>...)` group in the pattern itself** — no separate strip-list. Support topic **before** the trigger ("look for X in my open brain") and **after** ("open brain, what do I have on X"; "check my notes on X").
- **Politeness prefix preserved** on every pattern: `(?:(?:please|hey|ok|okay|um|uh)[,.]? )*`.
- **Cleaned topic is used for BOTH** the Open Brain search and the synthesis "Question:" (no separate original-utterance thread).
- **Empty topic → spoken clarification, no Open Brain call.** Clarification text: exactly `"What would you like me to look up?"`, intent `"knowledge_query"`.
- **`ask_open_brain` body must NOT change** — the fix is entirely in what `route()` passes it.
- **Must not over-capture:** timer/device/general utterances must keep routing exactly as they do today. Knowledge patterns sit **after** the timer intents in `intents.yaml` (first-match-wins ordering).
- **Never-5xx `route()` contract preserved** — dispatch stays inside the existing `try/except`.
- **Out of scope:** Finding 1 (cold-start ReadTimeout) and Finding 2 (Open Brain backend hybrid-search bug). Do not touch `ask_open_brain`'s timeout or the open-brain repo.
- Tests: `source .venv/bin/activate && python3 -m pytest` from repo root (176 passing baseline). Lint: `ruff check .` clean. Commit directly to `main` (solo-dev).

## File Structure

- **Modify** `router/intents.yaml` — rewrite the `knowledge_query` intent's `patterns` (topic-capturing); keep it positioned after `timer_*`, before `lights_*`.
- **Modify** `router/router.py` — the `open_brain` branch in `route()` (~lines 150-165): extract `topic`, clarify-if-empty, pass topic to `ask_open_brain`. `ask_open_brain` itself untouched.
- **Modify** `router/tests/test_router.py` — add topic-extraction, empty-topic, over-capture, and dispatch tests.

The knowledge patterns below are **verified working** against the spec's full test table (Groups A/B/C all pass) before this plan was written — they are not guesses. Task 1 is TDD, but the target patterns are known.

---

### Task 1: Topic-capturing `knowledge_query` grammar + `match_intent` extraction tests

**Files:**
- Modify: `router/intents.yaml` (the `knowledge_query` intent)
- Test: `router/tests/test_router.py`

**Interfaces:**
- Consumes: existing `router.match_intent(text) -> tuple[dict, re.Match] | None` (unchanged signature).
- Produces: for a knowledge utterance, `match_intent` returns the `knowledge_query` intent and a `re.Match` whose `.groupdict()["topic"]` holds the extracted topic (or is `None`/absent when no topic was spoken).

- [ ] **Step 1: Write the failing tests**

Append to `router/tests/test_router.py`:

```python
# --- knowledge_query topic extraction (2026-08-10 redesign) ---

import pytest


def _topic(text: str):
    """Route text, return (intent_name, extracted_topic) or (None, None)."""
    m = router.match_intent(text)
    if not m:
        return None, None
    intent, match = m
    return intent["name"], (match.groupdict().get("topic") or "").strip()


@pytest.mark.parametrize(
    ("utterance", "want_topic"),
    [
        ("look for the leek recipe in my open brain", "the leek recipe"),
        ("search my open brain for the anthropic harness", "the anthropic harness"),
        ("ask open brain about foundry rate limits", "foundry rate limits"),
        ("open brain, what do I have on recipes", "recipes"),
        ("check open brain for the leek recipe", "the leek recipe"),
        ("check my notes on the leek recipe", "the leek recipe"),
        ("hey, look for the harness article in my open brain", "the harness article"),
    ],
)
def test_knowledge_query_extracts_topic(utterance: str, want_topic: str) -> None:
    name, topic = _topic(utterance)
    assert name == "knowledge_query", f"{utterance!r} routed to {name}, not knowledge_query"
    assert topic == want_topic


@pytest.mark.parametrize("utterance", ["open brain", "check my notes", "check my brain"])
def test_knowledge_query_bare_trigger_has_empty_topic(utterance: str) -> None:
    name, topic = _topic(utterance)
    assert name == "knowledge_query"
    assert topic == ""


@pytest.mark.parametrize(
    "utterance",
    [
        "what's the weather",
        "set a timer for 2 minutes",
        "turn on the kitchen lights",
        "don't turn on the lights",
        "cancel my timer",
        "how long is left on my timer",
        "what did I decide about the harness",  # decision pattern was removed
    ],
)
def test_knowledge_query_does_not_over_capture(utterance: str) -> None:
    name, _ = _topic(utterance)
    assert name != "knowledge_query", f"{utterance!r} was wrongly stolen by knowledge_query"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python3 -m pytest router/tests/test_router.py -k "knowledge_query_extracts or bare_trigger or over_capture" -v`
Expected: FAIL — the current `knowledge_query` patterns have no `topic` group (`want_topic` mismatches / empty), and "what did I decide about the harness" still matches the old decision pattern (over-capture failure).

- [ ] **Step 3: Rewrite the `knowledge_query` intent in `router/intents.yaml`**

Replace the entire existing `knowledge_query` intent block with this (keep it in the same position — after the `timer_*` intents, before `lights_on`). The `(?:(?:please|hey|ok|okay|um|uh)[,.]? )*` prefix is inlined on each pattern per the file's convention:

```yaml
  - name: knowledge_query
    # 2026-08-10 redesign: primary trigger "open brain", secondary "check my notes/brain".
    # Each pattern captures (?P<topic>...) — the search topic — so trigger scaffolding
    # ("look for", "in my open brain") never reaches the Open Brain search query.
    # Order matters (first match wins): topic-bearing patterns before bare-trigger patterns.
    patterns:
      # topic BETWEEN verb and "open brain": "look for <topic> in/from/on my open brain"
      - "^(?:(?:please|hey|ok|okay|um|uh)[,.]? )*(?:look for|search for|find|get)\\s+(?P<topic>.+?)\\s+(?:in|from|on)\\s+(?:my\\s+|the\\s+)?open brain\\b"
      # topic AFTER "for": "search my open brain for <topic>" / "check open brain for <topic>"
      - "^(?:(?:please|hey|ok|okay|um|uh)[,.]? )*(?:search|check|look in|ask|search in)?\\s*(?:my\\s+|the\\s+)?open brain\\b.*?\\bfor\\s+(?P<topic>.+)"
      # topic AFTER trigger, on/about lead-in optional: "ask open brain about <topic>", "open brain, what do I have on <topic>"
      - "^(?:(?:please|hey|ok|okay|um|uh)[,.]? )*(?:ask\\s+)?(?:my\\s+|the\\s+)?open brain\\b[,\\s]+(?:.*?\\b(?:on|about)\\b\\s+)?(?P<topic>.+)"
      # secondary: "check my/the notes/brain on/about/for <topic>"
      - "^(?:(?:please|hey|ok|okay|um|uh)[,.]? )*check (?:my|the) (?:notes|brain)\\b(?:\\s+(?:on|about|for)\\s+(?P<topic>.+))?"
      # bare triggers (no topic) — matched last so topic-bearing forms win first:
      - "^(?:(?:please|hey|ok|okay|um|uh)[,.]? )*(?:ask\\s+)?(?:my\\s+|the\\s+)?open brain\\s*$"
      - "^(?:(?:please|hey|ok|okay|um|uh)[,.]? )*check (?:my|the) (?:notes|brain)\\s*$"
    action:
      type: open_brain
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source .venv/bin/activate && python3 -m pytest router/tests/test_router.py -k "knowledge_query_extracts or bare_trigger or over_capture" -v`
Expected: all PASS (7 extraction + 3 empty-topic + 7 over-capture).

- [ ] **Step 5: Run the FULL router suite — nothing else regressed**

Run: `python3 -m pytest router/tests/ -v`
Expected: all PASS, including the pre-existing intent tests (`test_match_intent_*`, timer/device collision tests). If any pre-existing knowledge test asserted the old `what did I decide` behavior, it will fail here — the removal is intentional; update or delete that specific assertion and note it in the commit.

- [ ] **Step 6: Lint and commit**

```bash
ruff check router/
git add router/intents.yaml router/tests/test_router.py
git commit -m "feat(router): topic-capturing knowledge_query grammar (open brain primary trigger)"
```

---

### Task 2: Dispatch the extracted topic + empty-topic clarification

**Files:**
- Modify: `router/router.py` (the `open_brain` branch inside `route()`)
- Test: `router/tests/test_router.py`

**Interfaces:**
- Consumes: `match_intent` returning a match with `.groupdict()["topic"]` (from Task 1); `ask_open_brain(query, http, anthropic)` (unchanged signature — receives the topic string).
- Produces: `route()` behavior — topic-bearing knowledge utterance → `ask_open_brain(topic, ...)`; empty-topic knowledge utterance → `{"speech": "What would you like me to look up?", "intent": "knowledge_query"}` with no HTTP call.

- [ ] **Step 1: Write the failing tests**

Append to `router/tests/test_router.py` (the file already imports `respx`, `httpx`, `Response`, `AsyncMock`, and defines the lifespan-aware `client` fixture and the `_FakeAnthropic` helper from Phase 2):

```python
# --- knowledge_query dispatch: cleaned topic reaches search; empty topic clarifies ---

@respx.mock
async def test_open_brain_searches_the_cleaned_topic_not_raw_utterance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    monkeypatch.setattr(router, "OPEN_BRAIN_API_KEY", "k")
    search = respx.post("http://ob.local/api/search").mock(
        return_value=Response(200, json={"thoughts": [
            {"content": "Braised leeks note", "similarity": 0.9, "id": "1"}
        ], "total": 1, "query": "q"})
    )
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="unused"))
    # synthesis LLM: patch the anthropic client the lifespan created
    router.app.state.anthropic = _FakeAnthropic("Here's your leek recipe.")

    with TestClient(router.app) as c:
        resp = c.post("/route", json={"text": "look for the leek recipe in my open brain"})

    assert resp.json()["intent"] == "knowledge_query"
    import json as _json
    body = _json.loads(search.calls[0].request.content)
    assert body == {"query": "the leek recipe", "limit": 5}  # cleaned topic, NOT the full utterance


def test_open_brain_empty_topic_clarifies_without_searching(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If a search were attempted with no Open Brain configured it'd fall to LLM;
    # assert we get the clarification and ask_open_brain is never called.
    called = AsyncMock()
    monkeypatch.setattr(router, "ask_open_brain", called)
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    resp = client.post("/route", json={"text": "open brain"})
    assert resp.json() == {"speech": "What would you like me to look up?",
                           "intent": "knowledge_query"}
    called.assert_not_awaited()
```

(Note: if the first test's anthropic-state patching fights the lifespan, follow the same pattern the Phase 2 `test_ask_open_brain_*` tests use to inject `_FakeAnthropic`; the assertion that matters is the search body equals the cleaned topic.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest router/tests/test_router.py -k "cleaned_topic or empty_topic_clarifies" -v`
Expected: FAIL — `route()` currently passes the raw `text` to `ask_open_brain` (search body would be the full utterance), and there is no empty-topic clarification branch (bare "open brain" currently forwards the whole string).

- [ ] **Step 3: Update the `open_brain` branch in `route()`**

In `router/router.py`, find the `if kind == "open_brain":` branch inside `route()` and replace it with:

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
                # documented degradation: no Open Brain configured -> LLM fallback
```

Do **not** change `ask_open_brain` itself — it already takes a `query` and uses it for both search and synthesis.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest router/tests/test_router.py -k "cleaned_topic or empty_topic_clarifies" -v`
Expected: both PASS.

- [ ] **Step 5: Full suite + lint**

Run: `python3 -m pytest && ruff check .`
Expected: all PASS (176 baseline + the new tests), ruff clean.

- [ ] **Step 6: Commit**

```bash
git add router/router.py router/tests/test_router.py
git commit -m "feat(router): dispatch cleaned topic to Open Brain, clarify on empty topic"
```

---

### Task 3: Docs — README voice examples + findings closure

**Files:**
- Modify: `README.md` (the voice/usage notes)
- Modify: `HANDOFF.md` (mark Findings 3+4 addressed)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Update README voice examples**

In `README.md`, near the fake-satellite / usage section, add a short note on how to query Open Brain by voice (so the trigger phrasing is discoverable):

```markdown
**Ask Open Brain by voice:** name it in the request — e.g. "look for the leek
recipe in my open brain", "ask open brain about X", "open brain, what do I have
on X". "check my notes on X" also works. The router extracts the topic (here,
"the leek recipe") and searches that, so trigger words don't pollute the search.
Say just "open brain" with no topic and it asks what to look up.
```

- [ ] **Step 2: Update HANDOFF findings**

In `HANDOFF.md`'s "Open findings" section, mark Findings 3 and 4 as addressed by this change (reference the spec/plan dates), leaving Findings 1 and 2 open. Keep it to a one-line edit per finding — don't rewrite the section.

- [ ] **Step 3: Commit and (optionally) push**

```bash
git add README.md HANDOFF.md
git commit -m "docs: Open Brain voice trigger examples; mark Findings 3+4 addressed"
```

Push per solo-dev convention when the feature is complete: `git push origin main`.

- [ ] **Step 4: Manual voice check (with Will, optional)**

Not subagent work. With the router (env sourced) + STT + fake satellite running, say "look for the leek recipe in my open brain" → expect the synthesized leek answer (proving topic extraction end-to-end via voice, not just tests). Note: a cold Open Brain container may still first-hit "couldn't reach my notes" — that's the separate open Finding 1, not this change.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** trigger grammar (open brain primary + check-my-notes secondary, decision pattern removed) → Task 1; named-group topic capture, both-sides → Task 1 (verified against the spec's Group A/B/C table before writing); dispatch passes cleaned topic, empty-topic clarification → Task 2; `ask_open_brain` body unchanged → Task 2 (explicit); over-capture safety → Task 1 Group C tests; docs → Task 3; Findings 1/2 out of scope → honored (not touched).
- **Patterns are verified, not placeholder:** the six `knowledge_query` regexes were run against all 17 spec-table cases (7 extraction + 3 empty + 7 over-capture) and all passed before this plan was committed.
- **Type/interface consistency:** `match_intent` signature unchanged; `.groupdict()["topic"]` is the single contract between Task 1 (produces) and Task 2 (consumes); `ask_open_brain(query, http, anthropic)` signature unchanged, now fed the topic string.
- **Known TDD-time detail:** topic keeps its leading article ("the leek recipe") — matches the natural capture; both articled and bare forms search fine (0.8–0.9). Pinned in the Task 1 table.
