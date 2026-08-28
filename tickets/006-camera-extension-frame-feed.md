# Ticket 006: Camera extension frame feed

Status: `implemented`

Spec: [`specs/002-native-macos-app.md`](../specs/002-native-macos-app.md)

Implemented: backend writes generated 1280x720 BGRA frames into an app-group latest-frame transport; the Camera Extension reads the newest complete frame at 25fps and repeats the last valid frame when generation is late. Local compile validation uses `CODE_SIGNING_ALLOWED=NO`; real camera selection still needs Apple signing/provisioning and system-extension approval.
