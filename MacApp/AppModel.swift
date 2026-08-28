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
    @Published var audioDelayMs = AppModel.storedInt("audioDelayMs", defaultValue: 400) {
        didSet { UserDefaults.standard.set(audioDelayMs, forKey: "audioDelayMs") }
    }
    @Published var selectedPrecision = UserDefaults.standard.string(forKey: "selectedPrecision") ?? "fp16" {
        didSet { UserDefaults.standard.set(selectedPrecision, forKey: "selectedPrecision") }
    }
    @Published var targetGeneratedFps = AppModel.storedDouble("targetGeneratedFps", defaultValue: 12.5) {
        didSet { UserDefaults.standard.set(targetGeneratedFps, forKey: "targetGeneratedFps") }
    }
    @Published var vadThreshold = AppModel.storedDouble("vadThreshold", defaultValue: 0.012) {
        didSet { UserDefaults.standard.set(vadThreshold, forKey: "vadThreshold") }
    }
    @Published var audioWindowSeconds = AppModel.storedDouble("audioWindowSeconds", defaultValue: 1.2) {
        didSet { UserDefaults.standard.set(audioWindowSeconds, forKey: "audioWindowSeconds") }
    }
    @Published var sendDelayedAudioToOutput = AppModel.storedBool("sendDelayedAudioToOutput", defaultValue: false) {
        didSet { UserDefaults.standard.set(sendDelayedAudioToOutput, forKey: "sendDelayedAudioToOutput") }
    }
    @Published var prepareBboxShift = AppModel.storedInt("prepareBboxShift", defaultValue: 15) {
        didSet { UserDefaults.standard.set(prepareBboxShift, forKey: "prepareBboxShift") }
    }
    @Published var prepareExtraMargin = AppModel.storedInt("prepareExtraMargin", defaultValue: 10) {
        didSet { UserDefaults.standard.set(prepareExtraMargin, forKey: "prepareExtraMargin") }
    }
    @Published var prepareMaxSeconds = AppModel.storedDouble("prepareMaxSeconds", defaultValue: 10.0) {
        didSet { UserDefaults.standard.set(prepareMaxSeconds, forKey: "prepareMaxSeconds") }
    }
    @Published var audioDevices: [AudioDeviceRecord] = []
    @Published var avatars: [AvatarRecord] = []
    @Published var selectedAvatarID: String?
    @Published var activityStatus = "No activity yet"
    @Published var isPreparingAvatar = false
    @Published var isSessionActionInFlight = false
    @Published var isAvatarSwitchInFlight = false
    @Published var isApplyingSessionConfig = false
    @Published var pendingAvatarID: String?
    @Published var pendingPrecision: String?
    @Published var sessionControlStatus = "Ready"
    @Published var sessionState = SessionStateResult.stopped
    @Published var sessionStats: SessionStatsResult?
    @Published var previewImage: NSImage?

    private var backendProcess: BackendProcess?
    private var backendClient: BackendClient?
    private lazy var cameraInstaller = CameraExtensionInstaller { [weak self] status in
        Task { @MainActor in
            self?.cameraStatus = status
        }
    }
    private var didStartBackend = false
    private var cameraFrameFeedAvailable = false
    private var latestPreviewSequence = 0

    private static func storedInt(_ key: String, defaultValue: Int) -> Int {
        guard UserDefaults.standard.object(forKey: key) != nil else { return defaultValue }
        return UserDefaults.standard.integer(forKey: key)
    }

    private static func storedDouble(_ key: String, defaultValue: Double) -> Double {
        guard UserDefaults.standard.object(forKey: key) != nil else { return defaultValue }
        return UserDefaults.standard.double(forKey: key)
    }

    private static func storedBool(_ key: String, defaultValue: Bool) -> Bool {
        guard UserDefaults.standard.object(forKey: key) != nil else { return defaultValue }
        return UserDefaults.standard.bool(forKey: key)
    }

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

    var pendingAvatarName: String {
        guard let pendingAvatarID else { return "None" }
        return avatars.first(where: { $0.id == pendingAvatarID })?.name ?? pendingAvatarID
    }

    var virtualCameraStatus: String {
        sessionState.virtualCamera ? "On" : (cameraFrameFeedAvailable ? "Off" : "Unavailable until signed")
    }

    var virtualMicrophoneStatus: String {
        if sessionState.virtualMicrophone { return "Sending delayed audio" }
        return "Off; choose BlackHole before start"
    }

    func startBackendIfNeeded() async {
        guard !didStartBackend else { return }
        didStartBackend = true
        appServiceStatus = "Starting..."
        do {
            let appSupportURL = try AppStoragePaths.appSupportURL()
            let socketURL = try AppStoragePaths.backendSocketURL()
            let cameraFrameDirectoryURL = AppStoragePaths.cameraFrameDirectoryURL()
            cameraFrameFeedAvailable = cameraFrameDirectoryURL != nil
            if cameraFrameDirectoryURL == nil {
                cameraStatus = "Camera app group unavailable; signing required"
            }
            let process = BackendProcess(projectRoot: AppStoragePaths.projectRootURL(), socketURL: socketURL, appSupportURL: appSupportURL, cameraFrameDirectoryURL: cameraFrameDirectoryURL)
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
            let result = try await backendClient.prepareAvatar(
                videoPath: url.path,
                precision: selectedPrecision,
                maxSeconds: prepareMaxSeconds,
                bboxShift: prepareBboxShift,
                extraMargin: prepareExtraMargin
            )
            try await refreshBackendData()
            await selectAvatar(id: result.avatarId)
            if pendingAvatarID == nil {
                activityStatus = "Prepared \(result.avatarId)"
            } else {
                activityStatus = "Prepared \(result.avatarId); loading without interrupting stream"
            }
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
        let wasRunning = sessionState.isRunning
        if !wasRunning {
            selectedAvatarID = id
        }
        guard let backendClient else { return }
        if wasRunning {
            isAvatarSwitchInFlight = true
            pendingAvatarID = id
            sessionControlStatus = "Loading \(avatars.first(where: { $0.id == id })?.name ?? id)"
        }
        do {
            let state = try await backendClient.setActiveAvatar(id)
            applySessionState(state)
            if wasRunning, state.pendingAvatarId != nil {
                activityStatus = "Loading avatar; stream stays on \(selectedAvatarName)"
            } else {
                activityStatus = "Selected \(avatars.first(where: { $0.id == id })?.name ?? id)"
            }
        } catch {
            selectedAvatarID = previousAvatarID
            pendingAvatarID = nil
            isAvatarSwitchInFlight = false
            sessionControlStatus = "Ready"
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
            previewImage = nil
            latestPreviewSequence = 0
            let state = try await backendClient.startSession(
                avatarId: selectedAvatarID,
                inputDeviceId: selectedInputDeviceID.nilIfEmpty,
                outputDeviceId: selectedOutputDeviceID.nilIfEmpty,
                audioDelayMs: audioDelayMs,
                precision: selectedPrecision,
                generatedFps: targetGeneratedFps,
                vadThreshold: vadThreshold,
                audioWindowSeconds: audioWindowSeconds,
                virtualCamera: cameraFrameFeedAvailable,
                virtualMicrophone: sendDelayedAudioToOutput && !selectedOutputDeviceID.isEmpty
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
        audioDelayMs = ms
        await applySessionConfigIfRunning(label: "Audio delay")
    }

    func applySessionConfigIfRunning(label: String = "Settings") async {
        guard let backendClient else { return }
        guard sessionState.isRunning else {
            activityStatus = "\(label) saved for next session"
            return
        }
        isApplyingSessionConfig = true
        defer { isApplyingSessionConfig = false }
        do {
            let state = try await backendClient.setSessionConfig(
                precision: selectedPrecision,
                generatedFps: targetGeneratedFps,
                audioDelayMs: audioDelayMs,
                vadThreshold: vadThreshold,
                audioWindowSeconds: audioWindowSeconds
            )
            applySessionState(state)
            if state.pendingPrecision != nil {
                activityStatus = "Loading \(selectedPrecision); stream stays live"
            } else {
                activityStatus = "\(label) applied"
            }
        } catch {
            activityStatus = "Could not apply \(label.lowercased()): \(error.localizedDescription)"
        }
    }

    func installCameraExtension() {
        cameraInstaller.activate()
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
        pendingAvatarID = state.pendingAvatarId
        pendingPrecision = state.pendingPrecision
        sessionControlStatus = state.controlStatus ?? "Ready"
        isAvatarSwitchInFlight = state.pendingAvatarId != nil
        if !state.isRunning {
            previewImage = nil
            latestPreviewSequence = 0
            pendingAvatarID = nil
            pendingPrecision = nil
            isAvatarSwitchInFlight = false
        }
        if let activeAvatarId = state.activeAvatarId {
            selectedAvatarID = activeAvatarId
        }
        if let inputDeviceId = state.inputDeviceId {
            selectedInputDeviceID = inputDeviceId
        }
        if let outputDeviceId = state.outputDeviceId {
            selectedOutputDeviceID = outputDeviceId
        }
        if state.isRunning || state.activeAvatarId != nil {
            if !state.isRunning || state.pendingPrecision == nil {
                selectedPrecision = state.precision
            }
            targetGeneratedFps = state.generatedFps
            audioDelayMs = state.audioDelayMs
            vadThreshold = state.vadThreshold
            audioWindowSeconds = state.audioWindowSeconds
        }
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
                if let pendingAvatarId = state.pendingAvatarId {
                    activityStatus = "Loading \(avatars.first(where: { $0.id == pendingAvatarId })?.name ?? pendingAvatarId); stream stays live"
                } else if let pendingPrecision = state.pendingPrecision {
                    activityStatus = "Loading \(pendingPrecision); stream stays live"
                } else {
                    activityStatus = event.state.map { "Session \($0)" } ?? "Session updated"
                }
            } else {
                activityStatus = event.state.map { "Session \($0)" } ?? "Session updated"
            }
        case "sessionStats":
            if let stats = sessionStats(from: event) {
                sessionStats = stats
            }
        case "previewFrame":
            loadPreviewFrame(from: event)
        case "sessionControl":
            sessionControlStatus = event.message ?? event.status ?? sessionControlStatus
            if event.status == "applied" {
                pendingAvatarID = nil
                pendingPrecision = nil
                isAvatarSwitchInFlight = false
                sessionControlStatus = "Ready"
                activityStatus = event.message ?? "Settings applied"
            } else if event.status == "failed" {
                pendingAvatarID = nil
                pendingPrecision = nil
                isAvatarSwitchInFlight = false
                activityStatus = "Update failed: \(event.message ?? "unknown error")"
            } else if let message = event.message {
                activityStatus = message
            }
        case "error":
            activityStatus = "Session failed: \(event.message ?? "unknown backend error")"
        default:
            activityStatus = event.type
        }
    }

    private func loadPreviewFrame(from event: BackendEvent) {
        guard let path = event.previewPath,
              let sequence = event.previewSequence,
              sequence > latestPreviewSequence else {
            return
        }
        latestPreviewSequence = sequence
        Task.detached(priority: .userInitiated) { [path, sequence] in
            let image = NSImage(contentsOfFile: path)
            await MainActor.run {
                guard sequence >= self.latestPreviewSequence else { return }
                self.previewImage = image
            }
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
            precision: event.precision ?? "fp16",
            generatedFps: event.generatedFps ?? 12.5,
            audioDelayMs: event.audioDelayMs ?? audioDelayMs,
            vadThreshold: event.vadThreshold ?? vadThreshold,
            audioWindowSeconds: event.audioWindowSeconds ?? audioWindowSeconds,
            pendingAvatarId: event.pendingAvatarId,
            pendingPrecision: event.pendingPrecision,
            controlStatus: event.controlStatus,
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
        precision: "fp16",
        generatedFps: 12.5,
        audioDelayMs: 400,
        vadThreshold: 0.012,
        audioWindowSeconds: 1.2,
        pendingAvatarId: nil,
        pendingPrecision: nil,
        controlStatus: "Ready",
        startedAt: nil
    )
}
