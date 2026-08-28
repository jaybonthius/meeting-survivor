# Feature 1: CLI/OBS/BlackHole MVP

Status: `implemented-mvp`

This feature captures the first working implementation track: a Python CLI that uses OBS Virtual Camera and BlackHole for manual Teams integration. It remains the baseline and regression target while Feature 2 moves toward a native macOS app.

## Problem Statement

I want to join a Microsoft Teams call from an M3 Max Mac while wearing VR goggles and use a prerecorded video of myself as my camera. My live microphone audio should animate the mouth in that video locally and in real time, so participants see a recognizable, photorealistic version of me speaking. The setup must use free, open-source components, must not send media to a cloud service, and must be simple enough to build and use today rather than becoming a polished product.

## Solution

Build a small Python command-line application around MuseTalk 1.5 and MLX. I record one short neutral 25-fps video of myself without the goggles. The application prepares that video once, then loops its frames during a call and uses live microphone audio to regenerate and composite only the speaking face region. It displays a continuous 720p preview that OBS sends to Teams through OBS Virtual Camera. It also sends a fixed-delay copy of the original microphone audio to BlackHole so the voice remains acceptably aligned with the generated mouth.

The MVP optimizes for one user, one M3 Max Mac, and one Teams call. Manual device selection and delay adjustment are acceptable. A native macOS application, installer, custom camera driver, and perfect studio-quality animation are not required.

## User Stories

1. As the caller, I want to prepare an avatar from a short video of myself, so that the camera output retains my actual appearance.
2. As the caller, I want avatar preparation to run entirely on my Mac, so that my biometric media is not uploaded to another service.
3. As the caller, I want to prepare the avatar once and reuse its cached data, so that meeting startup is quick.
4. As the caller, I want the application to use my live physical microphone, so that the avatar speaks when I speak without TTS or transcription.
5. As the caller, I want the original microphone audio sent to Teams, so that participants hear my real voice rather than resynthesized audio.
6. As the caller, I want the avatar mouth to follow my speech closely enough for a Teams call, so that the result appears plausibly synchronized.
7. As the caller, I want the source video to keep its original head movement, blinking, hair, lighting, and background, so that the result remains recognizable as me.
8. As the caller, I want untouched source frames while I am silent, so that the mouth does not quiver or remain stuck in a speaking pose.
9. As the caller, I want the output camera to continue producing frames if inference is briefly late, so that Teams does not see a frozen or disconnected camera.
10. As the caller, I want bounded latency rather than an ever-growing render queue, so that a long call does not drift further behind my speech.
11. As the caller, I want a visible local preview, so that I can verify the avatar before enabling it in Teams.
12. As the caller, I want to select the microphone and BlackHole output device from the command line, so that the program works with my actual audio setup without a settings UI.
13. As the caller, I want to adjust one audio-delay value, so that I can make a practical lip-sync correction without building automatic calibration.
14. As the caller, I want clear setup and run instructions, so that I can install the dependencies and start a call without understanding the model internals.
15. As the caller, I want the program to report frame rate, queue depth, and missed frames, so that I can tell whether the M3 Max is keeping up.
16. As the caller, I want a clean stop command, so that the microphone and output devices are released after the call.
17. As the caller, I want all inference to keep working after the initial model download with networking disabled, so that the runtime is demonstrably local.
18. As the builder, I want one end-to-end media pipeline and one high-level acceptance harness, so that I can finish the MVP without creating unnecessary abstractions or test layers.

## Implementation Decisions

- Implement one Python application for Apple Silicon. Do not build a Swift application or separate services.
- Use Python 3.11 or 3.12 with `uv` for reproducible environment setup.
- Use the published MuseTalk 1.5 MLX implementation and pin the selected source revision and model revisions. Do not rewrite the neural models.
- Start with the q8 MuseTalk weights on the M3 Max. Keep fp16 as an optional quality comparison, not a second implementation path.
- Use a prerecorded 5–10 second, constant-25-fps, 1280×720 video with a mostly frontal face, stable lighting, a closed neutral mouth, and subtle natural motion.
- Use MuseTalk's established rtmlib/DWPose, S3FD, and BiSeNet preparation path. Run face detection, landmarks, masks, crops, and source-latent generation once and cache the results as one reusable avatar directory.
- Do not substitute MediaPipe during the MVP. It changes crop and landmark behavior without helping the live path.
- Capture microphone audio at the device's native rate, expected to be 48 kHz. Keep this original stream for Teams and produce a timestamp-aligned 16-kHz mono copy for MuseTalk.
- Adapt MuseTalk's Whisper path to consume a bounded rolling audio window rather than complete audio files. Reuse LiveTalking's 20-ms block, rolling-context, microbatch, silence, and avatar-frame concepts without importing its CUDA inference path.
- Use a simple RMS-based voice activity threshold with short attack and release hysteresis. During silence, output the unmodified prerecorded frame and skip neural inference.
- Use one monotonic media timeline. Schedule output at 25 fps and associate every generated frame with its source-audio time.
- Begin with batch 1 and permit batch 2 or 4 only when measured throughput requires it. Do not allow inference work to accumulate without bound.
- When a frame misses its deadline, output the newest valid generated frame or the corresponding untouched avatar frame and discard stale pending work.
- Target 25 generated frames per second. If the M3 Max cannot sustain that rate, permit 12.5 generated frames per second with each frame repeated once into a continuous 25-fps output.
- Composite the generated 256-pixel face crop back onto the original 720p frame with the cached MuseTalk mask. Do not generate the full 720p image.
- Present the result in a fixed-size preview window. OBS captures that window and exposes OBS Virtual Camera; Teams selects OBS Virtual Camera. Do not build a camera extension or use WebRTC, RTMP, NDI, or an embedded OBS controller.
- Maintain a fixed-size ring containing the original microphone PCM and write the delayed stream to a user-selected BlackHole output. Teams selects BlackHole as its microphone.
- Default the audio delay to 400 ms and expose it as a command-line setting. Manual tuning is sufficient; do not build automatic flash/chirp calibration, clock-drift correction, or a custom audio driver.
- Require headphones during use to prevent speaker audio from retriggering the avatar and feeding back into Teams.
- Provide three command surfaces only: prepare an avatar, run the live camera, and list available audio devices. A separate graphical settings application is out of scope.
- The live command accepts the prepared avatar, physical microphone, BlackHole device, audio delay, model precision, and generated-frame-rate setting.
- Log startup checks, selected devices, achieved inference rate, missed frame count, and current queue depth. Do not record or persist microphone audio by default.
- Fail clearly if model weights, avatar cache, microphone, BlackHole, or MLX support are unavailable. Release audio devices and terminate cleanly on interruption.
- Download model weights only during explicit setup or first preparation. The prepared avatar and live command must run with networking disabled afterward.
- Document the minimal manual setup: install dependencies, install OBS and BlackHole, record the source video, prepare the avatar, start OBS Virtual Camera, run the application, and select the OBS camera and BlackHole microphone in Teams.
- Keep the implementation in one repository and one process. Add a module boundary only when required to isolate avatar preparation, the live media loop, or an external dependency.

## Testing Decisions

- Use one primary end-to-end seam: given a prepared avatar and a paced prerecorded WAV file, run the same live media loop used for the microphone and produce a fixed-cadence preview recording plus the delayed WAV output.
- Test externally visible behavior rather than MLX internals. The test should observe frame cadence, total duration, bounded queue depth, silence behavior, delayed-audio duration, and clean shutdown.
- Use a short fixture containing speech, pauses, and a final silence. Verify that speech frames differ in the mouth region, silent frames return to the source video, output remains 25 fps, and audio delay matches the configured value within one video frame.
- Run the fixture for at least five minutes in a loop and verify that queue depth and memory do not grow continuously.
- Benchmark q8 at generated rates of 25 and 12.5 fps on the M3 Max. Accept 25 fps when the warmed p95 render time stays within the available frame budget without queue growth; otherwise use the 12.5-fps fallback.
- Perform one manual Teams smoke test using OBS Virtual Camera and BlackHole. Confirm that Teams receives continuous video and audible microphone output, and manually tune the fixed delay until lip sync is acceptable.
- Perform one offline-runtime check by disabling networking after weights and avatar assets are prepared, then starting and using the live command successfully.
- The MVP is complete when it can run for a 15-minute Teams call on the M3 Max without crashing, losing the camera, growing its render queue, or producing obviously unusable lip sync.
- There is no existing test prior art because the repository is empty. Prefer the single end-to-end harness over a large unit-test suite.

## Out of Scope

- Windows, Linux, Intel Macs, iPhones, and Apple Silicon models other than the target M3 Max.
- Multiple avatars, multiple faces, live avatar switching, or multiple simultaneous microphones.
- A native Swift or SwiftUI application, menu-bar application, signed installer, notarization, or App Store distribution.
- A custom CoreMediaIO camera extension or CoreAudio virtual microphone driver.
- Automatic OBS configuration, OBS websocket control, direct Teams integration, or browser automation.
- Automatic A/V calibration, long-term clock-drift correction, Bluetooth-specific timing support, or studio-grade synchronization.
- Reactive gaze, emotion, nodding, gesture generation, upper-body generation, or live head-pose generation. Head motion comes only from the prerecorded loop.
- TTS, ASR, voice conversion, meeting transcription, meeting bots, cloud APIs, or remote inference.
- Training or fine-tuning a personal model.
- Supporting arbitrary source videos or difficult poses. The MVP may require a clean frontal recording made specifically for it.
- Perfect handling of hands, microphones, VR goggles, or other objects crossing the face in the prerecorded video.
- 1080p or 4K generation, 60-fps output, cinematic quality, or imperceptible latency.
- Bundling or redistributing OBS, BlackHole, model weights, or third-party checkpoints.
- General-purpose plugin architecture, RPC services, databases, web dashboards, telemetry, accounts, or configuration migration.

## Further Notes

- The project is intentionally an experiment for a Teams call, not a reusable commercial product.
- MuseTalk modifies the lower face; it does not generate responsive full-head behavior. The quality of the short source loop will strongly affect whether the result feels natural.
- Published MuseTalk MLX throughput was measured on a faster M5 Max and excludes parts of this pipeline. The M3 Max benchmark is therefore the first implementation gate, not an optional optimization exercise.
- Batching and Whisper context may require several hundred milliseconds of intentional audio delay. Practical Teams-call acceptability matters more than minimizing the number in isolation.
- Preserve all upstream license and model notices. Repository licenses do not automatically establish redistribution rights for every downloaded checkpoint, but redistribution is not required for this personal local MVP.
