import Foundation

let voice = "test.voice.dictation"
var current = voice
var selections = 0
func run(_ app: @escaping () -> Bool = { true }, _ select: (() throws -> Void)? = nil) throws -> Bool {
    try ensureVoiceSource(modeID: voice, currentSource: { current }, sameApplication: app,
        select: select ?? { selections += 1; current = voice })
}
func rejects(_ action: () throws -> Bool) {
    do { _ = try action(); fatalError("Expected selection to fail") } catch { }
}
let already = try run()
precondition(already && selections == 0)
current = "system.pinyin"
let switched = try run()
precondition(!switched && selections == 1 && current == voice)
_ = try run()
precondition(selections == 1) // no re-selection of an existing composition
current = ""
rejects { try run() }
precondition(selections == 1)
current = "system.abc"
rejects { try run({ false }) }
var identityReads = 0
rejects { try run({ identityReads += 1; return identityReads == 1 }) }
precondition(selections == 1)
rejects { try run({ true }, { throw SourceSelectionFailure("Not installed") }) }
rejects { try run({ true }, { }) } // unsuccessful system switch
precondition(current == "system.abc" && selections == 1)
print("Source selection: 8 scenarios passed; no AX or real source switching")
