"""The router — the ~100 lines to own.

transcript → local intent (device API) | knowledge query (Open Brain) | LLM.

Intents live in intents.yaml as regex patterns. First match wins; nothing
matches → LLM fallback. Knowledge routing is just another intent whose
action type is "open_brain".

Usage:
    ANTHROPIC_API_KEY=… OPEN_BRAIN_URL=http://localhost:8000 \
        uvicorn router:app --host 0.0.0.0 --port 8200
"""

import os
import re
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI
from pydantic import BaseModel

OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = (
    "You are a home voice assistant. Answers are spoken aloud: "
    "reply in one or two short sentences, no markdown, no lists."
)

app = FastAPI()
INTENTS = yaml.safe_load(Path(__file__).with_name("intents.yaml").read_text())["intents"]


class Utterance(BaseModel):
    text: str


def run_device_action(intent: dict, match: re.Match) -> str:
    """Call a device API directly. The URL/body can use {slot} groups from the regex."""
    action = intent["action"]
    url = action["url"].format(**match.groupdict())
    with httpx.Client(timeout=10) as client:
        client.request(action.get("method", "POST"), url, json=action.get("json"))
    return intent.get("response", "Done.").format(**match.groupdict())


def ask_open_brain(query: str) -> str:
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{OPEN_BRAIN_URL}/search", json={"query": query})
        resp.raise_for_status()
    # TODO(will): shape this once the Open Brain voice-answer endpoint settles —
    # probably want a synthesized one-liner, not raw search hits.
    hits = resp.json()
    return hits[0]["summary"] if hits else "I didn't find anything on that."


def ask_llm(text: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return msg.content[0].text


@app.post("/route")
def route(utt: Utterance) -> dict:
    text = utt.text.strip()

    for intent in INTENTS:
        for pattern in intent["patterns"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            kind = intent["action"]["type"]
            if kind == "device":
                return {"speech": run_device_action(intent, match), "intent": intent["name"]}
            if kind == "open_brain":
                return {"speech": ask_open_brain(text), "intent": intent["name"]}

    return {"speech": ask_llm(text), "intent": "llm_fallback"}
