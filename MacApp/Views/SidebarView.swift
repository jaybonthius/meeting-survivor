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
                            if model.selectedAvatarID == avatar.id {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(.tint)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(avatar.status != "ready")
                }
            }
        }
        .padding()
    }
}
