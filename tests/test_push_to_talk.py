from proximic_ring.push_to_talk import PushToTalkState


def test_push_to_talk_state_reports_only_real_transitions():
    transitions = []
    state = PushToTalkState(on_change=transitions.append)

    state.set_active(True)
    state.set_active(True)
    assert state.is_active()
    state.set_active(False)
    state.set_active(False)

    assert not state.is_active()
    assert transitions == [True, False]
