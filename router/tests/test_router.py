"""Router tests: intent matching, dispatch, fallbacks, error handling.

External calls (device APIs, Open Brain, Anthropic) are mocked — these tests
run offline with no API keys.
"""

from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

import router

client = TestClient(router.app)


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


def test_unmatched_text_falls_back_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="It's red and dusty."))
    resp = client.post("/route", json={"text": "what's the weather on Mars"})
    assert resp.status_code == 200
    assert resp.json() == {"speech": "It's red and dusty.", "intent": "llm_fallback"}


def test_knowledge_intent_without_open_brain_falls_back_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "")
    monkeypatch.setattr(router, "ask_llm", AsyncMock(return_value="You decided X."))
    resp = client.post("/route", json={"text": "what did I decide about Foundry"})
    assert resp.json() == {"speech": "You decided X.", "intent": "llm_fallback"}


def test_knowledge_intent_with_open_brain_routes_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    monkeypatch.setattr(router, "ask_open_brain", AsyncMock(return_value="You chose Piper."))
    resp = client.post("/route", json={"text": "check my notes on TTS"})
    assert resp.json() == {"speech": "You chose Piper.", "intent": "knowledge_query"}


def test_device_intent_dispatches_to_device_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        router, "run_device_action", AsyncMock(return_value="Okay, kitchen lights on.")
    )
    resp = client.post("/route", json={"text": "turn on the kitchen lights"})
    assert resp.json() == {"speech": "Okay, kitchen lights on.", "intent": "lights_on"}


def test_llm_failure_returns_spoken_error_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    speech = await router.run_device_action(intent, match)
    assert route_mock.called
    assert speech == "Okay, kitchen lights on."


@respx.mock
async def test_ask_open_brain_returns_first_hit_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/search").mock(
        return_value=Response(200, json=[{"summary": "You picked the ND91-4."}])
    )
    assert await router.ask_open_brain("speaker choice") == "You picked the ND91-4."


@respx.mock
async def test_ask_open_brain_empty_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "OPEN_BRAIN_URL", "http://ob.local")
    respx.post("http://ob.local/search").mock(return_value=Response(200, json=[]))
    assert await router.ask_open_brain("nothing") == "I didn't find anything on that."
