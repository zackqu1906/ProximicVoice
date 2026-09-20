import Foundation
import Darwin

/// Authenticated, current-user-only Unix socket transport. No network listener.
/// Socket work stays off AppKit's thread. Callbacks arrive on the main queue.
final class IPCClient {
    var onMessage: (([String: Any]) -> Void)?
    var onDisconnect: (() -> Void)?
    private let queue = DispatchQueue(label: "com.proximic.inputmethod.socket", qos: .userInitiated)
    private let lock = NSLock()
    private let outputQueue = DispatchQueue(label: "com.proximic.inputmethod.socket.write", qos: .userInitiated)
    private var descriptor: Int32 = -1
    private var running = false
    private var generation: UInt64 = 0
    private let maximumFrame = 8 * 1024 * 1024
    let socketPath: String

    init() {
        socketPath = ProcessInfo.processInfo.environment["PROXIMIC_IME_SOCKET"] ??
            (NSHomeDirectory() + "/Library/Application Support/ProxiMic/ime/bridge.sock")
    }

    func start() {
        lock.lock()
        guard !running else { lock.unlock(); return }
        running = true
        lock.unlock()
        queue.async { [weak self] in self?.run() }
    }

    func stop() {
        lock.lock()
        running = false
        let fd = descriptor
        descriptor = -1
        if fd >= 0 { _ = Darwin.shutdown(fd, SHUT_RDWR) }
        lock.unlock()
    }

    private var isRunning: Bool {
        lock.lock(); defer { lock.unlock() }
        return running
    }

    private func credentials() -> String? {
        let tokenPath = (socketPath as NSString).deletingLastPathComponent + "/bridge.token"
        var information = stat()
        guard lstat(tokenPath, &information) == 0,
              information.st_uid == getuid(), (information.st_mode & S_IFMT) == S_IFREG,
              information.st_mode & 0o077 == 0, information.st_size > 0,
              information.st_size <= 512,
              let token = try? String(contentsOfFile: tokenPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
              !token.isEmpty else { return nil }
        var socketInfo = stat()
        guard lstat(socketPath, &socketInfo) == 0, socketInfo.st_uid == getuid(),
              (socketInfo.st_mode & S_IFMT) == S_IFSOCK, socketInfo.st_mode & 0o077 == 0 else { return nil }
        return token
    }

    private func connectSocket() -> Int32 {
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return -1 }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(socketPath.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { Darwin.close(fd); return -1 }
        withUnsafeMutableBytes(of: &address.sun_path) { destination in
            bytes.withUnsafeBytes { destination.copyBytes(from: $0) }
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        var noSIGPIPE: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSIGPIPE, socklen_t(MemoryLayout<Int32>.size))
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard result == 0 else { Darwin.close(fd); return -1 }
        var peerUID: uid_t = 0
        var peerGID: gid_t = 0
        guard getpeereid(fd, &peerUID, &peerGID) == 0, peerUID == getuid() else { Darwin.close(fd); return -1 }
        // A wedged host must not hold the IME's main thread indefinitely during send.
        var timeout = timeval(tv_sec: 0, tv_usec: 100_000)
        _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        return fd
    }

    func send(_ object: [String: Any]) {
        lock.lock()
        let connection = generation
        let connected = descriptor >= 0
        lock.unlock()
        guard connected else { return }
        outputQueue.async { [weak self] in
            guard let self, let payload = try? JSONSerialization.data(withJSONObject: object),
                  payload.count < self.maximumFrame else { return }
            var frame = payload
            frame.append(10)
            self.writeFrame(frame, generation: connection)
        }
    }

    private func writeFrame(_ frame: Data, generation connection: UInt64) {
        lock.lock()
        let fd = generation == connection && descriptor >= 0 ? Darwin.dup(descriptor) : -1
        lock.unlock()
        guard fd >= 0 else { return }
        defer { Darwin.close(fd) }
        let deadline = ProcessInfo.processInfo.systemUptime + 1.5
        let succeeded = frame.withUnsafeBytes { bytes -> Bool in
            var offset = 0
            while offset < bytes.count {
                guard ProcessInfo.processInfo.systemUptime < deadline else { return false }
                let count = Darwin.send(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset, 0)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { return false }
                offset += count
            }
            return true
        }
        if !succeeded { _ = Darwin.shutdown(fd, SHUT_RDWR) }
    }

    private func run() {
        while isRunning {
            guard let token = credentials() else { Thread.sleep(forTimeInterval: 1); continue }
            let fd = connectSocket()
            guard fd >= 0 else { Thread.sleep(forTimeInterval: 1); continue }
            lock.lock()
            generation &+= 1
            descriptor = fd
            lock.unlock()
            send(["type": "hello", "protocol": 1, "token": token,
                  "component_version": "1.0.0", "bundle": "com.proximic.inputmethod.ProxiMicVoice"])
            var buffer = Data()
            var chunk = [UInt8](repeating: 0, count: 65_536)
            connection: while isRunning {
                let count = Darwin.read(fd, &chunk, chunk.count)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { break }
                buffer.append(contentsOf: chunk[0..<count])
                guard buffer.count <= maximumFrame else { break }
                while let newline = buffer.firstIndex(of: 10) {
                    let line = buffer.prefix(upTo: newline)
                    buffer.removeSubrange(...newline)
                    guard let decoded = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                          decoded["type"] is String else { break connection }
                    DispatchQueue.main.async { [weak self] in self?.onMessage?(decoded) }
                }
            }
            lock.lock()
            if descriptor == fd { descriptor = -1 }
            Darwin.close(fd)
            lock.unlock()
            DispatchQueue.main.async { [weak self] in self?.onDisconnect?() }
            if isRunning { Thread.sleep(forTimeInterval: 1) }
        }
    }
}
