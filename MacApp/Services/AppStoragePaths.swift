import Foundation

enum AppStoragePaths {
    static let cameraAppGroupID = "group.com.meetingsurvivor.camera"

    static func appSupportURL() throws -> URL {
        let base = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
        return base.appending(path: "Meeting Survivor")
    }

    static func ipcDirectoryURL() throws -> URL {
        try appSupportURL().appending(path: "ipc")
    }

    static func backendSocketURL() throws -> URL {
        try ipcDirectoryURL().appending(path: "backend.sock")
    }

    static func cameraFrameDirectoryURL() -> URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: cameraAppGroupID)?.appending(path: "CameraFrames")
    }

    static func projectRootURL() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }
}
