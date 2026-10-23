import QtQuick 2.0
import Sailfish.Silica 1.0
import "../logic.js" as Logic

// 10-band equalizer. Bands map to equalizer-10bands' fixed centre frequencies. Adjusting a band
// previews live on the active player; the value is persisted (and re-applied across videos) on
// release. Presets set all ten at once.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock / resume bar over the equalizer

    readonly property var freqLabels: ["29 Hz", "59 Hz", "119 Hz", "237 Hz", "474 Hz",
                                       "947 Hz", "1.9 kHz", "3.8 kHz", "7.5 kHz", "15 kHz"]
    // Preset names, in the order of the curves in Logic.EQ_PRESETS. "Flat" (all zero) is index 0.
    readonly property var presetNames: [qsTr("Flat"), qsTr("Bass boost"), qsTr("Treble boost"), qsTr("Rock"),
                                        qsTr("Pop"), qsTr("Vocal"), qsTr("Electronic")]

    function commitBand(i, v) {
        var b = app.backend.eqBands.slice()
        b[i] = v
        app.backend.setEqBands(b)
    }
    function applyPreset(idx) {
        var p = Logic.eqPreset(idx, app.backend.eqEnabled)
        if (!p) return
        app.backend.setEqBands(p.bands)
        if (p.enable)
            app.backend.setEqEnabled(true)          // picking a curve implies you want it on
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: qsTr("Equalizer") }

            TextSwitch {
                text: qsTr("Equalizer")
                description: qsTr("Apply the band gains below to playback.")
                automaticCheck: false
                checked: app.backend.eqEnabled
                onClicked: app.backend.setEqEnabled(!app.backend.eqEnabled)
            }

            ComboBox {
                width: parent.width
                label: qsTr("Preset")
                // Apply on the item's own click (not currentIndex, which fires on load and would
                // reset the saved bands to Flat every time the page opens).
                menu: ContextMenu {
                    Repeater {
                        model: page.presetNames
                        MenuItem {
                            text: modelData
                            onClicked: page.applyPreset(index)
                        }
                    }
                }
            }

            Item { width: 1; height: Theme.paddingMedium }

            Repeater {
                model: 10
                delegate: Slider {
                    width: col.width
                    // Musical range; the engine clamps to -24..+12 regardless.
                    minimumValue: -12
                    maximumValue: 12
                    stepSize: 1
                    enabled: app.backend.eqEnabled
                    value: app.backend.eqBands[index] || 0
                    label: page.freqLabels[index]
                    valueText: (value > 0 ? "+" : "") + Math.round(value) + " dB"
                    // Live preview while dragging; persist + re-establish the binding on release.
                    onValueChanged: if (app.activePlayer) app.activePlayer.setEqBand(index, value)
                    onReleased: {
                        page.commitBand(index, value)
                        value = Qt.binding(function() { return app.backend.eqBands[index] || 0 })
                    }
                }
            }
        }

        VerticalScrollDecorator { }
    }
}
