import Foundation

@MainActor
final class AppModel: ObservableObject {
    @Published var backendStatus = "Backend not connected"
    @Published var cameraStatus = "Camera extension not installed"
    @Published var audioStatus = "Virtual microphone not installed"
    @Published var selectedInputDevice = "None"
    @Published var selectedOutputDevice = "None"
    @Published var avatars: [AvatarSummary] = []
}

struct AvatarSummary: Identifiable, Hashable {
    let id: String
    let name: String
    let status: String
}
