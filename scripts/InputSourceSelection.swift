import AppKit
import Foundation

struct SourceSelectionFailure: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

/// A source-only operation: never queries AX, a text field or its contents.
/// Returns true when already selected, so an active composition is untouched.
func ensureVoiceSource(modeID: String, currentSource: () -> String,
                       sameApplication: () -> Bool, select: () throws -> Void) throws -> Bool {
    guard sameApplication() else { throw SourceSelectionFailure("前台应用已变化，请重新 tap") }
    let current = currentSource()
    guard !current.isEmpty else { throw SourceSelectionFailure("系统未返回当前输入法，请重试") }
    if current == modeID { return true }
    guard sameApplication() else { throw SourceSelectionFailure("前台应用已变化，请重新 tap") }
    try select()
    guard currentSource() == modeID else { throw SourceSelectionFailure("系统未确认切换到语音输入法") }
    return false
}

/// Restore the foreground client's input context through a real, bounded focus
/// round trip. No AX, keyboard events, clipboard access or source cycling.
func restoreInputFocus(pid: Int32, bundle: String, currentSource: @escaping () -> String,
                       modeID: String) throws {
    let workspace = NSWorkspace.shared
    let ownPID = ProcessInfo.processInfo.processIdentifier
    let parentPID = getppid()
    guard let origin = NSRunningApplication(processIdentifier: pid),
          origin.bundleIdentifier == bundle, origin.isActive else {
        throw SourceSelectionFailure("前台应用已变化，请重新 tap")
    }
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let rect = NSScreen.main?.visibleFrame ?? .zero
    let window = NSWindow(contentRect: NSRect(x: rect.minX + 8, y: rect.minY + 8, width: 1, height: 1),
                          styleMask: [.titled], backing: .buffered, defer: false)
    window.alphaValue = 0
    window.hasShadow = false
    window.ignoresMouseEvents = true
    window.isReleasedWhenClosed = false
    window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
    var interrupted = false
    let observer = workspace.notificationCenter.addObserver(
        forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
    ) { notification in
        if let running = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
           running.processIdentifier != pid && running.processIdentifier != ownPID {
            interrupted = true
        }
    }
    defer {
        workspace.notificationCenter.removeObserver(observer)
        window.orderOut(nil)
    }
    func check() throws {
        guard !interrupted, !origin.isTerminated, parentPID > 1, getppid() == parentPID else {
            throw SourceSelectionFailure("已切换应用或取消启动")
        }
        let front = workspace.frontmostApplication?.processIdentifier
        guard front == pid || front == ownPID else {
            throw SourceSelectionFailure("已切换应用，本句已取消")
        }
    }
    var outcome: Result<Void, Error>?
    var phase = "acquiring"
    var phaseStart = ProcessInfo.processInfo.systemUptime
    var originSince: Double?
    var timer: Timer?
    func complete(_ result: Result<Void, Error>) {
        guard outcome == nil else { return }
        outcome = result
        timer?.invalidate()
        app.stop(nil)
        // Wake only our own event loop; this never injects a system input event.
        if let event = NSEvent.otherEvent(with: .applicationDefined, location: .zero,
            modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
            subtype: 0, data1: 0, data2: 0) { app.postEvent(event, atStart: false) }
    }
    timer = Timer.scheduledTimer(withTimeInterval: 0.005, repeats: true) { _ in
        do {
            try check()
            let now = ProcessInfo.processInfo.systemUptime
            switch phase {
            case "acquiring":
                if app.isActive && window.isKeyWindow {
                    phase = "holding"; phaseStart = now
                } else if now - phaseStart > 0.7 {
                    throw SourceSelectionFailure("输入法激活未完成，请重试")
                }
            case "holding":
                if now - phaseStart >= 0.15 {
                    if workspace.frontmostApplication?.processIdentifier == ownPID {
                        guard origin.activate(options: []) else {
                            throw SourceSelectionFailure("未能返回原应用，请重新点入文本框")
                        }
                    }
                    window.orderOut(nil)
                    phase = "returning"; phaseStart = now
                }
            default:
                if workspace.frontmostApplication?.processIdentifier == pid {
                    if originSince == nil { originSince = now }
                    if now - originSince! >= 0.05 {
                        guard currentSource() == modeID else {
                            throw SourceSelectionFailure("输入法已变化，请重新 tap")
                        }
                        complete(.success(()))
                    }
                } else { originSince = nil }
                if now - phaseStart > 0.7 {
                    throw SourceSelectionFailure("未能返回原应用，请重新点入文本框")
                }
            }
        } catch { complete(.failure(error)) }
    }
    window.makeKeyAndOrderFront(nil)
    app.activate(ignoringOtherApps: true)
    app.run()
    timer?.invalidate()
    try (outcome ?? .failure(SourceSelectionFailure("输入法激活已取消"))).get()

}

func selectSourceSession(pid: Int32, bundle: String, modeID: String,
                         refreshOnly: Bool = false,
                         currentSource: @escaping () -> String, select: () throws -> Void) -> Never {
    func emit(_ fields: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: fields) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        }
    }
    alarm(5)
    let start = ProcessInfo.processInfo.systemUptime
    do {
        // Recovery of an already selected, inactive source never overrides a
        // newer manual input-source choice.
        if refreshOnly && currentSource() != modeID {
            throw SourceSelectionFailure("输入法已变化，请重新 tap")
        }
        let alreadySelected = try ensureVoiceSource(modeID: modeID, currentSource: currentSource,
            sameApplication: {
                let current = NSWorkspace.shared.frontmostApplication
                return current?.processIdentifier == pid && current?.bundleIdentifier == bundle
            }, select: select)
        let restore = !alreadySelected || refreshOnly
        if restore {
            try restoreInputFocus(pid: pid, bundle: bundle, currentSource: currentSource, modeID: modeID)
        }
        emit(["event": "selected", "already_selected": alreadySelected,
              "focus_restored": restore,
              "elapsed_ms": (ProcessInfo.processInfo.systemUptime - start) * 1000])
        exit(0)
    } catch {
        emit(["event": "error", "error": error.localizedDescription])
        exit(1)
    }
}
