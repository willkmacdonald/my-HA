# my-HA — Project Phase Plan

**Date:** 2026-07-11
**Status:** Approved (Approach A: parallel tracks, hardware-gated)

Companion to [requirements.md](../../requirements.md) and [hardware.md](../../hardware.md).
This doc is the roadmap: what gets built, in what order, and why. Those docs
hold the architecture and hardware detail.

## Scope decisions (settled 2026-07-11)

- **v1 definition of done:** Q&A/general assistant (LLM fallback), Open Brain
  knowledge queries, and timers/alarms/reminders — one boxed satellite in one
  room, daily-driver reliable.
- **Not in v1:** smart-device control. The `intents.yaml` device-API
  placeholders stay placeholders until a later milestone; light control is
  explicitly deferred.
- **Music playback:** later phase (Phase 6), planned-for but not designed.
  Hardware choices must not preclude it.
- **Roadmap extends through ESP32 satellite v2** (Phase 7). Custom KiCad PCB
  and woodworked enclosure remain parked sketches (see hardware.md Phase 2
  section) — recorded, not planned.
- **Timers are new architecture.** The current design is request/response
  only; a timer firing requires the router to *push* audio to the satellite
  unprompted. Designed inside Phase 2.

## Hardware (ordered 2026-07-11)

| Item | Why | Status |
|---|---|---|
| ReSpeaker XVF3800 w/ XIAO ESP32-S3, case version (~$65) | Dual-mode board: USB firmware serves v1 on the Pi; I2S firmware + onboard ESP32-S3 serves v2 with no new hardware. Same JST 5W amp output preserves the closed-loop audio rule. | **Ordered** |
| Dayton Audio ND91-4 3-1/2" full-range, 4Ω (~$20) | 85.6 dB sensitivity (vs 78 dB for the 2.5" ND65-4) — decisive with only a 5W amp. 65 Hz–17 kHz reach pays off for Phase 6 music. Published enclosure specs; happy in a 0.5–1.5 L sealed box. 30W RMS handling = pure headroom. | **Ordered** |
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
   └──► Phase 3 ──► Phase 4 ──► Phase 5  (hardware track — gated on delivery)
        (go/no-go)          │
                            ├──► Phase 6 (music)
                            └──► Phase 7 (ESP32 v2)     [6 and 7 in either order]

Parked beyond the map: custom PCB + woodworked enclosure.
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

## Phase 6 — Music playback (deliberately unscoped)

Source (Spotify Connect? radio streams?), playback control, ducking, and
barge-in-over-music get designed when this phase starts. The commitments made
now to keep it possible: audio out through the XVF3800 (AEC reference), and a
driver (ND91-4) that can actually play music.

## Phase 7 — ESP32 satellite v2

Same board, reflashed to I2S firmware; the onboard XIAO ESP32-S3 becomes the
satellite host, streaming audio over Wi-Fi to the Mac. Frees the Pi 5.

Known design decision at phase start: openWakeWord cannot run on an
ESP32-S3 — either microWakeWord on-chip, or continuous streaming to the Mac
with wake-word detection server-side.

## Parked (recorded, not planned)

Custom KiCad PCB (ESP32-S3 + PDM MEMS mics + XVF3800 chip) and the
woodworked enclosure — the father-daughter project. See hardware.md.
