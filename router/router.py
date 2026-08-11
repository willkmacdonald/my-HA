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

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import push as push_mod
import timers
import yaml
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request, WebSocket
from pydantic import BaseModel

OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "")
OPEN_BRAIN_API_KEY = os.environ.get("OPEN_BRAIN_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = (
    "You are a home voice assistant. Answers are spoken aloud: "
    "reply in one or two short sentences, no markdown, no lists."
)

SYNTH_PROMPT = (
    "Answer the question in one or two short spoken sentences using only "
    "these notes; if the notes don't answer it, say so."
)

ERROR_SPEECH = "Sorry, something went wrong answering that."

log = logging.getLogger("router")

INTENTS = yaml.safe_load(Path(__file__).with_name("intents.yaml").read_text())["intents"]

HANDLED_TYPES = {"device", "open_brain", "timer"}
TIMER_VERBS = {"set", "cancel", "query"}


def validate_intents(intents: list[dict]) -> None:
    """Fail fast at startup if intents.yaml declares an action the router
    can't dispatch (spec: Matching precision & dispatch safety)."""
    for intent in intents:
        action = intent.get("action") or {}
        kind = action.get("type")
        if kind not in HANDLED_TYPES:
            raise RuntimeError(
                f"intents.yaml: intent {intent.get('name')!r} declares "
                f"unhandled action type {kind!r} (handled: {sorted(HANDLED_TYPES)})"
            )
        if kind == "timer" and action.get("verb") not in TIMER_VERBS:
            raise RuntimeError(
                f"intents.yaml: intent {intent.get('name')!r} declares "
                f"unhandled timer verb {action.get('verb')!r} (handled: {sorted(TIMER_VERBS)})"
            )


validate_intents(INTENTS)


class Utterance(BaseModel):
    text: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=30)
    # Tests never call the real API (ask_llm is mocked); real runs source .env.
    app.state.anthropic = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "unset"))
    store = timers.TimerStore(
        os.environ.get("TIMERS_DB", str(Path(__file__).with_name("timers.db")))
    )
    await store.open()
    await timers.recover(store)
    channel = push_mod.PushChannel()
    scheduler = timers.Scheduler(store, channel.broadcast_announce)
    channel.set_ack_handler(scheduler.ack)
    app.state.store, app.state.push, app.state.scheduler = store, channel, scheduler

    def _log_task_death(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("background task %s died", task.get_name(), exc_info=task.exception())

    tasks = [
        asyncio.create_task(scheduler.run(), name="scheduler"),
        asyncio.create_task(channel.ping_loop(), name="ping-loop"),
    ]
    for t in tasks:
        t.add_done_callback(_log_task_death)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await store.close()
        await app.state.http.aclose()
        await app.state.anthropic.close()


app = FastAPI(lifespan=lifespan)


def match_intent(text: str) -> tuple[dict, re.Match] | None:
    """First intent whose regex matches, with its match object; None if nothing matches."""
    for intent in INTENTS:
        for pattern in intent["patterns"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return intent, match
    return None


async def run_device_action(intent: dict, match: re.Match, http: httpx.AsyncClient) -> str:
    """Call a device API directly. The URL/body can use {slot} groups from the regex."""
    action = intent["action"]
    url = action["url"].format(**match.groupdict())
    resp = await http.request(action.get("method", "POST"), url, json=action.get("json"))
    resp.raise_for_status()
    return intent.get("response", "Done.").format(**match.groupdict())


async def ask_open_brain(query: str, http: httpx.AsyncClient, anthropic: AsyncAnthropic) -> str:
    """Spec Component 5: search Open Brain, filter by similarity, synthesize
    a spoken answer with one Claude call. 'Found nothing' and 'couldn't
    reach' are deliberately distinct spoken outcomes."""
    try:
        resp = await http.post(
            f"{OPEN_BRAIN_URL}/api/search",
            headers={"X-API-Key": OPEN_BRAIN_API_KEY},
            json={"query": query, "limit": 5},
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        log.exception("open brain search failed")
        return "I couldn't reach my notes."
    hits = [t for t in resp.json().get("thoughts", []) if t.get("similarity", 0) >= 0.55]
    if not hits:
        return "I didn't find anything about that."
    notes = "\n\n".join(t["content"] for t in hits)
    msg = await anthropic.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=SYNTH_PROMPT,
        messages=[{"role": "user", "content": f"Notes:\n{notes}\n\nQuestion: {query}"}],
    )
    return msg.content[0].text


async def ask_llm(text: str, anthropic: AsyncAnthropic) -> str:
    msg = await anthropic.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return msg.content[0].text


@app.post("/route")
async def route(utt: Utterance, request: Request) -> dict:
    text = utt.text.strip()
    state = request.app.state
    try:
        matched = match_intent(text)
        if matched:
            intent, match = matched
            kind = intent["action"]["type"]
            if kind == "device":
                return {
                    "speech": await run_device_action(intent, match, state.http),
                    "intent": intent["name"],
                }
            if kind == "timer":
                speech = await timers.handle_timer(
                    intent["action"]["verb"], text, state.store, state.scheduler
                )
                return {"speech": speech, "intent": intent["name"]}
            if kind == "open_brain":
                topic = (match.groupdict().get("topic") or "").strip()
                if not topic:
                    # trigger fired but no topic captured — clarify, don't search
                    return {
                        "speech": "What would you like me to look up?",
                        "intent": "knowledge_query",
                    }
                if OPEN_BRAIN_URL:
                    return {
                        "speech": await ask_open_brain(topic, state.http, state.anthropic),
                        "intent": intent["name"],
                    }
                # documented degradation: no Open Brain configured -> LLM fallback
            else:
                # unreachable via validate_intents; runtime belt for hand-edited configs
                log.warning("intent %r declares unhandled action type %r", intent["name"], kind)
                return {"speech": "I don't know how to do that yet.", "intent": "unsupported"}
        return {"speech": await ask_llm(text, state.anthropic), "intent": "llm_fallback"}
    except Exception:
        log.exception("routing failed for %r", text)
        return {"speech": ERROR_SPEECH, "intent": "error"}


@app.websocket("/events")
async def events(ws: WebSocket) -> None:
    await ws.app.state.push.handle(ws)
