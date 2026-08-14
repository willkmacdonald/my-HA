"""Router tests: intent matching, dispatch, fallbacks, error handling.

External calls (device APIs, Open Brain, Anthropic) are mocked — these tests
run offline with no API keys.
"""

from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

import router


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Lifespan-aware test client with an isolated timer DB."""
    monkeypatch.setenv("TIMERS_DB", str(tmp_path / "timers.db"))
    with TestClient(router.app) as c:
        yield c


# --- spoken_text: response parsing robust to adaptive thinking ---


def _block(type_: str, text: str = ""):
    """A minimal stand-in for an anthropic content block (duck-typed)."""
    from types import SimpleNamespace

    return SimpleNamespace(type=type_, text=text)


def _msg(*blocks):
    from types import SimpleNamespace

    return SimpleNamespace(content=list(blocks))


def test_spoken_text_plain_text_block() -> None:
    assert router.spoken_text(_msg(_block("text", "hello"))) == "hello"


def test_spoken_text_skips_leading_thinking_block() -> None:
    """Sonnet 5 defaults to adaptive thinking: a non-trivial prompt yields
    [thinking, text]. spoken_text must return the text, not choke on content[0]
    (the live bug: content[0].text on a ThinkingBlock -> AttributeError -> the
    whole turn errored to ERROR_SPEECH)."""
    msg = _msg(_block("thinking"), _block("text", "the real answer"))
    assert router.spoken_text(msg) == "the real answer"


def test_spoken_text_raises_when_no_text_block() -> None:
    with pytest.raises(ValueError, match="no text block"):
        router.spoken_text(_msg(_block("thinking")))


# --- match_intent (pure function) ---


def test_match_intent_lights_on() -> None:
    matched = router.match_intent("turn on the kitchen lights")
    assert matched is not None
    intent, match = matched
    assert intent["name"] == "lights_on"
    assert match.groupdict()["room"] == "kitchen"


def test_match_intent_knowledge_query() -> None:
    matched = router.match_intent("check my notes on Foundry rate limits")
    assert matched is not None
    intent, _ = matched
    assert intent["name"] == "knowledge_query"


def test_match_intent_no_match_returns_none() -> None:
    assert router.match_intent("what's the weather on Mars") is None


# --- /route dispatch ---


def test_unmatched_text_falls_back_to_llm(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="It's red and dusty."))
    resp = client.post("/route", json={"text": "what's the weather on Mars"})
    assert resp.status_code == 200
    assert resp.json() == {"speech": "It's red and dusty.", "intent": "llm_fallback"}


def test_llm_fallback_survives_thinking_block_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full /route -> ask_llm path with a real (fake) Anthropic client that
    returns [thinking, text]. This is the exact live failure: an unmatched
    utterance ('look in open brand for a recipe for leaks') made Sonnet 5 think
    first, and content[0].text raised -> ERROR_SPEECH. Must now speak the text
    and disable thinking on the call."""
    with TestClient(router.app) as c:
        # Patch after lifespan startup (it installs a real client on app.state).
        router.app.state.anthropic = _FakeAnthropic(
            "I don't have that app, but here's a recipe.", lead_thinking=True
        )
        resp = c.post("/route", json={"text": "look in open brand for a recipe for leaks"})
    body = resp.json()
    assert body == {
        "speech": "I don't have that app, but here's a recipe.",
        "intent": "llm_fallback",
    }
    assert router.app.state.anthropic.calls[0]["thinking"] == router.THINKING_OFF


def test_timing_opt_in_adds_router_ms(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """?timing=1 adds timing_ms.router (a number) for latency diagnosis."""
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="It's red and dusty."))
    resp = client.post("/route?timing=1", json={"text": "what's the weather on Mars"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["speech"] == "It's red and dusty."
    assert body["intent"] == "llm_fallback"
    assert isinstance(body["timing_ms"]["router"], (int, float))
    assert body["timing_ms"]["router"] >= 0


def test_timing_absent_by_default(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ?timing, the response carries no timing_ms — keeps the lean
    production shape and the exact-equality contract other tests rely on."""
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="It's red and dusty."))
    resp = client.post("/route", json={"text": "what's the weather on Mars"})
    assert "timing_ms" not in resp.json()


def test_knowledge_intent_without_open_brain_falls_back_to_llm(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "")
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="You decided X."))
    resp = client.post("/route", json={"text": "check my notes on Foundry"})
    assert resp.json() == {"speech": "You decided X.", "intent": "llm_fallback"}


def test_knowledge_intent_with_open_brain_routes_there(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    monkeypatch.setattr(router, "ask_open_brain", AsyncMock(return_value="You chose Piper."))
    resp = client.post("/route", json={"text": "check my notes on TTS"})
    assert resp.json() == {"speech": "You chose Piper.", "intent": "knowledge_query"}


def test_device_intent_dispatches_to_device_action(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        router, "run_device_action", AsyncMock(return_value="Okay, kitchen lights on.")
    )
    resp = client.post("/route", json={"text": "turn on the kitchen lights"})
    assert resp.json() == {"speech": "Okay, kitchen lights on.", "intent": "lights_on"}


def test_llm_failure_returns_spoken_error_not_500(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "ask_llm", AsyncMock(side_effect=RuntimeError("api down")))
    resp = client.post("/route", json={"text": "hello there"})
    assert resp.status_code == 200
    assert resp.json() == {"speech": router.ERROR_SPEECH, "intent": "error"}


# --- action executors ---


@respx.mock
async def test_run_device_action_calls_url_with_slots() -> None:
    route_mock = respx.post("http://lights.local/api/kitchen/on").mock(return_value=Response(200))
    matched = router.match_intent("turn on the kitchen lights")
    assert matched is not None
    intent, match = matched
    async with httpx.AsyncClient() as http:
        speech = await router.run_device_action(intent, match, http)
    assert route_mock.called
    assert speech == "Okay, kitchen lights on."


@respx.mock
async def test_device_action_http_error_returns_spoken_error_not_success(client) -> None:
    respx.post("http://lights.local/api/kitchen/on").mock(return_value=Response(500))
    resp = client.post("/route", json={"text": "turn on the kitchen lights"})
    assert resp.status_code == 200
    assert resp.json() == {"speech": router.ERROR_SPEECH, "intent": "error"}


def _ob_payload(*sims: float) -> dict:
    return {
        "thoughts": [
            {
                "content": f"note {i} content",
                "similarity": s,
                "id": f"id-{i}",
                "created_at": "2026-07-01T00:00:00Z",
            }
            for i, s in enumerate(sims)
        ],
        "total": len(sims),
        "query": "q",
    }


class _FakeAnthropic:
    """Duck-typed messages.create capturing the synthesis call.

    Mirrors the real API's content shape: with lead_thinking=True the response
    is [thinking, text] (what Sonnet 5's adaptive thinking emits for anything
    non-trivial), so tests exercise the same block structure the router parses
    in production — not a convenient single-text-block fiction."""

    def __init__(self, reply: str, *, lead_thinking: bool = False) -> None:
        self.calls: list[dict] = []
        self._reply = reply
        self._lead_thinking = lead_thinking
        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                from types import SimpleNamespace

                blocks = []
                if outer._lead_thinking:
                    blocks.append(SimpleNamespace(type="thinking", thinking="hmm"))
                blocks.append(SimpleNamespace(type="text", text=outer._reply))
                return SimpleNamespace(content=blocks)

        self.messages = _Messages()

    async def close(self) -> None:
        """No-op: satisfies lifespan teardown when injected into app.state."""


@respx.mock
async def test_ask_open_brain_synthesizes_from_filtered_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    monkeypatch.setattr(router, "OPEN_BRAIN_API_KEY", "test-key")
    search = respx.post("http://ob.local/api/search").mock(
        return_value=Response(200, json=_ob_payload(0.9, 0.6, 0.3))
    )
    fake = _FakeAnthropic("You picked the ND91-4 driver.")
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("speaker choice", http, fake)

    assert speech == "You picked the ND91-4 driver."
    req = search.calls[0].request
    assert req.headers["X-API-Key"] == "test-key"
    import json as _json

    assert _json.loads(req.content) == {"query": "speaker choice", "limit": 5}
    prompt_text = fake.calls[0]["messages"][0]["content"]
    assert "note 0 content" in prompt_text and "note 1 content" in prompt_text
    assert "note 2 content" not in prompt_text  # similarity 0.3 < 0.55 filtered
    assert fake.calls[0]["thinking"] == router.THINKING_OFF  # no thinking latency on the voice path


@respx.mock
async def test_ask_open_brain_survives_thinking_block_in_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: SYNTH_MODEL may return [thinking, text]. The synthesized
    answer must be the text block, not content[0] (which would be the thinking
    block and used to raise AttributeError -> ERROR_SPEECH)."""
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/api/search").mock(return_value=Response(200, json=_ob_payload(0.9)))
    fake = _FakeAnthropic("Braise the leeks in butter.", lead_thinking=True)
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("leek recipe", http, fake)
    assert speech == "Braise the leeks in butter."


@respx.mock
async def test_ask_open_brain_no_hits_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/api/search").mock(return_value=Response(200, json=_ob_payload(0.2)))
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("nothing", http, _FakeAnthropic("x"))
    assert speech == "I didn't find anything about that."


@respx.mock
async def test_ask_open_brain_unreachable_distinct_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/api/search").mock(side_effect=httpx.ConnectError("boom"))
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("anything", http, _FakeAnthropic("x"))
    assert speech == "I couldn't reach my notes."


@respx.mock
async def test_ask_open_brain_401_treated_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/api/search").mock(return_value=Response(401))
    async with httpx.AsyncClient() as http:
        speech = await router.ask_open_brain("anything", http, _FakeAnthropic("x"))
    assert speech == "I couldn't reach my notes."


# --- lifespan wiring ---


def test_lifespan_creates_shared_clients_and_scheduler(client) -> None:
    assert isinstance(router.app.state.http, httpx.AsyncClient)
    assert router.app.state.store is not None
    assert router.app.state.scheduler is not None


def test_events_websocket_accepts_connection(client) -> None:
    with client.websocket_connect("/events"):
        pass  # connect + clean close is the assertion


# --- intent-matching collision tests (spec §Testing, audit 2026-07-15) ---


@pytest.mark.parametrize(
    ("text", "expected_intent"),
    [
        ("look for the kitchen lights schedule in my open brain", "knowledge_query"),
        ("check my notes about office lights off schedule", "knowledge_query"),
        ("remind me to turn on the porch lights at 8", "timer_set"),
        ("remind me to check my notes at 8 pm", "timer_set"),
        ("set a timer for 10 minutes", "timer_set"),
        ("cancel all my timers", "timer_cancel"),
        ("how long is left on my timer", "timer_query"),
        ("turn on the kitchen lights", "lights_on"),
        ("kitchen lights off", "lights_off"),
        ("Okay, set a timer for 10 minutes.", "timer_set"),
        ("Hey, cancel my timer.", "timer_cancel"),
    ],
)
def test_intent_routing_precision(text: str, expected_intent: str) -> None:
    matched = router.match_intent(text)
    assert matched is not None, f"expected {expected_intent}, got no match"
    assert matched[0]["name"] == expected_intent


@pytest.mark.parametrize(
    "text",
    [
        "don't turn on the kitchen lights",  # negation -> LLM
        "The lights on my dashboard are red, what does that mean",  # article slot -> LLM
        "turn on the living room lights",  # multi-word room (known gap)
    ],
)
def test_non_commands_fall_through_to_llm(text: str) -> None:
    assert router.match_intent(text) is None


# --- dispatch guard (spec: Matching precision & dispatch safety) ---


def test_validate_intents_rejects_unknown_type() -> None:
    bad = [{"name": "x", "patterns": ["^x$"], "action": {"type": "bogus"}}]
    with pytest.raises(RuntimeError, match="unhandled action type"):
        router.validate_intents(bad)


def test_validate_intents_rejects_unknown_timer_verb() -> None:
    bad = [{"name": "x", "patterns": ["^x$"], "action": {"type": "timer", "verb": "snooze"}}]
    with pytest.raises(RuntimeError, match="unhandled timer verb"):
        router.validate_intents(bad)


def test_unknown_action_type_speaks_clarification_never_llm(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = AsyncMock(return_value="should never be called")
    monkeypatch.setattr(router, "ask_llm", llm)
    monkeypatch.setattr(
        router,
        "match_intent",
        lambda text: ({"name": "x", "action": {"type": "bogus"}}, None),
    )
    resp = client.post("/route", json={"text": "whatever"})
    assert resp.json() == {"speech": "I don't know how to do that yet.", "intent": "unsupported"}
    llm.assert_not_awaited()


# --- timer dispatch wiring ---


def test_timer_intent_dispatches_to_handle_timer(client, monkeypatch: pytest.MonkeyPatch) -> None:
    handled: dict = {}

    async def fake_handle(verb, text, store, scheduler):
        handled["args"] = (verb, text)
        return "Timer set for 10 minutes."

    monkeypatch.setattr(router.timers, "handle_timer", fake_handle)
    resp = client.post("/route", json={"text": "set a timer for 10 minutes"})
    assert resp.json() == {"speech": "Timer set for 10 minutes.", "intent": "timer_set"}
    assert handled["args"] == ("set", "set a timer for 10 minutes")


def test_timer_set_end_to_end_through_route(client) -> None:
    resp = client.post("/route", json={"text": "set a timer for 10 minutes"})
    assert resp.json() == {"speech": "Timer set for 10 minutes.", "intent": "timer_set"}
    resp = client.post("/route", json={"text": "how long is left on my timer"})
    assert resp.json()["intent"] == "timer_query"
    assert resp.json()["speech"].startswith("Your 10-minute timer has ")


# --- knowledge_query topic extraction (2026-08-10 redesign) ---


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


# --- knowledge_query dispatch: cleaned topic reaches search; empty topic clarifies ---


@respx.mock
async def test_open_brain_searches_the_cleaned_topic_not_raw_utterance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    monkeypatch.setattr(router, "OPEN_BRAIN_API_KEY", "k")
    search = respx.post("http://ob.local/api/search").mock(
        return_value=Response(
            200,
            json={
                "thoughts": [{"content": "Braised leeks note", "similarity": 0.9, "id": "1"}],
                "total": 1,
                "query": "q",
            },
        )
    )
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="unused"))

    with TestClient(router.app) as c:
        # synthesis LLM: patch the anthropic client *after* lifespan startup,
        # which otherwise overwrites app.state.anthropic with a real client.
        router.app.state.anthropic = _FakeAnthropic("Here's your leek recipe.")
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
    assert resp.json() == {
        "speech": "What would you like me to look up?",
        "intent": "knowledge_query",
    }
    called.assert_not_awaited()
