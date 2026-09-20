import Foundation

final class Editor: CompositionClient {
    var body: String
    var selected: NSRange
    var marked = unspecifiedRange
    var reads = 0
    var writes = 0
    init(_ text: String = "") { body = text; selected = NSRange(location: (text as NSString).length, length: 0) }
    func snapshot() throws -> EditorSnapshot {
        reads += 1
        return EditorSnapshot(text: body, selection: selected, markedRange: marked,
                              selectedText: (body as NSString).substring(with: selected), documentAccess: true)
    }
    func readText(in range: NSRange) -> String? {
        reads += 1
        return isValidRange(range, length: (body as NSString).length) ? (body as NSString).substring(with: range) : nil
    }
    func selection() -> NSRange { selected }
    func probe() -> CompositionProbe {
        reads += 1
        return CompositionProbe(selection: selected, markedRange: marked,
                                markedText: isValidRange(marked) ? (body as NSString).substring(with: marked) : nil)
    }
    func replace(_ range: NSRange, with text: String) {
        writes += 1
        body = (body as NSString).replacingCharacters(in: range, with: text)
        selected = NSRange(location: range.location + (text as NSString).length, length: 0)
        marked = unspecifiedRange
    }
    func mark(_ text: String) {
        let range = isValidRange(marked) ? marked : selected
        replace(range, with: text)
        marked = NSRange(location: range.location, length: (text as NSString).length)
    }
    func commit(_ text: String) { replace(isValidRange(marked) ? marked : selected, with: text) }
}

final class Fixture {
    let editor: Editor
    let history: CompositionUndoHistory
    var session: CompositionSession!
    var source: String?
    init(_ text: String = "", enabled: Bool = true, capacity: Int = 100, budget: Int = 4 * 1024 * 1024) {
        editor = Editor(text)
        history = CompositionUndoHistory(capacity: capacity, unitBudget: budget)
        history.configure(enabled: enabled)
    }
    func begin() throws {
        let next = try CompositionSession(client: editor, utteranceID: UUID().uuidString, sequence: 1)
        history.begin(next.original)
        source = nil
        session = next
        wire()
    }
    func wire() {
        session.onSettled = { [unowned self] _, _ in history.settled(session, sourceUtteranceID: source) }
        session.onInterrupted = { [unowned self] _ in history.clear() }
    }
    func say(_ text: String) throws { try begin(); session.update(text, final: true) }
    func edit(_ instruction: String, to result: String) throws {
        try begin(); session.update(instruction, final: false); session.convert()
        session.update(instruction, final: true)
        session.applyEdit(text: result, revision: session.revision, error: nil)
    }
    func undo() {
        if session.phase == .undone, let record = history.pop() {
            source = record.sourceUtteranceID
            session = CompositionSession(client: editor, undo: record, utteranceID: session.utteranceID,
                                         sequence: session.lastSequence, revision: session.revision + 1)
            wire()
        }
        session.cancel()
    }
}

var passed = 0
func check(_ value: @autoclosure () -> Bool, _ message: String) { if !value() { fatalError(message) } }
func test(_ name: String, _ block: () throws -> Void) throws { try block(); passed += 1; print("PASS \(name)") }

try test("Three dictations undo in LIFO order including Unicode and line breaks") {
    let f = Fixture("原文")
    for text in ["一😀", "二\n", "👨‍👩‍👧‍👦三"] { try f.say(text) }
    for expected in ["原文一😀二\n", "原文一😀", "原文"] { f.undo(); check(f.editor.body == expected, "wrong undo order") }
    let writes = f.editor.writes
    f.undo(); check(f.editor.writes == writes && f.history.count == 0, "empty history wrote")
}
try test("Mixed edit restores dictation, then reaches previous dictation and edit") {
    let f = Fixture("原文")
    try f.say("甲")
    try f.edit("改成乙", to: "原文乙")
    try f.say("丙")
    try f.edit("改成丁", to: "原文丁")
    for expected in ["原文乙丙改成丁", "原文乙丙", "原文乙", "原文甲改成乙", "原文甲", "原文"] {
        f.undo(); check(f.editor.body == expected, "mixed undo failed: \(f.editor.body) expected \(expected)")
    }
}
try test("Disabled mode keeps only current sentence, including two-stage edit cancellation") {
    let f = Fixture(enabled: false)
    try f.say("甲"); try f.edit("改成乙", to: "乙")
    f.undo(); check(f.editor.body == "甲改成乙", "edit restore regressed")
    f.undo(); f.undo(); check(f.editor.body == "甲" && f.history.count == 0, "disabled history leaked")
}
try test("Toggle off clears older entries, toggling on cannot resurrect them") {
    let f = Fixture()
    try f.say("甲"); try f.say("乙")
    f.history.configure(enabled: false); f.history.configure(enabled: true)
    f.undo(); f.undo(); check(f.editor.body == "甲", "disabled entries returned")
}
try test("Empty/live cancelled sentence leaves earlier undo history reachable") {
    let f = Fixture()
    try f.say("甲"); try f.begin(); f.session.update("暂存", final: false)
    f.undo(); check(f.editor.body == "甲", "live cancel failed")
    f.undo(); check(f.editor.body.isEmpty, "previous sentence unreachable")
}
try test("New utterance after undo creates a new history branch") {
    let f = Fixture()
    try f.say("甲"); try f.say("乙"); f.undo(); try f.say("丙")
    f.undo(); f.undo(); check(f.editor.body.isEmpty, "new branch retained removed operation")
}
try test("Same-length external text change is rejected before deletion") {
    let f = Fixture()
    try f.say("甲"); try f.say("乙"); f.undo()
    f.editor.body = "X"
    let writes = f.editor.writes
    f.undo(); check(f.editor.body == "X" && f.editor.writes == writes, "foreign text deleted")
}
try test("Moved caret invalidates the stack at the next BEGIN") {
    let f = Fixture()
    try f.say("甲"); try f.say("乙")
    f.editor.selected = NSRange(location: 0, length: 0)
    try f.say("丙"); f.undo(); f.undo()
    check(f.editor.body == "甲乙" && f.history.count == 0, "history survived caret takeover")
}
try test("Capacity and memory budgets evict oldest entries without gaps") {
    let f = Fixture(capacity: 2, budget: 10)
    for _ in 0..<6 { try f.say("甲") }
    check(f.history.count == 2 && f.history.retainedUnits == 2, "capacity unbounded")
    for _ in 0..<5 { f.undo() }
    check(f.editor.body == "甲甲甲", "evicted entries were still undoable")
    let large = Fixture("原文", budget: 5)
    try large.say("甲"); try large.edit("编辑", to: "很长的结果")
    check(large.history.count == 0, "oversize record left a history gap")
}
try test("History capture/restore performs no additional client reads and stores compact dictation") {
    let f = Fixture(String(repeating: "长", count: 100_000))
    try f.say("甲"); let reads = f.editor.reads
    let record = f.session.undoRecord()!
    check(record.original.text == nil && record.original.readableContext == nil && record.retainedUnits == 1,
          "dictation retained a full document")
    f.history.settled(f.session)
    _ = CompositionSession(client: f.editor, undo: record, utteranceID: "restore", sequence: 1, revision: 7)
    check(f.editor.reads == reads, "history did extra IPC reads")
}
try test("Late model/ASR results cannot mutate a restored historical operation") {
    let f = Fixture()
    try f.say("甲"); try f.say("乙"); f.undo(); f.undo()
    let writes = f.editor.writes
    f.session.update("late", final: true)
    f.session.applyEdit(text: "late", revision: 1, error: nil)
    check(f.editor.writes == writes && f.editor.body.isEmpty, "late result wrote")
}
try test("Historical replacement restores selected text, not just an empty deletion") {
    let f = Fixture("原始选中")
    f.editor.selected = NSRange(location: 2, length: 2)
    try f.say("新的😀"); try f.say("后续")
    f.undo(); f.undo()
    check(f.editor.body == "原始选中", "original selection lost")
}
try test("Empty edit result remains undoable after a later sentence") {
    let f = Fixture("原文")
    try f.edit("清空", to: ""); try f.say("后一句")
    f.undo(); f.undo()
    check(f.editor.body == "原文清空", "empty edit was mistaken for no edit")
    f.undo(); check(f.editor.body == "原文", "instruction not removable")
}
print("\(passed) history tests passed")

if CommandLine.arguments.contains("--benchmark") {
    var rows: [[String: Any]] = []
    for size in [1_000, 100_000] {
        for enabled in [false, true] {
            let f = Fixture(String(repeating: "原", count: size), enabled: enabled)
            var times = ["begin": [Double](), "partial": [], "final": [], "undo_current": [], "undo_history": [], "edit_apply": []]
            func measure(_ key: String, _ work: () throws -> Void) rethrows {
                let t = ProcessInfo.processInfo.systemUptime
                try work()
                times[key]!.append((ProcessInfo.processInfo.systemUptime - t) * 1000)
            }
            for index in 0..<120 {
                try measure("begin") { try f.begin() }
                measure("partial") { f.session.update("第\(index)句😀", final: false) }
                measure("final") { f.session.update("第\(index)句话😀。", final: true) }
            }
            let reads = f.editor.reads
            let units = f.history.totalRetainedUnits
            measure("undo_current") { f.undo() }
            if enabled {
                for _ in 0..<100 { measure("undo_history") { f.undo() } }
            }
            for index in 0..<10 {
                try f.begin(); f.session.update("改写", final: false); f.session.convert()
                f.session.update("改写", final: true)
                let result = String(repeating: "文", count: size) + String(index)
                measure("edit_apply") { f.session.applyEdit(text: result, revision: f.session.revision, error: nil) }
                check(f.session.phase == .edited, "benchmark edit failed")
            }
            var metrics: [String: Any] = [:]
            for (key, values) in times {
                guard !values.isEmpty else { continue }
                let sorted = values.sorted()
                metrics[key] = ["median_ms": sorted[sorted.count / 2], "p95_ms": sorted[min(sorted.count - 1, sorted.count * 95 / 100)]]
            }
            rows.append(["initial_characters": size, "enabled": enabled, "metrics": metrics,
                         "dictation_retained_utf16_units": units,
                         "after_ten_edits_retained_utf16_units": f.history.totalRetainedUnits,
                         "client_reads_before_undo": reads])
        }
    }
    let data = try JSONSerialization.data(withJSONObject: rows, options: [.prettyPrinted, .sortedKeys])
    print("BENCHMARK_JSON\n" + String(data: data, encoding: .utf8)!)
}
