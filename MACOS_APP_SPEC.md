# Meeting Survivor macOS App Spec

Status: `draft`

## Goal

Build a native macOS application for Meeting Survivor that lets a user choose a microphone, choose an audio output/meeting microphone path, prepare and select talking-head videos, preview the generated avatar, and publish the result as a real virtual camera without OBS. The app should keep the existing Python/MuseTalk/MLX backend for media generation, but communicate with it through structured local IPC, not logs or shell parsing.

## Product Principles

- No hacks for app state: logs are diagnostics only; UI state, progress, errors, and commands use typed structured IPC.
- Keep the backend in Python because MuseTalk, MLX, OpenCV, DWPose, BiSeNet, sounddevice, and model-loading already work there.
- Keep the UI native SwiftUI because device selection, file picking, progress, permissions, and app lifecycle should feel like a Mac app.
- Replace OBS with a first-party virtual camera using Apple's supported CoreMediaIO Camera Extension path.
- Replace BlackHole with a first-party virtual audio output/microphone path using a CoreAudio virtual audio driver/plugin path.
- Build narrow vertical slices in the correct architecture rather than a polished product shell around fragile internals.
- Store media, model weights, avatar caches, settings, and logs locally; never upload biometric media or microphone audio.

## User Experience

The app opens to a single main window with a left sidebar and a main preview area.

Sidebar:

- Shows prepared avatars/videos.
- Has an `Add Video` button for selecting a source video.
- Shows preparation state per video: not prepared, preparing, ready, failed.
- Allows selecting the active avatar.
- Later: allows hot-switching between already prepared avatars during a running session.

Main area:

- Shows a large live preview of the generated camera output.
- Shows clear setup status: backend ready, camera extension ready, audio device ready, model ready, selected mic, selected output.
- Shows start/stop controls.
- Shows compact performance metrics: preview fps, generated fps, render time, dropped generation jobs, queue depth.

Controls:

- Input device picker: physical microphone, e.g. AirPods Pro or MacBook microphone.
- Output/meeting microphone picker: the app's virtual microphone once implemented.
- Precision picker: q8 default, fp16 quality comparison, q4 hidden unless explicitly enabled later.
- Generated frame rate picker: 12.5 or 25, default chosen by measured throughput.
- Audio delay slider/input, default 400ms.
- Optional advanced avatar tuning: bbox shift, extra margin, parsing mode, cheek widths. Hide behind an “Advanced” disclosure.

## Architecture

The app has four cooperating components.

1. `MeetingSurvivor.app` SwiftUI host:

- Owns UI, file selection, user settings, permissions, app lifecycle, and process lifecycle.
- Starts/stops the Python backend service.
- Talks to the backend through structured IPC.
- Talks to the camera/audio extension control surfaces as needed.
- Does not parse backend logs for state.

2. Python backend service:

- Owns MuseTalk/MLX inference, avatar preparation, frame generation, audio capture/processing, and model management.
- Exposes a local structured API over a Unix domain socket.
- Emits typed progress and stats events.
- Writes diagnostics logs separately.
- Produces video frames for preview and virtual camera publishing through an explicit frame transport.

3. CoreMediaIO Camera Extension:

- Publishes `Meeting Survivor Camera` as a selectable camera in Teams/Zoom/etc.
- Receives generated 720p/25fps frames from the app/backend via an app-group-safe local transport.
- Keeps output cadence stable even when generated mouth inference is late.
- Does not load ML models or Python.

4. Virtual audio device/plugin:

- Publishes `Meeting Survivor Microphone` as a selectable microphone in Teams/Zoom/etc.
- Receives delayed original microphone PCM from the backend/app audio pipeline.
- Avoids feedback by requiring headphones and by never routing meeting speaker audio back into the virtual mic.
- Does not synthesize or transform voice; it passes through delayed original mic audio.

## IPC Requirement

Use structured IPC between Swift and Python. Do not scrape stdout/stderr logs.

Initial protocol: JSON-RPC-like messages over a Unix domain socket using newline-delimited JSON.

Reasons:

- Local-only with no TCP port conflicts.
- Simple to implement in Python `asyncio` and Swift `Codable`.
- Supports request/response plus backend-pushed events.
- Easier to debug than protobuf/gRPC while still being typed and versionable.

Protocol rules:

- Every request has `id`, `method`, and optional `params`.
- Every response has `id` and either `result` or `error`.
- Events have `method: "event"` and typed `params`.
- Include `protocolVersion` during handshake.
- Unknown methods and incompatible protocol versions fail clearly.
- Cancellation is explicit via request id or operation id.

Example requests:

```json
{"id":"1","method":"handshake","params":{"protocolVersion":1}}
{"id":"2","method":"listAudioDevices"}
{"id":"3","method":"listAvatars"}
{"id":"4","method":"prepareAvatar","params":{"videoPath":"/Users/me/video.mov","avatarName":"walking","bboxShift":0,"extraMargin":10}}
{"id":"5","method":"startSession","params":{"avatarId":"walking","inputDeviceId":"6","virtualCamera":true,"virtualMicrophone":true,"precision":"q8","generatedFps":12.5,"delayMs":400}}
{"id":"6","method":"stopSession"}
```

Example events:

```json
{"method":"event","params":{"type":"prepareProgress","operationId":"op-1","stage":"landmarks","current":42,"total":241}}
{"method":"event","params":{"type":"sessionStats","previewFps":25.0,"generatedFps":8.4,"queueDepth":1,"droppedJobs":12,"renderMs":118}}
{"method":"event","params":{"type":"sessionState","state":"running"}}
{"method":"event","params":{"type":"error","severity":"recoverable","message":"Selected microphone disconnected"}}
```

## Frame Transport

The app must not rely on OBS/window capture for production camera output.

Preferred first implementation:

- Backend renders final 720p BGRA frames into a small shared-memory ring buffer or IOSurface-compatible transport.
- Camera extension reads the newest complete frame and publishes it at 25fps.
- If no fresh frame is available, the extension repeats the latest valid frame or a neutral avatar frame.
- Ring buffer is bounded; stale frames are overwritten, not queued indefinitely.

Acceptable spike fallback:

- Swift host receives compressed or raw frames from backend and feeds both preview and camera extension.
- This is acceptable only if measured latency and CPU are reasonable.

Not acceptable:

- OBS window capture as the product camera output.
- Parsing preview window pixels.
- Log-driven frame state.
- Unbounded frame queues.

## Audio Transport

The app must eventually remove the BlackHole dependency by publishing its own virtual microphone.

Preferred path:

- Use a CoreAudio HAL virtual audio device/plugin equivalent in purpose to BlackHole.
- Backend captures the physical mic, maintains a bounded delay buffer, and writes delayed PCM into the virtual mic path.
- The virtual mic exposes a normal CoreAudio input device selected by Teams.

Constraints:

- This is a driver/plugin-quality subsystem and must have installer, uninstaller, signing, and notarization treatment.
- Do not fork/bundle BlackHole without resolving GPL/commercial licensing.
- If replacing BlackHole blocks early app iteration, the spec still treats the virtual mic as required for the complete macOS app, not as optional polish.

## Storage

Use `~/Library/Application Support/Meeting Survivor/` for app-owned state:

```text
Application Support/Meeting Survivor/
  config.json
  avatars/
  models/
  logs/
  ipc/
  cache/
```

Source videos selected by the user remain in their original locations unless the user explicitly imports/copies them. In sandboxed builds, persist access using security-scoped bookmarks.

## Permissions and Entitlements

The app needs:

- Microphone permission and usage description.
- User-selected file read access for source videos.
- App group entitlement if sharing data/IPC with a Camera Extension.
- System Extension entitlement for the camera extension host flow.
- Correct signing/notarization for the app, extension, backend helper, Python native libraries, and audio driver/plugin.

## Virtual Camera Requirements

- Device name: `Meeting Survivor Camera`.
- Resolution: 1280×720.
- Output cadence: 25fps.
- If backend is late, camera remains connected and repeats latest valid frame.
- Camera extension must survive backend restart by showing a neutral placeholder frame and emitting a UI-visible error.
- Teams must be able to select the camera without OBS installed.

Implementation path:

1. Build a minimal camera extension that publishes a static test frame.
2. Feed it frames from the Swift host.
3. Feed it frames from Python backend output.
4. Add restart/error handling.
5. Validate in Teams.

## Virtual Microphone Requirements

- Device name: `Meeting Survivor Microphone`.
- Sample rate: 48kHz preferred.
- Channels: mono or stereo as required by CoreAudio compatibility; mono content is acceptable.
- Audio source: delayed original physical microphone audio.
- Default delay: 400ms.
- Teams must be able to select the microphone without BlackHole installed.

Implementation path:

1. Build or integrate a minimal local CoreAudio virtual audio device/plugin that emits silence.
2. Feed delayed mic PCM into it.
3. Validate that Teams receives audio.
4. Add clean install/uninstall and permission handling.

## Backend API Surface

Required backend methods:

- `handshake`
- `listAudioDevices`
- `listAvatars`
- `prepareAvatar`
- `cancelOperation`
- `deleteAvatar`
- `startSession`
- `stopSession`
- `getSessionState`
- `setActiveAvatar`
- `setAudioDelay`
- `shutdown`

Required event types:

- `backendReady`
- `prepareProgress`
- `prepareCompleted`
- `prepareFailed`
- `sessionState`
- `sessionStats`
- `deviceChanged`
- `error`

## Live Avatar Switching

Initial behavior:

- Only prepared avatars can be selected.
- Switching avatars during a session is allowed if the target avatar cache is ready.
- First implementation may briefly fade/freeze for one frame while swapping caches.
- Backend must not reload model weights on every avatar switch.

Non-goal for first app version:

- Seamless frame-perfect crossfade between avatars.
- Switching to an unprepared video during a live session.

## Packaging Strategy

Development build:

- SwiftUI app launches the repo-local backend via `uv run meeting-survivor-backend` or an explicit backend script.
- Used only for development.
- Still uses structured IPC.

Packaged personal build:

- Swift app bundles a Python backend helper using PyInstaller `onedir` or `python-build-standalone` plus a locked wheelhouse.
- Model weights are downloaded into Application Support on first setup or copied from existing local cache.
- The app signs nested helper binaries and native libraries.

Release-quality build:

- Use `python-build-standalone` or a similarly reproducible Python runtime layout.
- Sign inside-out; do not rely on `codesign --deep` as the signing method.
- Notarize and staple the final distribution.
- Include installer/uninstaller support for camera extension and virtual audio component.

## UI Implementation Plan

SwiftUI views:

```text
MacApp/
  MeetingSurvivorApp.swift
  AppModel.swift
  Views/
    RootView.swift
    SidebarView.swift
    PreviewPane.swift
    DeviceControlsView.swift
    AvatarDetailView.swift
    PrepareProgressView.swift
    AdvancedAvatarSettingsView.swift
  Services/
    BackendClient.swift
    BackendProcess.swift
    AudioDeviceStore.swift
    AvatarStore.swift
    AppStoragePaths.swift
    SecurityScopedBookmarks.swift
```

Root layout:

- `NavigationSplitView` with `SidebarView` and `PreviewPane`.
- Sidebar list uses stable avatar ids.
- Main preview uses native rendering; during the camera-extension phase, preview should show the same frames being sent to the virtual camera.
- Controls sit below or beside preview depending on window width.

## Python Backend Refactor Plan

Keep existing CLI commands, but add a long-running backend mode:

```bash
meeting-survivor backend --socket /path/to/app.sock --app-support /path/to/ApplicationSupport
```

Backend responsibilities:

- load models once
- prepare avatars with progress events
- maintain avatar registry
- run live session
- publish frames to frame transport
- publish delayed audio to virtual mic transport
- expose clean stop/shutdown

CLI remains useful for diagnostics and development, but the app uses backend mode.

## Acceptance Criteria

A working app slice is complete when:

1. User opens the macOS app and sees a native sidebar/main-preview layout.
2. User can add a source video with a file picker.
3. User can prepare that video and see real progress through structured events.
4. User can select microphone and virtual output devices from dropdowns.
5. User can start/stop a session from the app.
6. Preview remains 25fps while speaking.
7. `Meeting Survivor Camera` appears in Teams and receives continuous 720p/25fps video without OBS.
8. `Meeting Survivor Microphone` appears in Teams and receives delayed original microphone audio without BlackHole.
9. Backend crashes or disconnects produce clear UI errors, not silent failure.
10. No app behavior depends on parsing log text.
11. Media remains local after initial model downloads.

## Phased Delivery

Phase 1: structured backend foundation

- Add backend socket mode to Python.
- Define typed request/response/event protocol.
- SwiftUI development app can list devices and prepare avatars via IPC.
- No log scraping.

Phase 2: native app preview/control

- Add sidebar avatar library.
- Add video picker.
- Add prepare progress UI.
- Add start/stop session UI.
- Preview works inside app.

Phase 3: virtual camera

- Add CoreMediaIO Camera Extension with static frame.
- Add frame transport.
- Feed generated frames to `Meeting Survivor Camera`.
- Validate in Teams without OBS.

Phase 4: virtual microphone

- Add CoreAudio virtual audio device/plugin.
- Feed delayed original mic PCM.
- Validate in Teams without BlackHole.

Phase 5: packaging

- Bundle backend runtime and dependencies.
- Move models/avatars to Application Support.
- Add signing/notarization/install/uninstall workflows.

## Research-Backed Constraints

- Apple's supported modern virtual camera path is CoreMediaIO Camera Extensions packaged with a host app/system extension; legacy DAL plugins are not a good target for current macOS.
- Replacing BlackHole means building or integrating a CoreAudio virtual audio device/plugin, which is real driver/plugin work and must be signed/notarized correctly.
- Embedding CPython directly in the Swift process is possible but high-risk with MLX/OpenCV/Torch/ONNX/native wheels; a bundled backend process is the safer production architecture.
- MLX requires Apple Silicon and a native ARM Python runtime; the first packaged app should target Apple Silicon only.

## Explicit Non-Goals for the First Native App Slice

- Rewriting MuseTalk or MLX in Swift.
- Cloud inference, telemetry, accounts, or remote media upload.
- Supporting Intel Macs.
- Shipping a public notarized installer before the local app architecture works.
- Frame-perfect avatar hot switching.
- Automatic lip-sync calibration.
- Perfectly reproducing the user's real mouth motion; MuseTalk generates plausible audio-driven mouth motion, not exact personal mouth kinematics.
