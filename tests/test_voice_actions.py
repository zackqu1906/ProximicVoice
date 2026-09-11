from proximic_ring.voice_actions import (
    ACTION_CANCEL,
    ACTION_SWITCH_MODE,
    ACTION_UNDO,
    MacOSVoiceActionHotkeys,
    WindowsVoiceActionHotkeys,
    macos_mode_switch_key_code,
    normalize_mode_switch_shortcut,
    windows_mode_switch_key_code,
)
import proximic_ring.voice_actions as voice_actions_module
import itertools
import pytest


@pytest.mark.parametrize("gesture,key,mac_key", [
    ("swipe-left", 0x1B, 53), ("swipe-down", 0x1B, 53),
    ("swipe-right", 0x77, 100), ("swipe-up", 0x77, 100),
])
def test_gestures_match_keyboard_state_and_undo_priority(gesture, key, mac_key):
    for interaction, correction, undo in itertools.product((False, True), repeat=3):
        state = dict(interaction_active=interaction, correction_active=correction,
                     undo_active=undo)
        action = voice_actions_module.voice_action_for_gesture(gesture, **state)
        assert action == WindowsVoiceActionHotkeys._action_for_key(
            key, alt_down=False, **state
        )
        assert action == MacOSVoiceActionHotkeys._action_for_key(mac_key, **state)


@pytest.mark.parametrize("name", ["empty", "tap", "snap", "unknown", ""])
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
    assert macos_mode_switch_key_code("F7") == 98
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
    assert (
        MacOSVoiceActionHotkeys._action_for_key(
            98,
            mode_switch_key=macos_mode_switch_key_code("F7"),
            correction_active=True,
        )
        == ACTION_SWITCH_MODE
    )
    assert (
        MacOSVoiceActionHotkeys._action_for_key(
            100,
            mode_switch_key=macos_mode_switch_key_code("F7"),
            correction_active=True,
        )
        is None
    )
def test_macos_only_captures_cancel_and_post_application_correction():
    action = MacOSVoiceActionHotkeys._action_for_key
    assert action(100, correction_active=True) == ACTION_SWITCH_MODE
    assert action(48, correction_active=True) is None
    assert action(100, command_down=True, correction_active=True) is None
    assert action(100, option_down=True, correction_active=True) is None
    assert action(53, interaction_active=True) == ACTION_CANCEL
    assert action(53, undo_active=True) == ACTION_UNDO
    assert action(6, command_down=True, undo_active=True) is None
    assert (
        action(53, interaction_active=True, undo_active=True)
        == ACTION_UNDO
    )
    assert action(36) is None
    assert action(76) is None


def test_macos_event_tap_consumes_escape_only_during_interaction(monkeypatch):
    import threading

    monkeypatch.setattr(voice_actions_module.sys, "platform", "darwin")
    stopped = threading.Event()

    class FakeCoreFoundation:
        kCFRunLoopCommonModes = object()

        @staticmethod
        def CFRunLoopGetCurrent():
            return "loop"

        @staticmethod
        def CFRunLoopAddSource(*_args):
            return None

        @staticmethod
        def CFRunLoopRun():
            stopped.wait(2.0)

        @staticmethod
        def CFRunLoopStop(_loop):
            stopped.set()

        @staticmethod
        def CFRunLoopWakeUp(_loop):
            return None

    class FakeQuartz:
        kCGEventTapDisabledByTimeout = -1
        kCGEventTapDisabledByUserInput = -2
        kCGEventKeyDown = 10
        kCGKeyboardEventKeycode = 20
        kCGKeyboardEventAutorepeat = 21
        kCGSessionEventTap = 30
        kCGHeadInsertEventTap = 31
        kCGEventTapOptionDefault = 32
        kCGEventFlagMaskCommand = 1 << 20
        kCGEventFlagMaskShift = 1 << 17
        callback = None

        @staticmethod
        def CGEventMaskBit(value):
            return 1 << value

        @classmethod
        def CGEventTapCreate(cls, *_args):
            cls.callback = _args[4]
            return "tap"

        @staticmethod
        def CFMachPortCreateRunLoopSource(*_args):
            return "source"

        @staticmethod
        def CGEventTapEnable(*_args):
            return None

        @staticmethod
        def CGEventGetIntegerValueField(event, field):
            return event["repeat"] if field == 21 else event["key"]

        @staticmethod
        def CGEventGetFlags(event):
            return event.get("flags", 0)

        @staticmethod
        def CFMachPortInvalidate(_tap):
            return None

    active = {"value": True}
    correction = {"value": False}
    shortcut = {"value": "F7"}
    actions = []
    hook = MacOSVoiceActionHotkeys(
        actions.append,
        mode_switch_shortcut=lambda: shortcut["value"],
        is_interaction_active=lambda: active["value"],
        is_mode_correction_active=lambda: correction["value"],
        quartz=FakeQuartz,
        core_foundation=FakeCoreFoundation,
    )
    event = {"key": 53, "repeat": 0}
    assert FakeQuartz.callback(None, 10, event, None) is None
    assert actions == [ACTION_CANCEL]
    active["value"] = False
    assert FakeQuartz.callback(None, 10, event, None) is event
    active["value"] = True
    repeated_letter = {"key": 0, "repeat": 1}
    assert FakeQuartz.callback(None, 10, repeated_letter, None) is repeated_letter
    assert actions == [ACTION_CANCEL]
    active["value"] = False
    correction["value"] = True
    f7_event = {"key": 98, "repeat": 0}
    f8_event = {"key": 100, "repeat": 0}
    assert FakeQuartz.callback(None, 10, f7_event, None) is None
    assert FakeQuartz.callback(None, 10, f8_event, None) is f8_event
    assert actions == [ACTION_CANCEL, ACTION_SWITCH_MODE]
    shortcut["value"] = "F9"
    f9_event = {"key": 101, "repeat": 0}
    assert FakeQuartz.callback(None, 10, f7_event, None) is f7_event
    assert FakeQuartz.callback(None, 10, f9_event, None) is None
    assert actions == [ACTION_CANCEL, ACTION_SWITCH_MODE, ACTION_SWITCH_MODE]
    hook.close()
