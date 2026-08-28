# Meeting Survivor agent guide

## Project workflow

- Feature specs live in `specs/`; implementation tickets live in `tickets/`.
- Treat each spec as a feature track. Work one ticket at a time.
- Current ticket: `tickets/005-camera-extension-static-frame.md`.
- Keep the existing CLI/OBS/BlackHole MVP working while building the native macOS app track.

## Engineering style

- KISS: prefer the smallest vertical slice that proves the next architecture seam.
- YAGNI: do not build UI polish, packaging, extension, driver, or framework abstractions before the current ticket needs them.
- Preserve working behavior first; add seams around it before refactoring internals.
- Use typed state and IPC for app behavior. Logs are diagnostics only.
- Do not expose backend process controls in user-facing UI; the app owns service startup, shutdown, and recovery.
- Keep media local. Do not add cloud services, telemetry, accounts, or remote media upload.

## Native macOS app track

- Start with backend IPC before SwiftUI, virtual camera, virtual microphone, or packaging work.
- Keep Python as a separate backend process for MuseTalk/MLX/OpenCV/audio work.
- Use newline-delimited JSON-RPC over a Unix domain socket for backend communication.
- Preserve existing CLI commands as diagnostics and regression coverage.
