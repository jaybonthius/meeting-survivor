import SwiftUI

struct SidebarView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button("Add Video") {
                // Ticket 002 wires this to NSOpenPanel and prepareAvatar IPC.
            }
            .buttonStyle(.borderedProminent)

            if model.avatars.isEmpty {
                ContentUnavailableView("No Avatars", systemImage: "person.crop.rectangle", description: Text("Add a source video to prepare an avatar."))
            } else {
                List(model.avatars) { avatar in
                    VStack(alignment: .leading) {
                        Text(avatar.name)
                        Text(avatar.status).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .padding()
    }
}
