# Ticket 005: Camera extension static frame

Status: `implemented`

Spec: [`specs/002-native-macos-app.md`](../specs/002-native-macos-app.md)

Implemented: added a minimal CoreMediaIO Camera Extension that publishes `Meeting Survivor Camera` with a static 720p/25fps test frame. Local compile validation uses `CODE_SIGNING_ALLOWED=NO`; real activation still needs Apple signing/provisioning.
