import SwiftUI

struct PreviewPane: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 20) {
            ZStack {
                RoundedRectangle(cornerRadius: 16)
                    .fill(.black.gradient)
                if let image = model.previewImage {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                } else {
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
            }
            .aspectRatio(16.0 / 9.0, contentMode: .fit)
            .padding()

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                StatusRow(label: "App", value: model.appServiceStatus)
                StatusRow(label: "Session", value: model.sessionStatus)
                StatusRow(label: "Camera", value: model.cameraStatus)
                StatusRow(label: "Audio", value: model.audioStatus)
                StatusRow(label: "Virtual Camera", value: model.virtualCameraStatus)
                StatusRow(label: "Virtual Mic", value: model.virtualMicrophoneStatus)
                StatusRow(label: "Input", value: model.selectedInputDeviceName)
                StatusRow(label: "Output", value: model.selectedOutputDeviceName)
                StatusRow(label: "Precision", value: model.sessionState.precision)
                StatusRow(label: "Target FPS", value: String(format: "%.1f", model.targetGeneratedFps))
                StatusRow(label: "Delay", value: "\(model.audioDelayMs) ms")
                StatusRow(label: "Control", value: model.sessionControlStatus)
                if model.pendingAvatarID != nil {
                    StatusRow(label: "Pending Avatar", value: model.pendingAvatarName)
                }
                if let pendingPrecision = model.pendingPrecision {
                    StatusRow(label: "Pending Precision", value: pendingPrecision)
                }
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
                Toggle("Send delayed mic to output", isOn: $model.sendDelayedAudioToOutput)
                    .disabled(model.sessionState.isRunning)
            }

            GroupBox("Live tuning") {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Picker("Precision", selection: $model.selectedPrecision) {
                            Text("fp16 quality").tag("fp16")
                            Text("q8 speed").tag("q8")
                        }
                        .pickerStyle(.segmented)
                        .onChange(of: model.selectedPrecision) { _, _ in
                            Task { await model.applySessionConfigIfRunning(label: "Precision") }
                        }

                        Picker("Target FPS", selection: $model.targetGeneratedFps) {
                            Text("12.5").tag(12.5)
                            Text("25").tag(25.0)
                        }
                        .pickerStyle(.segmented)
                        .onChange(of: model.targetGeneratedFps) { _, _ in
                            Task { await model.applySessionConfigIfRunning(label: "Target FPS") }
                        }
                    }

                    HStack {
                        Stepper("Audio Delay: \(model.audioDelayMs) ms", value: $model.audioDelayMs, in: 0...2_000, step: 25)
                            .onChange(of: model.audioDelayMs) { _, value in
                                Task { await model.setAudioDelay(ms: value) }
                            }
                        Stepper("Speech Threshold: \(model.vadThreshold, specifier: "%.3f")", value: $model.vadThreshold, in: 0.001...0.050, step: 0.001)
                            .onChange(of: model.vadThreshold) { _, _ in
                                Task { await model.applySessionConfigIfRunning(label: "Speech threshold") }
                            }
                    }

                    Stepper("Audio Window: \(model.audioWindowSeconds, specifier: "%.1f") s", value: $model.audioWindowSeconds, in: 0.4...2.0, step: 0.1)
                        .onChange(of: model.audioWindowSeconds) { _, _ in
                            Task { await model.applySessionConfigIfRunning(label: "Audio window") }
                        }
                }
            }
            .disabled(model.isSessionActionInFlight)

            HStack {
                Spacer()
                Button("Install Camera") {
                    model.installCameraExtension()
                }
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
