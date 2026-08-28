import SwiftUI

@main
struct MeetingSurvivorApp: App {
    @StateObject private var appModel = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(appModel)
        }
        .windowStyle(.titleBar)
    }
}
