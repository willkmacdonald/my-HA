"""fake_satellite unit tests — subprocess and HTTP are mocked."""

from typing import Any
from unittest.mock import MagicMock

import fake_satellite
import pytest


def test_speak_uses_macos_say(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        fake_satellite.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock()
    )
    fake_satellite.speak("hello world")
    assert calls == [["say", "hello world"]]


def test_ask_router_posts_text_and_returns_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: dict[str, Any] = {}

    def fake_post(url: str, json: dict, timeout: int) -> MagicMock:
        posted["url"] = url
        posted["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"speech": "It is 9 PM.", "intent": "llm_fallback"}
        return resp

    monkeypatch.setattr(fake_satellite.requests, "post", fake_post)
    speech = fake_satellite.ask_router("what time is it")
    assert speech == "It is 9 PM."
    assert posted["json"] == {"text": "what time is it"}
    assert posted["url"] == fake_satellite.ROUTER_URL
