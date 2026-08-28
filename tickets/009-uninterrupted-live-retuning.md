# Ticket 009: Uninterrupted live retuning

Status: `implemented`

Spec: [`specs/002-native-macos-app.md`](../specs/002-native-macos-app.md)

Implemented: replace running-session stop/restart behavior with a thread-safe live control seam. Audio delay, target generated FPS, speech threshold, and audio window update in the running render loop. Avatar and precision switches are staged: the backend continues streaming the current avatar while loading requested assets in the background, then swaps only after the new avatar/model is ready.
