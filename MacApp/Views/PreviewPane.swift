import SwiftUI

struct PreviewPane: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 20) {
            RoundedRectangle(cornerRadius: 16)
                .fill(.black.gradient)
                .overlay {
                    VStack(spacing: 8) {
                        Image(systemName: "video")
                            .font(.system(size: 48))
                        Text("Preview will appear here")
                            .font(.title3)
                    }
                    .foregroundStyle(.white.opacity(0.85))
                }
                .aspectRatio(16.0 / 9.0, contentMode: .fit)
                .padding()

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                StatusRow(label: "Backend", value: model.backendStatus)
                StatusRow(label: "Camera", value: model.cameraStatus)
                StatusRow(label: "Audio", value: model.audioStatus)
                StatusRow(label: "Devices", value: "\(model.audioDevices.count)")
                StatusRow(label: "Avatars", value: "\(model.avatars.count)")
                StatusRow(label: "Last event", value: model.lastBackendEvent)
            }

            HStack {
                Picker("Input", selection: $model.selectedInputDevice) {
                    Text("None").tag("None")
                    ForEach(model.audioDevices.filter(\.isInput)) { device in
                        Text(device.name).tag(device.name)
                    }
                }
                Picker("Output", selection: $model.selectedOutputDevice) {
                    Text("None").tag("None")
                    ForEach(model.audioDevices.filter(\.isOutput)) { device in
                        Text(device.name).tag(device.name)
                    }
                }
            }

            HStack {
                Button("Refresh") {
                    Task { try? await model.refreshBackendData() }
                }
                Button("Start Session") {}
                    .disabled(true)
                Button("Stop Backend") {
                    model.stopBackend()
                }
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
