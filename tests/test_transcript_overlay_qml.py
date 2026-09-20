from pathlib import Path
from types import SimpleNamespace

import pytest


def _wait_for_layout(predicate):
    from PySide6.QtTest import QTest

    for _ in range(50):
        if predicate():
            return
        QTest.qWait(20)
    assert predicate(), "QML layout did not settle within one second"


@pytest.fixture
def overlay_ui(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem, QQuickWindow
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import proximic_ring.ui.controller as module

    app = QCoreApplication.instance()
    if app is not None and not isinstance(app, QApplication):
        pytest.skip("QML needs its own QApplication process")
    app = app or QApplication(["transcript-layout", "-platform", "offscreen"])
    monkeypatch.setattr(module, "app_data_root", lambda: tmp_path)
    # Native permission polling is unrelated to layout and can stall Qt's
    # animation clock in this offscreen test process.
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    controller = module.AppController(inline_input_enabled=False)
    controller._text_processing_worker.close(wait=True)
    monkeypatch.setattr(controller, "_capture_desktop_reference", lambda: None)
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda values: warnings.extend(str(value) for value in values))
    engine.rootContext().setContextProperty("appController", controller)
    engine.load(QUrl.fromLocalFile(str(Path(module.__file__).parent / "qml/Main.qml")))
    root = engine.rootObjects()[0]
    root.hide()
    overlay = root.findChild(QQuickWindow, "transcriptOverlay")
    text = root.findChild(QQuickItem, "asrOverlayText")
    viewport = root.findChild(QQuickItem, "transcriptViewport")
    cancel = root.findChild(QQuickItem, "cancelUtteranceButton")

    def publish(value):
        controller._apply_runtime_update(value, False, "", 1)
        app.processEvents()

    controller._recognition_enabled = True
    controller._apply_runtime_status("[ASR] START t=1.000s")
    publish("这是第一句。")
    QTest.qWait(240)
    try:
        yield SimpleNamespace(
            app=app, root=root, controller=controller, overlay=overlay,
            text=text, viewport=viewport, cancel=cancel, publish=publish,
        )
        assert not warnings
    finally:
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        controller._close_voice_history()


def test_streaming_text_wraps_and_expands_smoothly_from_fixed_bottom(overlay_ui):
    from PySide6.QtCore import QPointF
    from PySide6.QtTest import QTest

    ui = overlay_ui
    bottom = ui.overlay.y() + ui.overlay.height()
    button_y = ui.overlay.y() + ui.cancel.mapToScene(QPointF()).y()
    assert ui.overlay.height() == 64
    assert ui.text.property("lineCount") == 1
    long_text = "今天的会议讨论了多个方案，我希望把每个想法都完整记录下来，以便明天继续讨论。" * 3
    ui.publish(long_text)
    target = ui.overlay.property("naturalHeight")
    assert 64 < target < ui.overlay.property("maximumContentHeight")
    QTest.qWait(40)
    assert 64 < ui.overlay.height() < target
    assert ui.overlay.y() + ui.overlay.height() == bottom
    assert abs(ui.overlay.y() + ui.cancel.mapToScene(QPointF()).y() - button_y) <= 1
    _wait_for_layout(lambda: abs(ui.overlay.height() - target) <= 1)
    assert abs(ui.overlay.height() - target) <= 1
    assert ui.text.property("text") == long_text
    assert ui.text.property("lineCount") > 1
    assert not ui.text.property("truncated")
    assert ui.text.height() <= ui.viewport.height() + 1
    assert ui.text.property("contentWidth") <= ui.text.width() + 1
    assert ui.overlay.y() + ui.overlay.height() == bottom

    # Recognizer corrections can shorten text; collapse is animated too.
    ui.publish("修正后的短句。")
    QTest.qWait(40)
    assert 64 < ui.overlay.height() < target
    _wait_for_layout(lambda: ui.overlay.height() == 64)
    assert ui.overlay.height() == 64
    assert ui.overlay.y() + ui.overlay.height() == bottom
    assert ui.viewport.property("contentY") == 0


def test_very_long_transcript_remains_complete_and_scrollable(overlay_ui):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest

    ui = overlay_ui
    # Plain-text rendering must preserve literal markup and long tokens too.
    long_text = "<b>原样显示</b> " + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 8 + "\n" + "完整保留这段很长的语音内容。" * 120
    ui.publish(long_text)
    QTest.qWait(240)
    assert ui.text.property("text") == long_text
    assert not ui.text.property("truncated")
    assert ui.text.property("contentWidth") <= ui.text.width() + 1
    assert ui.overlay.property("textOverflows")
    assert ui.viewport.property("interactive")
    assert ui.overlay.height() == ui.overlay.property("maximumContentHeight")
    assert ui.overlay.y() >= 12
    assert ui.overlay.y() + ui.overlay.height() <= ui.overlay.screen().availableGeometry().bottom() + 1
    assert ui.viewport.property("atYEnd")

    # Exercise wheel input on the non-focus-stealing window, not only a
    # programmatic scrollbar position change.
    pointer = ui.viewport.mapToScene(QPointF(ui.viewport.width() / 2, ui.viewport.height() / 2))
    wheel = QWheelEvent(
        pointer, pointer + QPointF(ui.overlay.x(), ui.overlay.y()),
        QPoint(0, 100), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
        Qt.ScrollUpdate, False,
    )
    ui.app.sendEvent(ui.overlay, wheel)
    QTest.qWait(50)
    assert not ui.viewport.property("atYEnd")
    assert not ui.viewport.property("followTail")

    # Reading earlier lines must not be interrupted by the next ASR update.
    ui.viewport.setProperty("contentY", 100)
    ui.app.processEvents()
    assert not ui.viewport.property("followTail")
    ui.publish(long_text + "继续补充的最后一句。")
    QTest.qWait(50)
    assert ui.viewport.property("contentY") == 100
    ui.viewport.setProperty("contentY", ui.viewport.property("contentHeight") - ui.viewport.height())
    ui.app.processEvents()
    assert ui.viewport.property("followTail")
    ui.publish(long_text + "继续补充的最后一句。" * 8)
    QTest.qWait(50)
    assert ui.viewport.property("atYEnd")
    ui.publish("新的一句。")
    QTest.qWait(240)
    assert ui.overlay.height() == 64
    assert not ui.viewport.property("interactive")
    assert ui.viewport.property("contentY") == 0


def test_llm_indicator_is_visible_only_for_selected_text_processing(overlay_ui, tmp_path):
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AutoInteraction, _PendingInteraction

    ui = overlay_ui
    controller = ui.controller
    spinner = ui.root.findChild(QQuickItem, "llmProcessingSpinner")
    status = ui.root.findChild(QQuickItem, "statusOverlayText")
    assert not spinner.property("visible")
    assert "LLM" not in status.property("text")

    controller._latest_asr_session_id = 1
    controller._pending_text_requests.update((11, 12))
    controller._pending_interactions.update({
        11: _PendingInteraction(auto_route_id=10, session_id=1),
        12: _PendingInteraction(auto_route_id=10, session_id=1),
    })
    interaction = _AutoInteraction(
        route_id=10, session_id=1, raw_text="把这段话润色得更自然", target=None,
        selected_mode="edit", routed_at=0,
        request_ids={"edit": 11, "dictation": 12},
    )
    controller._active_auto_interaction = interaction
    controller._transcript_text = "正在判断听写或指令"
    controller._set_interaction_state("processing")
    controller.transcriptChanged.emit()
    ui.app.processEvents()
    assert not spinner.property("visible")
    assert status.property("text") == "正在判断听写或指令"

    interaction.classified = True
    controller._transcript_text = controller._processing_overlay_text("edit", interaction.raw_text)
    controller._transcript_primary_text = interaction.raw_text
    controller.transcriptChanged.emit()
    QTest.qWait(240)
    assert spinner.property("visible") and spinner.property("running")
    assert status.property("text") == "LLM 正在处理文本…"
    assert status.property("font").bold()
    assert status.property("font").pixelSize() == 14
    assert not status.property("truncated")
    assert ui.text.property("text") == interaction.raw_text
    screenshot = ui.overlay.grabWindow()
    assert not screenshot.isNull()
    assert screenshot.save(str(tmp_path / "llm-processing-overlay.png"))

    # The other candidate is still running, but the selected one has returned.
    # Keep preparing=True so this layout test does not write to a real app.
    controller._apply_text_processed(TextProcessingResult(
        11, 1, "edit", interaction.raw_text, "修改后的内容", 0.2, True,
        target_text="原文",
    ))
    ui.app.processEvents()
    assert controller._pending_text_requests == {12}
    assert not spinner.property("visible") and not spinner.property("running")
    assert "LLM" not in status.property("text")
