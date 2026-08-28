import Foundation

enum BackendProcessError: Error, LocalizedError, Sendable {
    case socketDidNotAppear
    case processExited(Int32)

    var errorDescription: String? {
        switch self {
        case .socketDidNotAppear:
            "Backend socket did not appear"
        case .processExited(let status):
            "Backend process exited with status \(status)"
        }
    }
}

@MainActor
final class BackendProcess {
    private let projectRoot: URL
    private let socketURL: URL
    private let appSupportURL: URL
    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?

    init(projectRoot: URL, socketURL: URL, appSupportURL: URL) {
        self.projectRoot = projectRoot
        self.socketURL = socketURL
        self.appSupportURL = appSupportURL
    }

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start() throws {
        if isRunning { return }
        try FileManager.default.createDirectory(at: socketURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: appSupportURL, withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: socketURL.path) {
            try FileManager.default.removeItem(at: socketURL)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [
            "uv",
            "run",
            "meeting-survivor",
            "backend",
            "--socket",
            socketURL.path,
            "--app-support",
            appSupportURL.path,
        ]
        process.currentDirectoryURL = projectRoot
        process.environment = Self.environment()

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        stdoutPipe.fileHandleForReading.readabilityHandler = Self.logHandler(prefix: "backend stdout")
        stderrPipe.fileHandleForReading.readabilityHandler = Self.logHandler(prefix: "backend stderr")

        try process.run()
        self.process = process
        self.stdoutPipe = stdoutPipe
        self.stderrPipe = stderrPipe
    }

    func waitForSocket(timeoutSeconds: TimeInterval = 10) async throws {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            if FileManager.default.fileExists(atPath: socketURL.path) {
                return
            }
            if let process, !process.isRunning {
                throw BackendProcessError.processExited(process.terminationStatus)
            }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        throw BackendProcessError.socketDidNotAppear
    }

    func stop() {
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        if process?.isRunning == true {
            process?.terminate()
            process?.waitUntilExit()
        }
        process = nil
        try? FileManager.default.removeItem(at: socketURL)
    }

    private static func environment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let path = [
            "\(home)/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            environment["PATH"],
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        .compactMap { $0 }
        .joined(separator: ":")
        environment["PATH"] = path
        return environment
    }

    private static func logHandler(prefix: String) -> @Sendable (FileHandle) -> Void {
        { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            NSLog("MeetingSurvivor \(prefix): \(text.trimmingCharacters(in: .whitespacesAndNewlines))")
        }
    }
}
