// Execute the production controller against reentrant, local IMK endpoints.
// Only the OS controller base and panel are replaced; no application is touched.
import AppKit
import InputMethodKit
import Carbon

class IMKInputController: NSObject {
    static var onActivate: (() -> Void)?
    func activateServer(_ sender: Any!) { let callback = Self.onActivate; Self.onActivate = nil; callback?() }
    func deactivateServer(_ sender: Any!) {}
    func inputControllerWillClose() {}
    func hidePalettes() {}
    func mark(forStyle style: Int, at range: NSRange) -> [AnyHashable: Any]! {
        [NSAttributedString.Key.underlineStyle: NSUnderlineStyle.double.rawValue]
    }
    func recognizedEvents(_ sender: Any!) -> Int { 0 }
    func client() -> IMKTextInput? { nil }
}
final class ActionPanel {
    var onCancel: (() -> Void)?
    var onConvert: (() -> Void)?
    var needsPosition: Bool { false }
    var positioningDiagnostics: [String: Any] { [:] }
    func hide() {}
    func update(_ session: CompositionSession, historyDepth: Int) {}
    func position(caret: NSRect?, clientLevel: Int32) {}
}
final class LocalClient: NSObject, IMKTextInput {
    var onBundle: (() -> Void)?
    var onRead: (() -> Void)?
    var onInsert: (() -> Void)?
    var reads = 0
    var writes = 0
    var markedReads = 0
    var text = ""
    var selection = NSRange(location: 0, length: 0)
    var marked = unspecifiedRange
    func bundleIdentifier() -> String! { let f = onBundle; onBundle = nil; f?(); return "test.activation" }
    func selectedRange() -> NSRange { reads += 1; let f = onRead; onRead = nil; f?(); return selection }
    func markedRange() -> NSRange { markedReads += 1; return marked }
    func length() -> Int { (text as NSString).length }
    func string(from range: NSRange, actualRange: NSRangePointer!) -> String! {
        guard range.length > 0, isValidRange(range, length: (text as NSString).length) else {
            actualRange?.pointee = NSRange(location: 0, length: 0); return nil
        }
        actualRange?.pointee = range
        return (text as NSString).substring(with: range)
    }
    func insertText(_ string: Any!, replacementRange range: NSRange) {
        writes += 1
        text = string as! String
        selection = NSRange(location: (text as NSString).length, length: 0)
        marked = unspecifiedRange
        let f = onInsert; onInsert = nil; f?()
    }
    func setMarkedText(_ string: Any!, selectionRange: NSRange, replacementRange: NSRange) {
        writes += 1
        text = (string as? NSAttributedString)?.string ?? (string as! String)
        marked = text.isEmpty ? unspecifiedRange : NSRange(location: 0, length: (text as NSString).length)
        selection = NSRange(location: (text as NSString).length, length: 0)
    }
    func attributedSubstring(from range: NSRange) -> NSAttributedString! { nil }
    func characterIndex(for point: NSPoint, tracking mode: IMKLocationToOffsetMappingMode, inMarkedRange: UnsafeMutablePointer<ObjCBool>!) -> Int { NSNotFound }
    func attributes(forCharacterIndex index: Int, lineHeightRectangle: UnsafeMutablePointer<NSRect>!) -> [AnyHashable: Any]! { [:] }
    func validAttributesForMarkedText() -> [Any]! { [] }
    func overrideKeyboard(withKeyboardNamed name: String!) { fatalError("must not switch input sources") }
    func selectMode(_ identifier: String!) { fatalError("must not switch input sources") }
    func supportsUnicode() -> Bool { true }
    func windowLevel() -> Int32 { 0 }
    func supportsProperty(_ property: TSMDocumentPropertyTag) -> Bool { true }
    func uniqueClientIdentifierString() -> String! { "local-activation" }
    func firstRect(forCharacterRange range: NSRange, actualRange: NSRangePointer!) -> NSRect { .zero }
}
func check(_ condition: @autoclosure () -> Bool, _ message: String) { if !condition() { fputs("FAIL: \(message)\n", stderr); exit(1) } }
func currentClientID(_ controller: ProxiMicInputController) -> String {
    Mirror(reflecting: controller).children.first { $0.label == "currentClientID" }!.value as! String
}
func drainActivation() {
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.10))
}
func firstSentence(_ controller: ProxiMicInputController, _ client: LocalClient) {
    let id = currentClientID(controller)
    controller.receive(["type": "begin", "client_id": id, "utterance_id": "first", "seq": 1])
    controller.receive(["type": "update", "client_id": id, "utterance_id": "first", "seq": 2, "text": "第一", "final": false])
    controller.receive(["type": "update", "client_id": id, "utterance_id": "first", "seq": 3, "text": "第一句话", "final": true])
    check(client.text == "第一句话" && !isValidRange(client.marked), "first Tap did not stream and commit")
}

for point in ["super", "bundle", "configure", "snapshot"] {
    let old = ProxiMicInputController(), newest = ProxiMicInputController()
    let oldClient = LocalClient(), newClient = LocalClient()
    let reenter = { newest.activateServer(newClient) }
    switch point {
    case "super": IMKInputController.onActivate = reenter
    case "bundle": oldClient.onBundle = reenter
    case "configure": oldClient.onBundle = { oldClient.onBundle = reenter }
    default: oldClient.onRead = reenter
    }
    old.activateServer(oldClient)
    drainActivation()
    check(IMEService.shared.activeController === newest, "stale \(point) activation replaced the newest target")
    old.deactivateServer(oldClient)
    check(IMEService.shared.activeController === newest, "old deactivation cleared new target")
    firstSentence(newest, newClient)
    check(oldClient.writes == 0, "first Tap wrote into the old window")
    newest.deactivateServer(newClient)
    print("PASS reentrant activation at \(point)")
}

// An IMK controller may be reused while its own previous activation is in flight.
do {
    let controller = ProxiMicInputController()
    let oldClient = LocalClient(), newClient = LocalClient()
    oldClient.onBundle = { controller.activateServer(newClient) }
    controller.activateServer(oldClient)
    drainActivation()
    firstSentence(controller, newClient)
    check(oldClient.writes == 0, "same-controller reentry restored an old adapter")
    controller.deactivateServer(newClient)
    print("PASS same-controller reactivation")
}

// Native composition cleanup can itself activate a different target.
do {
    let old = ProxiMicInputController(), newest = ProxiMicInputController()
    let oldClient = LocalClient(), newClient = LocalClient()
    old.activateServer(oldClient)
    drainActivation()
    let id = currentClientID(old)
    old.receive(["type": "begin", "client_id": id, "utterance_id": "cleanup", "seq": 1])
    old.receive(["type": "update", "client_id": id, "utterance_id": "cleanup", "seq": 2, "text": "保留", "final": false])
    oldClient.onInsert = { newest.activateServer(newClient) }
    old.deactivateServer(oldClient)
    check(IMEService.shared.activeController === newest, "cleanup cleared a nested activation")
    drainActivation()
    firstSentence(newest, newClient)
    newest.deactivateServer(newClient)
    print("PASS reactivation during composition cleanup")
}
// A transient activation must never read/write a field before AppKit returns.
do {
    let controller = ProxiMicInputController(), client = LocalClient()
    controller.activateServer(client)
    check(client.reads == 0, "activation queried the editor synchronously")
    check(!controller.isUnboundIdleClient, "source recovery interrupted an unfinished activation")
    controller.receive(["type": "begin", "client_id": currentClientID(controller), "utterance_id": "early", "seq": 1])
    check(client.reads == 0 && client.writes == 0, "BEGIN entered an unfinished activation")
    controller.deactivateServer(client)
    drainActivation()
    check(client.reads == 0 && client.writes == 0, "deferred callback resurrected a retired client")
    print("PASS transient activation never queries or writes")
}
// A host ping can arrive while a synchronous IMK read pumps the run loop.
do {
    let controller = ProxiMicInputController(), client = LocalClient()
    controller.activateServer(client)
    drainActivation()
    client.onRead = {
        let reads = client.reads
        controller.sendState(includeSnapshot: true, requestID: "nested-ping")
        check(client.reads == reads, "nested state ping recursively read the editor")
    }
    controller.sendState(includeSnapshot: true, requestID: "outer-ping")
    firstSentence(controller, client)
    controller.deactivateServer(client)
    print("PASS reentrant state reads coalesced")
}
print("PASS 8 activation lifecycle regressions")


// Exercise the production recovery policy with a simulated OS; never switch
// the machine's real input source from a test.
for scenario in ["normal", "busy", "other_source", "target_changed", "source_changed", "failed"] {
    let recovery = InputSourceRecovery()
    var selected = "voice", target = true, operations: [String] = []
    if scenario == "other_source" { selected = "pinyin" }
    var result = ""
    var scheduled: (() -> Void)?
    recovery.recover(request: "tap", now: 10,
        eligible: { target && scenario != "busy" }, selected: { selected == "voice" },
        leave: {
            operations.append("leave")
            if scenario == "failed" { return false }
            selected = "ascii"
            if scenario == "target_changed" { target = false }
            if scenario == "source_changed" { selected = "pinyin" }
            return true
        }, mayReturn: { target && selected == "ascii" },
        restore: { operations.append("return"); selected = "voice"; return true },
        schedule: { scheduled = $0 }, completion: { result = $0 })
    check(!operations.contains("return"), "source returned before deactivation could drain")
    scheduled?()
    if scenario == "normal" {
        check(result == "refreshed" && operations == ["leave", "return"], "missing client was not recovered")
        var duplicate = ""
        recovery.recover(request: "tap", now: 11, eligible: { true }, selected: { true },
            leave: { fatalError("repeated recovery") }, mayReturn: { true }, restore: { true },
            schedule: { $0() }, completion: { duplicate = $0 })
        check(duplicate == "duplicate", "duplicate recovery ran twice")
        var cooldown = ""
        recovery.recover(request: "tap2", now: 11, eligible: { true }, selected: { true },
            leave: { fatalError("recovery loop") }, mayReturn: { true }, restore: { true },
            schedule: { $0() }, completion: { cooldown = $0 })
        check(cooldown == "cooldown", "recovery was not rate limited")
    } else {
        check(!operations.contains("return"), "recovery overwrote a changed target/source")
        if scenario == "busy" || scenario == "other_source" { check(operations.isEmpty, "recovery touched an active sentence or another source") }
    }
    print("PASS input-source recovery \(scenario)")
}

for scenario in ["cancelled", "source_changed", "target_changed"] {
    let recovery = InputSourceRecovery()
    var allowed = true
    var selected = "voice"
    var callback: (() -> Void)?
    var result = ""
    recovery.recover(request: scenario, now: 10, eligible: { true }, selected: { true },
        leave: { selected = "ascii"; return true }, mayReturn: { allowed && selected == "ascii" },
        restore: { fatalError("restored over a newer action") }, schedule: { callback = $0 }, completion: { result = $0 })
    check(callback != nil && result.isEmpty, "recovery did not yield asynchronously")
    if scenario == "source_changed" { selected = "pinyin" } else { allowed = false }
    callback?()
    check(result == "changed", "delayed recovery ignored newer user action")
    print("PASS asynchronous recovery fences \(scenario)")
}


// Old sender callbacks on a reused controller cannot retire the newer target.
do {
    let controller = ProxiMicInputController()
    let old = LocalClient(), fresh = LocalClient()
    controller.activateServer(old); drainActivation()
    controller.activateServer(fresh); drainActivation()
    controller.deactivateServer(old)
    check(IMEService.shared.activeController === controller, "retired sender ended current activation")
    firstSentence(controller, fresh)
    check(old.writes == 0, "retired sender received new dictation")
    controller.deactivateServer(fresh)
    check(IMEService.shared.activeController == nil, "real deactivation was ignored")
    print("PASS reused controller discards old sender but accepts current deactivation")
}
do {
    let controller = ProxiMicInputController(), client = LocalClient()
    client.marked = NSRange(location: 0, length: 99)
    controller.activateServer(client); drainActivation()
    controller.sendState(includeSnapshot: true, requestID: "startup")
    controller.receive(["type": "begin", "client_id": currentClientID(controller), "utterance_id": "no-old-mark", "seq": 1])
    check(client.markedReads == 0, "activation/BEGIN queried old composition")
    check(client.reads > 0 && client.writes == 0, "selection not retained or old text rewritten")
    controller.deactivateServer(client)
    print("PASS activation/BEGIN reads selection but never old composition")
}
do {
    let controller = ProxiMicInputController(), client = LocalClient()
    client.onRead = { controller.deactivateServer(client) }
    controller.activateServer(client); drainActivation()
    check(client.markedReads == 0, "retired snapshot continued querying remote client")
    check(IMEService.shared.activeController == nil, "retired read resurrected its target")
    print("PASS retired remote snapshot stops immediately")
}

// Delayed switching callbacks must retire preparation before any editor read.
do {
    let controller = ProxiMicInputController(), old = LocalClient(), fresh = LocalClient()
    controller.activateServer(old)
    Timer.scheduledTimer(withTimeInterval: 0.005, repeats: false) { _ in
        controller.sendState(includeSnapshot: true, requestID: "early-ping")
        check(old.reads == 0, "ping bypassed activation settling")
        controller.deactivateServer(old)
        controller.activateServer(fresh)
    }
    drainActivation()
    check(old.reads == 0 && old.writes == 0, "retired preparation touched old client")
    firstSentence(controller, fresh)
    controller.deactivateServer(fresh)
    print("PASS queued deactivate retires preparation before first remote read")
}
