import Foundation
import Network

enum BackendClientError: Error, LocalizedError, Sendable {
    case notConnected
    case disconnected
    case missingResult(String)
    case responseDecodeFailed(String)

    var errorDescription: String? {
        switch self {
        case .notConnected:
            "Backend is not connected"
        case .disconnected:
            "Backend connection closed"
        case .missingResult(let method):
            "Backend response for \(method) did not include a result"
        case .responseDecodeFailed(let method):
            "Backend response for \(method) could not be decoded"
        }
    }
}

final class BackendClient: @unchecked Sendable {
    private let socketPath: String
    private let queue = DispatchQueue(label: "MeetingSurvivor.BackendClient")
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private var connection: NWConnection?
    private var receiveBuffer = Data()
    private var nextRequestID = 0
    private var connectContinuation: CheckedContinuation<Void, Error>?
    private var pending: [String: CheckedContinuation<JSONValue, Error>] = [:]
    private var isClosed = false
    private let onEvent: (@Sendable (BackendEvent) -> Void)?
    private let onDisconnect: (@Sendable (Error?) -> Void)?

    init(
        socketPath: String,
        onEvent: (@Sendable (BackendEvent) -> Void)? = nil,
        onDisconnect: (@Sendable (Error?) -> Void)? = nil
    ) {
        self.socketPath = socketPath
        self.onEvent = onEvent
        self.onDisconnect = onDisconnect
    }

    func connect() async throws {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                let connection = NWConnection(to: .unix(path: self.socketPath), using: .tcp)
                self.isClosed = false
                self.connection = connection
                self.connectContinuation = continuation
                connection.stateUpdateHandler = { [weak self] state in
                    guard let self else { return }
                    self.queue.async {
                        switch state {
                        case .ready:
                            self.finishConnect(.success(()))
                            self.receiveNext()
                        case .failed(let error):
                            self.finishConnect(.failure(error))
                            self.failPending(error)
                            self.notifyDisconnect(error)
                        case .cancelled:
                            self.finishConnect(.failure(BackendClientError.disconnected))
                            self.failPending(BackendClientError.disconnected)
                            self.notifyDisconnect(nil)
                        default:
                            break
                        }
                    }
                }
                connection.start(queue: self.queue)
            }
        }
    }

    func close() {
        queue.async {
            self.isClosed = true
            self.connection?.cancel()
            self.connection = nil
            self.failPending(BackendClientError.disconnected)
        }
    }

    func handshake() async throws -> HandshakeResult {
        let result = try await request(method: "handshake", params: ["protocolVersion": .number(1)])
        return try decode(HandshakeResult.self, from: result, method: "handshake")
    }

    func listAudioDevices() async throws -> [AudioDeviceRecord] {
        let result = try await request(method: "listAudioDevices")
        return try decode(AudioDevicesResult.self, from: result, method: "listAudioDevices").devices
    }

    func listAvatars() async throws -> [AvatarRecord] {
        let result = try await request(method: "listAvatars")
        return try decode(AvatarsResult.self, from: result, method: "listAvatars").avatars
    }

    func prepareAvatar(videoPath: String) async throws -> PrepareAvatarResult {
        let result = try await request(method: "prepareAvatar", params: ["videoPath": .string(videoPath)])
        return try decode(PrepareAvatarResult.self, from: result, method: "prepareAvatar")
    }

    private func request(method: String, params: [String: JSONValue] = [:]) async throws -> JSONValue {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                guard let connection = self.connection else {
                    continuation.resume(throwing: BackendClientError.notConnected)
                    return
                }
                self.nextRequestID += 1
                let id = String(self.nextRequestID)
                self.pending[id] = continuation
                do {
                    var data = try self.encoder.encode(BackendRequest(id: id, method: method, params: params))
                    data.append(0x0A)
                    connection.send(content: data, completion: .contentProcessed { [weak self] error in
                        guard let self, let error else { return }
                        self.queue.async {
                            self.pending.removeValue(forKey: id)?.resume(throwing: error)
                        }
                    })
                } catch {
                    self.pending.removeValue(forKey: id)?.resume(throwing: error)
                }
            }
        }
    }

    private func receiveNext() {
        connection?.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            self.queue.async {
                if let data, !data.isEmpty {
                    self.receiveBuffer.append(data)
                    self.processBufferedLines()
                }
                if let error {
                    self.failPending(error)
                    self.notifyDisconnect(error)
                    return
                }
                if isComplete {
                    self.failPending(BackendClientError.disconnected)
                    self.notifyDisconnect(nil)
                    return
                }
                self.receiveNext()
            }
        }
    }

    private func finishConnect(_ result: Result<Void, Error>) {
        guard let continuation = connectContinuation else { return }
        connectContinuation = nil
        switch result {
        case .success:
            continuation.resume()
        case .failure(let error):
            continuation.resume(throwing: error)
        }
    }

    private func processBufferedLines() {
        while let newline = receiveBuffer.firstIndex(of: 0x0A) {
            let line = receiveBuffer[..<newline]
            receiveBuffer.removeSubrange(...newline)
            guard !line.isEmpty else { continue }
            do {
                let message = try decoder.decode(BackendMessage.self, from: Data(line))
                handle(message)
            } catch {
                closeAfterProtocolError(error)
            }
        }
    }

    private func handle(_ message: BackendMessage) {
        if message.method == "event", let event = message.params {
            if !isClosed {
                onEvent?(event)
            }
            return
        }
        guard let id = message.id, let continuation = pending.removeValue(forKey: id) else {
            return
        }
        if let error = message.error {
            continuation.resume(throwing: error)
        } else if let result = message.result {
            continuation.resume(returning: result)
        } else {
            continuation.resume(throwing: BackendClientError.missingResult(id))
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from value: JSONValue, method: String) throws -> T {
        do {
            let data = try encoder.encode(value)
            return try decoder.decode(T.self, from: data)
        } catch {
            throw BackendClientError.responseDecodeFailed(method)
        }
    }

    private func closeAfterProtocolError(_ error: Error) {
        isClosed = true
        connection?.cancel()
        connection = nil
        failPending(error)
        onDisconnect?(error)
    }

    private func notifyDisconnect(_ error: Error?) {
        if !isClosed {
            onDisconnect?(error)
        }
    }

    private func failPending(_ error: Error) {
        let continuations = pending.values
        pending.removeAll()
        for continuation in continuations {
            continuation.resume(throwing: error)
        }
    }
}
