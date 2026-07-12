"""The router — the ~100 lines to own.

transcript → local intent (device API) | knowledge query (Open Brain) | LLM.

Intents live in intents.yaml as regex patterns. First match wins; nothing
matches → LLM fallback. Knowledge routing is just another intent whose
action type is "open_brain". If OPEN_BRAIN_URL is unset, knowledge queries
fall back to the LLM instead of failing.

Usage:
    ANTHROPIC_API_KEY=… OPEN_BRAIN_URL=http://localhost:8000 \
        uvicorn router:app --host 0.0.0.0 --port 8200
"""

import logging
import os
import re
from pathlib import Path

import httpx
import yaml
from anthropic import AsyncAnthropic
from fastapi import FastAPI
from pydantic import BaseModel

OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = (
    "You are a home voice assistant. Answers are spoken aloud: "
    "reply in one or two short sentences, no markdown, no lists."
)

ERROR_SPEECH = "Sorry, something went wrong answering that."

log = logging.getLogger("router")

app = FastAPI()
INTENTS = yaml.safe_load(Path(__file__).with_name("intents.yaml").read_text())["intents"]


class Utterance(BaseModel):
    text: str


def match_intent(text: str) -> tuple[dict, re.Match] | None:
    """First intent whose regex matches, with its match object; None if nothing matches."""
    for intent in INTENTS:
        for pattern in intent["patterns"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return intent, match
    return None


async def run_device_action(intent: dict, match: re.Match) -> str:
    """Call a device API directly. The URL/body can use {slot} groups from the regex."""
    action = intent["action"]
    url = action["url"].format(**match.groupdict())
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.request(action.get("method", "POST"), url, json=action.get("json"))
        resp.raise_for_status()
    return intent.get("response", "Done.").format(**match.groupdict())


async def ask_open_brain(query: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{OPEN_BRAIN_URL}/search", json={"query": query})
        resp.raise_for_status()
    # TODO(will): shape this once the Open Brain voice-answer endpoint settles —
    # probably want a synthesized one-liner, not raw search hits.
    hits = resp.json()
    return hits[0]["summary"] if hits else "I didn't find anything on that."


async def ask_llm(text: str) -> str:
    client = AsyncAnthropic()
    msg = await client.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return msg.content[0].text


@app.post("/route")
async def route(utt: Utterance) -> dict:
    text = utt.text.strip()
    try:
        matched = match_intent(text)
        if matched:
            intent, match = matched
            kind = intent["action"]["type"]
            if kind == "device":
                return {"speech": await run_device_action(intent, match), "intent": intent["name"]}
            if kind == "open_brain" and OPEN_BRAIN_URL:
                return {"speech": await ask_open_brain(text), "intent": intent["name"]}
        return {"speech": await ask_llm(text), "intent": "llm_fallback"}
    except Exception:
        log.exception("routing failed for %r", text)
        return {"speech": ERROR_SPEECH, "intent": "error"}
