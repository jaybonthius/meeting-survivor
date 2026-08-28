import AppKit
import Combine
import Foundation
import UniformTypeIdentifiers

@MainActor
final class AppModel: ObservableObject {
    @Published var appServiceStatus = "Starting..."
    @Published var cameraStatus = "Camera extension not installed"
    @Published var audioStatus = "Virtual microphone not installed"
    @Published var sessionStatus = "Stopped"
    @Published var selectedInputDeviceID = ""
    @Published var selectedOutputDeviceID = ""
    @Published var audioDelayMs = 400
    @Published var audioDevices: [AudioDeviceRecord] = []
    @Published var avatars: [AvatarRecord] = []
    @Published var selectedAvatarID: String?
    @Published var activityStatus = "No activity yet"
    @Published var isPreparingAvatar = false
    @Published var isSessionActionInFlight = false
    @Published var sessionState = SessionStateResult.stopped
    @Published var sessionStats: SessionStatsResult?

    private var backendProcess: BackendProcess?
    private var backendClient: BackendClient?
    private var didStartBackend = false

    var selectedAvatarName: String {
        guard let selectedAvatarID else { return "None" }
        return avatars.first(where: { $0.id == selectedAvatarID })?.name ?? selectedAvatarID
    }

    var selectedInputDeviceName: String {
        deviceName(for: selectedInputDeviceID)
    }

    var selectedOutputDeviceName: String {
        deviceName(for: selectedOutputDeviceID)
    }

    func startBackendIfNeeded() async {
        guard !didStartBackend else { return }
        didStartBackend = true
        appServiceStatus = "Starting..."
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
                        self?.appServiceStatus = error.map { "Disconnected: \($0.localizedDescription)" } ?? "Disconnected"
                        self?.sessionStatus = "Stopped"
                    }
                }
            )
            try await client.connect()
            backendClient = client

            _ = try await client.handshake()
            appServiceStatus = "Ready"
            try await refreshBackendData()
            applySessionState(try await client.getSessionState())
        } catch {
            appServiceStatus = "Error: \(error.localizedDescription)"
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
        preserveValidSelections()
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
            activityStatus = "App is not ready"
            return
        }
        isPreparingAvatar = true
        activityStatus = "Preparing \(url.lastPathComponent)"
        defer { isPreparingAvatar = false }
        do {
            let result = try await backendClient.prepareAvatar(videoPath: url.path)
            try await refreshBackendData()
            await selectAvatar(id: result.avatarId)
            activityStatus = "Prepared \(result.avatarId)"
        } catch {
            activityStatus = "Prepare failed: \(error.localizedDescription)"
        }
    }

    func selectAvatar(_ avatar: AvatarRecord) {
        Task {
            await selectAvatar(id: avatar.id)
        }
    }

    func selectAvatar(id: String) async {
        let previousAvatarID = selectedAvatarID
        selectedAvatarID = id
        guard let backendClient else { return }
        do {
            applySessionState(try await backendClient.setActiveAvatar(id))
            activityStatus = "Selected \(avatars.first(where: { $0.id == id })?.name ?? id)"
        } catch {
            selectedAvatarID = previousAvatarID
            activityStatus = "Could not select avatar: \(error.localizedDescription)"
        }
    }

    func startSession() async {
        guard let backendClient else {
            activityStatus = "App is not ready"
            return
        }
        guard let selectedAvatarID else {
            activityStatus = "Choose a prepared avatar first"
            return
        }
        isSessionActionInFlight = true
        defer { isSessionActionInFlight = false }
        do {
            let state = try await backendClient.startSession(
                avatarId: selectedAvatarID,
                inputDeviceId: selectedInputDeviceID.nilIfEmpty,
                outputDeviceId: selectedOutputDeviceID.nilIfEmpty,
                audioDelayMs: audioDelayMs
            )
            applySessionState(state)
            activityStatus = "Session started"
        } catch {
            activityStatus = "Could not start session: \(error.localizedDescription)"
        }
    }

    func stopSession() async {
        guard let backendClient else {
            activityStatus = "App is not ready"
            return
        }
        isSessionActionInFlight = true
        defer { isSessionActionInFlight = false }
        do {
            applySessionState(try await backendClient.stopSession())
            activityStatus = "Session stopped"
        } catch {
            activityStatus = "Could not stop session: \(error.localizedDescription)"
        }
    }

    func setAudioDelay(ms: Int) async {
        guard let backendClient else { return }
        do {
            applySessionState(try await backendClient.setAudioDelay(ms: ms))
        } catch {
            activityStatus = "Could not set audio delay: \(error.localizedDescription)"
        }
    }

    func stopAppServiceForExit() {
        backendClient?.close()
        backendProcess?.stop()
        backendClient = nil
        backendProcess = nil
        didStartBackend = false
        appServiceStatus = "Stopped"
        sessionStatus = "Stopped"
    }

    private func applySessionState(_ state: SessionStateResult) {
        sessionState = state
        sessionStatus = state.isRunning ? "Running" : "Stopped"
        if let activeAvatarId = state.activeAvatarId {
            selectedAvatarID = activeAvatarId
        }
        if let inputDeviceId = state.inputDeviceId {
            selectedInputDeviceID = inputDeviceId
        }
        if let outputDeviceId = state.outputDeviceId {
            selectedOutputDeviceID = outputDeviceId
        }
        audioDelayMs = state.audioDelayMs
    }

    private func handleBackendEvent(_ event: BackendEvent) {
        switch event.type {
        case "backendReady":
            activityStatus = "App ready"
        case "prepareProgress":
            let stage = event.stage ?? "preparing"
            if let current = event.current, let total = event.total {
                activityStatus = "Prepare \(stage): \(current)/\(total)"
            } else {
                activityStatus = "Prepare \(stage)"
            }
        case "prepareCompleted":
            activityStatus = "Prepare completed"
        case "prepareFailed":
            activityStatus = "Prepare failed: \(event.message ?? "unknown error")"
        case "sessionState":
            if let state = sessionState(from: event) {
                applySessionState(state)
            }
            activityStatus = event.state.map { "Session \($0)" } ?? "Session updated"
        case "sessionStats":
            if let stats = sessionStats(from: event) {
                sessionStats = stats
            }
        default:
            activityStatus = event.type
        }
    }

    private func preserveValidSelections() {
        if !selectedInputDeviceID.isEmpty, !audioDevices.contains(where: { $0.id == selectedInputDeviceID && $0.isInput }) {
            selectedInputDeviceID = ""
        }
        if selectedInputDeviceID.isEmpty {
            selectedInputDeviceID = audioDevices.first(where: { $0.isDefaultInput })?.id ?? ""
        }
        if !selectedOutputDeviceID.isEmpty, !audioDevices.contains(where: { $0.id == selectedOutputDeviceID && $0.isOutput }) {
            selectedOutputDeviceID = ""
        }
        if selectedOutputDeviceID.isEmpty {
            selectedOutputDeviceID = audioDevices.first(where: { $0.isDefaultOutput })?.id ?? ""
        }
        if let selectedAvatarID, avatars.contains(where: { $0.id == selectedAvatarID && $0.status == "ready" }) {
            return
        }
        selectedAvatarID = avatars.first(where: { $0.status == "ready" })?.id
    }

    private func deviceName(for id: String) -> String {
        guard !id.isEmpty else { return "None" }
        return audioDevices.first(where: { $0.id == id })?.name ?? id
    }

    private func sessionState(from event: BackendEvent) -> SessionStateResult? {
        guard let state = event.state else { return nil }
        return SessionStateResult(
            state: state,
            activeAvatarId: event.activeAvatarId,
            inputDeviceId: event.inputDeviceId,
            outputDeviceId: event.outputDeviceId,
            virtualCamera: event.virtualCamera ?? false,
            virtualMicrophone: event.virtualMicrophone ?? false,
            precision: event.precision ?? "q8",
            generatedFps: event.generatedFps ?? 12.5,
            audioDelayMs: event.audioDelayMs ?? audioDelayMs,
            startedAt: event.startedAt
        )
    }

    private func sessionStats(from event: BackendEvent) -> SessionStatsResult? {
        guard let state = event.state,
              let previewFps = event.previewFps,
              let generatedFps = event.generatedFps,
              let queueDepth = event.queueDepth,
              let droppedJobs = event.droppedJobs,
              let renderMs = event.renderMs else {
            return nil
        }
        return SessionStatsResult(state: state, previewFps: previewFps, generatedFps: generatedFps, queueDepth: queueDepth, droppedJobs: droppedJobs, renderMs: renderMs)
    }
}

private extension String {
    var nilIfEmpty: String? {
        isEmpty ? nil : self
    }
}

private extension SessionStateResult {
    static let stopped = SessionStateResult(
        state: "stopped",
        activeAvatarId: nil,
        inputDeviceId: nil,
        outputDeviceId: nil,
        virtualCamera: false,
        virtualMicrophone: false,
        precision: "q8",
        generatedFps: 12.5,
        audioDelayMs: 400,
        startedAt: nil
    )
}
