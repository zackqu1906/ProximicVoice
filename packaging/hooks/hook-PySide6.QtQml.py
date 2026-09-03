"""Collect only the QML modules used by Proximic Voice."""

from pathlib import PurePath

from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
qml_binaries, qml_datas = pyside6_library_info.collect_qtqml_files()

_ROOTS = {
    # PyInstaller 6.22 follows the PySide6 6.11 wheel layout. Keep the QtCore,
    # QtQml, and QtQuick trees because Controls styles import private helpers
    # below these roots at runtime.
    "PySide6/Qt/qml/QtCore",
    "PySide6/Qt/qml/QtQml",
    "PySide6/Qt/qml/QtQuick",
}

_EXCLUDED_QML_SUBTREES = {
    "PySide6/Qt/qml/QtQml/StateMachine",
    "PySide6/Qt/qml/QtQml/XmlListModel",
    # The UI imports QtQuick, Window, Layouts and Material Controls only.
    # These optional modules pull in sizeable, otherwise unused Qt frameworks.
    "PySide6/Qt/qml/QtQuick/Dialogs",
    "PySide6/Qt/qml/QtQuick/Effects",
    "PySide6/Qt/qml/QtQuick/LocalStorage",
    "PySide6/Qt/qml/QtQuick/NativeStyle",
    "PySide6/Qt/qml/QtQuick/Particles",
    "PySide6/Qt/qml/QtQuick/Pdf",
    "PySide6/Qt/qml/QtQuick/Scene2D",
    "PySide6/Qt/qml/QtQuick/Scene3D",
    "PySide6/Qt/qml/QtQuick/Shapes",
    "PySide6/Qt/qml/QtQuick/Timeline",
    "PySide6/Qt/qml/QtQuick/VectorImage",
    "PySide6/Qt/qml/QtQuick/VirtualKeyboard",
    "PySide6/Qt/qml/QtQuick/tooling",
    # Material is selected explicitly. Basic stays as Qt's lightweight
    # fallback, while platform and alternate visual styles are not shipped.
    "PySide6/Qt/qml/QtQuick/Controls/FluentWinUI3",
    "PySide6/Qt/qml/QtQuick/Controls/Fusion",
    "PySide6/Qt/qml/QtQuick/Controls/Imagine",
    "PySide6/Qt/qml/QtQuick/Controls/Universal",
    "PySide6/Qt/qml/QtQuick/Controls/iOS",
    "PySide6/Qt/qml/QtQuick/Controls/macOS",
    "PySide6/Qt/qml/QtQuick/Controls/designer",
}


def _used_qml_module(item) -> bool:
    destination = PurePath(item[1]).as_posix()
    belongs_to_used_root = any(
        destination == root or destination.startswith(root + "/")
        for root in _ROOTS
    )
    belongs_to_excluded_subtree = any(
        destination == root or destination.startswith(root + "/")
        for root in _EXCLUDED_QML_SUBTREES
    )
    return belongs_to_used_root and not belongs_to_excluded_subtree


selected_qml_binaries = [item for item in qml_binaries if _used_qml_module(item)]
selected_qml_datas = [item for item in qml_datas if _used_qml_module(item)]
if not selected_qml_binaries or not selected_qml_datas:
    raise RuntimeError(
        "PySide6 QML collection produced no QtQuick runtime files; "
        "check the PyInstaller destination layout."
    )

binaries += selected_qml_binaries
datas += selected_qml_datas
