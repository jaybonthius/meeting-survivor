import AppKit
import Foundation
import UniformTypeIdentifiers

@MainActor
final class AppModel: ObservableObject {
    @Published var backendStatus = "Backend not connected"
    @Published var cameraStatus = "Camera extension not installed"
    @Published var audioStatus = "Virtual microphone not installed"
    @Published var selectedInputDevice = "None"
    @Published var selectedOutputDevice = "None"
    @Published var audioDevices: [AudioDeviceRecord] = []
    @Published var avatars: [AvatarRecord] = []
    @Published var lastBackendEvent = "No backend events yet"
    @Published var isPreparingAvatar = false

    private var backendProcess: BackendProcess?
    private var backendClient: BackendClient?
    private var didStartBackend = false

    func startBackendIfNeeded() async {
        guard !didStartBackend else { return }
        didStartBackend = true
        backendStatus = "Starting backend..."
        do {
            let appSupportURL = try AppStoragePaths.appSupportURL()
            let socketURL = try AppStoragePaths.backendSocketURL()
            let process = BackendProcess(projectRoot: AppStoragePaths.projectRootURL(), socketURL: socketURL, appSupportURL: appSupportURL)
            try process.start()
            backendProcess = process
            try await process.waitForSocket()

            let client = BackendClient(
                socketPath: socketURL.path,
                onEvent: { [weak self] event in
                    Task { @MainActor in
                        self?.handleBackendEvent(event)
                    }
                },
                onDisconnect: { [weak self] error in
                    Task { @MainActor in
                        self?.backendStatus = error.map { "Backend disconnected: \($0.localizedDescription)" } ?? "Backend disconnected"
                    }
                }
            )
            try await client.connect()
            backendClient = client

            let handshake = try await client.handshake()
            backendStatus = "Connected to \(handshake.backend) v\(handshake.protocolVersion)"
            try await refreshBackendData()
        } catch {
            backendStatus = "Backend error: \(error.localizedDescription)"
            backendProcess?.stop()
            backendProcess = nil
            backendClient = nil
            didStartBackend = false
        }
    }

    func refreshBackendData() async throws {
        guard let backendClient else { return }
        async let devices = backendClient.listAudioDevices()
        async let avatarRecords = backendClient.listAvatars()
        audioDevices = try await devices
        avatars = try await avatarRecords
        selectedInputDevice = audioDevices.first(where: { $0.isDefaultInput })?.name ?? "None"
        selectedOutputDevice = audioDevices.first(where: { $0.isDefaultOutput })?.name ?? "None"
    }

    func chooseVideoAndPrepare() {
        let panel = NSOpenPanel()
        panel.title = "Choose source video"
        panel.allowedContentTypes = [.movie, .mpeg4Movie, .quickTimeMovie]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            await prepareAvatar(from: url)
        }
    }

    func prepareAvatar(from url: URL) async {
        guard let backendClient else {
            lastBackendEvent = "Backend is not connected"
            return
        }
        isPreparingAvatar = true
        lastBackendEvent = "Preparing \(url.lastPathComponent)"
        do {
            let result = try await backendClient.prepareAvatar(videoPath: url.path)
            lastBackendEvent = "Prepared \(result.avatarId)"
            try await refreshBackendData()
        } catch {
            lastBackendEvent = "Prepare failed: \(error.localizedDescription)"
        }
        isPreparingAvatar = false
    }

    func stopBackend() {
        backendClient?.close()
        backendProcess?.stop()
        backendClient = nil
        backendProcess = nil
        didStartBackend = false
        backendStatus = "Backend stopped"
    }

    private func handleBackendEvent(_ event: BackendEvent) {
        switch event.type {
        case "backendReady":
            lastBackendEvent = "Backend ready"
        case "prepareProgress":
            let stage = event.stage ?? "preparing"
            if let current = event.current, let total = event.total {
                lastBackendEvent = "Prepare \(stage): \(current)/\(total)"
            } else {
                lastBackendEvent = "Prepare \(stage)"
            }
        case "prepareCompleted":
            lastBackendEvent = "Prepare completed"
        case "prepareFailed":
            lastBackendEvent = "Prepare failed: \(event.message ?? "unknown error")"
        default:
            lastBackendEvent = event.type
        }
    }
}
