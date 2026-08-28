# Ticket 001: Backend IPC foundation

Status: `implemented`

Spec: [`specs/002-native-macos-app.md`](../specs/002-native-macos-app.md)

## Goal

Add the first native-app backend slice: a long-running Python backend mode that exposes typed newline-delimited JSON-RPC over a Unix domain socket, without changing the existing CLI/OBS/BlackHole workflow.

## Scope

- Add `meeting-survivor backend --socket <path> --app-support <path>` or equivalent CLI subcommand.
- Define request, response, event, and error envelopes for protocol version 1.
- Implement `handshake`, `listAudioDevices`, `listAvatars`, `prepareAvatar`, `cancelOperation`, and `shutdown`.
- Return structured audio device records for IPC while preserving the existing human-readable `list-devices` CLI output.
- Scan app-support avatar storage for `listAvatars` and read existing avatar metadata where available.
- Emit typed prepare progress/completion/failure events; logs remain diagnostics only.
- Run long `prepareAvatar` work off the socket event loop and support cancellation by operation id where safe.
- Add automated smoke tests for socket framing, handshake, errors, device listing shape, avatar listing shape, and shutdown.

## Non-goals

- No SwiftUI app implementation.
- No CoreMediaIO camera extension.
- No CoreAudio virtual microphone plugin.
- No frame transport implementation.
- No Python packaging, signing, notarization, installer, or updater work.
- No rewrite of MuseTalk, MLX, avatar cache format, or the live render loop.

## Acceptance criteria

- Existing commands `meeting-survivor list-devices`, `meeting-survivor prepare`, and `meeting-survivor run` keep their current behavior.
- A backend process listens on a caller-provided Unix socket and accepts newline-delimited JSON requests.
- `handshake` succeeds for `protocolVersion: 1` and fails clearly for incompatible versions.
- Malformed JSON, missing request fields, and unknown methods return structured errors rather than crashing the backend.
- `listAudioDevices` returns JSON records with stable fields suitable for Swift `Codable` models.
- `listAvatars` returns structured avatar records from app-support storage.
- `prepareAvatar` reports state through typed events and returns a final structured result or error.
- `shutdown` closes the server cleanly.
- Tests can exercise the backend IPC without parsing stdout/stderr logs for state.

## Validation

- Run the new IPC test suite.
- Run a CLI compatibility smoke test for `meeting-survivor list-devices` where CoreAudio access is available.
- If a prepared avatar and WAV fixture are available, run the existing prerecorded smoke path after the refactor to confirm the live CLI was not broken.

## Notes and risks

- The current live loop is blocking and process-oriented; defer `startSession` until that loop has explicit stop, event, and frame-sink seams.
- The current avatar preparation path reports progress through Rich/log output; this ticket should add a callback/event seam without broad restructuring.
- The native app track should use `~/Library/Application Support/Meeting Survivor/` in production, but tests should use temp directories.
