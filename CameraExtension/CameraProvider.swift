import CoreMedia
import CoreMediaIO
import CoreVideo
import Foundation
import IOKit.audio
import os.log

private let cameraWidth: Int32 = 1280
private let cameraHeight: Int32 = 720
private let cameraFrameRate: Int32 = 25
private let deviceID = UUID(uuidString: "9D3A4A13-476E-4C54-9EA3-9E02C8CB28D4")!
private let streamID = UUID(uuidString: "B1E19E4D-3400-48DB-817B-C5F4CF59E472")!

final class CameraProviderSource: NSObject, CMIOExtensionProviderSource {
    private(set) var provider: CMIOExtensionProvider!
    private var deviceSource: CameraDeviceSource!

    override init() {
        super.init()
        provider = CMIOExtensionProvider(source: self, clientQueue: nil)
        deviceSource = CameraDeviceSource(localizedName: "Meeting Survivor Camera")
        do {
            try provider.addDevice(deviceSource.device)
        } catch {
            fatalError("Could not add Meeting Survivor camera device: \(error.localizedDescription)")
        }
    }

    var availableProperties: Set<CMIOExtensionProperty> {
        [.providerManufacturer]
    }

    func providerProperties(forProperties properties: Set<CMIOExtensionProperty>) throws -> CMIOExtensionProviderProperties {
        let providerProperties = CMIOExtensionProviderProperties(dictionary: [:])
        if properties.contains(.providerManufacturer) {
            providerProperties.manufacturer = "Meeting Survivor"
        }
        return providerProperties
    }

    func setProviderProperties(_ providerProperties: CMIOExtensionProviderProperties) throws {}

    func connect(to client: CMIOExtensionClient) throws {}

    func disconnect(from client: CMIOExtensionClient) {}
}

final class CameraDeviceSource: NSObject, CMIOExtensionDeviceSource {
    private(set) var device: CMIOExtensionDevice!
    private var streamSource: CameraStreamSource!
    private var timer: DispatchSourceTimer?
    private let timerQueue = DispatchQueue(label: "MeetingSurvivor.CameraExtension.timer", qos: .userInteractive)
    private var streamingCounter: UInt32 = 0
    private var videoDescription: CMFormatDescription!
    private var bufferPool: CVPixelBufferPool!
    private let bufferAuxAttributes: NSDictionary = [kCVPixelBufferPoolAllocationThresholdKey: 5]

    init(localizedName: String) {
        super.init()
        device = CMIOExtensionDevice(localizedName: localizedName, deviceID: deviceID, legacyDeviceID: "MeetingSurvivorCamera", source: self)
        let dimensions = CMVideoDimensions(width: cameraWidth, height: cameraHeight)
        CMVideoFormatDescriptionCreate(allocator: kCFAllocatorDefault, codecType: kCVPixelFormatType_32BGRA, width: dimensions.width, height: dimensions.height, extensions: nil, formatDescriptionOut: &videoDescription)
        let pixelBufferAttributes: NSDictionary = [
            kCVPixelBufferWidthKey: dimensions.width,
            kCVPixelBufferHeightKey: dimensions.height,
            kCVPixelBufferPixelFormatTypeKey: videoDescription.mediaSubType,
            kCVPixelBufferIOSurfacePropertiesKey: [:],
        ]
        CVPixelBufferPoolCreate(kCFAllocatorDefault, nil, pixelBufferAttributes, &bufferPool)
        let streamFormat = CMIOExtensionStreamFormat(
            formatDescription: videoDescription,
            maxFrameDuration: CMTime(value: 1, timescale: cameraFrameRate),
            minFrameDuration: CMTime(value: 1, timescale: cameraFrameRate),
            validFrameDurations: nil
        )
        streamSource = CameraStreamSource(localizedName: "MeetingSurvivor.Camera.Source", streamFormat: streamFormat, device: device)
        do {
            try device.addStream(streamSource.stream)
        } catch {
            fatalError("Could not add Meeting Survivor camera stream: \(error.localizedDescription)")
        }
    }

    var availableProperties: Set<CMIOExtensionProperty> {
        [.deviceTransportType, .deviceModel]
    }

    func deviceProperties(forProperties properties: Set<CMIOExtensionProperty>) throws -> CMIOExtensionDeviceProperties {
        let deviceProperties = CMIOExtensionDeviceProperties(dictionary: [:])
        if properties.contains(.deviceTransportType) {
            deviceProperties.transportType = kIOAudioDeviceTransportTypeVirtual
        }
        if properties.contains(.deviceModel) {
            deviceProperties.model = "Meeting Survivor Virtual Camera"
        }
        return deviceProperties
    }

    func setDeviceProperties(_ deviceProperties: CMIOExtensionDeviceProperties) throws {}

    func startStreaming() {
        streamingCounter += 1
        guard timer == nil else { return }
        let timer = DispatchSource.makeTimerSource(flags: .strict, queue: timerQueue)
        timer.schedule(deadline: .now(), repeating: 1.0 / Double(cameraFrameRate), leeway: .milliseconds(1))
        timer.setEventHandler { [weak self] in
            self?.sendStaticFrame()
        }
        timer.setCancelHandler {}
        self.timer = timer
        timer.resume()
    }

    func stopStreaming() {
        if streamingCounter > 1 {
            streamingCounter -= 1
            return
        }
        streamingCounter = 0
        timer?.cancel()
        timer = nil
    }

    private func sendStaticFrame() {
        var pixelBuffer: CVPixelBuffer?
        let createStatus = CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(kCFAllocatorDefault, bufferPool, bufferAuxAttributes, &pixelBuffer)
        guard createStatus == kCVReturnSuccess, let pixelBuffer else {
            os_log(.error, "Meeting Survivor camera could not allocate pixel buffer: %d", createStatus)
            return
        }
        fill(pixelBuffer: pixelBuffer)

        var timingInfo = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: cameraFrameRate),
            presentationTimeStamp: CMClockGetTime(CMClockGetHostTimeClock()),
            decodeTimeStamp: .invalid
        )
        var sampleBuffer: CMSampleBuffer?
        let sampleStatus = CMSampleBufferCreateForImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: pixelBuffer,
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: videoDescription,
            sampleTiming: &timingInfo,
            sampleBufferOut: &sampleBuffer
        )
        guard sampleStatus == noErr, let sampleBuffer else {
            os_log(.error, "Meeting Survivor camera could not create sample buffer: %d", sampleStatus)
            return
        }
        let hostTime = UInt64(timingInfo.presentationTimeStamp.seconds * Double(NSEC_PER_SEC))
        streamSource.stream.send(sampleBuffer, discontinuity: [], hostTimeInNanoseconds: hostTime)
    }

    private func fill(pixelBuffer: CVPixelBuffer) {
        CVPixelBufferLockBaseAddress(pixelBuffer, [])
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, []) }
        guard let baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer) else { return }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
        let badgeGreen: UInt8 = 140
        for y in 0..<height {
            let row = baseAddress.advanced(by: y * bytesPerRow).assumingMemoryBound(to: UInt8.self)
            for x in 0..<width {
                let offset = x * 4
                let inBadge = x > width / 4 && x < width * 3 / 4 && y > height / 3 && y < height * 2 / 3
                row[offset + 0] = inBadge ? 20 : UInt8(x % 255)
                row[offset + 1] = inBadge ? badgeGreen : UInt8(y % 255)
                row[offset + 2] = inBadge ? 220 : 35
                row[offset + 3] = 255
            }
        }
    }
}

final class CameraStreamSource: NSObject, CMIOExtensionStreamSource {
    private(set) var stream: CMIOExtensionStream!
    private let streamFormat: CMIOExtensionStreamFormat
    private let device: CMIOExtensionDevice

    init(localizedName: String, streamFormat: CMIOExtensionStreamFormat, device: CMIOExtensionDevice) {
        self.streamFormat = streamFormat
        self.device = device
        super.init()
        stream = CMIOExtensionStream(localizedName: localizedName, streamID: streamID, direction: .source, clockType: .hostTime, source: self)
    }

    var formats: [CMIOExtensionStreamFormat] {
        [streamFormat]
    }

    var activeFormatIndex = 0

    var availableProperties: Set<CMIOExtensionProperty> {
        [.streamActiveFormatIndex, .streamFrameDuration]
    }

    func streamProperties(forProperties properties: Set<CMIOExtensionProperty>) throws -> CMIOExtensionStreamProperties {
        let streamProperties = CMIOExtensionStreamProperties(dictionary: [:])
        if properties.contains(.streamActiveFormatIndex) {
            streamProperties.activeFormatIndex = activeFormatIndex
        }
        if properties.contains(.streamFrameDuration) {
            streamProperties.frameDuration = CMTime(value: 1, timescale: cameraFrameRate)
        }
        return streamProperties
    }

    func setStreamProperties(_ streamProperties: CMIOExtensionStreamProperties) throws {
        if let activeFormatIndex = streamProperties.activeFormatIndex {
            guard activeFormatIndex == 0 else { return }
            self.activeFormatIndex = activeFormatIndex
        }
    }

    func authorizedToStartStream(for client: CMIOExtensionClient) -> Bool {
        true
    }

    func startStream() throws {
        guard let deviceSource = device.source as? CameraDeviceSource else { return }
        deviceSource.startStreaming()
    }

    func stopStream() throws {
        guard let deviceSource = device.source as? CameraDeviceSource else { return }
        deviceSource.stopStreaming()
    }
}
