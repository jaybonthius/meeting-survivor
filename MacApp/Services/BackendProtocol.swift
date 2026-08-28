import Foundation

enum JSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    case int(Int)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null:
            try container.encodeNil()
        case .bool(let value):
            try container.encode(value)
        case .int(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .string(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        }
    }
}

struct BackendRequest: Encodable, Sendable {
    let id: String
    let method: String
    let params: [String: JSONValue]
}

struct BackendMessage: Decodable, Sendable {
    let id: String?
    let result: JSONValue?
    let error: BackendRPCError?
    let method: String?
    let params: BackendEvent?
}

struct BackendRPCError: Decodable, Error, LocalizedError, Sendable, Equatable {
    let code: String
    let message: String

    var errorDescription: String? {
        "\(code): \(message)"
    }
}

struct BackendEvent: Decodable, Sendable, Equatable {
    let type: String
    let protocolVersion: Int?
    let appSupport: String?
    let operationId: String?
    let stage: String?
    let current: Int?
    let total: Int?
    let message: String?
    let state: String?
    let activeAvatarId: String?
    let inputDeviceId: String?
    let outputDeviceId: String?
    let virtualCamera: Bool?
    let virtualMicrophone: Bool?
    let precision: String?
    let generatedFps: Double?
    let audioDelayMs: Int?
    let startedAt: Double?
    let previewFps: Double?
    let queueDepth: Int?
    let droppedJobs: Int?
    let renderMs: Double?
}

struct HandshakeResult: Decodable, Sendable, Equatable {
    let protocolVersion: Int
    let backend: String
    let ok: Bool
}

struct AudioDeviceRecord: Identifiable, Decodable, Sendable, Equatable {
    let id: String
    let index: Int
    let name: String
    let hostApi: String?
    let maxInputChannels: Int
    let maxOutputChannels: Int
    let defaultSampleRate: Double
    let isInput: Bool
    let isOutput: Bool
    let isDefaultInput: Bool
    let isDefaultOutput: Bool
}

struct AvatarRecord: Identifiable, Decodable, Sendable, Equatable {
    let id: String
    let name: String
    let path: String
    let status: String
    let sourceVideo: String?
    let createdAt: Double?
    let frameCount: Int?
    let width: Int?
    let height: Int?
    let sourceFps: Double?
}

struct AudioDevicesResult: Decodable, Sendable {
    let devices: [AudioDeviceRecord]
}

struct AvatarsResult: Decodable, Sendable {
    let avatars: [AvatarRecord]
}

struct PrepareAvatarResult: Decodable, Sendable, Equatable {
    let operationId: String
    let avatarId: String
    let avatarPath: String
}

struct SessionStateResult: Decodable, Sendable, Equatable {
    let state: String
    let activeAvatarId: String?
    let inputDeviceId: String?
    let outputDeviceId: String?
    let virtualCamera: Bool
    let virtualMicrophone: Bool
    let precision: String
    let generatedFps: Double
    let audioDelayMs: Int
    let startedAt: Double?

    var isRunning: Bool {
        state == "running"
    }
}

struct SessionStatsResult: Decodable, Sendable, Equatable {
    let state: String
    let previewFps: Double
    let generatedFps: Double
    let queueDepth: Int
    let droppedJobs: Int
    let renderMs: Double
}
