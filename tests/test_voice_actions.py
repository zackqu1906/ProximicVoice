from proximic_ring.voice_actions import (
    ACTION_CONFIRM,
    ACTION_REASON_ASR_ERROR,
    ACTION_REASON_LLM_ERROR,
    ACTION_REASON_OTHER,
    MacOSVoiceActionHotkeys,
    WindowsVoiceActionHotkeys,
)
import proximic_ring.voice_actions as voice_actions_module


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


def test_macos_review_keys_only_act_while_review_is_visible():
    action = MacOSVoiceActionHotkeys._action_for_key
    assert action(36, review_active=True) == ACTION_CONFIRM
    assert action(76, review_active=True) == ACTION_CONFIRM
    assert action(53, review_active=True) == "cancel"
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
