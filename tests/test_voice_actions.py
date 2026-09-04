from proximic_ring.voice_actions import (
    ACTION_CANCEL,
    ACTION_SWITCH_MODE,
    ACTION_UNDO,
    MacOSVoiceActionHotkeys,
    WindowsVoiceActionHotkeys,
)
import proximic_ring.voice_actions as voice_actions_module


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
    assert _action(0x09, alt=False, correction=True) == ACTION_SWITCH_MODE
    assert _action(0x1B, alt=False) is None
    assert _action(0x09, alt=False) is None
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x5A,
            alt_down=False,
            control_down=True,
            undo_active=True,
        )
        == ACTION_UNDO
    )
    assert (
        WindowsVoiceActionHotkeys._action_for_key(
            0x5A,
            alt_down=False,
            control_down=True,
            shift_down=True,
            undo_active=True,
        )
        is None
    )


def test_macos_only_captures_cancel_and_post_application_correction():
    action = MacOSVoiceActionHotkeys._action_for_key
    assert action(48, correction_active=True) == ACTION_SWITCH_MODE
    assert action(53, interaction_active=True) == ACTION_CANCEL
    assert action(6, command_down=True, undo_active=True) == ACTION_UNDO
    assert action(6, command_down=True, shift_down=True, undo_active=True) is None
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
    actions = []
    hook = MacOSVoiceActionHotkeys(
        actions.append,
        is_interaction_active=lambda: active["value"],
        quartz=FakeQuartz,
        core_foundation=FakeCoreFoundation,
    )
    event = {"key": 53, "repeat": 0}
    assert FakeQuartz.callback(None, 10, event, None) is None
    assert actions == [ACTION_CANCEL]
    active["value"] = False
    assert FakeQuartz.callback(None, 10, event, None) is event
    hook.close()
