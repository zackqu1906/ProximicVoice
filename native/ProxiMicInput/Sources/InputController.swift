import AppKit
import InputMethodKit
import Carbon

final class IMEService {
    static let shared = IMEService()
    let transport = IPCClient()
    let activation = InputActivation<ProxiMicInputController>()
    var activeController: ProxiMicInputController? { activation.current }
    private(set) var epoch: String?
    var modeShortcut = "F8"
    var cancelShortcut = "Esc"
    var multiUndoEnabled = false

    private init() {
        transport.onMessage = { [weak self] in self?.receive($0) }
        transport.onDisconnect = { [weak self] in
            guard let self else { return }
            self.epoch = nil
            self.activeController?.hostDisconnected()
        }
    }
    func start() { transport.start() }
    func send(_ message: [String: Any]) {
        guard let epoch else { return }
        var envelope = message
        envelope["epoch"] = epoch
        transport.send(envelope)
    }
    func inactiveState(requestID: String? = nil, lifecycleEvent: String = "inactive") {
        var state: [String: Any] = ["type": "state", "ready": false, "phase": "idle", "client_id": "",
                                    "utterance_id": "", "application": "", "revision": 0,
                                    "edit_requested": false, "raw": "", "error": "请手动选中 ProxiMic 语音输入法并进入文本框",
                                    "lifecycle_event": lifecycleEvent, "has_composition": false,
                                    "original": "", "selection": [0, 0], "context_complete": false,
                                    "capabilities": ["marked_text": false, "context_complete": false,
                                                     "document_access": false, "range_replacement": "unavailable",
                                                     "selection_restore": false]]
        if let requestID { state["request_id"] = requestID }
        send(state)
    }

    private func receive(_ message: [String: Any]) {
        guard let type = message["type"] as? String else { return }
        if type == "welcome" {
            guard message["protocol"] as? Int == 1, let next = message["epoch"] as? String, !next.isEmpty else { return }
            if epoch != next { activeController?.hostConnected() }
            epoch = next
            if let active = activeController { active.sendState(includeSnapshot: true) }
            else { inactiveState() }
            return
        }
        guard let epoch, message["epoch"] as? String == epoch else { return }
        if type == "configure" {
            if let value = message["mode_switch_shortcut"] as? String { modeShortcut = value }
            if let value = message["cancel_shortcut"] as? String { cancelShortcut = value }
            if let value = message["multi_undo_enabled"] as? Bool {
                multiUndoEnabled = value
                activeController?.configureUndoHistory(value)
            }
            return
        }
        if type == "recover_activation" {
            // Older hosts must not initiate an ABC round-trip after an update.
            send(["type": "activation_recovery", "request_id": message["request_id"] as? String ?? "",
                  "result": "disabled", "error": ""])
            return
        }
        if type == "ping" {
            let request = message["request_id"] as? String
            if let active = activeController { active.sendState(includeSnapshot: true, requestID: request) }
            else { inactiveState(requestID: request) }
            return
        }
        guard let active = activeController else { inactiveState(); return }
        active.receive(message)
    }
}

@objc(ProxiMicInputController)
final class ProxiMicInputController: IMKInputController {
    private var adapter: NativeInputClient?
    private let sessionLifetime = CompositionSessionLifetime()
    private var session: CompositionSession? {
        get { sessionLifetime.current }
        set { sessionLifetime.current = newValue }
    }
    private var timer: Timer?
    private var activationTimer: Timer?
    private var currentClientID = UUID().uuidString
    private var activated = false
    private var preparingActivation = false
    private var stateReadInProgress = false
    private var activationLease: UInt64 = 0
    private let activationSenders = InputActivationSenders()
    private var activationApplication = ""
    private var ownsActivation: Bool { IMEService.shared.activation.owns(self, activationLease) }
    private let panel = ActionPanel()
    private var isWriting = false
    private var lastError = ""
    private var lastContext: EditorSnapshot?
    private var lastLifecycleEvent = "created"
    private var rejectedBeginID: String?
    private var compatibilityKeyReply: (id: String, callback: (String?) -> Void)?
    private let undoHistory = CompositionUndoHistory()
    private var undoSourceUtteranceID: String?
    private var preservingHistory = false

    var isUnboundIdleClient: Bool {
        activated && !preparingActivation && session == nil
            && (lastContext == nil || lastContext?.startError != nil)
    }

    func configureUndoHistory(_ enabled: Bool) {
        undoHistory.configure(enabled: enabled)
        if let session, [.dictated, .edited].contains(session.phase) {
            undoHistory.settled(session, sourceUtteranceID: undoSourceUtteranceID)
        }
        if activated { changed() }
    }

    private func traceLifecycle(_ event: String, sender: AnyObject? = nil) {
        let fields: [String: Any] = [
            "event": event, "controller": String(describing: ObjectIdentifier(self)),
            "lease": activationLease, "client_id": currentClientID,
            "activated": activated, "preparing": preparingActivation,
            "native_read": adapter?.activeRead ?? "", "state_read": stateReadInProgress,
            "phase": session?.phase.rawValue ?? "idle",
            "application": activationApplication,
            "front_application": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "",
            "sender": sender.map { String(describing: ObjectIdentifier($0)) } ?? "",
            "bound_sender": activationSenders.current.map { String(describing: ObjectIdentifier($0)) } ?? ""]
        CaretDiagnostics.shared.recordLifecycle(fields)
        IMEService.shared.send(fields.merging(["type": "activation_trace"]) { _, value in value })
    }

    override func activateServer(_ sender: Any!) {
        let service = IMEService.shared
        let previous = service.activeController
        let lease = service.activation.begin(self)
        activationLease = lease
        activationSenders.begin(sender as AnyObject?)
        // Retire local timers before any remote IMK call can reenter us.
        activated = false
        preparingActivation = false
        activationTimer?.invalidate()
        activationTimer = nil
        timer?.invalidate()
        timer = nil
        panel.hide()
        undoHistory.clear()
        undoHistory.configure(enabled: service.multiUndoEnabled)
        undoSourceUtteranceID = nil
        if let previous, previous !== self {
            previous.lastLifecycleEvent = "client_switched"
            previous.activated = false
            previous.timer?.invalidate()
            previous.timer = nil
            previous.panel.hide()
            previous.invalidate("已切换输入框")
        }
        guard service.activation.owns(self, lease) else { return }
        if session != nil {
            lastLifecycleEvent = "reactivate"
            invalidate("输入法已重新激活")
        }
        guard service.activation.owns(self, lease) else { return }
        session = nil
        super.activateServer(sender)
        guard service.activation.owns(self, lease) else { return }
        traceLifecycle("activate_callback")
        // DispatchQueue.main.async can run before the remaining IMK switching
        // callbacks: our first remote read then pumps a queued deactivation.
        // Allow a short, cancellable run-loop settling window before querying.
        // This is only preparation, never readiness or permission to write.
        preparingActivation = true
        activationTimer = Timer.scheduledTimer(withTimeInterval: 0.025, repeats: false) { [weak self] _ in
            guard let self, service.activation.owns(self, lease) else { return }
            self.activationTimer = nil
            self.prepareActivation(sender, lease: lease)
        }
    }

    private func prepareActivation(_ sender: Any!, lease: UInt64) {
        let service = IMEService.shared
        guard service.activation.owns(self, lease) else { return }
        preparingActivation = true
        defer { if activationLease == lease { preparingActivation = false } }
        guard let input = sender as? IMKTextInput ?? client() else {
            if service.activation.end(self, lease) { service.inactiveState() }
            return
        }
        guard service.activation.owns(self, lease) else { return }
        let nextAdapter = NativeInputClient(input, isCurrent: { [weak self] in
            guard let self else { return false }
            return self.activated && service.activation.owns(self, lease)
        }, preeditAttributes: { [weak self] range in
            self?.mark(forStyle: kTSMHiliteSelectedRawText, at: range) as? [NSAttributedString.Key: Any]
        })
        let bundle = input.bundleIdentifier() ?? ""
        activationApplication = bundle
        guard service.activation.owns(self, lease) else { return }
        if !bundle.isEmpty {
            nextAdapter.configureKeyDelivery { [weak self] command, count, tag, reply in
                guard let self, self.activated, self.ownsActivation, self.session?.active == true else { reply("输入会话已结束"); return }
                let id = UUID().uuidString
                self.compatibilityKeyReply = (id, reply)
                self.sendEvent("wechat_key", extra: ["request_id": id, "command": command, "count": count,
                                                     "event_tag": tag, "application": bundle])
            }
        }
        guard service.activation.owns(self, lease) else { return }
        adapter = nextAdapter
        currentClientID = UUID().uuidString
        activated = true
        lastLifecycleEvent = "activate"
        lastError = ""
        lastContext = nil
        rejectedBeginID = nil
        panel.onCancel = { [weak self] in self?.cancelCurrentOrHistory() }
        panel.onConvert = { [weak self] in self?.perform { $0.convert() } }
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in self?.tick() }
        guard service.activation.owns(self, lease) else { return }
        preparingActivation = false
        traceLifecycle("activate_prepared")
        sendState(includeSnapshot: true)
    }

    private func retireActivation(_ event: String, reason: String) {
        traceLifecycle(event)
        let lease = activationLease
        activated = false
        preparingActivation = false
        activationTimer?.invalidate()
        activationTimer = nil
        timer?.invalidate()
        timer = nil
        panel.hide()
        lastLifecycleEvent = event
        // Publish inactivity before cleanup (which can activate a newer client).
        if IMEService.shared.activation.end(self, lease) {
            IMEService.shared.inactiveState(lifecycleEvent: event)
        }
        invalidate(reason)
        if activationLease == lease { adapter = nil }
    }

    override func deactivateServer(_ sender: Any!) {
        let source = sender as AnyObject?
        if activationSenders.isKnownRetired(source) {
            traceLifecycle("ignored_retired_sender", sender: source)
            return
        }
        traceLifecycle("deactivate_received", sender: source)
        retireActivation("deactivate", reason: "已切换输入法或输入框")
        super.deactivateServer(sender)
    }

    override func inputControllerWillClose() {
        retireActivation("close", reason: "输入会话已关闭")
        super.inputControllerWillClose()
    }

    override func hidePalettes() { panel.hide() }

    override func recognizedEvents(_ sender: Any!) -> Int {
        // Geometry-only ACKs normalize full-composition selections. Observe
        // direct mouse takeover in every client before a same-length user
        // selection could be mistaken for the client's composition convention.
        return Int(NSEvent.EventTypeMask.keyDown.union(.leftMouseDown).rawValue)
    }

    override func handle(_ event: NSEvent!, client sender: Any!) -> Bool {
        guard activated, ownsActivation, let event else { return false }
        if event.type == .leftMouseDown {
            if session?.active == true || undoHistory.enabled || adapter?.clientOperationPending == true {
                invalidate("鼠标操作已接管本句")
            }
            return false
        }
        guard event.type == .keyDown else { return false }
        if let compatibility = adapter?.pendingCompatibility,
           compatibility.acceptsKey(code: Int(event.keyCode), command: event.modifierFlags.contains(.command),
                                    shift: event.modifierFlags.contains(.shift),
                                    otherModifiers: !event.modifierFlags.intersection([.control, .option]).isEmpty,
                                    tag: event.cgEvent?.getIntegerValueField(.eventSourceUserData) ?? 0) { return false }
        guard let session else { return false }
        if matches(IMEService.shared.cancelShortcut, event: event),
           session.active || (session.phase == .undone && undoHistory.count > 0) {
            cancelCurrentOrHistory()
            return true
        }
        if session.phase == .error {
            lastLifecycleEvent = "keyboard_input"
            invalidate("键盘输入已接管本句")
            return false
        }
        guard session.active else { undoHistory.clear(); sendState(); return false }
        if matches(IMEService.shared.modeShortcut, event: event) {
            perform { $0.convert() }
            return true
        }
        if (event.keyCode == 36 || event.keyCode == 76), [.listening, .finishing].contains(session.phase) {
            perform { $0.finish() }
            return true
        }
        lastLifecycleEvent = "keyboard_input"
        invalidate("键盘输入已接管本句")
        // This component intentionally has no Pinyin engine. Ordinary keys go to the client.
        return false
    }

    override func commitComposition(_ sender: Any!) {
        guard activated, ownsActivation, !isWriting, let session else { return }
        lastLifecycleEvent = session.active && session.hasComposition ? "commit_composition" : "commit_composition_ignored"
        isWriting = true
        let interrupted = session.commitCompositionRequested()
        isWriting = false
        if interrupted { panel.hide() }
        else { sendState() }
    }

    func invalidate(_ reason: String, preservingUndo: Bool = false) {
        preservingHistory = preservingUndo
        defer { preservingHistory = false }
        if !preservingUndo { undoHistory.clear(); adapter?.discardContextHint() }
        compatibilityKeyReply = nil
        adapter?.cancelClientOperation()
        guard let session, session.active || session.phase == .error else {
            panel.hide()
            if activated { sendState() }
            return
        }
        let wasWriting = isWriting
        isWriting = true
        defer { isWriting = wasWriting }
        session.interrupt(reason, commitIfOwned: session.active)
        panel.hide()
    }

    func hostDisconnected() {
        undoHistory.clear()
        rejectedBeginID = nil
        lastLifecycleEvent = "host_disconnected"
        let wasWriting = isWriting
        isWriting = true
        defer { isWriting = wasWriting }
        sessionLifetime.clear("主程序连接已断开，已保留当前文字")
        lastContext = nil
        panel.hide()
        lastError = "主程序未连接"
    }

    func hostConnected() {
        undoHistory.clear()
        rejectedBeginID = nil
        lastLifecycleEvent = "host_connected"
        let wasWriting = isWriting
        isWriting = true
        defer { isWriting = wasWriting }
        sessionLifetime.clear("输入连接已重新建立")
        lastContext = nil
        lastError = ""
        panel.hide()
    }

    private func matches(_ shortcut: String, event: NSEvent) -> Bool {
        let pieces = shortcut.lowercased().replacingOccurrences(of: " ", with: "").split(separator: "+").map(String.init)
        guard let key = pieces.last, !key.isEmpty else { return false }
        var required: NSEvent.ModifierFlags = []
        for modifier in pieces.dropLast() {
            switch modifier {
            case "cmd", "command", "meta", "⌘": required.insert(.command)
            case "ctrl", "control", "⌃": required.insert(.control)
            case "alt", "option", "opt", "⌥": required.insert(.option)
            case "shift", "⇧": required.insert(.shift)
            default: return false
            }
        }
        let meaningful: NSEvent.ModifierFlags = [.command, .control, .option, .shift]
        guard event.modifierFlags.intersection(meaningful) == required else { return false }
        let codes: [String: UInt16] = ["esc": 53, "escape": 53, "f1": 122, "f2": 120, "f3": 99, "f4": 118,
                                      "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
                                      "f11": 103, "f12": 111, "return": 36, "enter": 36, "space": 49]
        if let code = codes[key] { return event.keyCode == code }
        return event.charactersIgnoringModifiers?.lowercased() == key
    }

    func receive(_ message: [String: Any]) {
        guard activated, ownsActivation, !preparingActivation, let adapter, let type = message["type"] as? String else { return }
        if type == "reset", message["all"] as? Bool == true {
            lastLifecycleEvent = "reset"
            invalidate("主程序已结束本句")
            sendState(includeSnapshot: true)
            return
        }
        guard message["client_id"] as? String == currentClientID,
              let utteranceID = message["utterance_id"] as? String,
              let sequence = message["seq"] as? Int else { return }
        if type == "begin" {
            let requestedClientID = currentClientID
            if session?.utteranceID == utteranceID { return }
            if let session, session.phase == .error, session.hasComposition || session.awaitingReadback {
                // A queued tap may predate ready=false. Keep the old ownership
                // proof until recovery completes, but retire the new request.
                rejectedBeginID = utteranceID
                sendState(includeSnapshot: true)
                IMEService.shared.send(["type": "interrupted", "client_id": currentClientID,
                                        "utterance_id": utteranceID,
                                        "reason": "上一句输入组合尚未结束，请稍后重试"])
                return
            }
            rejectedBeginID = nil
            invalidate("开始了新一句", preservingUndo: true)
            guard activated, ownsActivation, currentClientID == requestedClientID, self.adapter === adapter else { return }
            lastLifecycleEvent = "begin"
            traceLifecycle("begin_snapshot")
            do {
                let next = try CompositionSession(client: adapter, utteranceID: utteranceID, sequence: sequence)
                guard activated, ownsActivation, currentClientID == requestedClientID, self.adapter === adapter else { return }
                undoHistory.begin(next.original)
                undoSourceUtteranceID = nil
                session = next
                lastContext = next.original
                lastError = ""
                wire(next)
                traceLifecycle("begin_ready")
                changed(includeSnapshot: true)
            } catch {
                guard activated, ownsActivation, currentClientID == requestedClientID, self.adapter === adapter else { return }
                session = nil
                undoHistory.clear()
                lastError = error.localizedDescription
                sendState(includeSnapshot: true, failedUtteranceID: utteranceID)
                IMEService.shared.send(["type": "interrupted", "client_id": currentClientID,
                                        "utterance_id": utteranceID, "revision": 0,
                                        "lifecycle_event": lastLifecycleEvent, "has_composition": false,
                                        "reason": lastError, "error": lastError])
            }
            return
        }
        guard let session, session.utteranceID == utteranceID, session.accepts(sequence: sequence) else { return }
        if type == "cancel" {
            cancelCurrentOrHistory()
            return
        }
        if type == "wechat_key_result" {
            guard let reply = compatibilityKeyReply, message["request_id"] as? String == reply.id else { return }
            compatibilityKeyReply = nil
            reply.callback((message["error"] as? String).flatMap { $0.isEmpty ? nil : $0 })
            return
        }
        perform { session in
            switch type {
            case "update":
                if let error = message["error"] as? String, !error.isEmpty { session.failASR(error) }
                else if let text = message["text"] as? String {
                    if (text as NSString).length > 65_536 { session.failASR("本句超过长度限制，已保留当前听写") }
                    else { session.update(text, final: message["final"] as? Bool ?? false) }
                }
            case "convert": session.convert()
            case "finish": session.finish()
            case "edit_result":
                if let revision = message["revision"] as? Int {
                    let result = message["text"] as? String ?? ""
                    let error = (result as NSString).length > 512 * 1024 ? "编辑结果超过长度限制，已保留听写" : message["error"] as? String
                    session.applyEdit(text: result, revision: revision, error: error)
                }
            case "reset":
                undoHistory.clear()
                lastLifecycleEvent = "reset"
                session.interrupt("主程序已结束本句")
            default: break
            }
        }
    }

    private func wire(_ next: CompositionSession) {
        let isCurrent = { [weak self, weak next] in
            guard let self, let next else { return false }
            return self.activated && self.ownsActivation && self.session === next
        }
        next.onChanged = { [weak self] in if isCurrent() { self?.changed() } }
        next.onFinishAudio = { [weak self] in if isCurrent() { self?.sendEvent("finish_audio") } }
        next.onInterrupted = { [weak self] reason in
            if isCurrent() {
                if self?.preservingHistory != true { self?.undoHistory.clear() }
                self?.sendEvent("interrupted", extra: ["reason": reason])
            }
        }
        next.onSettled = { [weak self] phase, text in
            if isCurrent(), let self, let session = self.session {
                self.undoHistory.settled(session, sourceUtteranceID: self.undoSourceUtteranceID)
                self.sendEvent("settled", extra: ["phase": phase, "text": text])
            }
        }
        next.onEdit = { [weak self] original, instruction, revision in
            guard isCurrent() else { return }
            self?.sendEvent("edit_requested", extra: ["original": original, "instruction": instruction, "revision": revision])
        }
    }

    private func cancelCurrentOrHistory() {
        guard activated, ownsActivation, !isWriting, let current = session, let adapter else { return }
        if current.phase == .undone, let record = undoHistory.pop() {
            let restored = CompositionSession(client: adapter, undo: record,
                                              utteranceID: current.utteranceID,
                                              sequence: current.lastSequence, revision: current.revision + 1)
            undoSourceUtteranceID = record.sourceUtteranceID
            session = restored
            lastContext = restored.original
            wire(restored)
            // Publish this transaction phase before an asynchronous native key
            // request, so the host never posts keys for an inactive old phase.
            sendState()
        }
        perform { $0.cancel() }
    }

    private func perform(_ operation: (CompositionSession) -> Void) {
        guard let session, activated, ownsActivation, !isWriting else { return }
        isWriting = true
        operation(session)
        isWriting = false
        tick()
    }

    private func sendEvent(_ type: String, extra: [String: Any] = [:]) {
        guard activated, ownsActivation, let session else { return }
        let lease = activationLease
        sendState()
        guard IMEService.shared.activation.owns(self, lease), self.session === session else { return }
        var event = extra
        event["type"] = type
        event["client_id"] = currentClientID
        event["utterance_id"] = session.utteranceID
        event["revision"] = session.revision
        event["raw"] = session.raw
        event["error"] = session.error
        event["lifecycle_event"] = lastLifecycleEvent
        event["has_composition"] = session.hasComposition
        event["edit_context_available"] = session.editContextAvailable
        event["edit_scope"] = session.editScope
        event["source_utterance_id"] = undoSourceUtteranceID ?? session.utteranceID
        IMEService.shared.send(event)
    }

    func sendState(includeSnapshot: Bool = false, requestID: String? = nil, failedUtteranceID: String? = nil) {
        guard activated, ownsActivation, !preparingActivation, !stateReadInProgress, let adapter else { return }
        // IMK reads can pump the run loop; coalesce nested host pings instead
        // of recursively reading the same document on a half-built snapshot.
        stateReadInProgress = true
        defer { stateReadInProgress = false }
        let stateClientID = currentClientID
        let context: EditorSnapshot?
        if let session, session.active, requestID == nil || [.listening, .finishing].contains(session.phase) { context = session.original }
        else if includeSnapshot { context = try? adapter.snapshotForBeginning() }
        else { context = lastContext }
        guard activated, ownsActivation, currentClientID == stateClientID, self.adapter === adapter else { return }
        if includeSnapshot, requestID == nil { lastContext = context }
        let complete = context?.complete ?? false
        let access = context?.documentAccess ?? false
        // Starting requires a usable selection, never a previous IME's mark.
        let targetError = context?.startError ?? (context == nil ? "请先点入可编辑的文本框" : nil)
        let recovering = session.map { $0.phase == .error && ($0.hasComposition || $0.awaitingReadback) } ?? false
        let ready = IMEService.shared.epoch != nil && targetError == nil && !recovering
        needsIdleTargetRefresh = !ready
        let capabilities: [String: Any] = ["marked_text": true, "context_complete": complete,
                                           "document_access": access,
                                           "document_verified": session?.documentVerified ?? (complete && access),
                                           "committed_range_verified": session?.committedRangeVerified ?? false,
                                           "range_replacement": session?.replacementVerified == true ? "verified" : (access && complete && (session?.documentVerified ?? true) ? "advertised" : "unavailable"),
                                           "selection_restore": false]
        let application = adapter.client.bundleIdentifier() ?? ""
        guard activated, ownsActivation, currentClientID == stateClientID, self.adapter === adapter else { return }
        var state: [String: Any] = ["type": "state", "ready": ready,
                                    "client_id": currentClientID, "utterance_id": rejectedBeginID ?? session?.utteranceID ?? failedUtteranceID ?? "",
                                    "phase": session?.phase.rawValue ?? (failedUtteranceID == nil ? "idle" : "error"),
                                    "application": application,
                                    "revision": session?.revision ?? 0, "edit_requested": session?.editRequested ?? false,
                                    "raw": session?.raw ?? "", "error": session?.error ?? (targetError ?? lastError),
                                    "lifecycle_event": lastLifecycleEvent, "has_composition": session?.hasComposition ?? false,
                                    "awaiting_readback": session?.awaitingReadback ?? false, "readback_stage": session?.readbackStage ?? "",
                                    "readback_diagnostics": session?.readbackDiagnostics ?? [:],
                                    "client_read_diagnostics": adapter.lastReadDiagnostics,
                                    "capabilities": capabilities, "can_convert": session?.canEdit ?? false,
                                    "can_cancel": (session?.canCancel ?? false) || (session?.phase == .undone && undoHistory.count > 0),
                                    "undo_history_depth": undoHistory.count,
                                    "multi_undo_enabled": undoHistory.enabled,
                                    "edit_context_available": session?.editContextAvailable ?? false,
                                    "edit_scope": session?.editScope ?? "unavailable",
                                    "context_complete": complete]
        // A sentence snapshot is stable. Fresh idle snapshots only answer explicit refreshes.
        if includeSnapshot {
            state["original"] = context?.text ?? ""
            let selection = context?.selection ?? unspecifiedRange
            state["selection"] = isValidRange(selection) ? [selection.location, selection.length] : [-1, -1]
        }
        if let requestID { state["request_id"] = requestID }
        IMEService.shared.send(state)
    }

    private func changed(includeSnapshot: Bool = false) {
        guard activated, ownsActivation, let session else { return }
        if !preservingHistory, [.error, .interrupted].contains(session.phase) { undoHistory.clear() }
        sendState(includeSnapshot: includeSnapshot || session.phase == .error)
        guard activated, ownsActivation, self.session === session else { return }
        panel.update(session, historyDepth: undoHistory.count)
        tick()
    }

    private var selectionPolls = 0
    private var needsIdleTargetRefresh = false
    private func tick() {
        guard activated, ownsActivation, let adapter else { return }
        let clientID = currentClientID
        if !isWriting, adapter.clientOperationPending {
            isWriting = true
            adapter.advanceClientOperation()
            isWriting = false
        }
        guard activated, ownsActivation, currentClientID == clientID, self.adapter === adapter else { return }
        selectionPolls += 1
        if !isWriting, needsIdleTargetRefresh, selectionPolls % 15 == 0,
           session?.hasComposition != true, session?.awaitingReadback != true {
            // Retry an unavailable selection without reading or modifying
            // composition left by another input source.
            if adapter.startingSelection() != lastContext?.selection {
                sendState(includeSnapshot: true)
            }
        }
        guard let session else { return }
        if !isWriting, session.awaitingReadback || (session.phase == .error && session.hasComposition && selectionPolls % 15 == 0) {
            isWriting = true
            session.advanceReadback()
            isWriting = false
        }
        if !isWriting, session.active, selectionPolls % 3 == 0 {
            isWriting = true
            session.checkSelection()
            isWriting = false
        }
        guard session.active || (session.phase == .undone && undoHistory.count > 0) else { return }
        let trace = CaretDiagnostics.shared.shouldSample()
        let length = session.hasComposition ? (session.raw as NSString).length : 0
        if panel.needsPosition {
            panel.position(caret: adapter.caret(compositionLength: length, trace: trace), clientLevel: adapter.client.windowLevel())
        }
        if trace {
            CaretDiagnostics.shared.record(["application": adapter.client.bundleIdentifier() ?? "",
                "phase": session.phase.rawValue, "characters": length, "edit_requested": session.editRequested,
                "geometry": adapter.caretDiagnostics, "panel": panel.positioningDiagnostics])
        }
    }
}
