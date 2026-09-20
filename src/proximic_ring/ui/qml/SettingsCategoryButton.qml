import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: button
    required property string description
    implicitHeight: 76
    leftPadding: 18; rightPadding: 18; topPadding: 12; bottomPadding: 12
    background: Rectangle {
        radius: 12
        color: button.down ? "#252F45" : button.hovered ? "#1D2637" : "#151B27"
        border.color: button.activeFocus ? "#7892FF" : "#283246"
        border.width: 1
    }
    contentItem: RowLayout {
        spacing: 12
        ColumnLayout {
            Layout.fillWidth: true; spacing: 5
            Label { text: button.text; color: "#F5F7FB"; font.pixelSize: 15; font.bold: true }
            Label {
                Layout.fillWidth: true
                text: button.description; color: "#A4AEC0"; font.pixelSize: 12
                wrapMode: Text.Wrap
            }
        }
        Label { text: "›"; color: "#A4AEC0"; font.pixelSize: 24 }
    }
}
