import QtQuick 2.0
import Sailfish.Silica 1.0

// Providers — the third-party tools the app manages for you: yt-dlp (the extractor), ffmpeg (HD
// download merging) and the optional PO-token provider (Deno sidecar) that unlocks full quality.
// Split out of Settings to keep that panel focused on playback. Reached from Home → More → Providers.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock / resume bar over Providers

    // Re-read provider status each time the page is shown, and nudge the sidecar up if it isn't
    // answering yet — so the line goes from "starting…" to "working" on its own, without needing a
    // played video or a diagnostics run to kick it. (The startup-cached status is often stale here.)
    onStatusChanged: {
        if (status === PageStatus.Active) {
            app.backend.loadPotStatus()
            if (app.backend.potEnabled && !app.backend.potResponding)
                app.backend.startPotProvider()
        }
    }

    // While the provider is enabled but not answering (and hasn't reported an error), poll so
    // "starting…" flips to "working" the moment the sidecar comes up. The binding stops it as soon
    // as it responds or a real error appears, so it never spins indefinitely.
    Timer {
        interval: 1200
        repeat: true
        running: page.status === PageStatus.Active && app.backend.potEnabled
                 && !app.backend.potResponding && app.backend.potLastError.length === 0
        onTriggered: app.backend.loadPotStatus()
    }

    property string ytdlpStatus: ""
    property bool diagRunning: false

    Connections {
        target: app.backend
        onUpdateFinished: page.ytdlpStatus = message
    }

    // PO-provider status line — kept as functions so the many states stay readable. Both read the
    // backend properties directly, so QML re-binds them whenever the status refreshes.
    function potStateText() {
        var b = app.backend
        if (b.potInstalling) return b.potStatusMsg || qsTr("Setting up…")
        if (!b.potDeno) return qsTr("Deno runtime not found. Tap Download Deno below.")
        if (!b.potInstalled) return qsTr("Provider not installed.")
        if (!b.potEnabled) return qsTr("Installed · off")
        if (b.potResponding)
            return b.potServerVersion ? qsTr("Installed · working (v%1)").arg(b.potServerVersion)
                                      : qsTr("Installed · working")
        // A real failure sets potLastError (shown in full just below) → say so and colour it red.
        // Otherwise the server is simply still coming up: a calm "starting…", not an alarming error.
        if (b.potLastError) return qsTr("Installed · on — not working (see below)")
        return qsTr("Installed · on — starting…")
    }
    function potStateColor() {
        var b = app.backend
        if (b.potInstalling) return Theme.secondaryColor
        if (!b.potDeno) return Theme.errorColor
        if (b.potEnabled && b.potResponding) return Theme.secondaryHighlightColor
        if (b.potEnabled && b.potLastError) return Theme.errorColor   // red only on a genuine error
        return Theme.secondaryColor                                    // "starting…" etc.: neutral
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: qsTr("Providers") }

            // Setup first: without yt-dlp nothing plays, so it leads the panel.
            SectionHeader { text: "yt-dlp" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.installing
                      ? qsTr("Downloading yt-dlp… %1%").arg(Math.round(app.backend.installPct))
                      : (app.backend.ready
                         ? qsTr("Installed — version %1").arg(app.backend.ytdlpVersion)
                         : qsTr("Not found. Tap Download to fetch a self-contained copy into the "
                              + "app's own folder (kept independent of any system yt-dlp)."))
                color: app.backend.ready ? Theme.secondaryHighlightColor : Theme.errorColor
                font.pixelSize: Theme.fontSizeSmall
            }

            Label {
                visible: page.ytdlpStatus.length > 0
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: page.ytdlpStatus
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Primary actions — at most two side by side so the labels never clip. The
            // left button is context-aware: Download when yt-dlp is missing, Update when
            // it's present (and it shows the % while a download runs, whatever started it).
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.paddingMedium

                Button {
                    text: app.backend.installing
                          ? (Math.round(app.backend.installPct) + "%")
                          : (!app.backend.ready ? qsTr("Download")
                             : (app.backend.updating ? qsTr("Updating…") : qsTr("Update")))
                    enabled: !app.backend.installing && !app.backend.updating
                    onClicked: {
                        if (!app.backend.ready) {
                            page.ytdlpStatus = qsTr("Downloading yt-dlp…")
                            app.backend.installYtdlp()
                        } else {
                            // Keep the in-process zipapp in lockstep with the binary so a YouTube
                            // breakage fix reaches the fast path too (else it silently stays stale).
                            // On a capable device this also (re)fetches a missing copy — Update is
                            // the self-heal lever, since fast resolve has no row of its own.
                            page.ytdlpStatus = app.backend.fastResolvePythonOk
                                ? qsTr("Updating yt-dlp + fast-resolve copy…") : qsTr("Updating yt-dlp…")
                            app.backend.updateYtdlp()
                            if (app.backend.fastResolvePythonOk)
                                app.backend.installFastResolve()
                        }
                    }
                }
                Button {
                    text: qsTr("Recheck")
                    enabled: !app.backend.updating && !app.backend.installing
                    onClicked: app.backend.recheck()
                }
            }

            // Fetch a fresh copy — rarely needed, only if the installed binary breaks.
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.backend.ready && !app.backend.installing
                enabled: !app.backend.updating
                text: qsTr("Reinstall")
                onClicked: {
                    page.ytdlpStatus = qsTr("Reinstalling yt-dlp…")
                    app.backend.installYtdlp()
                }
            }

            TextSwitch {
                visible: app.backend.ready
                text: qsTr("Use nightly builds")
                description: qsTr("Nightly yt-dlp ships fixes for YouTube breakages days sooner, at the "
                                  + "cost of less testing. Applies on the next Update.")
                automaticCheck: false
                checked: app.backend.ytdlpChannel === "nightly"
                onClicked: app.backend.setSetting("ytdlp_channel",
                             app.backend.ytdlpChannel === "nightly" ? "stable" : "nightly")
            }

            ComboBox {
                id: clientCombo
                width: parent.width
                label: qsTr("Player client")
                description: qsTr("How yt-dlp identifies to YouTube. If a video won't play, try "
                                  + "TV or iOS — several clients stream without a PO token.")
                // Index ↔ value map; kept in sync with the menu below.
                property var vals: ["", "tv", "ios", "android_vr", "mweb", "web"]
                currentIndex: Math.max(0, vals.indexOf(app.backend.playerClient))
                menu: ContextMenu {
                    MenuItem { text: qsTr("Auto (yt-dlp default)") }
                    MenuItem { text: "TV" }
                    MenuItem { text: "iOS" }
                    MenuItem { text: "Android VR" }
                    MenuItem { text: qsTr("Mobile web") }
                    MenuItem { text: qsTr("Web") }
                }
                onCurrentIndexChanged: {
                    var v = vals[currentIndex]
                    if (v !== app.backend.playerClient)
                        app.backend.setSetting("player_client", v)
                }
            }

            // Fast resolve is plumbing, not a preference — no toggle. The importable yt-dlp
            // arrives with the install, refreshes with Update, and backfills itself at launch
            // (engine-side _autofetch_zipapp); this label just tells the truth about its state.
            // On an older OS python it explains why the binary is used instead.
            Label {
                visible: app.backend.ready
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                text: !app.backend.fastResolvePythonOk
                    ? qsTr("Fast resolve unavailable: needs OS Python 3.10 or newer to run yt-dlp "
                           + "in-process — this device has %1. The self-contained binary "
                           + "(with its own bundled Python) is used instead.")
                        .arg(app.backend.fastResolvePythonVersion || qsTr("an older one"))
                    : app.backend.fastResolveInstalling
                    ? qsTr("Fast resolve: downloading the importable yt-dlp… %1%")
                        .arg(Math.round(app.backend.fastResolvePct))
                    : (app.backend.fastResolveInstalled
                       ? qsTr("Fast resolve active — yt-dlp %1 runs in-process (no per-resolve respawn). "
                              + "Any error falls back to the binary above.")
                            .arg(app.backend.fastResolveVersion)
                       : qsTr("Fast resolve: the importable copy isn't here yet — it downloads "
                              + "automatically, or rides along with the next Update."))
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Label {
                visible: app.backend.fastResolveStatusMsg.length > 0
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.fastResolveStatusMsg
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Version-skew alarm: the fast path runs whatever zipapp is on disk, so a copy older
            // than the binary quietly misses the breakage fix an Update just delivered (it fails
            // or thins out in-process and every resolve detours through the binary). Covers the
            // cases the Update-lockstep can't see: a zipapp fetch that failed after a successful
            // binary update, and a system yt-dlp another package manager moved forward.
            Label {
                visible: app.backend.fastResolveInstalled
                         && app.backend.ytdlpVersion.length > 0
                         && app.backend.fastResolveVersion.length > 0
                         && app.backend.fastResolveVersion !== app.backend.ytdlpVersion
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: qsTr("Fast-resolve copy (%1) is out of step with the yt-dlp binary (%2) "
                           + "— tap Update above to refresh both.")
                      .arg(app.backend.fastResolveVersion).arg(app.backend.ytdlpVersion)
                color: Theme.secondaryHighlightColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            SectionHeader { text: "ffmpeg" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.ffmpegInstalling
                      ? qsTr("Downloading ffmpeg… %1%").arg(Math.round(app.backend.ffmpegPct))
                      : (app.backend.ffmpegReady
                         ? qsTr("Installed — %1").arg(app.backend.ffmpegVersion)
                         : qsTr("Needed for video downloads — YouTube removed the combined formats, "
                              + "so video is always fetched as separate tracks and merged by "
                              + "ffmpeg. Audio downloads work without it. Tap Download to fetch a "
                              + "static build into the app's folder."))
                color: app.backend.ffmpegReady ? Theme.secondaryHighlightColor : Theme.secondaryColor
                font.pixelSize: Theme.fontSizeSmall
            }

            Label {
                visible: app.backend.ffmpegStatusMsg.length > 0 && !app.backend.ffmpegInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.ffmpegStatusMsg
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.paddingMedium

                Button {
                    text: app.backend.ffmpegInstalling
                          ? (Math.round(app.backend.ffmpegPct) + "%")
                          : (app.backend.ffmpegReady ? qsTr("Update") : qsTr("Download"))
                    enabled: !app.backend.ffmpegInstalling
                    onClicked: app.backend.installFfmpeg()
                }
                Button {
                    text: qsTr("Recheck")
                    enabled: !app.backend.ffmpegInstalling
                    onClicked: app.backend.recheckFfmpeg()
                }
            }

            // Shown only when the last download was refused because its SHA-256 didn't match the
            // pinned known-good build — an explicit opt-in to install the unverified newer build. (M12)
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.backend.ffmpegNeedsConfirm && !app.backend.ffmpegInstalling
                text: qsTr("Install unverified build")
                onClicked: app.backend.installFfmpeg(true)
            }

            SectionHeader { text: qsTr("PO token provider") }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: qsTr("YouTube now demands a fresh proof-of-origin token per video. This sets up "
                           + "the bgutil provider (v%1): a small "
                           + "Deno helper that mints one automatically for each video, so you get full "
                           + "quality instead of 403s.\n\n"
                           + "It runs sandboxed by Deno: network only, no file writes, no subprocesses, "
                           + "no native code, and it can't read anything outside its own folder. Opt-in — "
                           + "nothing is downloaded unless you tap Set up.").arg(app.backend.potTag || "…")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Status line. "running" means the port is open; "working" is the stronger signal that
            // the server actually answered an HTTP probe — that's the state that unlocks full quality.
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: page.potStateText()
                color: page.potStateColor()
                font.pixelSize: Theme.fontSizeSmall
            }

            // The reason it isn't working, when the app knows one (Deno missing, server crashed,
            // clone/deps failed…). Hidden when the provider is healthy.
            Label {
                visible: app.backend.potLastError.length > 0 && !app.backend.potInstalling
                         && !(app.backend.potEnabled && app.backend.potResponding)
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.potLastError
                color: Theme.errorColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Label {
                visible: app.backend.potStatusMsg.length > 0 && !app.backend.potInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.potStatusMsg
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Visible when Deno is missing, OR when the APP-MANAGED copy is in use — that copy
            // has no other updater (the button hid forever once any Deno existed, so a managed
            // Deno stayed at its install-day version for life). A system/user Deno stays theirs.
            Button {
                visible: !app.backend.potDeno || app.backend.denoInstalling
                         || app.backend.potDenoManaged
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.backend.denoInstalling
                      ? qsTr("Downloading Deno… %1%").arg(Math.round(app.backend.denoPct))
                      : (app.backend.potDeno ? qsTr("Update Deno") : qsTr("Download Deno"))
                enabled: !app.backend.denoInstalling
                onClicked: app.backend.installDeno()
            }

            Label {
                visible: app.backend.denoStatusMsg.length > 0 && !app.backend.denoInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.denoStatusMsg
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Confirm WHICH Deno is in use — the launcher trims PATH, so this reassures the user
            // that their ~/.local/bin (or the app-managed) Deno was actually found.
            Label {
                visible: app.backend.potDeno && app.backend.potDenoPath.length > 0
                         && !app.backend.denoInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: qsTr("Deno found: %1").arg(app.backend.potDenoPath)
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            TextSwitch {
                visible: app.backend.potInstalled
                text: qsTr("Use PO token provider")
                description: qsTr("Fetch a per-video token so age/robot-gated videos return full quality.")
                automaticCheck: false
                checked: app.backend.potEnabled
                onClicked: app.backend.setPotEnabled(!app.backend.potEnabled)
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.backend.potInstalling
                      ? qsTr("Setting up…")
                      : (app.backend.potInstalled ? qsTr("Reinstall provider") : qsTr("Set up provider"))
                enabled: !app.backend.potInstalling && app.backend.potDeno
                onClicked: app.backend.installPotProvider()
            }

            Button {
                visible: app.backend.potInstalled
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Update to latest")
                enabled: !app.backend.potInstalling && app.backend.potDeno
                onClicked: app.backend.updatePotProvider()
            }

            SectionHeader { text: qsTr("YouTube account") }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: qsTr("Sign in to YouTube in the Sailfish Browser, then import the session here. It "
                      + "unlocks age-restricted, members-only and premium content, eases the robot "
                      + "check, and lets you import your subscriptions.\n\n"
                      + "Cookies stay on this device (owner-only) and are never uploaded. The session "
                      + "eventually expires — just Re-import when it does.")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.youtubeLoggedIn
                      ? qsTr("Signed in — %1 cookies imported.").arg(app.backend.youtubeCookieCount)
                      : qsTr("Not signed in.")
                color: app.backend.youtubeLoggedIn ? Theme.highlightColor : Theme.secondaryColor
                font.pixelSize: Theme.fontSizeSmall
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.backend.youtubeLoggedIn ? qsTr("Re-import from browser")
                                                  : qsTr("Import login from browser")
                onClicked: app.backend.importBrowserLogin(function(res) {
                    if (res && res.ok)
                        app.showToast(qsTr("Signed in (%1 cookies)").arg(res.count || 0))
                    else
                        app.showToast((res && res.error) ? res.error : qsTr("Import failed"))
                })
            }

            Label {
                visible: app.backend.youtubeLoggedIn
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: qsTr("Import your subscriptions and playlists from More → Import from YouTube.")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Account picker: when several Google accounts are signed in to the Sailfish Browser,
            // choose WHICH one the subscription/playlist import reads (?authuser=N). Switches between
            // separate Google logins only — brand-account channels within one login can't be picked
            // here (a yt-dlp limitation). Only worth touching with more than one account signed in.
            ValueButton {
                visible: app.backend.youtubeLoggedIn
                label: qsTr("Account")
                value: app.backend.ytSelectedName || qsTr("Default account")
                description: qsTr("Which account/channel (incl. brand accounts) to import from. Re-run "
                                 + "the import after changing it.")
                onClicked: pageStack.push(Qt.resolvedUrl("YtAccountsPage.qml"))
            }

            Button {
                visible: app.backend.youtubeLoggedIn
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Log out")
                onClicked: app.backend.youtubeLogout(function() { app.showToast(qsTr("Logged out")) })
            }

            SectionHeader { text: qsTr("Diagnostics") }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: qsTr("A full report of what the app can see: which binaries were found, whether the "
                           + "token server is alive and answering, and the tail of its log. Copy it if you "
                           + "need help getting the provider working.")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Run diagnostics")
                onClicked: {
                    page.diagRunning = true
                    diagArea.text = qsTr("Running…")
                    app.backend.potDiagnostics(function(res) {
                        page.diagRunning = false
                        diagArea.text = (res && res.report) ? res.report : qsTr("No report.")
                    })
                }
            }

            TextArea {
                id: diagArea
                visible: text.length > 0
                width: parent.width
                readOnly: true
                label: qsTr("Provider report")
                font.pixelSize: Theme.fontSizeExtraSmall
                font.family: "monospace"
                text: ""
            }

            Button {
                visible: diagArea.text.length > 0 && !page.diagRunning
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Copy report")
                onClicked: Clipboard.text = diagArea.text
            }
        }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: app.backend.updating || app.backend.installing || app.backend.potInstalling
                 || app.backend.ffmpegInstalling || app.backend.denoInstalling
    }
}
