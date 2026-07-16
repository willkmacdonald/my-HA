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


# --- match_intent (pure function) ---


def test_match_intent_lights_on() -> None:
    matched = router.match_intent("turn on the kitchen lights")
    assert matched is not None
    intent, match = matched
    assert intent["name"] == "lights_on"
    assert match.groupdict()["room"] == "kitchen"


def test_match_intent_knowledge_query() -> None:
    matched = router.match_intent("what did I decide about Foundry rate limits")
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


def test_knowledge_intent_without_open_brain_falls_back_to_llm(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "")
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="You decided X."))
    resp = client.post("/route", json={"text": "what did I decide about Foundry"})
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


@respx.mock
async def test_ask_open_brain_returns_first_hit_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/search").mock(
        return_value=Response(200, json=[{"summary": "You picked the ND91-4."}])
    )
    async with httpx.AsyncClient() as http:
        result = await router.ask_open_brain("speaker choice", http, None)
    assert result == "You picked the ND91-4."


@respx.mock
async def test_ask_open_brain_empty_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/search").mock(return_value=Response(200, json=[]))
    async with httpx.AsyncClient() as http:
        result = await router.ask_open_brain("nothing", http, None)
    assert result == "I didn't find anything on that."


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
        ("what did I decide about the kitchen lights on the porch", "knowledge_query"),
        ("check my notes about office lights off schedule", "knowledge_query"),
        ("remind me to turn on the porch lights at 8", "timer_set"),
        ("remind me to check my notes at 8 pm", "timer_set"),
        ("set a timer for 10 minutes", "timer_set"),
        ("cancel all my timers", "timer_cancel"),
        ("how long is left on my timer", "timer_query"),
        ("turn on the kitchen lights", "lights_on"),
        ("kitchen lights off", "lights_off"),
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
