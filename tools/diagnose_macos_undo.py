#!/usr/bin/env python3
"""Observe undo-related macOS metadata, without reading or editing any text.

This diagnostic never posts keys, opens menus, reads AXValue, or accesses the
clipboard. A notification is evidence to inspect, not an undo acknowledgement.
"""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proximic_ring.desktop_target import _MacOSAccessibilityTextBridge


class MetadataObserver:
    def __init__(self, pid: int, emit):
        self.bridge = b = _MacOSAccessibilityTextBridge()
        self.pid = pid
        self.emit = emit
        self.observer = ctypes.c_void_p()
        self.application = None
        self.menu_owner = None
        self.menu_root = None
        self.menu_arrays = []
        self.commands = {}
        self.registrations = []
        self.source = None
        self.loop = None
        self.mode = None
        self.callback_type = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self.callback = self.callback_type(self._notification)
        s, cf = b._application_services, b._core_foundation
        s.AXObserverCreate.argtypes = (
            ctypes.c_int, self.callback_type, ctypes.POINTER(ctypes.c_void_p),
        )
        s.AXObserverCreate.restype = ctypes.c_int
        s.AXObserverAddNotification.argtypes = (ctypes.c_void_p,) * 4
        s.AXObserverAddNotification.restype = ctypes.c_int
        s.AXObserverRemoveNotification.argtypes = (ctypes.c_void_p,) * 3
        s.AXObserverRemoveNotification.restype = ctypes.c_int
        s.AXObserverGetRunLoopSource.argtypes = (ctypes.c_void_p,)
        s.AXObserverGetRunLoopSource.restype = ctypes.c_void_p
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopAddSource.argtypes = (ctypes.c_void_p,) * 3
        cf.CFRunLoopRemoveSource.argtypes = (ctypes.c_void_p,) * 3
        cf.CFRunLoopRunInMode.argtypes = (
            ctypes.c_void_p, ctypes.c_double, ctypes.c_bool,
        )
        cf.CFRunLoopRunInMode.restype = ctypes.c_int
        try:
            error = s.AXObserverCreate(pid, self.callback, ctypes.byref(self.observer))
            if error:
                raise RuntimeError(f"AXObserverCreate: {error}")
            self.application = s.AXUIElementCreateApplication(pid)
            for name in (
                "AXFocusedUIElementChanged", "AXValueChanged",
                "AXSelectedTextChanged",
            ):
                attribute = b._cf_string(name)
                error = s.AXObserverAddNotification(
                    self.observer, self.application, attribute, None,
                )
                emit({"type": "subscription", "name": name, "error": error})
                if error == 0:
                    self.registrations.append(attribute)
                else:
                    b._release(attribute)
            self.source = s.AXObserverGetRunLoopSource(self.observer)
            self.loop = cf.CFRunLoopGetCurrent()
            self.mode = b._cf_string("kCFRunLoopDefaultMode")
            cf.CFRunLoopAddSource(self.loop, self.source, self.mode)
            self._find_commands()
        except BaseException:
            self.close()
            raise

    def _notification(self, _observer, element, notification, _context):
        # Do not query AXValue or notification userInfo, which may contain text.
        try:
            self.emit({
                "type": "notification",
                "name": self.bridge._cf_text(notification),
                "element_id": int(self.bridge._core_foundation.CFHash(element)),
            })
        except Exception:
            pass  # Never let Python exceptions escape a native callback.

    def _find_commands(self):
        b = self.bridge
        root = b._copy_application_element(self.pid, "AXMenuBar")
        if root is None:
            return
        self.menu_owner, self.menu_root = root
        pending = [(self.menu_root, 0)]
        seen = set()
        while pending and len(seen) < 256:
            element, depth = pending.pop(0)
            if element in seen:
                continue
            seen.add(element)
            # Match shortcut metadata, never menu labels or input contents.
            key = b._copy_text_attribute(element, "AXMenuItemCmdChar")
            if key and key.casefold() == "z":
                value = b._copy_attribute_value(element, "AXMenuItemCmdModifiers")
                try:
                    modifiers = b._cf_number(value)
                finally:
                    b._release(value)
                if modifiers in {0, 1}:
                    self.commands["redo" if modifiers == 1 else "undo"] = element
            if depth < 7:
                children, array = b._copy_recent_children(element)
                if array:
                    self.menu_arrays.append(array)
                pending.extend((child, depth + 1) for child in children)

    def snapshot(self):
        b = self.bridge
        focus = []
        elements = b._copy_focused_elements(self.pid)
        try:
            for _owner, element, source in elements:
                focus.append({
                    "source": source,
                    "role": b._copy_text_attribute(element, "AXRole"),
                    "editable": b._is_editable_text_element(element),
                    "id": int(b._core_foundation.CFHash(element)),
                })
        finally:
            for owner, element, _source in elements:
                b._release(element, owner)
        commands = {}
        for command, element in self.commands.items():
            value = b._copy_attribute_value(element, "AXEnabled")
            try:
                cf = b._core_foundation
                commands[command] = (
                    bool(cf.CFBooleanGetValue(value))
                    if value and cf.CFGetTypeID(value) == cf.CFBooleanGetTypeID()
                    else None
                )
            finally:
                b._release(value)
        return {"focus": focus, "commands_enabled": commands}

    def pump(self, seconds):
        self.bridge._core_foundation.CFRunLoopRunInMode(self.mode, seconds, False)

    def close(self):
        b = self.bridge
        if self.loop and self.source and self.mode:
            b._core_foundation.CFRunLoopRemoveSource(self.loop, self.source, self.mode)
        for attribute in self.registrations:
            b._application_services.AXObserverRemoveNotification(
                self.observer, self.application, attribute,
            )
            b._release(attribute)
        self.registrations.clear()
        b._release(
            *reversed(self.menu_arrays), self.menu_root, self.menu_owner,
            self.mode, self.application, self.observer.value,
        )
        self.menu_arrays.clear()
        self.menu_root = self.menu_owner = self.mode = self.application = None
        self.source = self.loop = None
        self.observer = ctypes.c_void_p()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="微信主进程 PID；默认自动发现")
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--output", type=Path, help="保存元数据 JSONL")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("此工具只支持 macOS")
    from AppKit import NSWorkspace

    pid = args.pid
    if pid is None:
        matches = [
            app for app in NSWorkspace.sharedWorkspace().runningApplications()
            if str(app.localizedName() or "").casefold() in {"wechat", "weixin", "微信"}
        ]
        primary = [app for app in matches if int(app.activationPolicy()) == 0]
        if len(primary) != 1:
            parser.error("无法唯一识别微信主进程，请用 --pid 指定")
        pid = int(primary[0].processIdentifier())
    output = args.output.open("w", encoding="utf-8") if args.output else None
    started = time.monotonic()

    def emit(value):
        line = json.dumps({"elapsed_s": round(time.monotonic() - started, 3), **value}, ensure_ascii=False)
        print(line, flush=True)
        if output:
            output.write(line + "\n")
            output.flush()

    observer = None
    try:
        emit({"type": "start", "pid": pid, "text_reading": False, "key_posting": False})
        observer = MetadataObserver(pid, emit)
        previous = None
        last_heartbeat = 0.0
        while time.monotonic() - started < args.seconds:
            observer.pump(0.2)
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            front_pid = int(app.processIdentifier()) if app else None
            state = {"frontmost_pid": front_pid, **observer.snapshot()}
            elapsed = time.monotonic() - started
            if state != previous or elapsed - last_heartbeat >= 5:
                emit({"type": "state", **state})
                previous, last_heartbeat = state, elapsed
    except KeyboardInterrupt:
        pass
    finally:
        if observer:
            observer.close()
        emit({"type": "finished"})
        if output:
            output.close()


if __name__ == "__main__":
    main()
