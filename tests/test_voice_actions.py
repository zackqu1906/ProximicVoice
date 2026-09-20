from proximic_ring.voice_actions import (
    ACTION_CANCEL,
    ACTION_SWITCH_MODE,
    ACTION_UNDO,
    WindowsVoiceActionHotkeys,
    normalize_mode_switch_shortcut,
    windows_mode_switch_key_code,
)
import proximic_ring.voice_actions as voice_actions_module
import itertools
import pytest


@pytest.mark.parametrize("gesture,key", [
    ("swipe-left", 0x1B),
    ("swipe-right", 0x77),
])
def test_gestures_match_keyboard_state_and_undo_priority(gesture, key):
    for interaction, correction, undo in itertools.product((False, True), repeat=3):
        state = dict(interaction_active=interaction, correction_active=correction,
                     undo_active=undo)
        action = voice_actions_module.voice_action_for_gesture(gesture, **state)
        assert action == WindowsVoiceActionHotkeys._action_for_key(
            key, alt_down=False, **state
        )


@pytest.mark.parametrize("name", ["empty", "tap", "snap", "swipe-up", "swipe-down", "unknown", ""])
def test_other_gestures_do_not_trigger_actions(name):
    assert voice_actions_module.voice_action_for_gesture(
        name, interaction_active=True, correction_active=True, undo_active=True
    ) is None


def _action(
    key: int,
    *,
    alt: bool = True,
    interaction: bool = False,
    correction: bool = False,
):
    return WindowsVoiceActionHotkeys._action_for_key(
        key,
        alt_down=alt,
        interaction_active=interaction,
        correction_active=correction,
    )


def test_cancel_and_mode_correction_only_capture_during_an_interaction():
    assert ACTION_UNDO == "undo"
    assert _action(0x1B, alt=False, interaction=True) == ACTION_CANCEL
    assert _action(0x77, alt=False, correction=True) == ACTION_SWITCH_MODE
    assert _action(0x1B, alt=False) is None
    assert _action(0x09, alt=False) is None
    assert _action(0x09, alt=False, correction=True) is None
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x77,
            alt_down=True,
            correction_active=True,
        )
        is None
    )
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x77,
            alt_down=False,
            control_down=True,
            correction_active=True,
        )
        is None
    )
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x1B,
            alt_down=False,
            undo_active=True,
        )
        == ACTION_UNDO
    )
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x5A,
            alt_down=False,
            undo_active=True,
        )
        is None
    )
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x1B,
            alt_down=False,
            interaction_active=True,
            undo_active=True,
        )
        == ACTION_UNDO
    )


def test_configured_mode_switch_function_key_replaces_f8():
    assert normalize_mode_switch_shortcut("f7") == "F7"
    assert normalize_mode_switch_shortcut("Tab") == "F8"
    assert windows_mode_switch_key_code("F7") == 0x76
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x76,
            alt_down=False,
            mode_switch_key=windows_mode_switch_key_code("F7"),
            correction_active=True,
        )
        == ACTION_SWITCH_MODE
    )
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x77,
            alt_down=False,
            mode_switch_key=windows_mode_switch_key_code("F7"),
            correction_active=True,
        )
        is None
    )
