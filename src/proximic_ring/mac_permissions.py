"""Current-process macOS permission checks; no events, TCC resets or prompts by default."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


@dataclass(frozen=True)
class PermissionState:
    accessibility: bool | None = None
    post_events: bool | None = None
    error: str = ""

    @property
    def ready(self) -> bool:
        return self.accessibility is True and self.post_events is True

    @property
    def title(self) -> str:
        if self.ready:
            return "辅助功能权限已生效"
        if self.post_events is False and self.accessibility is True:
            return "按键控制权限尚未生效"
        if self.accessibility is False or self.post_events is False:
            return "辅助功能权限尚未生效"
        return "暂时无法确认辅助功能权限"

    @property
    def guidance(self) -> str:
        if self.ready:
            return "当前进程可读取辅助功能信息并发送按键。"
        return ("编辑、撤销和发送可能无法完成。授权后程序会自动重新检测，生效后即可继续操作。"
                "若持续未生效，请核对授权的是当前运行的应用；系统仍未放行时再退出重开。")


def read_permission_state(*, post_events: bool | None = None) -> PermissionState:
    errors = []
    accessibility = None
    try:
        import ApplicationServices
        accessibility = bool(ApplicationServices.AXIsProcessTrusted())
    except Exception as exc:
        errors.append("accessibility:" + type(exc).__name__)
    if post_events is None:
        try:
            import Quartz
            post_events = bool(Quartz.CGPreflightPostEventAccess())
        except Exception as exc:
            errors.append("post_events:" + type(exc).__name__)
    return PermissionState(accessibility, post_events, ";".join(errors))


class MacPermissionError(RuntimeError):
    def __init__(self, state: PermissionState):
        self.state = state
        super().__init__(state.title + "。授权后会自动重新检测，生效后请重新操作。")


def require_post_event_access() -> None:
    import Quartz
    # Recheck at the operation boundary, never trust a cached UI flag. AX trust
    # alone cannot override a denied event-posting preflight.
    if not Quartz.CGPreflightPostEventAccess():
        raise MacPermissionError(read_permission_state(post_events=False))


def request_post_event_access() -> None:
    """Only called by an explicit permission button, never by voice/gesture actions."""
    import Quartz
    Quartz.CGRequestPostEventAccess()


def running_identity() -> dict:
    executable = Path(sys.executable).resolve()
    frozen = bool(getattr(sys, "frozen", False))
    bundle = next((p for p in executable.parents if p.suffix == ".app"), None) if frozen else None
    path = str(bundle or executable)
    return {"frozen": frozen, "executable": str(executable), "app_path": str(bundle or ""),
            "temporary_location": bool(bundle and (path.startswith("/Volumes/") or "/AppTranslocation/" in path))}
