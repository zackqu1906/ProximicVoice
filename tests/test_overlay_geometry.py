from types import SimpleNamespace

from PySide6.QtCore import QPoint, QRect

from proximic_ring.desktop_target import DesktopTargetRef
from proximic_ring.ui.overlay_geometry import popup_screen


def test_popup_selects_screen_using_global_logical_coordinates():
    primary = SimpleNamespace(geometry=lambda: QRect(0, 0, 1440, 900))
    left = SimpleNamespace(geometry=lambda: QRect(-1920, 80, 1920, 1080))
    above = SimpleNamespace(geometry=lambda: QRect(0, -900, 1440, 900))
    screens = [primary, left, above]
    pointer = QPoint(10, 10)
    for x, y, expected in [(-1200, 400, left), (800, -300, above), (400, 500, primary)]:
        target = DesktopTargetRef(1, 2, caret_x=x, caret_y=y, caret_height=20)
        assert popup_screen(target, screens, pointer) is expected
    # Missing/off-screen caret: the text field determines the monitor.
    target = DesktopTargetRef(1, 2, screen_x=-1000, screen_y=300,
                             screen_width=600, screen_height=100,
                             caret_x=9999, caret_y=9999, caret_height=20)
    assert popup_screen(target, screens, pointer) is left
    assert popup_screen(None, screens, QPoint(100, -200)) is above
    assert popup_screen(None, [], pointer) is None
