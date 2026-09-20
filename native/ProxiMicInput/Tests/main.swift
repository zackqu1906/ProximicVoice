import Foundation

final class FakeClient: CompositionClient {
    var body: String
    var selected: NSRange
    var marked: NSRange = unspecifiedRange
    var fullContext = true
    var omitWholeWhileMarked = false
    var access = true
    var rangeReads = false
    var contextRange: NSRange?
    var ignoreReplacement = false
    var snapshotCount = 0
    var markCount = 0
    var commitCount = 0
    var replaceCount = 0
    var now: TimeInterval = 0
    var delayMark = false
    var delayCommit = false
    var delayReplace = false
    var selectEntireMarkedRange = false
    var anchorMarkedSelection = false
    var emptyMarkClearsComposition = false
    var emptyMarkUsesZeroRange = false
    var snapshotOverride: EditorSnapshot?
    var probeOverride: CompositionProbe?
    init(_ body: String = "", selection: NSRange? = nil) {
        self.body = body
        self.selected = selection ?? NSRange(location: (body as NSString).length, length: 0)
    }
    func snapshot() throws -> EditorSnapshot {
        snapshotCount += 1
        if let snapshotOverride { return snapshotOverride }
        return EditorSnapshot(text: fullContext && !(omitWholeWhileMarked && isValidRange(marked)) ? body : nil, selection: selected, markedRange: marked,
                              selectedText: isValidRange(selected, length: (body as NSString).length) ? (body as NSString).substring(with: selected) : nil, documentAccess: access,
                              readableContext: contextRange.flatMap { range in
                                  isValidRange(range, length: (body as NSString).length) ? EditorTextRange(text: (body as NSString).substring(with: range), range: range) : nil
                              })
    }
    func selection() -> NSRange { probeOverride?.selection ?? selected }
    func readText(in range: NSRange) -> String? {
        let text: String?
        if rangeReads { text = body }
        else { text = try? snapshot().text }
        guard let text, isValidRange(range, length: (text as NSString).length) else { return nil }
        return (text as NSString).substring(with: range)
    }
    func probe() -> CompositionProbe {
        probeOverride ?? CompositionProbe(selection: selected, markedRange: marked,
                         markedText: isValidRange(marked, length: (body as NSString).length) && !(emptyMarkClearsComposition && marked.length == 0) ? (body as NSString).substring(with: marked) : nil)
    }
    func holdReads() {
        snapshotOverride = try! snapshot()
        probeOverride = probe()
    }
    func publishReads() {
        snapshotOverride = nil
        probeOverride = nil
    }
    func mark(_ text: String) {
        if delayMark { holdReads() }
        markCount += 1
        let range = isValidRange(marked) ? marked : selected
        body = (body as NSString).replacingCharacters(in: range, with: text)
        marked = NSRange(location: range.location, length: (text as NSString).length)
        selected = NSRange(location: NSMaxRange(marked), length: 0)
        if selectEntireMarkedRange { selected = marked }
        if anchorMarkedSelection { selected = NSRange(location: marked.location, length: 0) }
        // Qt can clear its preedit buffer for an empty update. There is then
        // no attributed substring to read, rather than a readable empty mark.
        if text.isEmpty && emptyMarkClearsComposition {
            marked = emptyMarkUsesZeroRange ? NSRange(location: range.location, length: 0) : unspecifiedRange
        }
    }
    func commit(_ text: String) {
        if delayCommit { holdReads() }
        commitCount += 1
        let range = isValidRange(marked) ? marked : selected
        body = (body as NSString).replacingCharacters(in: range, with: text)
        selected = NSRange(location: range.location + (text as NSString).length, length: 0)
        marked = unspecifiedRange
    }
    func replace(_ range: NSRange, with text: String) {
        if delayReplace { holdReads() }
        replaceCount += 1
        let effective = ignoreReplacement ? selected : range
        body = (body as NSString).replacingCharacters(in: effective, with: text)
        selected = NSRange(location: effective.location + (text as NSString).length, length: 0)
        marked = unspecifiedRange
    }
}

var passed = 0
func check(_ condition: @autoclosure () -> Bool, _ message: String, file: StaticString = #file, line: UInt = #line) {
    if !condition() { fatalError("\(message)", file: file, line: line) }
}
func test(_ name: String, _ work: () throws -> Void) {
    do { try work(); passed += 1; print("PASS \(name)") }
    catch { fatalError("\(name): \(error)") }
}
func begin(_ client: FakeClient) throws -> CompositionSession {
    try CompositionSession(client: client, utteranceID: UUID().uuidString, sequence: 1, clock: { client.now })
}
func expireReadback(_ session: CompositionSession, _ client: FakeClient) {
    client.now += 0.501
    session.advanceReadback()
}

test("Chinese partial revisions replace one native marked range and commit exactly once") {
    let client = FakeClient()
    let session = try begin(client)
    for text in ["今天", "今天下雨", "今天下午"] { session.update(text, final: false) }
    check(client.body == "今天下午", "partial duplicated")
    check(client.marked.length == 4, "missing native composition")
    check(client.snapshotCount == 1, "partial must not reread entire document")
    session.update("今天下午三点开会。", final: true)
    check(client.body == "今天下午三点开会。", "final mismatch")
    check(!isValidRange(client.marked), "final must remove marked state")
    let writes = client.commitCount
    session.update("late duplicate", final: true)
    check(client.commitCount == writes && client.body == "今天下午三点开会。", "duplicate final wrote")
}

test("UTF16 middle insertion preserves emoji and surrounding lines") {
    let client = FakeClient("👨‍👩‍👧‍👦 A\n尾巴", selection: NSRange(location: 12, length: 1))
    let before = client.body
    let session = try begin(client)
    session.update("中文😀", final: true)
    check(client.body == "👨‍👩‍👧‍👦 中文😀\n尾巴", "UTF16 replacement incorrect")
    session.cancel()
    check(client.body == before && session.phase == .undone, "selection original not restored")
    check(client.selected == NSRange(location: 13, length: 0), "caret should follow restored selection")
}

test("Speaking undo restores selected original and stops audio") {
    let client = FakeClient("before OLD after", selection: NSRange(location: 7, length: 3))
    let session = try begin(client)
    var ended = 0
    session.onFinishAudio = { ended += 1 }
    session.update("新", final: false)
    session.cancel()
    check(client.body == "before OLD after", "original selection lost")
    check(session.phase == .undone && ended == 1, "undo did not stop audio")
    session.update("late", final: true)
    check(client.body == "before OLD after", "late ASR wrote after undo")
}

test("Empty final after partial still permits restoring replaced selection") {
    let client = FakeClient("ABC", selection: NSRange(location: 1, length: 1))
    let session = try begin(client)
    session.update("middle", final: false)
    session.update("", final: true)
    check(client.body == "AC", "empty final not applied")
    session.cancel()
    check(client.body == "ABC", "empty final undo lost original")
}

test("No-text final never erases a selected original") {
    let client = FakeClient("ABC", selection: NSRange(location: 0, length: 3))
    let session = try begin(client)
    session.update("", final: true)
    check(client.body == "ABC" && client.commitCount == 0, "empty utterance erased selection")
}

test("Convert while speaking ends audio, waits final and passes original separately") {
    let client = FakeClient("明天三点开会。")
    let session = try begin(client)
    var ended = 0
    var edits: [(String, String, Int)] = []
    session.onFinishAudio = { ended += 1 }
    session.onEdit = { edits.append(($0, $1, $2)) }
    session.update("改成", final: false)
    session.convert()
    check(ended == 1 && edits.isEmpty && session.phase == .finishing, "converted too soon")
    session.update("改成四点。", final: true)
    check(edits.count == 1 && edits[0].0 == "明天三点开会。" && edits[0].1 == "改成四点。", "context includes dictated instruction")
    check(client.body == "明天三点开会。改成四点。" && isValidRange(client.marked), "instruction must remain visible until model result")
    session.applyEdit(text: "明天四点开会。", revision: edits[0].2, error: nil)
    check(client.body == "明天四点开会。" && session.phase == .edited, "edit not applied directly")
}

test("Cancel conversion while final pending becomes ordinary dictation") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var edits = 0
    session.onEdit = { _, _, _ in edits += 1 }
    session.update("指令", final: false)
    session.convert()
    session.cancel()
    check(session.phase == .finishing && !session.editRequested, "cancel must still wait final")
    session.update("完整句子", final: true)
    check(edits == 0 && session.phase == .dictated && client.body == "原文完整句子", "cancelled conversion called LLM")
}

test("Cancel pending model invalidates late revision") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    let revision = session.revision
    session.cancel()
    session.applyEdit(text: "迟到结果", revision: revision, error: nil)
    check(client.body == "原文指令" && session.phase == .dictated, "cancelled model wrote")
}

test("Applied edit cancel restores dictation, second cancel restores original") {
    let client = FakeClient("原来尾巴", selection: NSRange(location: 2, length: 0))
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    session.applyEdit(text: "编辑结果", revision: session.revision, error: nil)
    session.cancel()
    check(client.body == "原来指令尾巴" && session.phase == .dictated, "cancel didn't restore D")
    session.cancel()
    check(client.body == "原来尾巴" && session.phase == .undone, "second cancel didn't restore B")
}

test("Failed model restores dictated content and rejects postfinal relabeling") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    let old = session.revision
    session.applyEdit(text: "", revision: old, error: "网络断开")
    check(client.body == "原文指令" && session.phase == .dictated, "error erased dictation")
    session.convert()
    check(session.phase == .dictated && session.revision > old && !session.canEdit, "committed dictation became an edit")
}

test("ASR failure preserves partial as reversible dictation") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("已经识别", final: false)
    session.convert()
    session.failASR("识别断开")
    check(client.body == "原文已经识别" && session.phase == .dictated && !session.editRequested, "ASR failure erased partial")
    session.cancel()
    check(client.body == "原文", "ASR failure dictation not reversible")
}

test("Manual caret takeover stops late writes without moving it back") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("部分", final: false)
    client.selected = NSRange(location: 0, length: 0)
    session.checkSelection()
    let writes = client.commitCount
    session.update("late", final: true)
    check(session.phase == .interrupted && client.commitCount == writes && client.selected.location == 0, "takeover overwritten")
}

test("Manual text changes prevent applying or undoing committed snapshots") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    client.body = "用户后来修改"
    session.applyEdit(text: "过期结果", revision: session.revision, error: nil)
    expireReadback(session, client)
    check(client.body == "用户后来修改" && client.replaceCount == 0 && session.phase == .error, "stale snapshot overwrote user")
}

test("Final preserves changes outside composition and disables whole-document operations") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("部分", final: false)
    client.body = "改文部分"
    session.update("最终结果", final: true)
    check(client.body == "改文最终结果" && session.phase == .dictated, "local final overwrote surrounding user changes")
    check(!session.documentVerified && !session.canEdit && session.canCancel, "local undo should not grant whole-document edit powers")
    session.convert()
    session.cancel()
    check(client.body == "改文" && client.replaceCount == 1 && session.phase == .undone, "local undo lost outside changes")
}

test("Local composition readback rejects client-modified marked text") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("部分", final: false)
    client.body = "原文不同"
    session.update("新部分", final: false)
    check(client.body == "原文不同" && session.phase == .error, "partial overwrote changed composition")
}

test("Unsupported complete context allows basic composition but no full edit") {
    let client = FakeClient("old")
    client.fullContext = false
    client.access = false
    let session = try begin(client)
    session.update("new", final: false)
    session.convert()
    session.update("new", final: true)
    check(session.phase == .dictated && !session.canEdit && client.body == "oldnew", "unavailable context must retain dictation")
    check(!session.error.isEmpty, "unavailable conversion was silent")
    // A separate still-marked sentence remains cancellable without context.
    let live = try begin(client)
    live.update("temporary", final: false)
    live.cancel()
    check(client.body == "oldnew", "native composition undo should still work")
}

test("Client that ignores replacement fails closed after one write") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    client.ignoreReplacement = true
    session.applyEdit(text: "结果", revision: session.revision, error: nil)
    expireReadback(session, client)
    check(session.phase == .error && client.replaceCount == 1 && !session.replacementVerified, "dishonest client not detected")
    session.cancel()
    check(client.replaceCount == 1, "failed verification must not retry destructively")
}

test("Lifecycle interruption commits visible partial and rejects later model or ASR") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("可见部分", final: false)
    session.interrupt("切换输入法")
    check(client.body == "原文可见部分" && !isValidRange(client.marked), "visible partial not preserved")
    session.update("迟到", final: true)
    check(client.body == "原文可见部分", "late ASR crossed session")
}

test("Client composition flush before first ASR text does not cancel listening") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var ended = 0
    var interrupted = 0
    session.onFinishAudio = { ended += 1 }
    session.onInterrupted = { _ in interrupted += 1 }
    check(!session.commitCompositionRequested(), "empty composition flush claimed audio")
    check(session.phase == .listening && ended == 0 && interrupted == 0, "empty flush stopped listening")
    check(client.body == "原文" && client.commitCount == 0, "empty flush wrote the input field")
    session.update("听写", final: false)
    check(client.body == "原文听写", "ASR could not continue after empty flush")
    check(session.commitCompositionRequested(), "marked composition flush was ignored")
    check(session.phase == .interrupted && ended == 1 && interrupted == 1, "marked flush did not end the sentence")
    check(!isValidRange(client.marked) && client.commitCount == 1, "marked text was not committed exactly once")
    session.update("迟到", final: true)
    check(client.body == "原文听写", "late result wrote after client flush")
}

test("Lifecycle deactivation still cancels before any ASR text") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var ended = 0
    session.onFinishAudio = { ended += 1 }
    session.interrupt("已切换输入法或输入框")
    check(session.phase == .interrupted && ended == 1, "empty sentence survived deactivation")
    session.update("迟到", final: true)
    check(client.body == "原文" && client.markCount == 0, "deactivated sentence wrote to old client")
}

test("Native sequence counter rejects duplicate and out-of-order commands") {
    let session = try begin(FakeClient())
    check(session.accepts(sequence: 2), "fresh sequence rejected")
    check(!session.accepts(sequence: 2) && !session.accepts(sequence: 1), "stale sequence accepted")
    check(session.accepts(sequence: 3), "later sequence rejected")
}

test("New sentence captures latest edited document and starts dictation") {
    let client = FakeClient("原文")
    let first = try begin(client)
    first.update("改写", final: false)
    first.convert()
    first.update("改写", final: true)
    first.applyEdit(text: "结果", revision: first.revision, error: nil)
    first.interrupt("下一句")
    let second = try begin(client)
    check(second.original.text == "结果" && second.phase == .listening && !second.editRequested, "mode/context leaked across sentences")
}

test("Delayed marked ACK coalesces partials and preserves the final barrier without replaying writes") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("甲", final: false)
    check(session.awaitingReadback && client.markCount == 1, "stale mark read did not defer")
    client.probeOverride = CompositionProbe(selection: NSRange(location: 1, length: 0), markedRange: NSRange(location: 0, length: 1), markedText: "乙")
    client.now = 0.03
    session.advanceReadback()
    check(session.awaitingReadback && client.markCount == 1, "same-length stale text was accepted or rewritten")
    session.update("中间", final: false)
    session.update("最新部分", final: false)
    session.update("最终结果", final: true)
    session.update("迟到部分", final: false)
    check(client.markCount == 1 && client.commitCount == 0, "wrote before the first ACK")
    client.delayMark = false
    client.publishReads()
    client.now = 0.06
    session.advanceReadback()
    check(session.phase == .dictated && client.body == "最终结果", "queued final did not commit")
    check(client.markCount == 2 && client.commitCount == 1, "coalesced ASR updates replayed writes")
}

test("Commit ACK waits for cleared marked range and never repeats insertText") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayCommit = true
    session.update("完成", final: true)
    check(session.awaitingReadback && session.readbackStage == "commit", "stale commit was reported settled")
    for time in [0.03, 0.06, 0.09] { client.now = time; session.advanceReadback() }
    check(client.commitCount == 1 && session.phase != .dictated, "commit was retried or settled before ACK")
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .dictated && !session.awaitingReadback && client.commitCount == 1, "commit ACK did not settle once")
}

test("Readback timeout fails closed after one native mark") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("可见", final: false)
    session.update("后续", final: true)
    expireReadback(session, client)
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .error && client.markCount == 1 && client.commitCount == 0, "timeout wrote or accepted a late ACK")
    check(!session.awaitingReadback, "timeout retained pending work")
}

test("Cancel while mark ACK is pending stops audio and restores original selection after ACK") {
    let client = FakeClient("前旧后", selection: NSRange(location: 1, length: 1))
    let session = try begin(client)
    var ended = 0
    session.onFinishAudio = { ended += 1 }
    client.delayMark = true
    session.update("新", final: false)
    session.update("排队定稿", final: true)
    session.cancel()
    check(ended == 1 && client.markCount == 1 && client.commitCount == 0, "cancel did not stop audio before ACK")
    client.delayMark = false
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .undone && client.body == "前旧后", "cancel failed to restore selection")
    check(client.markCount == 1 && client.commitCount == 1, "cancel wrote queued ASR or repeated restoration")
}

test("Convert during delayed mark ends audio immediately and waits for final then edits") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var ended = 0
    var edits = 0
    session.onFinishAudio = { ended += 1 }
    session.onEdit = { original, instruction, _ in
        check(original == "原文" && instruction == "改为四点", "conversion lost original or final")
        edits += 1
    }
    client.delayMark = true
    session.update("改", final: false)
    session.convert()
    session.update("改为四点", final: true)
    check(ended == 1 && session.phase == .finishing && edits == 0, "conversion waited to end audio or edited before ACK")
    client.delayMark = false
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .editing && edits == 1 && client.commitCount == 0, "conversion removed text before model result")
}

test("Cancel conversion during delayed mark retains queued final as dictation") {
    let client = FakeClient()
    let session = try begin(client)
    var edits = 0
    session.onEdit = { _, _, _ in edits += 1 }
    client.delayMark = true
    session.update("改", final: false)
    session.convert()
    session.update("改为四点", final: true)
    session.cancel()
    client.delayMark = false
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .dictated && client.body == "改为四点" && edits == 0, "conversion cancel lost ordinary dictation")
}

test("Lifecycle interruption invalidates pending writes and late readback") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("部分", final: false)
    session.update("定稿", final: true)
    session.interrupt("已切换输入框")
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .interrupted && !session.awaitingReadback, "lifecycle retained pending callback")
    check(client.markCount == 1 && client.commitCount == 0, "lifecycle tried unconfirmed cleanup or final")
}

test("A mouse-selected caret during delayed ACK is never adopted as our own") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("文字", final: false)
    session.update("排队定稿", final: true)
    client.publishReads()
    client.selected = NSRange(location: 1, length: 0)
    session.advanceReadback()
    check(session.awaitingReadback && client.markCount == 1, "changed caret was adopted")
    expireReadback(session, client)
    check(session.phase == .error && client.selected.location == 1 && client.commitCount == 0, "changed caret was overwritten")
}

test("Final commits owned composition while a stale document snapshot waits for native commit ACK") {
    let client = FakeClient("原文")
    let session = try begin(client)
    let oldSnapshot = try client.snapshot()
    session.update("部分", final: false)
    client.snapshotOverride = oldSnapshot
    session.update("最后", final: true)
    check(session.readbackStage == "commit" && client.markCount == 2 && client.commitCount == 1, "ordinary final incorrectly required whole-document preflight")
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .dictated && client.body == "原文最后" && client.commitCount == 1, "final did not recover from stale complete snapshot")
}

test("Cancel while result-triggered instruction removal ACK is pending restores dictation") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var edits = 0
    session.onEdit = { _, _, _ in edits += 1 }
    session.update("指令", final: false)
    session.convert()
    client.delayCommit = true
    session.update("指令", final: true)
    session.applyEdit(text: "编辑结果", revision: session.revision, error: nil)
    check(session.readbackStage == "commit" && client.body == "原文", "instruction removal was not awaiting ACK")
    session.cancel()
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .dictated && client.body == "原文指令" && edits == 1 && client.replaceCount == 1, "cancel did not restore dictation once")
}

test("Cancel model preflight prevents the first edit write") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    client.snapshotOverride = EditorSnapshot(text: nil, selection: client.selected, markedRange: client.marked, selectedText: "", documentAccess: true)
    session.applyEdit(text: "结果", revision: session.revision, error: nil)
    check(session.awaitingReadback && client.replaceCount == 0, "model preflight unexpectedly wrote")
    session.cancel()
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .dictated && client.body == "原文指令" && client.replaceCount == 0, "cancel must retain dictation without applying model")
}

test("Cancel after edit write awaits replacement ACK then restores dictation once") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    client.delayReplace = true
    session.applyEdit(text: "编辑结果", revision: session.revision, error: nil)
    check(session.readbackStage == "replace" && client.replaceCount == 1, "edit did not await ACK")
    session.cancel()
    check(client.replaceCount == 1, "cancel wrote before edit acknowledgement")
    client.delayReplace = false
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .dictated && client.body == "原文指令" && client.replaceCount == 2, "pending edit cancel did not restore dictation exactly once")
}

test("Client may report the entire owned marked range as selected") {
    let client = FakeClient()
    client.selectEntireMarkedRange = true
    let session = try begin(client)
    session.update("你好呀", final: false)
    check(!session.awaitingReadback && session.phase == .listening && client.selected == client.marked, "whole marked selection was rejected")
    session.checkSelection()
    session.update("你好世界", final: false)
    check(client.markCount == 2 && session.phase == .listening, "whole marked selection blocked next partial")
    session.update("你好世界。", final: true)
    check(session.phase == .dictated && client.commitCount == 1, "whole marked selection could not finish")
}

test("Only the entire owned marked range is a valid nonempty selection") {
    let client = FakeClient()
    client.selectEntireMarkedRange = true
    let session = try begin(client)
    session.update("你好呀", final: false)
    client.selected = NSRange(location: 0, length: 1)
    session.checkSelection()
    session.update("后续", final: false)
    check(session.phase == .interrupted && client.markCount == 1, "arbitrary selected subrange was adopted")
}

test("A client exposing full context only outside composition can finish and retain safe undo") {
    let client = FakeClient("原文")
    let session = try begin(client)
    client.omitWholeWhileMarked = true
    session.update("部分", final: false)
    session.update("定稿", final: true)
    check(session.phase == .dictated && client.body == "原文定稿" && session.documentVerified, "missing whole context during composition blocked native commit")
    session.cancel()
    check(session.phase == .undone && client.body == "原文", "verified postcommit document lost undo")
}

test("Missing postcommit full context degrades dictation without attempting model conversion or undo") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var edits = 0
    session.onEdit = { _, _, _ in edits += 1 }
    session.update("改", final: false)
    session.convert()
    client.fullContext = false
    session.update("改为四点", final: true)
    check(session.phase == .dictated && client.body == "原文改为四点" && client.commitCount == 1, "unreadable full context blocked ordinary dictation")
    check(!session.documentVerified && !session.canEdit && !session.canCancel && !session.editRequested && edits == 0, "unverifiable document enabled full-text mutation")
    check(!session.error.isEmpty && !session.awaitingReadback, "capability limitation was not reported")
    session.cancel()
    session.convert()
    check(client.replaceCount == 0 && client.body == "原文改为四点", "disabled actions still wrote")
}

test("A changed marked range is rejected even when whole-document reads are unavailable") {
    let client = FakeClient("原文")
    let session = try begin(client)
    client.omitWholeWhileMarked = true
    session.update("部分", final: false)
    client.marked = NSRange(location: 1, length: 2)
    session.update("不得写入", final: true)
    expireReadback(session, client)
    check(session.phase == .error && client.markCount == 1 && client.commitCount == 0, "local commit bypassed marked ownership")
}

test("An invalid selected range is not a writable target and cannot start a transaction") {
    let client = FakeClient("", selection: unspecifiedRange)
    let snapshot = try client.snapshot()
    check(snapshot.startError != nil, "invalid idle target advertised ready")
    do {
        _ = try begin(client)
        check(false, "invalid target created a listening transaction")
    } catch {
        check(error.localizedDescription == "请先点入可编辑的文本框", "invalid target lost actionable reason")
    }
    check(client.markCount == 0 && client.commitCount == 0 && client.replaceCount == 0, "invalid target received a write")
}

test("An old composition is not a startup gate") {
    let client = FakeClient("旧字")
    client.marked = NSRange(location: 0, length: 2)
    let snapshot = try client.snapshot()
    check(snapshot.startError == nil, "old combination still blocks startup")
    _ = try begin(client)
    check(client.body == "旧字" && client.commitCount == 0 && client.markCount == 0,
          "begin tried to clean up another input method")
}

test("Host lifetime reset clears transaction identity before callbacks and rejects deferred old work") {
    let client = FakeClient()
    let old = try begin(client)
    let lifetime = CompositionSessionLifetime()
    lifetime.current = old
    var stopped = 0
    old.onInterrupted = { _ in
        check(lifetime.current == nil, "reconnect callback still exposed the old utterance")
        stopped += 1
    }
    client.delayMark = true
    old.update("部分", final: false)
    old.update("迟到定稿", final: true)
    lifetime.clear("主程序已断开")
    client.publishReads()
    old.advanceReadback()
    old.update("新主机不应收到", final: true)
    check(lifetime.current == nil && old.phase == .interrupted && stopped == 1, "host disconnect retained stale session identity")
    check(!old.awaitingReadback && client.commitCount == 0 && client.markCount == 1, "old readback performed a write after reconnect")
    lifetime.clear("输入连接已重新建立")
    let next = try begin(FakeClient())
    lifetime.current = next
    check(next.utteranceID != old.utteranceID && next.phase == .listening, "new host inherited old transaction")
}

test("Empty final settles after finish without writing or waiting for native readback") {
    let client = FakeClient("保留", selection: NSRange(location: 0, length: 2))
    let session = try begin(client)
    var settled = 0
    session.onSettled = { phase, text in
        check(phase == "dictated" && text.isEmpty, "empty final settled with wrong content")
        settled += 1
    }
    session.finish()
    session.update("", final: true)
    check(session.phase == .dictated && !session.awaitingReadback && settled == 1, "received empty final remained finishing")
    check(client.body == "保留" && client.markCount == 0 && client.commitCount == 0, "empty final changed selected original")
}

test("Exact marked text can confirm a client that preserves the insertion anchor while composing") {
    let client = FakeClient("原文")
    client.anchorMarkedSelection = true
    let session = try begin(client)
    for partial in ["你", "你好", "你好世界"] {
        session.update(partial, final: false)
        session.checkSelection()
        check(session.phase == .listening && !session.awaitingReadback, "verified anchor was mistaken for caret takeover")
        check(client.selected == NSRange(location: 2, length: 0), "test client did not preserve composition anchor")
    }
    session.update("你好世界。", final: true)
    check(session.phase == .dictated && client.body == "原文你好世界。", "anchored client could not finish")
    check(client.markCount == 4 && client.commitCount == 1, "anchor handling replayed a write")
    check(client.selected == NSRange(location: 7, length: 0) && !isValidRange(client.marked), "postcommit still requires normal end caret and cleared marked range")
}

test("Learned anchor does not authorize later external caret movement") {
    for location in [0, 3, 5] {
        let client = FakeClient("原文")
        client.anchorMarkedSelection = true
        let session = try begin(client)
        session.update("文字段", final: false)
        client.selected = NSRange(location: location, length: 0)
        session.checkSelection()
        session.update("不能继续", final: false)
        check(session.phase == .interrupted && client.markCount == 1 && client.commitCount == 0, "anchor convention adopted a user-moved caret")
        check(client.selected.location == location, "takeover caret moved back")
    }
}

test("Learned anchor does not authorize changed marked text") {
    let client = FakeClient("原文")
    client.anchorMarkedSelection = true
    let session = try begin(client)
    session.update("文字段", final: false)
    client.body = "原文别人改"
    session.update("不能覆盖", final: false)
    check(session.phase == .error && client.body == "原文别人改" && client.markCount == 1, "anchor convention overwrote external marked-text changes")
}

test("An anchor is not learned without exact positive marked-text readback") {
    let client = FakeClient()
    client.anchorMarkedSelection = true
    client.delayMark = true
    let session = try begin(client)
    session.update("文字", final: false)
    client.probeOverride = CompositionProbe(selection: NSRange(location: 0, length: 0), markedRange: NSRange(location: 0, length: 2), markedText: nil)
    session.advanceReadback()
    check(session.awaitingReadback, "missing marked text was sufficient to learn anchor")
    expireReadback(session, client)
    check(session.phase == .error && client.markCount == 1 && client.commitCount == 0, "unknown composition was committed")
}

test("A confirmed normal client cannot relearn a user-moved caret as anchor") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("第一", final: false)
    client.delayMark = true
    session.update("第二段", final: false)
    client.publishReads()
    client.selected = NSRange(location: 2, length: 0)
    session.advanceReadback()
    check(session.awaitingReadback, "normal caret convention was replaced by anchor after user movement")
    expireReadback(session, client)
    check(session.phase == .error && client.markCount == 2 && client.commitCount == 0, "relearned anchor allowed further writes")
}

test("Lifecycle commits only its owned composition without requiring full document access") {
    let client = FakeClient("原文")
    client.anchorMarkedSelection = true
    let session = try begin(client)
    client.omitWholeWhileMarked = true
    session.update("留下", final: false)
    session.interrupt("切换输入法")
    check(session.phase == .interrupted && client.body == "原文留下" && client.commitCount == 1, "missing full text stranded our acknowledged composition")
    check(!isValidRange(client.marked), "lifecycle left confirmed marked text uncommitted")
}

test("Lifecycle never commits composition after range or text takeover") {
    for changeRange in [false, true] {
        let client = FakeClient("原文")
        client.anchorMarkedSelection = true
        let session = try begin(client)
        session.update("留下", final: false)
        if changeRange { client.marked = NSRange(location: 1, length: 2) }
        else { client.body = "原文改变" }
        session.interrupt("切换输入法")
        check(session.phase == .interrupted && client.commitCount == 0, "lifecycle committed a user-owned composition")
    }
}

test("Error recovery commits an exactly owned visible mark once and permits the next sentence") {
    let client = FakeClient("前旧后", selection: NSRange(location: 1, length: 1))
    let session = try begin(client)
    client.delayMark = true
    session.update("可见", final: false)
    session.update("不能再应用的最终句", final: true)
    // The original ACK deadline has expired, but a fresh positive probe now
    // proves that the first native mark really is the one still on screen.
    client.publishReads()
    expireReadback(session, client)
    check(session.phase == .error && !session.hasComposition && !session.awaitingReadback, "known owned mark remained stranded")
    check(client.body == "前可见后" && client.markCount == 1 && client.commitCount == 1, "recovery replayed ASR or restored instead of retaining visible text")
    session.advanceReadback()
    session.update("迟到", final: true)
    check(client.commitCount == 1 && client.markCount == 1, "terminal recovery restarted input")
    let next = try begin(client)
    check(next.original.text == "前可见后" && next.phase == .listening, "self-owned residual blocked next begin")
}

test("Recovery waits for positive text ownership and never commits a missing marked-text read") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("可见", final: false)
    client.probeOverride = CompositionProbe(selection: NSRange(location: 2, length: 0), markedRange: NSRange(location: 0, length: 2), markedText: nil)
    expireReadback(session, client)
    check(session.phase == .error && session.readbackStage == "recovery" && client.commitCount == 0, "unknown marked text was committed")
    client.publishReads()
    session.advanceReadback()
    check(client.commitCount == 1 && client.body == "可见" && !session.hasComposition, "fresh exact ownership did not permit bounded retention")
}

test("Recovery does not replay a commit whose acknowledgement timed out") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayCommit = true
    session.update("只出现一次", final: true)
    check(session.readbackStage == "commit" && client.commitCount == 1, "test did not enter commit ACK")
    expireReadback(session, client)
    check(session.phase == .error && session.readbackStage == "recovery" && session.hasComposition, "unconfirmed old mark was not tracked")
    for time in [0.55, 0.65, 0.75] { client.now = time; session.advanceReadback() }
    check(client.commitCount == 1, "recovery repeated an already-issued insertText")
    client.publishReads()
    session.advanceReadback()
    check(!session.hasComposition && !session.awaitingReadback && client.body == "只出现一次", "cleared commit was not observed")
}

test("A recovery timeout permits only read-only observation and manual clearing afterwards") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("可见", final: false)
    client.probeOverride = CompositionProbe(selection: NSRange(location: 2, length: 0), markedRange: NSRange(location: 0, length: 2), markedText: nil)
    expireReadback(session, client)
    expireReadback(session, client)
    check(session.phase == .error && session.hasComposition && !session.awaitingReadback && client.commitCount == 0, "recovery was unbounded or guessed ownership")
    client.publishReads()
    session.advanceReadback()
    check(client.commitCount == 0 && session.hasComposition, "late positive read restarted a timed-out cleanup")
    client.body = ""
    client.marked = unspecifiedRange
    client.selected = NSRange(location: 0, length: 0)
    var clearedNotifications = 0
    session.onChanged = { clearedNotifications += 1 }
    session.advanceReadback()
    check(!session.hasComposition && clearedNotifications == 1 && client.commitCount == 0, "manual clear did not refresh readiness without writes")
    _ = try begin(client)
}

test("Recovery never clears a foreign marked string even at the same range") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("原句", final: false)
    client.publishReads()
    client.body = "外来"
    expireReadback(session, client)
    expireReadback(session, client)
    check(session.phase == .error && session.hasComposition && client.commitCount == 0 && client.body == "外来", "foreign composition was cleared or committed")
}

test("Recovery does not adopt a changed caret as a different client convention") {
    let client = FakeClient("原文")
    let session = try begin(client)
    session.update("第一", final: false)
    client.delayMark = true
    session.update("第二段", final: false)
    client.publishReads()
    client.selected = NSRange(location: 2, length: 0)
    expireReadback(session, client)
    expireReadback(session, client)
    check(client.commitCount == 0 && client.selected.location == 2 && client.body == "原文第二段", "recovery moved a user caret back")
}

test("User interruption cancels error recovery without any cleanup write") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("可见", final: false)
    client.probeOverride = CompositionProbe(selection: NSRange(location: 2, length: 0), markedRange: NSRange(location: 0, length: 2), markedText: nil)
    expireReadback(session, client)
    session.interrupt("键盘接管", commitIfOwned: false)
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .interrupted && !session.awaitingReadback && client.commitCount == 0, "error recovery survived manual takeover")
}

test("System composition callback cancels error recovery without replaying input") {
    let client = FakeClient()
    let session = try begin(client)
    client.delayMark = true
    session.update("可见", final: false)
    client.probeOverride = CompositionProbe(selection: NSRange(location: 2, length: 0), markedRange: NSRange(location: 0, length: 2), markedText: nil)
    expireReadback(session, client)
    check(session.commitCompositionRequested(), "terminal lifecycle callback left recovery running")
    client.publishReads()
    session.advanceReadback()
    check(session.phase == .interrupted && !session.awaitingReadback && client.commitCount == 0, "system callback reissued old composition text")
}

test("Anchored listening undo works without whole-document access and permits the next sentence") {
    let client = FakeClient("之前已有文字")
    client.fullContext = false
    client.anchorMarkedSelection = true
    let original = client.body
    let session = try begin(client)
    var ended = 0
    session.onFinishAudio = { ended += 1 }
    for partial in ["撤", "撤销本句"] { session.update(partial, final: false) }
    check(session.canCancel && !session.canReplace, "local undo incorrectly requires whole-document capability")
    session.cancel()
    check(session.phase == .undone && !session.awaitingReadback && ended == 1, "anchored local undo did not settle")
    check(client.body == original && !isValidRange(client.marked) && client.commitCount == 1, "local undo damaged surrounding text or left composition")
    session.update("迟到的最终文本", final: true)
    let next = try begin(client)
    next.update("下一句", final: true)
    check(next.phase == .dictated && client.body == original + "下一句", "undo stranded the next sentence")
    check(client.commitCount == 2 && client.replaceCount == 0, "undo or next sentence replayed a write")
}

test("Listening undo restores the original selection when full text is unavailable during composition") {
    let client = FakeClient("前缀旧文后缀", selection: NSRange(location: 2, length: 2))
    client.anchorMarkedSelection = true
    client.omitWholeWhileMarked = true
    let session = try begin(client)
    session.update("替换片段", final: false)
    session.cancel()
    check(session.phase == .undone && !session.awaitingReadback, "composition undo waited for an unavailable full document")
    check(client.body == "前缀旧文后缀" && client.selected == NSRange(location: 4, length: 0), "original selected text was not restored locally")
    check(client.commitCount == 1 && client.replaceCount == 0 && !isValidRange(client.marked), "undo performed a whole-document write or left a mark")
    let next = try begin(client)
    next.update("接着说", final: true)
    check(next.phase == .dictated && client.body == "前缀旧文接着说后缀", "restored selection blocked subsequent dictation")
}

test("Local listening undo never overwrites an externally changed marked string") {
    let client = FakeClient("前文")
    client.fullContext = false
    client.anchorMarkedSelection = true
    let session = try begin(client)
    session.update("本句", final: false)
    client.body = "前文外改"
    session.cancel()
    expireReadback(session, client)
    expireReadback(session, client)
    check(session.phase == .error && client.body == "前文外改", "local undo adopted foreign marked text")
    check(client.commitCount == 0 && client.replaceCount == 0, "undo or recovery overwrote a user-owned composition")
}

test("An empty ASR utterance preserves an unreadable document's selected original") {
    let client = FakeClient("前旧后", selection: NSRange(location: 1, length: 1))
    client.fullContext = false
    client.anchorMarkedSelection = true
    client.emptyMarkClearsComposition = true
    let session = try begin(client)
    session.update("", final: false)
    session.finish()
    session.update("", final: true)
    check(session.phase == .dictated && !session.awaitingReadback, "empty utterance failed to settle without full context")
    check(client.body == "前旧后" && client.selected == NSRange(location: 1, length: 1), "empty utterance changed selected original")
    check(client.markCount == 0 && client.commitCount == 0, "empty utterance wrote to the client")
    let next = try begin(client)
    next.update("新", final: true)
    check(next.phase == .dictated && client.body == "前新后", "empty utterance prevented the next replacement")
}

test("An anchored empty partial clears visible preedit and still permits subsequent ASR revisions") {
  for zeroRange in [false, true] {
    let client = FakeClient("前文")
    client.fullContext = false
    client.anchorMarkedSelection = true
    client.emptyMarkClearsComposition = true
    client.emptyMarkUsesZeroRange = zeroRange
    let session = try begin(client)
    session.update("临时词", final: false)
    session.update("", final: false)
    check(session.phase == .listening && !session.awaitingReadback && client.body == "前文", "valid empty preedit revision was rejected")
    check((!isValidRange(client.marked) || client.marked.length == 0) && client.probe().markedText == nil, "test did not model absent empty composition readback")
    session.update("重新识别", final: false)
    session.update("重新识别。", final: true)
    check(session.phase == .dictated && client.body == "前文重新识别。", "empty revision broke the next partial or final")
    check(client.markCount == 4 && client.commitCount == 1, "empty revision replayed writes")
  }
}

test("An anchored empty final after a partial settles without requiring readable empty marked text") {
  for zeroRange in [false, true] {
    let client = FakeClient("前旧后", selection: NSRange(location: 1, length: 1))
    client.anchorMarkedSelection = true
    client.emptyMarkClearsComposition = true
    client.emptyMarkUsesZeroRange = zeroRange
    let session = try begin(client)
    session.update("临时", final: false)
    session.finish()
    session.update("", final: true)
    check(session.phase == .dictated && !session.awaitingReadback && client.body == "前后", "empty final remained waiting for nonexistent marked text")
    check(!isValidRange(client.marked) && client.selected == NSRange(location: 1, length: 0), "empty final retained a mark or misplaced caret")
    session.cancel()
    check(session.phase == .undone && client.body == "前旧后", "verified empty-final undo did not restore original selection")
    let next = try begin(client)
    next.update("下一句", final: true)
    check(next.phase == .dictated && client.body == "前旧下一句后", "empty final plus undo blocked the next sentence")
  }
}

test("Cancelling after an anchored empty partial restores selected original without whole context") {
    let client = FakeClient("前旧后", selection: NSRange(location: 1, length: 1))
    client.fullContext = false
    client.anchorMarkedSelection = true
    client.emptyMarkClearsComposition = true
    let session = try begin(client)
    session.update("临时", final: false)
    session.update("", final: false)
    session.cancel()
    check(session.phase == .undone && !session.awaitingReadback && client.body == "前旧后", "empty partial made local selection restoration unavailable")
    check(client.commitCount == 1 && client.replaceCount == 0, "empty partial undo was not a single local write")
    _ = try begin(client)
}

test("Committed sentence undo uses its exact range without whole-document access") {
    let client = FakeClient("前😀尾巴", selection: NSRange(location: 3, length: 0))
    client.fullContext = false; client.rangeReads = true; client.anchorMarkedSelection = true
    let session = try begin(client)
    session.update("口述内容", final: true)
    check(session.phase == .dictated && session.canCancel && !session.canEdit, "local undo was coupled to full edit")
    check(session.error.isEmpty, "successful dictation displayed a capability failure")
    session.cancel()
    check(session.phase == .undone && client.body == "前😀尾巴" && client.selected.location == 3, "local undo changed surrounding Unicode")
    let next = try begin(client)
    next.update("下一句", final: true)
    check(client.body == "前😀下一句尾巴" && next.phase == .dictated, "undo stranded next sentence")
}

test("Local committed undo restores a selected original and preserves changes outside its range") {
    let client = FakeClient("前旧😀后", selection: NSRange(location: 1, length: 3))
    client.fullContext = false; client.rangeReads = true
    let session = try begin(client)
    session.update("新", final: true)
    client.body = "前新外部新尾巴"
    session.cancel()
    check(client.body == "前旧😀外部新尾巴" && session.phase == .undone, "undo required or replaced unrelated content")
}

test("Local committed undo refuses changed sentence, moved caret, or another active composition") {
    for change in 0..<3 {
        let client = FakeClient("前后", selection: NSRange(location: 1, length: 0))
        client.fullContext = false; client.rangeReads = true
        let session = try begin(client)
        session.update("本句", final: true)
        if change == 0 { client.body = "前改变后" }
        if change == 1 { client.selected = NSRange(location: 0, length: 0) }
        if change == 2 { client.marked = NSRange(location: 1, length: 2) }
        let before = client.body
        session.cancel(); expireReadback(session, client)
        check(client.replaceCount == 0 && client.body == before && session.phase == .error, "undo overwrote user takeover")
    }
}

test("Local undo checks replacement acknowledgement and never replays a failed deletion") {
    let client = FakeClient("前后", selection: NSRange(location: 1, length: 0))
    client.fullContext = false; client.rangeReads = true; client.ignoreReplacement = true
    let session = try begin(client)
    session.update("本句", final: true)
    session.cancel(); expireReadback(session, client)
    check(client.body == "前本句后" && client.replaceCount == 1 && session.phase == .error, "ignored range replacement was accepted or repeated")
}

test("Local undo needs exact readable text and advertised range access") {
    for access in [false, true] {
        let client = FakeClient("原文")
        client.fullContext = false; client.rangeReads = !access; client.access = access
        let session = try begin(client)
        session.update("本句", final: true)
        check(!session.canCancel && session.error.isEmpty, "unverified mutation was advertised or dictation looked failed")
        session.cancel()
        check(client.replaceCount == 0 && !session.error.isEmpty, "unavailable undo wrote or gave no explanation")
    }
}

test("Committed dictation cannot be relabeled as an edit") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var requests = 0
    session.onEdit = { _, _, _ in requests += 1 }
    session.update("改成四点", final: true)
    session.convert()
    check(requests == 0 && !session.canEdit && session.phase == .dictated, "postfinal conversion invoked a model")
    check(client.body == "原文改成四点" && client.replaceCount == 0, "postfinal swipe changed text")
    session.cancel()
    check(client.body == "原文" && session.phase == .undone, "rejected conversion blocked undo")
}

test("Tap then conversion before final still identifies the underlined utterance") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var requests = 0
    session.onEdit = { _, instruction, _ in
        check(instruction == "最终指令" && client.body == "原文最终指令", "instruction disappeared before model result")
        requests += 1
    }
    session.update("指令", final: false)
    session.finish()
    check(session.canEdit, "underlined finishing sentence lost conversion")
    session.convert()
    session.update("最终指令", final: true)
    check(requests == 1 && session.phase == .editing, "underlined conversion was ignored")
}

test("Scoped IMK context edits only the verified original and restores dictation then selected text") {
    let client = FakeClient("前缀😀明天三点开会。保留后缀", selection: NSRange(location: 6, length: 1))
    client.fullContext = false
    client.rangeReads = true
    client.contextRange = NSRange(location: 4, length: 7)
    let before = client.body
    let session = try begin(client)
    check(session.editScope == "readable_range" && !session.original.complete, "partial context was called whole")
    var requested = 0
    session.onEdit = { original, instruction, _ in
        check(original == "明天三点开会。" && instruction == "改为四", "instruction mixed into original context")
        check(client.body == (before as NSString).replacingCharacters(in: NSRange(location: 6, length: 1), with: "改为四") && isValidRange(client.marked), "instruction disappeared before model")
        requested += 1
    }
    session.update("改", final: false)
    session.convert()
    session.update("改为四", final: true)
    check(requested == 1 && session.phase == .editing, "range context could not enter model")
    client.body = "前缀😀明天改为四点开会。新的后缀"
    session.applyEdit(text: "明天四点开会。", revision: session.revision, error: nil)
    check(client.body == "前缀😀明天四点开会。新的后缀" && session.phase == .edited, "edit changed outside its scope")
    session.cancel()
    check(client.body == "前缀😀明天改为四点开会。新的后缀" && session.phase == .dictated, "cancel did not restore dictation in range")
    session.cancel()
    check(client.body == "前缀😀明天三点开会。新的后缀" && session.phase == .undone, "undo lost original selection or outside text")
}

test("Scoped pending edit cancellation and model error restore dictation and reject late results") {
    for failure in [false, true] {
        let client = FakeClient("前原文后", selection: NSRange(location: 3, length: 0))
        client.fullContext = false
        client.rangeReads = true
        client.contextRange = NSRange(location: 1, length: 2)
        let session = try begin(client)
        session.update("指令", final: false)
        session.convert()
        session.update("指令", final: true)
        let revision = session.revision
        check(client.body == "前原文指令后" && isValidRange(client.marked), "pending model must retain instruction")
        if failure { session.applyEdit(text: "", revision: revision, error: "离线") }
        else { session.cancel() }
        session.applyEdit(text: "迟到", revision: revision, error: nil)
        check(session.phase == .dictated && client.body == "前原文指令后", "cancel/error failed to restore spoken text")
        session.cancel()
        check(client.body == "前原文后", "restored dictation lost local undo")
    }
}

test("An altered original scope keeps dictated instruction and does not call the model") {
    let client = FakeClient("原文")
    let session = try begin(client)
    var requests = 0
    session.onEdit = { _, _, _ in requests += 1 }
    session.update("指令", final: false)
    session.convert()
    client.body = "新文指令"
    session.update("指令", final: true)
    check(requests == 0 && client.body == "新文指令" && session.phase == .dictated, "conversion erased instruction without original ownership")
    session.cancel()
    check(client.body == "新文", "unavailable edit damaged local undo")
}

test("Scoped context outside original selection does not grant edit capability") {
    let client = FakeClient("前原文后")
    client.fullContext = false
    client.rangeReads = true
    client.contextRange = NSRange(location: 1, length: 2)
    let session = try begin(client)
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    check(!session.editContextAvailable && session.phase == .dictated && client.body == "前原文后指令", "unrelated context accepted")
}

test("Unreadable suffix cannot make an insertion at scope start look like replacement") {
    let client = FakeClient("原文后文", selection: NSRange(location: 0, length: 0))
    client.fullContext = false
    client.rangeReads = true
    client.contextRange = NSRange(location: 0, length: 2)
    client.ignoreReplacement = true
    let session = try begin(client)
    var requests = 0
    session.onEdit = { _, _, _ in requests += 1 }
    session.update("指令", final: false)
    session.convert()
    session.update("指令", final: true)
    check(requests == 0 && client.body == "指令原文后文" && session.phase == .dictated, "ambiguous range entered edit")
    check(client.replaceCount == 0, "unverified scope was used as a destructive probe")
}

print("\(passed) native composition tests passed")
