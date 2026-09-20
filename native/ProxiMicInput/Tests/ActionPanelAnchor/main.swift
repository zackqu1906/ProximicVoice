import AppKit

func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fputs("FAIL: \(message)\n", stderr); exit(1) }
}
let size = NSSize(width: 290, height: 40)
let caret = NSRect(x: 400, y: 300, width: 1, height: 20)
let origin = NSPoint(x: 400, y: 254)
var anchor = ActionPanelAnchor()
anchor.update(utteranceID: "sentence", editing: false, currentSize: size)
check(anchor.needsCaret, "dictation must track its caret")
check(anchor.observe(caret) == caret, "dictation lost caret")
check(anchor.place(origin, size: size) == origin, "initial placement failed")
let nextCaret = NSRect(x: 430, y: 300, width: 1, height: 20)
check(anchor.observe(nextCaret) == nextCaret, "live dictation no longer follows partials")
let editOrigin = NSPoint(x: 430, y: 254)
_ = anchor.place(editOrigin, size: size)
anchor.update(utteranceID: "sentence", editing: true, currentSize: size)
check(!anchor.needsCaret, "editing still requests remote caret polling")
// Simulate five minutes at 30 Hz: delayed layout, missing caret, all-selected
// source text, caret restoration, and repeated model/context revisions.
for frame in 0..<9000 {
    anchor.update(utteranceID: "sentence", editing: true, currentSize: size)
    let jitter = NSRect(x: Double(frame % 1500), y: Double(frame % 800), width: 200, height: 20)
    check(anchor.observe(frame % 4 == 0 ? nil : jitter) == nextCaret, "waiting adopted a transient caret")
    check(anchor.place(jitter.origin, size: NSSize(width: 180, height: 40)) == editOrigin, "waiting moved the palette")
    check(anchor.layoutSize(NSSize(width: Double(132 + frame % 250), height: 40), busy: true) == size, "busy layout changed size")
}
print("PASS long wait and repeated edit rounds preserve position and size")
anchor.update(utteranceID: "sentence", editing: false, currentSize: size)
check(anchor.heldOrigin == nil && anchor.needsCaret, "result hint did not resume caret tracking")
let resultSize = NSSize(width: 320, height: 40)
check(anchor.layoutSize(resultSize, busy: false) == resultSize, "result text cannot fit its layout")
check(anchor.observe(nil) == nextCaret, "missing result geometry discarded the last valid anchor")
let resultCaret = NSRect(x: 520, y: 220, width: 1, height: 20)
let resultOrigin = NSPoint(x: 520, y: 174)
check(anchor.observe(resultCaret) == resultCaret && anchor.place(resultOrigin, size: resultSize) == resultOrigin,
      "result hint stayed at the instruction instead of the new caret")
print("PASS settled result follows the replacement caret and retries missing geometry")
// A palette hide changes visibility, not the identity of the current utterance.
anchor.update(utteranceID: "sentence", editing: true, currentSize: size)
check(anchor.heldOrigin == resultOrigin, "next pending edit did not freeze at its current origin")
print("PASS returning to the same edit retains anchor")
anchor.update(utteranceID: "next-sentence", editing: false, currentSize: size)
check(anchor.needsCaret && anchor.caret == nil && anchor.heldOrigin == nil, "next utterance inherited edit anchor")
check(anchor.observe(caret) == caret && anchor.place(origin, size: size) == origin, "next dictation did not resume following")
print("PASS new utterance resumes tracking without an old caret")
var initiallyUnpositioned = ActionPanelAnchor()
initiallyUnpositioned.update(utteranceID: "early-edit", editing: true, currentSize: size)
check(initiallyUnpositioned.needsCaret && initiallyUnpositioned.observe(nil) == nil, "missing first caret fabricated a position")
_ = initiallyUnpositioned.observe(caret)
_ = initiallyUnpositioned.place(origin, size: size)
check(!initiallyUnpositioned.needsCaret && initiallyUnpositioned.heldOrigin == origin, "first valid edit position did not freeze")
print("PASS edit before initial geometry waits for one valid anchor")
