import Foundation
import SystemExtensions

final class CameraExtensionInstaller: NSObject, OSSystemExtensionRequestDelegate {
    private let extensionIdentifier = "com.meetingsurvivor.app.cameraextension"
    private let onStatus: @Sendable (String) -> Void

    init(onStatus: @escaping @Sendable (String) -> Void) {
        self.onStatus = onStatus
    }

    func activate() {
        onStatus("Requesting camera installation...")
        let request = OSSystemExtensionRequest.activationRequest(forExtensionWithIdentifier: extensionIdentifier, queue: .main)
        request.delegate = self
        OSSystemExtensionManager.shared.submitRequest(request)
    }

    func request(_ request: OSSystemExtensionRequest, actionForReplacingExtension existing: OSSystemExtensionProperties, withExtension ext: OSSystemExtensionProperties) -> OSSystemExtensionRequest.ReplacementAction {
        .replace
    }

    func requestNeedsUserApproval(_ request: OSSystemExtensionRequest) {
        onStatus("Approve camera in System Settings")
    }

    func request(_ request: OSSystemExtensionRequest, didFinishWithResult result: OSSystemExtensionRequest.Result) {
        switch result {
        case .completed:
            onStatus("Meeting Survivor Camera installed")
        case .willCompleteAfterReboot:
            onStatus("Camera installs after restart")
        @unknown default:
            onStatus("Camera install finished")
        }
    }

    func request(_ request: OSSystemExtensionRequest, didFailWithError error: Error) {
        onStatus("Camera install failed: \(error.localizedDescription)")
    }
}
