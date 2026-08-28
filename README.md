# meeting-survivor

Local talking-head preview for a Teams call while wearing VR goggles. It prepares a short 720p/25fps source video, uses MuseTalk 1.5 MLX locally, shows a preview window for OBS Virtual Camera, and sends delayed original mic audio to BlackHole.

This is intentionally a simple MVP, not a polished camera driver or app.

## What exists

- `meeting-survivor list-devices` prints audio devices.
- `meeting-survivor prepare` extracts avatar frames, runs MuseTalk's S3FD/DWPose prep, caches BiSeNet jaw masks, and caches MuseTalk latents.
- `meeting-survivor run` opens a fixed preview window, captures the mic, writes delayed mic audio to BlackHole, and swaps in generated lower-face crops while speech is detected.
- `scripts/acceptance.py` runs the same loop from a prerecorded WAV and records the preview plus delayed audio.

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

After this, the avatar and model weights should be reusable offline.

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
- Default model: `mlx-community/MuseTalk-1.5-q8`

## Known MVP shortcuts

- Live audio encoding uses a short rolling window and latest chunk, not a heavily optimized streaming Whisper cache.
- OBS and BlackHole are configured manually.
- No mic audio is recorded by default in live mode.
