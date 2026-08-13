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
  auto-selected beam. A 6-channel firmware variant additionally exposes the 4
  raw mics (useful for debugging, not for v1). **Which channel to feed the wake
  word is settled empirically by `wakeword_bench.py --capture-channel {left,right}`,
  not assumed** — the winner (>90% detection) gets wired into `satellite.py`.
  Measured 2026-08-13 on USB 2-ch firmware: both channels carry strong signal,
  left (processed) hotter than right (RMS ~2250 vs ~1405 on a spoken phrase).
- Configuration/tuning via the vendor's `xvf_host` tool (gain, AEC
  parameters, LED effects); firmware variants: USB 2-ch, USB 6-ch, I2S.

## Firmware flashing (XIAO ESP32-S3 variant — done 2026-08-13)

**Our board is the XIAO ESP32-S3 variant, which ships with I2S firmware** and
therefore does NOT appear as a USB mic out of the box — plugged into a host it
enumerates as an *"Espressif USB JTAG_serial debug unit"* (the ESP32-S3), not
audio. For the v1 Pi satellite it must be reflashed to **USB 2-channel**
firmware so the XMOS presents as a standard USB Audio Class 2.0 mic.

**Two USB-C ports — this is the gotcha that cost the most time.** The board has
two USB-C ports: one routes to the **ESP32-S3**, one to the **XMOS** chip.
**Flashing (and USB-mic operation) requires the XMOS port — the one next to the
3.5 mm headphone jack.** Plugging into the ESP32 port shows nothing usable and
looks like a dead board (it also power-cycled/flickered on a marginal path).

Working procedure (verified on macOS 2026-08-13, board came back as a mic):

1. **Get the tools + firmware** (both reversible, no board risk):
   ```
   brew install dfu-util
   git clone https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git ~/respeaker-xvf3800-fw
   ```
   USB firmware bins live in `~/respeaker-xvf3800-fw/xmos_firmwares/usb/`; we
   flashed the latest 2-channel: `respeaker_xvf3800_usb_dfu_firmware_v2.0.10.bin`.
   (Don't "Save As" individual GitHub files — they corrupt; clone or Download-ZIP.)
2. **Cable into the XMOS port** (by the 3.5 mm jack).
3. **Enter Safe Mode:** power the board off (unplug), **press and hold the Mute
   button**, and while holding it plug the cable back in. The **red LED blinks**
   = Safe Mode. In this state `dfu-util -l` shows three interfaces at
   `[2886:001a]`: `alt=0` Factory, `alt=1` Upgrade, `alt=2` DataPartition.
4. **Flash the Upgrade partition (`alt=1`):**
   ```
   dfu-util -R -e -a 1 -D ~/respeaker-xvf3800-fw/xmos_firmwares/usb/respeaker_xvf3800_usb_dfu_firmware_v2.0.10.bin
   ```
   `Invalid DFU suffix signature` is a harmless warning; `-R` auto-reboots.
5. **Verify:** it should re-enumerate as USB **"reSpeaker XVF3800 4-Mic Array"**
   (vendor "Seeed Studio"), a **2-channel input at 16 kHz**. On macOS confirm
   with `ioreg -p IOUSB -l | grep "USB Product Name"` and a sounddevice device
   list. (It still answers DFU on the XMOS port even in run-time mode — normal.)

**⚠️ Do NOT run `xvf_host save_configuration` casually.** Upstream
[issue #8](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/issues/8)
reports it corrupted the config partition so the board no longer enumerated as
USB in normal mode — and a reflash did **not** fix it (no maintainer fix yet).
Factory recovery image, if ever needed: `xmos_firmwares/recover/4mb_all_ff.bin`.

**Reflash back to I2S** (for the Phase 7 ESP32-S3 satellite-v2 work) uses the
same Safe-Mode + dfu-util flow with a bin from `xmos_firmwares/i2s/`.

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
