# FinTube

A native **YouTube client for Sailfish OS**. Silica/QML UI, a Python engine over
PyOtherSide, stream resolution through a **user-managed `yt-dlp` binary**, and a
custom **C++ GStreamer player** (software *and* hardware decode) for real DASH
playback — the things QtMultimedia can't do on its own.

FinTune, its sibling YouTube **Music** client, runs an audio-only cut of this engine.

## Features

- **Playback** — dual-source pipeline (separate video + audio, ≤1080p H.264/VP9;
  AV1 excluded — no decoder on target). Software decode by default, hardware
  (`droidvdec` → `droideglsink`, zero-copy) as a toggle. Quality menu,
  audio-track (dub) picker, captions, chapters.
- **Fast starts** — streams are fetched in-process and served through a localhost
  proxy (no helper spawned per playback; ~250 ms preroll), with self-healing
  re-resolve on stream 403s. Format selection is **property-based** (resolution /
  fps / codec) — never hardcoded itags, so it doesn't go stale.
- **Fast resolve** — runs yt-dlp in-process from an importable copy, skipping the
  per-resolve binary spawn. Not a setting: automatic wherever the OS Python can run
  it (SFOS 5.1+), with the copy installed and updated alongside the binary; older
  devices just use the self-contained binary, which stays the fallback for any
  failure everywhere.
- **Subscriptions** + feed, channel pages, search (filters, autocomplete,
  infinite scroll), related videos, comments, SponsorBlock auto-skip, Shorts filter.
- **Account import** — subscriptions/playlists from your YouTube login
  (browser-cookie import) or a NewPipe backup.
- **History** — watch history, resume positions, watched indicators with
  progress bars on thumbnails.
- **Playlists** — local ones, plus saved YouTube playlists.
- **Downloads** — audio (single m4a, no dependencies) or merged HD video
  (needs ffmpeg — YouTube removed the combined formats).
- **Audio** — 10-band EQ, volume boost + soft limiter, auto audio-only when
  backgrounded (video decoder frozen, sound keeps playing).
- **PO-token provider** *(opt-in)* — a sandboxed Deno `bgutil` sidecar minting the
  per-video Proof-of-Origin token YouTube increasingly demands, pre-warmed at launch.

## Architecture

| Layer | Where | What |
|---|---|---|
| UI | `qml/` (Silica) | `SearchPage`, `VideoPage`, `ChannelPage`, `HistoryPage`, `SettingsPage`, … |
| Bridge | `qml/Backend.qml` | PyOtherSide — all Python calls run off the UI thread |
| Engine | `python/youfish.py` | drives `yt-dlp` (binary or in-process), picks formats, runs the media proxy + the PO-token sidecar |
| Player | `src/videoplayer.cpp` · `src/hwvideosink.cpp` | C++ GStreamer `VideoPlayer` (a QML type) + the hardware EGLImage sink |

## Helpers (nothing to preinstall)

The app fetches each helper itself after a confirmation tap, into its own data dir —
no packages to hunt down. When YouTube breaks something, you update a *helper* from
the Providers page, not the app:

- **yt-dlp** — the extractor (the part YouTube breaks most). One-tap Update, on a
  **stable or nightly** channel; with Fast resolve on, the importable copy updates
  in lockstep with the binary.
- **Deno 2.x** *(optional)* — runtime for the PO-token provider; installed and
  updated with one tap.
- **ffmpeg** *(optional)* — video downloads only; audio downloads never need it.

**32-bit phones (armv7hl):** releases include an armv7hl RPM. The app fetches the 32-bit
yt-dlp and ffmpeg builds there. yt-dlp stays on the stable channel (no nightly build exists for
32-bit ARM), and the PO-token provider is unavailable because Deno has no 32-bit build;
videos play through the clients that need no token. This build is tested in the SDK's
armv7hl emulator, not on a real 32-bit phone — reports welcome.

## Build

With the Sailfish SDK (`sfdk`) configured. Shadow build (recommended — RPM lands in
a sibling `harbour-fintube.build/`):

```sh
sh build.sh          # override target: TARGET=SailfishOS-5.1.0.11-aarch64 sh build.sh
```

or in-source (`sh clean.sh` tidies up):

```sh
sfdk -c target=SailfishOS-5.1.0.11-aarch64.default build
```

Install: `rpm -U --force harbour-fintube-<ver>.aarch64.rpm`

## Tests

Offline tests for the engine and for the decisions the pages make. No device, network or yt-dlp
needed; the QML half needs Node.js:

```sh
sh test.sh
```

`python3 tests/live_ytdlp.py` checks the app against the real, current yt-dlp and live
YouTube (needs the network; `--pot` adds the PO-token provider).

[TESTING.md](TESTING.md) has the release procedure (beta tag first) and the on-device checklist
for everything the tests can't reach.

## Translating

Translations are welcome.

1. Copy `translations/harbour-fintube.ts` (the up-to-date list of every string in the app) to
   `translations/harbour-fintube-<lang>.ts`, e.g. `harbour-fintube-de.ts` or `harbour-fintube-pt_BR.ts`.
2. Open it in [Qt Linguist](https://doc.qt.io/qt-5/linguist-translators.html) (package `qt5-linguist`
   / `qttools5-dev-tools`; any text editor works too) and set the target language
   (*Edit → Translation File Settings*). Translate, marking each string done (✓).
   `%1`, `%2` are placeholders — keep them. Strings with `%n` have a form per plural.
3. Add the file to `TRANSLATIONS` in `harbour-fintube.pro` and open a pull request (or attach the
   file to an issue).

Untranslated or unfinished strings fall back to English, so a partial translation is fine. The
build refreshes every `.ts` file automatically when strings change. Messages that come from the
engine itself (yt-dlp/YouTube errors, the diagnostics report) stay in English.

## License

GNU General Public License v3

## Notice

This application is vibecoded. If you don't like that, feel free to not install it.
