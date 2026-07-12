# Hardware Design

## v1 satellite — bill of materials

| Item | Notes | ~Cost |
|---|---|---|
| Raspberry Pi 5 (4GB is plenty) | wake word only; STT/LLM run on the Mac | owned |
| ReSpeaker XVF3800 USB 4-mic array | XMOS XU316: AEC, beamforming, noise suppression, de-reverb, DoA; 360° far-field to ~5m | ~$60 |
| Full-range speaker driver, 4Ω, 3–5W | driven by the XVF3800's onboard amp (JST connector, "5W amplified speakers") | ~$10 |
| USB-C cable (data, not charge-only) | XVF3800 is powered and controlled over one USB-C | — |
| Pi 5 PSU (5V/5A official) | the array draws from the Pi's USB; don't starve it | owned |

## The one rule that shapes everything: closed-loop audio

**All assistant playback (TTS, beeps) must go out through the XVF3800's own
output** — the JST speaker connector or its 3.5mm jack — never the Pi's HDMI
audio or a separate USB speaker.

Why: echo cancellation needs a reference copy of what the speaker is playing.
The XVF3800 only has that reference for audio routed through itself. Break
this rule and the assistant deafens itself whenever it talks: no barge-in, and
we're back to the unprocessed-far-field problem the XVF3800 was bought to fix.

Practical consequences:

- The Pi treats the XVF3800 as **both** its microphone and its sound card
  (USB Audio Class 2.0, 16 kHz). `satellite.py` takes an `APLAY_DEVICE` env
  var — point it at the XVF3800's playback device, not `default`.
- Channel map on capture: by default the **left channel is the processed
  AEC/beamform output, the right channel is the ASR-tuned output** of the
  auto-selected beam. Feed the ASR channel to openWakeWord/whisper. A
  6-channel firmware variant additionally exposes the 4 raw mics (useful for
  debugging, not for v1).
- Configuration/tuning via the vendor's `xvf_host` tool (gain, AEC
  parameters, LED effects); firmware variants: USB 2-ch (default), USB 6-ch,
  I2S.

## Enclosure (v1: functional, not furniture)

Applying the earlier lesson — **speaker driver volume, not electronics, sets
minimum enclosure size**:

- Size the box around the driver's recommended enclosure volume (for a small
  2–3" full-range driver, roughly 0.5–1.5 L sealed); the Pi and array add
  almost nothing.
- **Acoustically decouple mics from the speaker**: separate chamber for the
  driver, foam gasket between the array PCB and the top panel, mic ports as
  clean short holes directly above the 4 mics (no grille fabric pressed
  against the ports). Vibration reaching the mics through the enclosure is
  the main way DIY builds sabotage the XVF3800's AEC.
- Mics face **up**, array roughly centered, unobstructed 360° — placement in
  the room matters too: open shelf or table center beats a corner or against
  a wall.
- Seeed publishes 3D files for the array (main assembly + enclosure
  top/bottom) — usable as-is for a bench mule while validating milestone 1;
  the woodworked enclosure stays a later, deliberate build.
- Front panel niceties for later: the board's 12× WS2812 LED ring
  (listening/thinking state) and its hardware mute button both deserve
  windows in the final enclosure.

## Bench-mule plan for milestone 1

No enclosure needed to validate the theory. Bare board flat on the table,
mics up, speaker driver connected to the JST output and sitting a hand-width
away, `wakeword_bench.py` for the 20-trial count. Only after the detection
rate is proven, move into a box — and re-run the bench in the box to catch
enclosure-induced regressions.

## Phase 2 — custom build (father-daughter project, parked)

Original sketch: custom PCB in KiCad — ESP32-S3 + MEMS mic array + XMOS
front end — plus woodworked enclosure.

Useful discovery: Seeed ships a **XIAO ESP32-S3 variant of this same XVF3800
board** (array runs I2S "INT-Device" firmware into the ESP32-S3 instead of
USB). That's a natural intermediate step with real learning value and far
less risk than a from-scratch 4-mic PCB:

1. **Step A (satellite v2):** XVF3800 + XIAO ESP32-S3 variant — satellite
   shrinks from Pi 5 to a microcontroller streaming Wi-Fi audio to the Mac.
   Teaches embedded audio (I2S, DMA, streaming) on known-good hardware.
2. **Step B (the real PCB):** custom KiCad board — ESP32-S3 module, 4× PDM
   MEMS mics, XVF3800 chip + supporting analog. The hard, interesting part is
   mic placement geometry and analog layout; Seeed's board is the reference
   design to study.
3. **Step C:** the woodworked enclosure, sized by the speaker driver as
   always.

Not in scope for v1; recorded here so the v1 choices (I2S-capable array,
enclosure lessons) keep the door open.

## Open hardware questions

- Which speaker driver — pick one with published enclosure-volume specs
  (small full-range from Dayton/Visaton/Tang Band class) before designing
  any box.
- Confirm on the bench which capture channel (left processed vs right ASR)
  gives better openWakeWord scores; wire the choice into `satellite.py`.
- Whether the Pi 5's USB budget comfortably powers array + 5W speaker peaks,
  or the array should get its own USB-C supply (reports differ; measure).

Sources: [Seeed XVF3800 wiki](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/) ([mirror](https://github.com/Seeed-Studio/wiki-documents/blob/docusaurus-version/sites/en/docs/Sensor/reSpeaker_XVF3800_USB_4_Mic_Array/respeaker_xvf3800_usb_4_mic_array.md)), [product page](https://www.seeedstudio.com/ReSpeaker-XVF3800-USB-Mic-Array-p-6488.html), [host-control README](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md), [CNX on the ESP32-S3 variant](https://www.cnx-software.com/2025/07/29/respeaker-xmos-xvf3800-4-mic-array-board-features-esp32-s3-module-works-over-usb/).
