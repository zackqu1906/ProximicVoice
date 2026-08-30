"""Collect only the QML modules used by Proximic Voice."""

from pathlib import PurePath

from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
qml_binaries, qml_datas = pyside6_library_info.collect_qtqml_files()

_ROOTS = {
    # PyInstaller's destination layout for current PySide6 releases is
    # ``PySide6/qml`` (the source installation also uses that layout).
    # Using ``PySide6/Qt/qml`` silently filters every QML module out.
    "PySide6/qml/QtCore",
    "PySide6/qml/QtQml",
    "PySide6/qml/QtQuick",
}


def _used_qml_module(item) -> bool:
    destination = PurePath(item[1]).as_posix()
    return any(
        destination == root or destination.startswith(root + "/")
        for root in _ROOTS
    )


selected_qml_binaries = [item for item in qml_binaries if _used_qml_module(item)]
selected_qml_datas = [item for item in qml_datas if _used_qml_module(item)]
if not selected_qml_binaries or not selected_qml_datas:
    raise RuntimeError(
        "PySide6 QML collection produced no QtQuick runtime files; "
        "check the PyInstaller destination layout."
    )

binaries += selected_qml_binaries
datas += selected_qml_datas
