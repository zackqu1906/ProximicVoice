import Foundation

/// Screen-space presentation state, separate from the editor's live selection.
/// Hold the palette during edit processing; follow the live caret once settled.
struct ActionPanelAnchor {
    private var utteranceID: String?
    private var holding = false
    private(set) var caret: NSRect?
    private(set) var origin: NSPoint?
    private var heldSize: NSSize?

    var needsCaret: Bool { !holding || origin == nil }

    mutating func update(utteranceID: String, editing: Bool, currentSize: NSSize) {
        if self.utteranceID != utteranceID {
            self = ActionPanelAnchor()
            self.utteranceID = utteranceID
        }
        if editing && !holding {
            holding = true
            if origin != nil { heldSize = currentSize }
        } else if !editing && holding {
            holding = false
            heldSize = nil
        }
    }

    func layoutSize(_ proposed: NSSize, busy: Bool) -> NSSize {
        busy ? (heldSize ?? proposed) : proposed
    }

    mutating func observe(_ next: NSRect?) -> NSRect? {
        if needsCaret, let next { caret = next }
        return caret
    }

    mutating func place(_ proposed: NSPoint, size: NSSize) -> NSPoint {
        if holding, let origin { return origin }
        origin = proposed
        if holding { heldSize = size }
        return proposed
    }

    var heldOrigin: NSPoint? { holding ? origin : nil }
}
