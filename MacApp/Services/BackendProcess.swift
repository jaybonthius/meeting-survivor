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
    private let cameraFrameDirectoryURL: URL?
    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var logSink: BackendLogSink?

    init(projectRoot: URL, socketURL: URL, appSupportURL: URL, cameraFrameDirectoryURL: URL?) {
        self.projectRoot = projectRoot
        self.socketURL = socketURL
        self.appSupportURL = appSupportURL
        self.cameraFrameDirectoryURL = cameraFrameDirectoryURL
    }

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start() throws {
        if isRunning { return }
        try FileManager.default.createDirectory(at: socketURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: appSupportURL, withIntermediateDirectories: true)
        if let cameraFrameDirectoryURL {
            try FileManager.default.createDirectory(at: cameraFrameDirectoryURL, withIntermediateDirectories: true)
        }
        if FileManager.default.fileExists(atPath: socketURL.path) {
            try FileManager.default.removeItem(at: socketURL)
        }
        let logSink = try BackendLogSink(logsDirectoryURL: appSupportURL.appending(path: "logs"))

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        var arguments = [
            "uv",
            "run",
            "meeting-survivor",
            "backend",
            "--socket",
            socketURL.path,
            "--app-support",
            appSupportURL.path,
        ]
        if let cameraFrameDirectoryURL {
            arguments.append(contentsOf: ["--camera-frame-dir", cameraFrameDirectoryURL.path])
        }
        process.arguments = arguments
        process.currentDirectoryURL = projectRoot
        process.environment = Self.environment()

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        stdoutPipe.fileHandleForReading.readabilityHandler = logSink.handler(prefix: "backend stdout")
        stderrPipe.fileHandleForReading.readabilityHandler = logSink.handler(prefix: "backend stderr")
        logSink.writeLine("backend launch: \(arguments.joined(separator: " "))")

        do {
            try process.run()
        } catch {
            logSink.close()
            throw error
        }
        self.process = process
        self.stdoutPipe = stdoutPipe
        self.stderrPipe = stderrPipe
        self.logSink = logSink
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
        writeStoppedCameraFrameMetadata()
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        if process?.isRunning == true {
            process?.terminate()
            process?.waitUntilExit()
            writeStoppedCameraFrameMetadata()
        }
        logSink?.writeLine("backend stopped")
        logSink?.close()
        logSink = nil
        process = nil
        try? FileManager.default.removeItem(at: socketURL)
    }

    private func writeStoppedCameraFrameMetadata() {
        guard let cameraFrameDirectoryURL else { return }
        do {
            try FileManager.default.createDirectory(at: cameraFrameDirectoryURL, withIntermediateDirectories: true)
            let metadataURL = cameraFrameDirectoryURL.appending(path: "latest.json")
            let currentSequence: Int
            if let data = try? Data(contentsOf: metadataURL),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let sequence = object["sequence"] as? Int {
                currentSequence = sequence
            } else {
                currentSequence = 0
            }
            let metadata: [String: Any] = [
                "sequence": currentSequence + 1,
                "state": "stopped",
                "transportVersion": 1,
                "timestamp": Date().timeIntervalSince1970,
            ]
            let data = try JSONSerialization.data(withJSONObject: metadata)
            let temporaryURL = cameraFrameDirectoryURL.appending(path: ".latest.json.tmp")
            try data.write(to: temporaryURL, options: .atomic)
            if FileManager.default.fileExists(atPath: metadataURL.path) {
                try FileManager.default.removeItem(at: metadataURL)
            }
            try FileManager.default.moveItem(at: temporaryURL, to: metadataURL)
        } catch {
            NSLog("MeetingSurvivor could not write stopped camera metadata: \(error.localizedDescription)")
        }
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
        environment["PYTHONFAULTHANDLER"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        return environment
    }
}

private final class BackendLogSink: @unchecked Sendable {
    private let url: URL
    private let handle: FileHandle
    private let lock = NSLock()

    init(logsDirectoryURL: URL) throws {
        try FileManager.default.createDirectory(at: logsDirectoryURL, withIntermediateDirectories: true)
        url = logsDirectoryURL.appending(path: "backend.log")
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        writeLine("---- backend log opened at \(Self.timestamp()) ----")
    }

    func handler(prefix: String) -> @Sendable (FileHandle) -> Void {
        { [weak self] fileHandle in
            self?.write(prefix: prefix, data: fileHandle.availableData)
        }
    }

    func write(prefix: String, data: Data) {
        guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
        NSLog("MeetingSurvivor \(prefix): \(text.trimmingCharacters(in: .whitespacesAndNewlines))")
        writeLine(text.split(separator: "\n", omittingEmptySubsequences: false).map { "\(prefix): \($0)" }.joined(separator: "\n"))
    }

    func writeLine(_ line: String) {
        let entry = "\(Self.timestamp()) \(line)\n"
        guard let data = entry.data(using: .utf8) else { return }
        lock.lock()
        defer { lock.unlock() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: data)
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        try? handle.close()
    }

    private static func timestamp() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}
