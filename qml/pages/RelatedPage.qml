import QtQuick 2.0
import Sailfish.Silica 1.0
import "../logic.js" as Logic

// Recommendations for a video — its YouTube autoplay Mix, pulled flat by the engine (no InnerTube).
// Reached from a video's pulldown ("Related videos"). Tap a row to watch it; long-press for the
// same per-video actions as everywhere else.
Page {
    id: page
    allowedOrientations: Orientation.All

    property string videoId: ""
    property string seedTitle: ""
    property bool loading: true
    property string errorText: ""

    ListModel { id: relModel }

    function fmtDur(sec) { return Logic.fmtDur(sec) }
    function fmtCount(n) { return Logic.fmtCount(n) }

    function load() {
        page.loading = true
        page.errorText = ""
        app.backend.relatedVideos(page.videoId, function(res) {
            if (!page)          // left before the mix landed → the page (and relModel) are gone
                return
            page.loading = false
            relModel.clear()
            if (!res || !res.ok) {
                page.errorText = (res && res.error) ? res.error : qsTr("no recommendations")
                return
            }
            var items = res.items || []
            for (var i = 0; i < items.length; i++)
                relModel.append(items[i])
        })
    }
    Component.onCompleted: load()

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: relModel

        header: PageHeader {
            title: qsTr("Related")
            description: page.seedTitle
        }

        PullDownMenu {
            MenuItem {
                text: qsTr("Reload")
                onClicked: page.load()
            }
        }

        delegate: ListItem {
            id: item
            width: listView.width
            property string vidId: model.id
            property string vidTitle: model.title
            property string vidUploader: model.uploader || ""
            property real vidDuration: model.duration || 0
            property string vidThumb: model.thumbnail || ""
            property real thumbW: Math.round(width * 0.42)
            property real thumbH: Math.round(thumbW * 9 / 16)
            contentHeight: thumbH + 2 * Theme.paddingSmall

            menu: ContextMenu {
                MenuItem {
                    text: (app.backend.watchMap && app.backend.watchMap[item.vidId]
                           && app.backend.watchMap[item.vidId].w) ? qsTr("Mark as unwatched") : qsTr("Mark as watched")
                    onClicked: {
                        var e = app.backend.watchMap ? app.backend.watchMap[item.vidId] : undefined
                        app.backend.setWatched(item.vidId, !(e && e.w), item.vidTitle, item.vidUploader)
                    }
                }
                MenuItem {
                    text: qsTr("Video details")
                    onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                        { videoId: item.vidId, title: item.vidTitle, infoOnly: true,
                          infoThumbnail: item.vidThumb })
                }
                MenuItem {
                    text: qsTr("Add to playlist")
                    onClicked: pageStack.push(Qt.resolvedUrl("AddToPlaylistPage.qml"),
                        { video: { id: item.vidId, title: item.vidTitle, uploader: item.vidUploader,
                                   duration: item.vidDuration, thumbnail: item.vidThumb } })
                }
                MenuItem {
                    text: qsTr("Download audio")
                    onClicked: Remorse.popupAction(page, qsTr("Downloading audio"),
                        function() { app.backend.download(item.vidId, item.vidTitle, "audio") })
                }
                MenuItem {
                    text: qsTr("Download video")
                    onClicked: Remorse.popupAction(page, qsTr("Downloading video"),
                        function() { app.backend.download(item.vidId, item.vidTitle, "video") })
                }
            }

            Rectangle {
                id: vThumb
                anchors {
                    left: parent.left; leftMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                width: item.thumbW; height: item.thumbH
                radius: Theme.paddingSmall / 2
                color: Theme.rgba(Theme.secondaryColor, 0.2)
                clip: true
                Image {
                    anchors.fill: parent
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    sourceSize: Qt.size(parent.width, parent.height)   // #9: decode to the display box, not full res
                    source: model.thumbnail || ""
                }
                WatchOverlay { anchors.fill: parent; videoId: model.id || ""; live: !!model.live }
                Rectangle {
                    visible: model.duration > 0
                    anchors { right: parent.right; bottom: parent.bottom; margins: Theme.paddingSmall / 2 }
                    radius: 3
                    color: "#C8000000"
                    width: durLabel.width + Theme.paddingSmall
                    height: durLabel.height + Theme.paddingSmall / 2
                    Label { id: durLabel; anchors.centerIn: parent; text: page.fmtDur(model.duration)
                            color: "white"; font.pixelSize: Theme.fontSizeExtraSmall }
                }
            }
            Column {
                anchors {
                    left: vThumb.right; leftMargin: Theme.paddingMedium
                    right: parent.right; rightMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                Label {
                    width: parent.width
                    text: model.title
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                    font.pixelSize: Theme.fontSizeSmall
                }
                Label {
                    width: parent.width
                    visible: text.length > 0
                    text: model.uploader || ""
                    truncationMode: TruncationMode.Fade
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
                Label {
                    width: parent.width
                    visible: text.length > 0
                    // views · age (age is often absent on mix rows — then just views)
                    text: {
                        var parts = []
                        if (model.views > 0) parts.push(qsTr("%1 views").arg(page.fmtCount(model.views)))
                        if (model.posted) parts.push(model.posted)
                        return parts.join("  ·  ")
                    }
                    truncationMode: TruncationMode.Fade
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }

            onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                { videoId: model.id, title: model.title })
        }

        ViewPlaceholder {
            enabled: !page.loading && relModel.count === 0
            text: qsTr("Nothing related")
            hintText: page.errorText
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading && relModel.count === 0
    }
}
