import SwiftUI

struct PreviewPane: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 20) {
            RoundedRectangle(cornerRadius: 16)
                .fill(.black.gradient)
                .overlay {
                    VStack(spacing: 8) {
                        Image(systemName: model.sessionState.isRunning ? "video.fill" : "video")
                            .font(.system(size: 48))
                        Text(model.sessionState.isRunning ? "Session running" : "Preview will appear here")
                            .font(.title3)
                        Text(model.selectedAvatarName)
                            .font(.caption)
                    }
                    .foregroundStyle(.white.opacity(0.85))
                }
                .aspectRatio(16.0 / 9.0, contentMode: .fit)
                .padding()

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                StatusRow(label: "App", value: model.appServiceStatus)
                StatusRow(label: "Session", value: model.sessionStatus)
                StatusRow(label: "Camera", value: model.cameraStatus)
                StatusRow(label: "Audio", value: model.audioStatus)
                StatusRow(label: "Input", value: model.selectedInputDeviceName)
                StatusRow(label: "Output", value: model.selectedOutputDeviceName)
                StatusRow(label: "Delay", value: "\(model.audioDelayMs) ms")
                StatusRow(label: "Activity", value: model.activityStatus)
            }

            HStack {
                Picker("Input", selection: $model.selectedInputDeviceID) {
                    Text("None").tag("")
                    ForEach(model.audioDevices.filter(\.isInput)) { device in
                        Text(device.name).tag(device.id)
                    }
                }
                Picker("Output", selection: $model.selectedOutputDeviceID) {
                    Text("None").tag("")
                    ForEach(model.audioDevices.filter(\.isOutput)) { device in
                        Text(device.name).tag(device.id)
                    }
                }
            }

            HStack {
                Stepper("Audio Delay: \(model.audioDelayMs) ms", value: $model.audioDelayMs, in: 0...2_000, step: 25)
                    .onChange(of: model.audioDelayMs) { _, value in
                        Task { await model.setAudioDelay(ms: value) }
                    }
                Spacer()
                Button("Refresh") {
                    Task { try? await model.refreshBackendData() }
                }
                Button(model.sessionState.isRunning ? "Stop Session" : "Start Session") {
                    Task {
                        if model.sessionState.isRunning {
                            await model.stopSession()
                        } else {
                            await model.startSession()
                        }
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.isSessionActionInFlight || (!model.sessionState.isRunning && model.selectedAvatarID == nil))
            }

            if let stats = model.sessionStats {
                HStack(spacing: 16) {
                    Text("Preview \(stats.previewFps, specifier: "%.1f") fps")
                    Text("Generated \(stats.generatedFps, specifier: "%.1f") fps")
                    Text("Queue \(stats.queueDepth)")
                    Text("Dropped \(stats.droppedJobs)")
                    Text("Render \(stats.renderMs, specifier: "%.1f") ms")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .padding()
    }
}

private struct StatusRow: View {
    let label: String
    let value: String

    var body: some View {
        GridRow {
            Text(label).fontWeight(.semibold)
            Text(value).foregroundStyle(.secondary)
        }
    }
}
