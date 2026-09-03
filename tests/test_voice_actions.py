from proximic_ring.voice_actions import (
    ACTION_CONFIRM,
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
    review: bool = False,
    interaction: bool = False,
    correction: bool = False,
):
    return WindowsVoiceActionHotkeys._action_for_key(
        key,
        alt_down=alt,
        review_active=review,
        interaction_active=interaction,
        correction_active=correction,
    )


def test_cancel_and_mode_correction_only_capture_during_an_interaction():
    assert ACTION_UNDO == "undo"
    assert _action(0x1B, alt=False, interaction=True) == ACTION_CANCEL
    assert _action(0x09, alt=False, correction=True) == ACTION_SWITCH_MODE
    assert _action(0x1B, alt=False) is None
    assert _action(0x09, alt=False) is None


def test_macos_review_keys_only_act_while_review_is_visible():
    action = MacOSVoiceActionHotkeys._action_for_key
    assert action(36, review_active=True) == ACTION_CONFIRM
    assert action(76, review_active=True) == ACTION_CONFIRM
    assert action(53, review_active=True) == "cancel"
    assert (
        action(48, review_active=False, correction_active=True)
        == ACTION_SWITCH_MODE
    )
    assert action(53, review_active=False, interaction_active=True) == ACTION_CANCEL
    assert action(36, review_active=False) is None
    assert action(0, review_active=True) is None


def test_macos_event_tap_consumes_enter_and_escape_only_during_review(monkeypatch):
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
        def CFMachPortInvalidate(_tap):
            return None

    active = {"value": True}
    actions = []
    hook = MacOSVoiceActionHotkeys(
        actions.append,
        is_review_active=lambda: active["value"],
        quartz=FakeQuartz,
        core_foundation=FakeCoreFoundation,
    )
    event = {"key": 36, "repeat": 0}
    assert FakeQuartz.callback(None, 10, event, None) is None
    assert actions == [ACTION_CONFIRM]
    active["value"] = False
    assert FakeQuartz.callback(None, 10, event, None) is event
    hook.close()
