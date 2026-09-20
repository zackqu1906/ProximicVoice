import Foundation

/// Recover a missing IMK client without issuing leave/return in the same
/// run-loop turn: delayed deactivation must drain before the source is restored.
final class InputSourceRecovery {
    private var lastAttempt = -Double.infinity
    private var lastRequest = ""
    private var pending = false

    func recover(request: String, now: Double, eligible: () -> Bool,
                 selected: () -> Bool, leave: () -> Bool,
                 mayReturn: @escaping () -> Bool, restore: @escaping () -> Bool,
                 schedule: (@escaping () -> Void) -> Void,
                 completion: @escaping (String) -> Void) {
        guard !request.isEmpty, request != lastRequest else { completion("duplicate"); return }
        guard !pending, eligible() else { completion("busy"); return }
        guard selected() else { completion("not_selected"); return }
        guard now - lastAttempt >= 2 else { completion("cooldown"); return }
        lastRequest = request
        lastAttempt = now
        guard eligible(), selected() else { completion("changed"); return }
        pending = true
        guard leave() else { pending = false; completion("failed"); return }
        schedule { [self] in
            // Recheck target, expiry and user's current input-source choice
            // after yielding. Never restore over a newer user action.
            guard mayReturn() else { pending = false; completion("changed"); return }
            let restored = restore()
            pending = false
            completion(restored ? "refreshed" : "failed")
        }
    }
}
