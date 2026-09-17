"""Choose a popup's monitor in global logical coordinates, including its origin."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect


def popup_screen(target, screens, fallback_point: QPoint):
    if not screens:
        return None
    if target is not None:
        if target.caret_height > 0:
            point = QPoint(target.caret_x, target.caret_y)
            for screen in screens:
                if screen.geometry().contains(point):
                    return screen
        field = QRect(
            target.screen_x, target.screen_y,
            target.screen_width, target.screen_height,
        )
        if not field.isEmpty():
            def overlap(screen):
                rect = field.intersected(screen.geometry())
                return max(0, rect.width()) * max(0, rect.height())
            best = max(screens, key=overlap)
            if overlap(best) > 0:
                return best
    return next(
        (screen for screen in screens if screen.geometry().contains(fallback_point)),
        screens[0],
    )
