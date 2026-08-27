"""Collect only the QML modules used by Proximic Voice."""

from pathlib import PurePath

from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
qml_binaries, qml_datas = pyside6_library_info.collect_qtqml_files()

_ROOTS = {
    "PySide6/Qt/qml/QtCore",
    "PySide6/Qt/qml/QtQml",
    "PySide6/Qt/qml/QtQml/Models",
    "PySide6/Qt/qml/QtQml/WorkerScript",
    "PySide6/Qt/qml/QtQuick",
    "PySide6/Qt/qml/QtQuick/Controls",
    "PySide6/Qt/qml/QtQuick/Layouts",
    "PySide6/Qt/qml/QtQuick/Templates",
    "PySide6/Qt/qml/QtQuick/Window",
}


def _used_qml_module(item) -> bool:
    destination = PurePath(item[1]).as_posix()
    return any(
        destination == root or destination.startswith(root + "/")
        for root in _ROOTS
    )


binaries += [item for item in qml_binaries if _used_qml_module(item)]
datas += [item for item in qml_datas if _used_qml_module(item)]
