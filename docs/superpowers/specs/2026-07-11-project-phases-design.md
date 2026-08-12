# my-HA — Project Phase Plan

**Date:** 2026-07-11
**Status:** Approved (Approach A: parallel tracks, hardware-gated)

Companion to [requirements.md](../../requirements.md) and [hardware.md](../../hardware.md).
This doc is the roadmap: what gets built, in what order, and why. Those docs
hold the architecture and hardware detail.

## Current status (as of 2026-08-12)

> Keep this block updated as phases land — this doc is the roadmap-of-record.
> Session-to-session baton-pass (findings, gotchas, how-it's-built) is in
> [HANDOFF.md](../../../HANDOFF.md); deep execution detail is in `.superpowers/sdd/progress.md`.

| Phase | Status |
|---|---|
| 0 — Procurement | ✅ Done 2026-07-11. Board **delivered 2026-08-09**. |
| 1 — Mac backend + fake satellite | ✅ Done 2026-07-12 (both exit criteria passed). |
| 2 — Open Brain + timers | ✅ **Done 2026-08-09** — 176 tests; Open Brain X-API-Key auth deployed to prod; both exit criteria passed live (voice knowledge query + timer surviving a router restart). 4 non-blocking follow-up findings logged (see HANDOFF.md "Open findings"). |
| 3 — Board bring-up + wake-word gate | 🔜 **NEXT** — unblocked (board arrived). Go/no-go: >90% wake-word detection. |
| 4 — Full loop on the Pi | ⬜ needs Phase 3. |
| 5 — Enclosure + daily-driver hardening | ⬜ |
| 6 — Conversational paths | ⬜ **NEW** (added 2026-08-12) — committed; first post-hardening software push. Gated on 3–5 (real wake word + a week of daily use). See Phase 6 below. |
| 7 — Music playback | ⬜ parked (was Phase 6). |
| 8 — ESP32 satellite v2 | ⬜ parked (was Phase 7). |

**Post-2026-08-09 developments not yet folded into the phase bodies below:**

- **Knowledge-query latency halved (2026-08-12, shipped).** `ask_open_brain` now
  synthesizes with Haiku (`SYNTH_MODEL`) instead of Sonnet; open-ended fallback keeps
  Sonnet (`LLM_MODEL`). End-to-end knowledge query ~4s → ~1.8s. Rerank untouched (it is
  cheap and inside the ~1.2s search call; synthesis was the dominant cost). Commit on `main`.
- **Finding 1 (cold-start ReadTimeout) resolved operationally (2026-08-12).** Rather than
  the "long read timeout + retry" originally proposed, the Open Brain `web` container is
  kept warm by a Synology cron ping (`GET /health` every minute, all day) so it never
  scales to zero. Voice queries no longer hit the >30s cold-start. Operational knowledge,
  not code — see HANDOFF.md. (A read-timeout hardening remains a reasonable belt-and-suspenders
  if the ping ever lapses, but is no longer urgent.)
- **Render surface ("send it to my phone") split out to the Second Brain repo (2026-08-12).**
  A voice command renders retrieved content as rich HTML on demand and writes it to a single
  fixed Azure-web-app URL the user keeps open on their phone. Deliberately **not** a my-HA
  build item — my-HA owns the *conversational* paths (Phase 6); the surface is Second Brain's
  to design/host. No Open Brain↔Second Brain data dependency implied.

## Scope decisions (settled 2026-07-11)

- **v1 definition of done:** Q&A/general assistant (LLM fallback), Open Brain
  knowledge queries, and timers/alarms/reminders — one boxed satellite in one
  room, daily-driver reliable.
- **Not in v1:** smart-device control. The `intents.yaml` device-API
  placeholders stay placeholders until a later milestone; light control is
  explicitly deferred.
- **Music playback:** later phase (Phase 7), planned-for but not designed.
  Hardware choices must not preclude it.
- **Roadmap extends through ESP32 satellite v2** (Phase 8). Custom KiCad PCB
  and woodworked enclosure remain parked sketches (see hardware.md Phase 2
  section) — recorded, not planned.
- **Timers are new architecture.** The current design is request/response
  only; a timer firing requires the router to *push* audio to the satellite
  unprompted. Designed inside Phase 2.

## Hardware (ordered 2026-07-11)

| Item | Why | Status |
|---|---|---|
| ReSpeaker XVF3800 w/ XIAO ESP32-S3, case version (~$65) | Dual-mode board: USB firmware serves v1 on the Pi; I2S firmware + onboard ESP32-S3 serves v2 with no new hardware. Same JST 5W amp output preserves the closed-loop audio rule. | **Ordered** |
| Dayton Audio ND91-4 3-1/2" full-range, 4Ω (~$20) | 85.6 dB sensitivity (vs 78 dB for the 2.5" ND65-4) — decisive with only a 5W amp. 65 Hz–17 kHz reach pays off for Phase 7 music. Published enclosure specs; happy in a 0.5–1.5 L sealed box. 30W RMS handling = pure headroom. | **Ordered** |
| JST 2-pin pigtail (likely PH 2.0mm — confirm on arrival) | Board's JST amp output → speaker wire → driver tabs. Mono, no external amp or PSU. | Check box contents first |
| Pi 5, USB-C data cable, 5V/5A PSU | | Owned |

Connection: the board **is** the amplifier. One USB-C to the Pi carries
power + audio both ways; the driver hangs off the board's JST output.
Whether the Pi's USB budget survives 5W speaker peaks is an open question —
measured in Phase 3/4; fallback is a separate supply for the board.

## Phase map

```
Phase 0 ──► Phase 1 ──► Phase 2          (software track — no hardware needed)
   │                       │
   └──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6   (hardware track, then
        (go/no-go)          │              (convo)     conversational once
                            │                          the box is daily-driven)
                            ├──► Phase 7 (music)
                            └──► Phase 8 (ESP32 v2)     [7 and 8 in either order]

Phase 6 (conversational paths) is gated on 3–5: it needs a real wake word AND a
stretch of daily use, because how conversation should *feel* is only knowable
after living with the basic loop. Parked beyond the map: custom PCB + enclosure.
```

Phase 4 needs Phase 1 complete and ideally Phase 2; Phases 1–2 run while
the hardware ships.

## Phase 0 — Procurement ✅ (complete 2026-07-11)

Board + driver ordered. Everything in Phases 3–5 gates on delivery.

## Phase 1 — Mac backend + fake satellite

All on the Mac Studio; nothing needs the Pi or the board.

- `stt_server.py` (faster-whisper, :8100) running and tested.
- `router.py` (:8200) running with the Claude API fallback path working.
- **New component: `fake_satellite.py`** — runs on the Mac using its own mic
  and speakers, exercising the identical record → STT → route → TTS loop the
  Pi will run. Same code paths, different audio device.

**Exit criterion:** speak a question at the Mac, hear a spoken answer.

## Phase 2 — Features: Open Brain + timers

- Open Brain knowledge route wired to the real MCP server and answering
  "what did I decide about X" queries.
- Timers/alarms/reminders:
  - Router-side scheduler with persistence (timers survive a router restart).
  - **Push channel** from router to satellite — likely a persistent websocket
    the satellite holds open; exact design decided at phase start.
  - Intents: set / cancel / query ("how long left on my timer").
  - Alarm sound + spoken announcement on the satellite.

**Exit criterion:** "set a timer for 2 minutes" on the fake satellite fires
audibly 2 minutes later, and still fires after killing and restarting the
router mid-countdown.

## Phase 3 — Board bring-up + wake-word gate (go/no-go)

First hardware phase, on delivery.

- Confirm box contents (JST pigtail included?).
- The board ships with I2S firmware and **will not enumerate over USB** until
  reflashed: boot safe mode, flash USB firmware, verify it appears as a USB
  Audio Class 2.0 device on the Pi.
- `wakeword_bench.py`, 20 trials, on **both** capture channels (left
  processed vs right ASR) — settles that open question from hardware.md.
  Wire the winning channel into `satellite.py`.

**Gate:** >90% detection (bare-mic baseline was ~5%). Below that: stop,
diagnose, spend nothing further on enclosure materials until resolved.

## Phase 4 — Full loop on the Pi

- `satellite.py` against the real Mac backend over Tailscale.
- Closed-loop audio enforced: `APLAY_DEVICE` → the XVF3800's playback device,
  never the Pi's own audio out (hardware.md's one rule).
- Endpointing (`SILENCE_RMS`, `SILENCE_SECONDS`) tuned on-device against the
  XVF3800's noise floor.
- Barge-in verified: wake word detected while TTS is playing.
- Timer push channel proven on the real satellite.
- Measure USB power draw at speaker peaks (the open question).

**Exit criterion:** full conversation plus a timer firing, in-room, from
the Pi.

## Phase 5 — Enclosure v1 + daily-driver hardening

- Sealed box sized by the ND91-4 (0.5–1.5 L), separate speaker chamber, foam
  gasket under the array PCB, clean mic ports — per hardware.md.
- **Re-run the wake-word bench inside the box** — regression check against
  enclosure-induced vibration reaching the mics.
- Service management: systemd units on the Pi, launchd on the Mac; everything
  survives reboots and network blips unattended.

**Exit criterion:** one week of real family use in the chosen room without
intervention.

## Phase 6 — Conversational paths (added 2026-08-12; design deferred to phase start)

The first big software push once the box is a reliable daily driver. Turns my-HA
from a one-shot Q&A device ("wake, ask, answer, forget") into something that holds
a conversation. **Explicitly gated on Phases 3–5** — not just because the mic/wake
word must work (Phase 3) and the loop must be closed (Phase 4), but because *how
conversation should feel* is only knowable after living with the basic loop for a
stretch (Phase 5). Designing it earlier would be guessing.

**The core architectural fact (established while scoping, 2026-08-12):** the system
has **no memory between turns** today. `fake_satellite.main()` loops record → STT →
`POST /route` → speak → forget; `route()` takes one `Utterance` and returns one
`speech`, fully stateless by design. Every conversational feature below therefore
requires new architecture: a notion of *session/context* the router does not have,
plus the satellite listening again **without a wake word**.

**The candidate pieces (brainstormed, not yet chosen or designed):**

- **Follow-ups without a wake word.** After an answer, the mic stays open briefly so
  "why that one?" / "what's next?" continues the exchange without re-waking. Silence
  closes the window. (Alexa Follow-Up-Mode feel.) An alternative "open/close a session
  deliberately" model was also floated — the choice between them is a phase-start
  decision, and one best made *after* Phase 5 daily use.
- **Multi-turn continuity / paced walk-throughs.** "Read me the recipe" → "what's
  next?" → next step, with the assistant remembering position in the retrieved note.
  This is the piece that most needs real conversation state.
- **Background "fuller answer" offer.** Haiku answers fast (already shipped as the
  synthesis model); a Sonnet pass re-checks in the background and offers a richer
  answer *only if it materially differs*. Rides on whatever follow-up/push mechanism
  the phase builds. Note: the push channel built for timers (`ws /events` +
  `push.py`, "speak unprompted + wait for ack") is already most of the delivery
  primitive this needs.

**Not in this phase:** the "send it to my phone" render surface — split out to the
Second Brain repo (see the status block). Conversational is voice-native back-and-forth;
the surface is a separate visual channel Second Brain owns.

**Exit criterion (provisional, refine at phase start):** a genuine multi-turn exchange
in-room — ask, get an answer, ask a context-dependent follow-up without re-waking, and
get a correct answer that used the prior turn — plus a paced walk-through ("what's next?")
that tracks position through a multi-step note.

## Phase 7 — Music playback (deliberately unscoped)

Source (Spotify Connect? radio streams?), playback control, ducking, and
barge-in-over-music get designed when this phase starts. The commitments made
now to keep it possible: audio out through the XVF3800 (AEC reference), and a
driver (ND91-4) that can actually play music.

## Phase 8 — ESP32 satellite v2

Same board, reflashed to I2S firmware; the onboard XIAO ESP32-S3 becomes the
satellite host, streaming audio over Wi-Fi to the Mac. Frees the Pi 5.

Known design decision at phase start: openWakeWord cannot run on an
ESP32-S3 — either microWakeWord on-chip, or continuous streaming to the Mac
with wake-word detection server-side.

## Carry-over notes for Phase 2 (from Phase 1 final review, 2026-07-12)

Deliberately deferred, to be picked up when Phase 2 touches these files:

- **Router hot-path latency:** `router.py` creates a fresh `AsyncAnthropic()`
  and `httpx.AsyncClient()` per call (TCP+TLS handshake per voice turn).
  Move to module-level clients when Phase 2 modifies the router.
- **`pipeline.transcribe` has no receive timeout** — a wedged STT server
  hangs the satellite forever. Add a deadline when Phase 2 touches pipeline.
- **Push-channel design constraint:** `fake_satellite.main()` blocks on
  `input()`; it cannot simultaneously hold a router→satellite websocket
  open. The Phase 2 push listener needs a background task/thread — decide
  this shape early, it reshapes `main()`.
- **`satellite.py` is never imported by the Mac test suite** (openwakeword
  is Pi-only) — a cheap `py_compile` smoke test would catch name-level
  breakage before Phase 3 hardware bring-up.
- **Hardening for later phases:** STT server accepts unbounded audio before
  "END" (cap at ~60 s), and both services bind `0.0.0.0` (bind the
  Tailscale IP when the Pi joins).

## Parked (recorded, not planned)

Custom KiCad PCB (ESP32-S3 + PDM MEMS mics + XVF3800 chip) and the
woodworked enclosure — the father-daughter project. See hardware.md.
