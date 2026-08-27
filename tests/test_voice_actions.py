from proximic_ring.voice_actions import (
    ACTION_CONFIRM,
    ACTION_REASON_ASR_ERROR,
    ACTION_REASON_LLM_ERROR,
    ACTION_REASON_OTHER,
    WindowsVoiceActionHotkeys,
)


def _action(key: int, *, alt: bool = True, review: bool = False, reason: bool = False):
    return WindowsVoiceActionHotkeys._action_for_key(
        key,
        alt_down=alt,
        review_active=review,
        feedback_reason_active=reason,
    )


def test_failure_reason_shortcuts_only_capture_keys_while_prompt_is_active():
    assert _action(0x41, reason=True) == ACTION_REASON_ASR_ERROR
    assert _action(0x4C, reason=True) == ACTION_REASON_LLM_ERROR
    assert _action(0x4F, reason=True) == ACTION_REASON_OTHER
    assert _action(0x41, reason=False) is None
    assert _action(0x4C, alt=False, reason=True) is None


def test_review_key_still_works_while_failure_reason_prompt_is_visible():
    assert _action(0x0D, alt=False, review=True, reason=True) == ACTION_CONFIRM
