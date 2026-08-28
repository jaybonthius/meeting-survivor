import CoreMediaIO
import Foundation

let providerSource = CameraProviderSource()
CMIOExtensionProvider.startService(provider: providerSource.provider)
CFRunLoopRun()
