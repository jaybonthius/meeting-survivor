# meeting-survivor

Local talking-head preview for a Teams call while wearing VR goggles. It prepares a short 720p/25fps source video, uses MuseTalk 1.5 MLX locally, shows a preview window for OBS Virtual Camera, and sends delayed original mic audio to BlackHole.

This is intentionally a simple MVP, not a polished camera driver or app.

## What exists

- `meeting-survivor list-devices` prints audio devices.
- `meeting-survivor prepare` extracts avatar frames, runs MuseTalk's S3FD/DWPose prep, caches BiSeNet jaw masks, and caches MuseTalk latents.
- `meeting-survivor run` opens a fixed preview window, captures the mic, writes delayed mic audio to BlackHole, and swaps in generated lower-face crops while speech is detected.
- `scripts/acceptance.py` runs the same loop from a prerecorded WAV and records the preview plus delayed audio.
- `MeetingSurvivor.xcodeproj` builds a native SwiftUI control app that launches the Python backend, previews generated frames, and can feed the local CoreMediaIO camera-extension transport when signing allows it.

## Native macOS app dev build

Build and run the unsigned development app from the repo root:

```bash
uv sync
xcodebuild -project MeetingSurvivor.xcodeproj \
  -scheme MeetingSurvivor \
  -configuration Debug \
  -derivedDataPath .build/DerivedData \
  build CODE_SIGNING_ALLOWED=NO
open .build/DerivedData/Build/Products/Debug/MeetingSurvivor.app
```

The native app defaults to `fp16`, target generated FPS `12.5`, `400ms` delay, speech threshold `0.012`, and a `1.2s` audio window. Live tuning controls update delay, target generated FPS, VAD threshold, and audio window without restarting the session.

Changing precision or selecting another prepared avatar while running is staged: the backend keeps streaming the current avatar, loads the requested avatar/model in the background, then swaps only after the new assets are ready. The sidebar shows a spinner for the pending avatar and the preview/camera feed keeps repeating current frames until the swap completes.

The app can optionally send delayed mic audio to the selected output device for BlackHole-style demos; choose `BlackHole 2ch`, enable **Send delayed mic to output**, then start the session. This toggle is start-time only for now. The first-party virtual microphone remains future driver/plugin work.

Useful app diagnostics are written to `~/Library/Application Support/Meeting Survivor/logs/backend.log`. The unsigned dev build can preview locally; real camera selection in Zoom/Teams still requires Apple signing/provisioning and system-extension approval.

## Manual setup

1. Install system apps:
   - OBS Studio
   - BlackHole 2ch
2. Use headphones. Do not play meeting audio through speakers.
3. Record a 5-10 second source video:
   - 1280x720
   - 25 fps
   - front-facing face
   - stable lighting
   - neutral closed mouth
4. Install Python deps:

```bash
uv sync
```

## Prepare avatar

First run downloads q8 MuseTalk MLX weights into `models/` unless you pass your own `--weights-dir`.

```bash
uv run meeting-survivor prepare path/to/source.mp4 --download-model --avatar-dir avatars/me
```

After this, the avatar and model weights should be reusable offline. If mouth motion is too weak, prepare a variant with `--bbox-shift 10` or `--bbox-shift 20`; if the jaw/chin is clipped, adjust `--extra-margin` between 0 and 40.

## List audio devices

```bash
uv run meeting-survivor list-devices
```

Find your physical mic and BlackHole output device id/name.

## Run live

```bash
uv run meeting-survivor run \
  --avatar-dir avatars/me \
  --input-device "Your Microphone" \
  --output-device "BlackHole 2ch" \
  --delay-ms 400 \
  --generated-fps 25.0
```

If inference cannot keep up, try:

```bash
uv run meeting-survivor run \
  --avatar-dir avatars/me \
  --input-device "Your Microphone" \
  --output-device "BlackHole 2ch" \
  --delay-ms 400 \
  --generated-fps 12.5
```

Press `q` in the preview window or Ctrl-C to stop.

## OBS and Teams

1. Start OBS.
2. Add a Window Capture source for the `meeting-survivor` preview window.
3. Start OBS Virtual Camera.
4. In Teams:
   - Camera: OBS Virtual Camera
   - Microphone: BlackHole 2ch
5. Tune `--delay-ms` until the mouth and voice are acceptable.

## Prerecorded acceptance run

Use a WAV with speech, pauses, and final silence:

```bash
uv run python scripts/acceptance.py \
  --avatar-dir avatars/me \
  --wav path/to/test.wav \
  --seconds 300 \
  --delay-ms 400
```

Outputs go to `outputs/acceptance/`.

## Pins

- MuseTalk MLX source: `https://github.com/xocialize/musetalk-mlx`
- Revision: `c6eb30ebd1ed4d043983209813370153de9346bf`
- CLI default model: `mlx-community/MuseTalk-1.5-q8`; native app default: `mlx-community/MuseTalk-1.5-fp16`

## Known MVP shortcuts

- Live audio encoding uses a short rolling window and latest chunk, not a heavily optimized streaming Whisper cache.
- OBS and BlackHole are configured manually.
- No mic audio is recorded by default in live mode.
