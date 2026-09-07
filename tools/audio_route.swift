// macOS: mirror the current listening output to BlackHole, or restore it.
// Usage: swift tools/audio_route.swift enable|status|restore
import Foundation
import CoreAudio

let system = AudioObjectID(kAudioObjectSystemObject)
let routeUID = "com.agentic-music-visualizer.monitor"
let stateURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("artifacts/audio-route.json")
func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr { throw NSError(domain: what, code: Int(status)) }
}
func address(_ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}
func devices() throws -> [AudioObjectID] {
    var a = address(kAudioHardwarePropertyDevices), size: UInt32 = 0
    try check(AudioObjectGetPropertyDataSize(system, &a, 0, nil, &size), "device list size")
    var ids = [AudioObjectID](repeating: 0, count: Int(size)/4)
    try check(AudioObjectGetPropertyData(system, &a, 0, nil, &size, &ids), "device list")
    return ids
}
func string(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) throws -> String {
    var a = address(selector), value: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    try withUnsafeMutablePointer(to: &value) { pointer in
        try check(AudioObjectGetPropertyData(id, &a, 0, nil, &size, pointer), "device string")
    }
    return value as String
}
func currentOutput(_ selector: AudioObjectPropertySelector = kAudioHardwarePropertyDefaultOutputDevice) throws -> AudioObjectID {
    var a = address(selector), id: AudioObjectID = 0, size: UInt32 = 4
    try check(AudioObjectGetPropertyData(system, &a, 0, nil, &size, &id), "default output")
    return id
}
func setOutput(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector = kAudioHardwarePropertyDefaultOutputDevice) throws {
    var a = address(selector), value = id
    try check(AudioObjectSetPropertyData(system, &a, 0, nil, 4, &value), "set output")
}
func outputCapable(_ id: AudioObjectID) -> Bool {
    var a = address(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput), size: UInt32 = 0
    return AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size) == noErr && size > 0
}
do {
    let command = CommandLine.arguments.dropFirst().first ?? "status"
    let ids = try devices()
    let current = try currentOutput()
    let uid = try string(current, kAudioDevicePropertyDeviceUID)
    if command == "enable" {
        if uid == routeUID { print("AMV monitoring is already enabled."); exit(0) }
        guard outputCapable(current), uid != "BlackHole2ch_UID" else { throw NSError(domain: "Select your listening output first", code: 1) }
        guard ids.contains(where: { (try? string($0, kAudioDevicePropertyDeviceUID)) == "BlackHole2ch_UID" }) else { throw NSError(domain: "BlackHole 2ch is missing", code: 1) }
        // Preserve original output BEFORE mutation, to allow restoration.
        let systemUID = try string(currentOutput(kAudioHardwarePropertyDefaultSystemOutputDevice), kAudioDevicePropertyDeviceUID)
        let state = ["originalOutputUID": uid, "originalOutputName": try string(current, kAudioObjectPropertyName), "originalSystemOutputUID": systemUID == routeUID ? uid : systemUID]
        try FileManager.default.createDirectory(at: stateURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: state, options: .prettyPrinted).write(to: stateURL, options: .atomic)
        var aggregate: AudioObjectID = 0
        if let existing = ids.first(where: { (try? string($0, kAudioDevicePropertyDeviceUID)) == routeUID }) {
            // Never reuse a route that may point at a different listening device.
            try check(AudioHardwareDestroyAggregateDevice(existing), "replace AMV route")
        }
        let description: [String: Any] = [
            kAudioAggregateDeviceUIDKey: routeUID,
            kAudioAggregateDeviceNameKey: "AMV - " + (try string(current, kAudioObjectPropertyName)) + " + BlackHole",
            kAudioAggregateDeviceIsPrivateKey: 0,
            // On this macOS, 1 creates Multi-Output (0 creates Aggregate).
            // Verified in Audio MIDI Setup and by its 0-in / 2-out layout.
            kAudioAggregateDeviceIsStackedKey: 1,
            kAudioAggregateDeviceMainSubDeviceKey: "BlackHole2ch_UID",
            kAudioAggregateDeviceSubDeviceListKey: [
                [kAudioSubDeviceUIDKey: "BlackHole2ch_UID", kAudioSubDeviceDriftCompensationKey: 0],
                [kAudioSubDeviceUIDKey: uid, kAudioSubDeviceDriftCompensationKey: 1]
            ]
        ]
        try check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregate), "create multi-output")
        try setOutput(aggregate)
    } else if command == "restore" {
        let state = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as! [String: String]
        guard let original = ids.first(where: { (try? string($0, kAudioDevicePropertyDeviceUID)) == state["originalOutputUID"] }) else { throw NSError(domain: "Original output is disconnected; reconnect it before restore", code: 1) }
        try setOutput(original)
        let savedSystemUID = state["originalSystemOutputUID"] ?? state["originalOutputUID"]
        if let originalSystem = ids.first(where: { (try? string($0, kAudioDevicePropertyDeviceUID)) == savedSystemUID }) {
            try setOutput(originalSystem, kAudioHardwarePropertyDefaultSystemOutputDevice)
        }
    } else if command != "status" { throw NSError(domain: "Use enable, status or restore", code: 1) }
    let selected = try currentOutput()
    print("Default output: \(try string(selected, kAudioObjectPropertyName))")
    print("UID: \(try string(selected, kAudioDevicePropertyDeviceUID))")
} catch { fputs("Audio route failed: \(error)\n", stderr); exit(1) }
