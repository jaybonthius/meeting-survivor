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
                StatusRow(label: "Input", value: model.selectedInputDevice)
                StatusRow(label: "Output", value: model.selectedOutputDevice)
            }

            HStack {
                Button("Start Session") {}
                    .disabled(true)
                Button("Stop") {}
                    .disabled(true)
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
