import Foundation

/// A client can synchronously deliver a lifecycle callback while a native
/// mark/commit call is on the stack. These tests never create an actual client.
final class ReentrantClient: CompositionClient {
    var body = "原文"
    var selected = NSRange(location: 2, length: 0)
    var marked = unspecifiedRange
    var marks = 0
    var commits = 0
    var replacements = 0
    var delayMark = false
    var delayCommit = false
    var probeOverride: CompositionProbe?
    var snapshotOverride: EditorSnapshot?
    var afterMark: (() -> Void)?
    var afterCommit: (() -> Void)?
    var afterReplace: (() -> Void)?
    var afterSnapshot: (() -> Void)?
    var afterProbe: (() -> Void)?

    func snapshot() throws -> EditorSnapshot {
        let value = snapshotOverride ?? EditorSnapshot(text: body, selection: selected, markedRange: marked,
                       selectedText: (body as NSString).substring(with: selected), documentAccess: true)
        let callback = afterSnapshot
        afterSnapshot = nil
        callback?()
        return value
    }
    func selection() -> NSRange { selected }
    func probe() -> CompositionProbe {
        let value = probeOverride ?? CompositionProbe(selection: selected, markedRange: marked,
                         markedText: isValidRange(marked) ? (body as NSString).substring(with: marked) : nil)
        let callback = afterProbe
        afterProbe = nil
        callback?()
        return value
    }
    func mark(_ text: String) {
        if delayMark { holdReads() }
        marks += 1
        let range = isValidRange(marked) ? marked : selected
        body = (body as NSString).replacingCharacters(in: range, with: text)
        marked = NSRange(location: range.location, length: (text as NSString).length)
        selected = NSRange(location: NSMaxRange(marked), length: 0)
        let callback = afterMark
        afterMark = nil
        callback?()
    }
    func commit(_ text: String) {
        if delayCommit { holdReads() }
        commits += 1
        let range = isValidRange(marked) ? marked : selected
        body = (body as NSString).replacingCharacters(in: range, with: text)
        selected = NSRange(location: range.location + (text as NSString).length, length: 0)
        marked = unspecifiedRange
        let callback = afterCommit
        afterCommit = nil
        callback?()
    }
    func replace(_ range: NSRange, with text: String) {
        replacements += 1
        body = (body as NSString).replacingCharacters(in: range, with: text)
        selected = NSRange(location: range.location + (text as NSString).length, length: 0)
        marked = unspecifiedRange
        let callback = afterReplace
        afterReplace = nil
        callback?()
    }
    func holdReads() {
        snapshotOverride = try! snapshot()
        probeOverride = probe()
    }
    func publishReads() {
        snapshotOverride = nil
        probeOverride = nil
    }
}

var failures = 0
func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { failures += 1; print("FAIL \(message)") }
}

do {
    let client = ReentrantClient()
    let session = try CompositionSession(client: client, utteranceID: "mark-reentry", sequence: 1)
    var settled = 0
    session.onSettled = { _, _ in settled += 1 }
    client.afterMark = { session.interrupt("输入法已切换", commitIfOwned: false) }
    session.update("正在听写", final: true)
    require(session.phase == .interrupted, "mark reentry must not revive an interrupted transaction")
    require(client.marks == 1 && client.commits == 0, "mark reentry must not commit after a lifecycle takeover")
    require(settled == 0, "mark reentry must not emit a successful final after interruption")
    session.update("迟到最终句", final: true)
    require(client.marks == 1 && client.commits == 0, "late final must remain rejected after mark reentry")
}

do {
    let client = ReentrantClient()
    let session = try CompositionSession(client: client, utteranceID: "commit-reentry", sequence: 1)
    var settled = 0
    session.onSettled = { _, _ in settled += 1 }
    session.update("正在听写", final: false)
    client.afterCommit = { session.interrupt("输入法已切换", commitIfOwned: false) }
    session.update("正在听写", final: true)
    require(session.phase == .interrupted, "commit reentry must not revive an interrupted transaction")
    require(client.marks == 1 && client.commits == 1, "commit reentry must not issue a second commit")
    require(settled == 0, "commit reentry must not emit a successful final after interruption")
}

do {
    let client = ReentrantClient()
    let session = try CompositionSession(client: client, utteranceID: "replace-reentry", sequence: 1)
    session.update("改写原文", final: false)
    session.convert()
    session.update("改写原文", final: true)
    var settled = 0
    session.onSettled = { _, _ in settled += 1 }
    let revision = session.revision
    client.afterReplace = { session.interrupt("输入法已切换", commitIfOwned: false) }
    session.applyEdit(text: "改写结果", revision: revision, error: nil)
    session.applyEdit(text: "迟到结果", revision: revision, error: nil)
    require(session.phase == .interrupted && settled == 0, "replace reentry must not restore edited phase after interruption")
    require(client.replacements == 1 && client.body == "改写结果", "late model callback rewrote text after switch-out")
}

do {
    for fromProbe in [false, true] {
        let client = ReentrantClient()
        let session = try CompositionSession(client: client, utteranceID: "read-reentry", sequence: 1)
        let interrupt = { session.interrupt("读取时输入法已切换", commitIfOwned: false) }
        if fromProbe { client.afterProbe = interrupt }
        else { client.afterSnapshot = interrupt }
        session.update("不能写入", final: !fromProbe)
        require(session.phase == .interrupted && !session.awaitingReadback, "read reentry revived a terminated readback")
        require(client.marks == 0 && client.commits == 0, "preflight read reentry allowed a new native write")
    }
}

do {
    let client = ReentrantClient()
    client.delayMark = true
    let session = try CompositionSession(client: client, utteranceID: "pending-owned-mark", sequence: 1)
    session.update("保留本句", final: false)
    require(session.awaitingReadback, "test did not enter pending mark ACK")
    client.publishReads()
    session.interrupt("切换到其他输入法")
    session.advanceReadback()
    require(session.phase == .interrupted && !session.awaitingReadback, "switch-out retained pending mark work")
    require(client.commits == 1 && client.body == "原文保留本句" && !isValidRange(client.marked), "fresh exact mark proof did not finish the owned composition once")
    _ = try CompositionSession(client: client, utteranceID: "next-after-switch", sequence: 1)
}

do {
    for takeover in ["text", "range", "selection", "unavailable"] {
        let client = ReentrantClient()
        client.delayMark = true
        let session = try CompositionSession(client: client, utteranceID: "pending-foreign-mark", sequence: 1)
        session.update("保留本句", final: false)
        client.publishReads()
        client.probeOverride = CompositionProbe(
            selection: takeover == "selection" ? NSRange(location: 0, length: 0) : client.selected,
            markedRange: takeover == "range" ? NSRange(location: 0, length: 4) : client.marked,
            markedText: takeover == "text" ? "别人文字" : (takeover == "unavailable" ? nil : "保留本句"))
        session.interrupt("切换到其他输入法")
        client.publishReads()
        session.advanceReadback()
        require(client.commits == 0 && client.marks == 1, "pending switch-out committed a \(takeover) takeover")
    }
}

do {
    let client = ReentrantClient()
    client.delayCommit = true
    let session = try CompositionSession(client: client, utteranceID: "pending-commit", sequence: 1)
    session.update("只提交一次", final: true)
    require(session.readbackStage == "commit" && client.commits == 1, "test did not enter pending commit ACK")
    session.interrupt("切换到其他输入法")
    client.publishReads()
    session.advanceReadback()
    require(session.phase == .interrupted && client.commits == 1 && client.body == "原文只提交一次", "switch-out replayed a previously issued commit")
}

do {
    let client = ReentrantClient()
    client.delayMark = true
    let session = try CompositionSession(client: client, utteranceID: "pending-read-reentry", sequence: 1)
    session.update("正在等待确认", final: true)
    client.publishReads()
    client.afterProbe = { session.interrupt("确认时输入法已切换", commitIfOwned: false) }
    session.advanceReadback()
    require(session.phase == .interrupted && !session.awaitingReadback, "ACK read reentry reinstalled a canceled pending operation")
    require(client.marks == 1 && client.commits == 0, "ACK read reentry continued into final commit")
}

if failures > 0 {
    print("\(failures) lifecycle reentry assertions failed")
    exit(1)
}
print("8 native lifecycle reentry tests passed")
