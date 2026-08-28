import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationSplitView {
            SidebarView()
                .navigationTitle("Avatars")
        } detail: {
            PreviewPane()
        }
        .frame(minWidth: 900, minHeight: 600)
        .task {
            await model.startBackendIfNeeded()
        }
        .onDisappear {
            model.stopBackend()
        }
    }
}
