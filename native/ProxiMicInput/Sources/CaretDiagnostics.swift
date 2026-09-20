import AppKit
import Foundation

/// Explicitly enabled, time/size-bounded coordinate-only troubleshooting.
/// No text, clipboard, key events, or editor mutations are collected here.
final class CaretDiagnostics {
    static let shared = CaretDiagnostics()
    private let queue = DispatchQueue(label: "com.proximic.inputmethod.caret-diagnostics")
    private let expiry: TimeInterval
    private let destination: URL
    private var lastSample: TimeInterval = 0
    private var count = 0
    private var lifecycleCount = 0
    private init() {
        let directory = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support/ProxiMic/ime")
        let flag = directory.appendingPathComponent("caret-trace.enabled")
        let requested = (try? String(contentsOf: flag, encoding: .utf8)).flatMap { Double($0.trimmingCharacters(in: .whitespacesAndNewlines)) } ?? 0
        expiry = min(requested, Date().timeIntervalSince1970 + 600)
        destination = directory.appendingPathComponent("caret-trace-\(ProcessInfo.processInfo.processIdentifier).jsonl")
    }
    func shouldSample() -> Bool {
        guard Date().timeIntervalSince1970 < expiry, count < 600 else { return false }
        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastSample >= 1 else { return false }
        lastSample = now
        count += 1
        return true
    }
    func recordLifecycle(_ fields: [String: Any]) {
        guard Date().timeIntervalSince1970 < expiry, lifecycleCount < 300 else { return }
        lifecycleCount += 1
        var fields = fields
        fields["kind"] = "activation"
        record(fields)
    }
    func record(_ fields: [String: Any]) {
        var fields = fields
        fields["timestamp"] = Date().timeIntervalSince1970
        fields["pid"] = ProcessInfo.processInfo.processIdentifier
        guard JSONSerialization.isValidJSONObject(fields),
              var data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]) else { return }
        data.append(10)
        let destination = self.destination
        queue.async {
            if !FileManager.default.fileExists(atPath: destination.path) {
                FileManager.default.createFile(atPath: destination.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            guard let handle = try? FileHandle(forWritingTo: destination) else { return }
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: data)
        }
    }
    static func rect(_ value: NSRect) -> [Double] {
        [value.origin.x, value.origin.y, value.width, value.height].map { $0.isFinite ? Double($0) : -1 }
    }
    static func range(_ value: NSRange) -> [Int] { isValidRange(value) ? [value.location, value.length] : [-1, -1] }
}
