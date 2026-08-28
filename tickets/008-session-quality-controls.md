# Ticket 008: Session quality controls

Status: `implemented`

Spec: [`specs/002-native-macos-app.md`](../specs/002-native-macos-app.md)

Implemented: add SwiftUI controls for precision (`fp16` default, `q8` speed mode), target generated frame rate (`12.5`/`25`), audio delay, speech threshold, audio window, delayed audio output toggle, and basic avatar-prep tuning (`bboxShift`, `extraMargin`, clip length). Persist selections locally and send them through `prepareAvatar`, `startSession`, and `setSessionConfig` IPC.
