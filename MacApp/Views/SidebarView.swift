import SwiftUI

struct SidebarView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button(model.isPreparingAvatar ? "Preparing..." : "Add Video") {
                model.chooseVideoAndPrepare()
            }
            .buttonStyle(.borderedProminent)
            .disabled(model.isPreparingAvatar)

            GroupBox("Avatar prep") {
                VStack(alignment: .leading) {
                    Stepper("BBox shift: \(model.prepareBboxShift)", value: $model.prepareBboxShift, in: -20...40, step: 5)
                    Stepper("Extra margin: \(model.prepareExtraMargin)", value: $model.prepareExtraMargin, in: 0...40, step: 5)
                    Stepper("Clip length: \(model.prepareMaxSeconds, specifier: "%.0f") s", value: $model.prepareMaxSeconds, in: 3...20, step: 1)
                    Text("Used only when adding a new video.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .disabled(model.isPreparingAvatar)

            if model.avatars.isEmpty {
                ContentUnavailableView("No Avatars", systemImage: "person.crop.rectangle", description: Text("Add a source video to prepare an avatar."))
            } else {
                List(model.avatars) { avatar in
                    Button {
                        model.selectAvatar(avatar)
                    } label: {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(avatar.name)
                                Text(avatar.status).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if model.pendingAvatarID == avatar.id {
                                ProgressView()
                                    .controlSize(.small)
                            } else if model.selectedAvatarID == avatar.id {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(.tint)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(avatar.status != "ready" || model.isAvatarSwitchInFlight)
                }
            }
        }
        .padding()
    }
}
