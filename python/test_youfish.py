#!/usr/bin/env python3
"""Offline regression tests for FinTube's shared engine (youfish.py).

Runs with NO device, network, yt-dlp, or PO-token server — resolve()'s externals are all mocked,
so this exercises the pure format-selection + resolve wiring in isolation. Run:  python3 test_youfish.py

Property-based format selection (R93 video / R103 audio) kept regressing, so the selection helpers
and a mocked resolve() are pinned here. Notably the dub-instead-of-source audio pick (R105) is
covered so it can't come back. youfish.py imports `pyotherside` only inside functions, so
`import youfish` is safe here.
"""

import base64
import contextlib
import gzip
import hashlib
import http.client
import http.server
import io
import json
import re
import os
import shutil
import socket
import sqlite3
import tarfile
import time
import sys
import tempfile
import threading as _threading
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import youfish  # noqa: E402
import innertube  # noqa: E402

# The InnerTube fast paths would hit the network, so the suite runs with them OFF by default (every
# engine path then takes its yt-dlp/RSS fallback, which the fixtures mock). The InnerTube* tests
# below switch them back on against canned responses.
_REAL_IT = youfish._it
youfish._it = lambda: None
# Same for the in-process yt-dlp: a zipapp on the dev machine must not turn binary-path tests into
# live extractions. Tests that exercise the in-process tier install a fake module themselves.
_REAL_IMPORT_YT_DLP = youfish._import_yt_dlp
youfish._import_yt_dlp = lambda: None


def vf(fid, height, vcodec, fps=30, url="v", note=None):
    """A video-only format. ext mirrors YouTube reality: avc1 ships in mp4, vp9/av01 in webm —
    resolve() forwards it (video_ext / qualities[].ext) for the player's seek strategy."""
    f = {"format_id": fid, "height": height, "vcodec": vcodec, "acodec": "none",
         "fps": fps, "url": url, "http_headers": {"User-Agent": "UA"},
         "ext": "mp4" if vcodec.startswith("avc") else "webm"}
    if note is not None:
        f["format_note"] = note
    return f


def af(fid, abr, acodec, url="a", note=None, lang_pref=None, lang=None):
    """An audio-only format. ext mirrors YouTube reality: mp4a ships in m4a, opus in webm."""
    f = {"format_id": fid, "abr": abr, "acodec": acodec, "vcodec": "none", "url": url,
         "http_headers": {"User-Agent": "UA"},
         "ext": "m4a" if acodec.startswith("mp4a") else "webm"}
    if note is not None:
        f["format_note"] = note
    if lang_pref is not None:
        f["language_preference"] = lang_pref
    if lang is not None:
        f["language"] = lang
    return f


def muxed(fid="18", url="m"):
    return {"format_id": fid, "height": 360, "vcodec": "avc1", "acodec": "mp4a.40.2",
            "url": url, "protocol": "https", "http_headers": {"User-Agent": "UA"}}


# --- pure helpers ----------------------------------------------------------------------------- #

class CodecFamily(unittest.TestCase):
    def test_video(self):
        self.assertEqual(youfish._codec_family("avc1.4d401f"), "h264")
        self.assertEqual(youfish._codec_family("vp09.00.10"), "vp9")
        self.assertEqual(youfish._codec_family("av01.0.08M"), "")   # AV1 undecodable here
        self.assertEqual(youfish._codec_family(None), "")

    def test_audio(self):
        self.assertEqual(youfish._audio_family("opus"), "opus")
        self.assertEqual(youfish._audio_family("mp4a.40.2"), "aac")
        self.assertEqual(youfish._audio_family("none"), "")
        self.assertEqual(youfish._audio_family(None), "")


class AudioOrigPref(unittest.TestCase):
    def test_language_preference_field_wins(self):
        self.assertEqual(youfish._audio_orig_pref({"language_preference": 10}), 10)
        self.assertEqual(youfish._audio_orig_pref({"language_preference": -1}), -1)

    def test_note_fallback(self):
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English original (default)"}), 10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "English descriptive"}), -10)
        self.assertEqual(youfish._audio_orig_pref({"format_note": "Portuguese"}), 0)
        self.assertEqual(youfish._audio_orig_pref({}), 0)


class VideoCandidates(unittest.TestCase):
    def setUp(self):
        self._gs = youfish.get_settings
        youfish.get_settings = lambda: {"hw_decode": False}

    def tearDown(self):
        youfish.get_settings = self._gs

    def test_excludes_av1_and_over_max_height(self):
        fmts = [vf("137", 1080, "avc1"), vf("399", 1080, "av01"), vf("271", 1440, "vp09")]
        got = [f["format_id"] for f in youfish._video_candidates(fmts)]
        self.assertEqual(got, ["137"])   # av01 + 1440p dropped

    def test_sw_prefers_h264_hw_prefers_vp9(self):
        fmts = [vf("137", 1080, "avc1"), vf("248", 1080, "vp09")]
        self.assertEqual(youfish._video_candidates(fmts)[0]["format_id"], "137")
        youfish.get_settings = lambda: {"hw_decode": True}
        self.assertEqual(youfish._video_candidates(fmts)[0]["format_id"], "248")

    def test_sorted_by_height_desc(self):
        fmts = [vf("135", 480, "avc1"), vf("137", 1080, "avc1"), vf("136", 720, "avc1")]
        got = [f["height"] for f in youfish._video_candidates(fmts)]
        self.assertEqual(got, [1080, 720, 480])

    def test_pick_video_honours_cap(self):
        fmts = [vf("137", 1080, "avc1"), vf("136", 720, "avc1"), vf("135", 480, "avc1")]
        self.assertEqual(youfish._pick_video(fmts, 720)["height"], 720)      # capped
        self.assertEqual(youfish._pick_video(fmts, 0)["height"], 1080)       # uncapped = best
        self.assertEqual(youfish._pick_video(fmts, 240)["height"], 480)      # cap below all -> lowest
                                                                             # offered (nearest the cap)


class VideoManifestTiebreak(unittest.TestCase):
    def setUp(self):
        self._gs = youfish.get_settings
        youfish.get_settings = lambda: {"hw_decode": False}

    def tearDown(self):
        youfish.get_settings = self._gs

    def test_prefers_direct_over_manifest(self):
        # tv_embedded exposes an HLS/manifest AND a direct DASH format at every resolution; the direct
        # one must win the tie (no manifest round-trip; the app's proven range-seekable proxy path).
        # Manifest listed FIRST, as in real responses — so only the tiebreak keeps it from winning.
        hls = {"format_id": "614", "height": 1080, "vcodec": "vp09", "acodec": "none", "fps": 30,
               "url": "https://manifest.googlevideo.com/api/manifest/hls/x",
               "protocol": "m3u8_native", "http_headers": {"User-Agent": "UA"}}
        dash = vf("248", 1080, "vp09", url="https://rr1---sn.googlevideo.com/x")
        self.assertEqual(youfish._video_candidates([hls, dash])[0]["format_id"], "248")


class AudioCandidates(unittest.TestCase):
    def test_ladder_prefers_opus_then_bitrate(self):
        # The ranking is opus-family-first (best on-device playback), each family then ordered by
        # bitrate desc — NOT a single global bitrate sort. So every opus rung (251,250,249,600)
        # precedes every AAC rung (140,139,599), each group high-bitrate first.
        fmts = [af("250", 70, "opus"), af("140", 128, "mp4a.40.2"), af("251", 160, "opus"),
                af("599", 31, "mp4a"), af("249", 50, "opus"), af("139", 48, "mp4a.40.5"),
                af("600", 35, "opus")]
        got = [f["format_id"] for f in youfish._audio_candidates(fmts)]
        self.assertEqual(got, ["251", "250", "249", "600", "140", "139", "599"])

    def test_source_beats_same_bitrate_dub(self):
        fmts = [af("251-3", 160, "opus", lang_pref=-1),    # dub
                af("251-0", 160, "opus", lang_pref=10)]    # source
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-0")

    def test_source_beats_higher_bitrate_dub(self):
        fmts = [af("251-3", 160, "opus", lang_pref=-1),        # dub, higher bitrate
                af("140-0", 128, "mp4a.40.2", lang_pref=10)]   # source, lower bitrate
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "140-0")

    def test_original_beats_same_codec_drc(self):
        # web_embedded exposes a DRC ("stable volume") variant beside the original; same language +
        # codec + bitrate, so only the DRC tie-break separates them — the ORIGINAL must win.
        fmts = [af("251-drc", 120, "opus", lang_pref=10),   # loudness-normalized
                af("251-7", 120, "opus", lang_pref=10)]     # original dynamics
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-7")

    def test_drc_used_when_only_option(self):
        # If DRC is all that's offered, it's still picked (no regression to "no audio").
        fmts = [af("251-drc", 120, "opus", lang_pref=10)]
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-drc")

    def test_opus_drc_kept_over_nondrc_aac(self):
        # The DRC penalty sits AFTER codec: we never trade the opus push-seek win for a non-DRC AAC.
        fmts = [af("251-drc", 120, "opus", lang_pref=10),        # opus, DRC
                af("140-7", 128, "mp4a.40.2", lang_pref=10)]     # aac, original
        self.assertEqual(youfish._pick_audio(fmts)["format_id"], "251-drc")

    def test_excludes_video_only_and_empty(self):
        self.assertEqual(youfish._audio_candidates([vf("137", 1080, "avc1")]), [])
        self.assertIsNone(youfish._pick_audio([vf("137", 1080, "avc1")]))

    def test_prefers_direct_over_manifest(self):
        # Same language/codec/bitrate → the manifest (m3u8) audio must lose to the direct URL.
        m = af("233", 160, "opus", lang_pref=10, url="https://manifest.googlevideo.com/a")
        m["protocol"] = "m3u8_native"
        d = af("251", 160, "opus", lang_pref=10, url="https://rr1---sn.googlevideo.com/a")
        self.assertEqual(youfish._audio_candidates([m, d])[0]["format_id"], "251")


class AudioLangName(unittest.TestCase):
    def test_strips_bitrate_tier(self):
        self.assertEqual(youfish._audio_lang_name({"format_note": "German, low"}), "German")
        self.assertEqual(youfish._audio_lang_name({"format_note": "Portuguese, high"}), "Portuguese")

    def test_keeps_region_parens_drops_role_marker(self):
        self.assertEqual(youfish._audio_lang_name(
            {"format_note": "Chinese (Simplified), medium"}), "Chinese (Simplified)")
        self.assertEqual(youfish._audio_lang_name(
            {"format_note": "English original (default), low"}), "English")

    def test_falls_back_to_code_then_placeholder(self):
        self.assertEqual(youfish._audio_lang_name({"format_note": "", "language": "ja"}), "ja")
        self.assertEqual(youfish._audio_lang_name({}), "Audio")

    def test_bare_tier_note_never_masquerades_as_name(self):
        # A note that is ONLY a tier word (no comma) must not leak as the language name.
        self.assertEqual(youfish._audio_lang_name({"format_note": "medium", "language": "de"}), "de")
        self.assertEqual(youfish._audio_lang_name({"format_note": "low"}), "Audio")


class PickAudioPreferredLang(unittest.TestCase):
    def _dubs(self):
        return [af("251-en", 160, "opus", lang_pref=10, lang="en"),   # source/original
                af("251-pt", 158, "opus", lang_pref=-1, lang="pt"),
                af("251-es", 158, "opus", lang_pref=-1, lang="es-419")]

    def test_no_preference_picks_source(self):
        self.assertEqual(youfish._pick_audio(self._dubs())["format_id"], "251-en")

    def test_exact_preference_picks_that_dub(self):
        self.assertEqual(youfish._pick_audio(self._dubs(), "pt")["format_id"], "251-pt")

    def test_base_code_fallback(self):
        # remembered "es" matches the offered "es-419"
        self.assertEqual(youfish._pick_audio(self._dubs(), "es")["format_id"], "251-es")

    def test_missing_preference_falls_back_to_source(self):
        self.assertEqual(youfish._pick_audio(self._dubs(), "de")["format_id"], "251-en")


# --- resolve() smoke tests (externals mocked) ------------------------------------------------- #

class ResolveSmoke(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args",
                     "_yt_extractor_args", "_proxied", "get_settings", "_import_yt_dlp"):
            self._saved[name] = getattr(youfish, name)
        self._saved["run"] = youfish.subprocess.run

        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False}
        youfish._import_yt_dlp = lambda: None   # pin the BINARY path (no zipapp), whatever's on disk
        youfish.invalidate_resolve_cache()   # resolve() is cache-first (keyed by video id); every
                                             # test reuses id "vid", so isolate each test's fixture

    def tearDown(self):
        for name, fn in self._saved.items():
            if name == "run":
                youfish.subprocess.run = fn
            else:
                setattr(youfish, name, fn)

    def _mock_ytdlp(self, formats, title="T"):
        data = {"title": title, "formats": formats, "duration": 100}
        def fake_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def test_video_resolve_picks_source_audio_and_hd_video(self):
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("251-3", 160, "opus", lang_pref=-1),   # dub
                          af("251-0", 160, "opus", lang_pref=10),   # source
                          muxed()])
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["audio_itag"], "251-0")       # source, not the dub
        self.assertEqual(res["info"]["video_itag"], "137")
        self.assertTrue(res["info"]["qualities"])                  # quality menu populated
        # Containers surface everywhere a URL does — the player keys per-branch
        # downloadbuffers off these (qtdemux/mp4 can't push-seek; matroska can).
        self.assertEqual(res["info"]["video_ext"], "mp4")          # avc1 pick → mp4 branch
        self.assertEqual(res["info"]["audio_ext"], "webm")         # opus pick → webm branch
        self.assertTrue(all(q.get("ext") for q in res["info"]["qualities"]))

    def test_quality_menu_dedups_by_height(self):
        self._mock_ytdlp([vf("137", 1080, "avc1"), vf("248", 1080, "vp09"),
                          vf("136", 720, "avc1"), af("251", 160, "opus", lang_pref=10)])
        res = youfish.resolve("vid")
        heights = [q["label"] for q in res["info"]["qualities"]]
        self.assertEqual(heights, ["1080p", "720p"])               # 30fps, no-premium → one per res

    def test_quality_menu_surfaces_fps_and_premium(self):
        self._mock_ytdlp([
            vf("137", 1080, "avc1", 30),
            vf("299", 1080, "avc1", 60),                           # 1080p60 (free, 60fps source)
            vf("620", 1080, "avc1", 60, note="Premium"),           # enhanced-bitrate premium
            vf("136", 720, "avc1", 30),
            af("251", 160, "opus", lang_pref=10)])
        labels = [q["label"] for q in youfish.resolve("vid")["info"]["qualities"]]
        self.assertIn("1080p", labels)                             # 30fps rung still there
        self.assertIn("1080p60", labels)                           # 60fps its own row
        self.assertTrue(any("Premium" in l for l in labels))       # premium its own row
        self.assertEqual(labels[0], "1080p60 Premium")             # premium first, then higher fps
        self.assertEqual(labels[-1], "720p")                       # lowest resolution last

    def test_audio_tracks_one_per_language_original_first(self):
        # A dubbed video: two rungs each of English (source) + Portuguese; the picker collapses to
        # one entry per language, best rung, original/default first.
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("140-en", 129, "mp4a.40.2", lang_pref=10, lang="en",
                             note="English original (default), medium"),
                          af("251-en", 124, "opus", lang_pref=10, lang="en",
                             note="English original (default), medium"),
                          af("251-pt", 128, "opus", lang_pref=-1, lang="pt",
                             note="Portuguese, medium"),
                          af("249-pt", 48, "opus", lang_pref=-1, lang="pt",
                             note="Portuguese, low")])
        info = youfish.resolve("vid")["info"]
        tracks = info["audio_tracks"]
        self.assertEqual([t["lang"] for t in tracks], ["en", "pt"])   # original language first
        self.assertEqual(tracks[0]["name"], "English")
        self.assertTrue(tracks[0]["is_original"])
        self.assertEqual(tracks[1]["name"], "Portuguese")
        self.assertFalse(tracks[1]["is_original"])
        self.assertEqual(tracks[0]["itag"], "251-en")                 # best rung (opus preferred over AAC)
        self.assertTrue(tracks[0]["audio_url"])
        self.assertEqual(info["audio_itag"], "251-en")               # started on the original

    def test_untagged_original_prepended_as_original(self):
        # A dubbed video whose SOURCE audio yt-dlp left untagged: it's what plays, so it must appear
        # (as "Original"), highlighted, even though it carries no language tag.
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("251-src", 160, "opus", lang_pref=10),   # original, NO language tag
                          af("251-pt", 158, "opus", lang_pref=-1, lang="pt",
                             note="Portuguese, medium")])
        info = youfish.resolve("vid")["info"]
        tracks = info["audio_tracks"]
        self.assertEqual([t["lang"] for t in tracks], ["", "pt"])     # original prepended, blank lang
        self.assertEqual(tracks[0]["itag"], "251-src")
        self.assertEqual(tracks[0]["name"], "Original")
        self.assertFalse(tracks[0]["is_original"])                    # no double "(original)" marker
        self.assertTrue(tracks[0]["audio_url"])
        self.assertEqual(info["audio_itag"], "251-src")              # playing track is highlightable

    def test_audio_tracks_empty_when_untagged(self):
        # A normal single-audio video has many audio rungs, all with NO language tag. None are a dub
        # choice, so audio_tracks stays empty (the UI hides the row) — never one bogus entry / rung.
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("139", 48, "mp4a.40.5"), af("249", 50, "opus"),
                          af("250", 70, "opus"), af("140", 128, "mp4a.40.2"),
                          af("251", 160, "opus")])
        info = youfish.resolve("vid")["info"]
        self.assertEqual(info["audio_tracks"], [])

    def test_resolve_honours_remembered_audio_lang(self):
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False,
                                        "audio_lang": "pt"}
        self._mock_ytdlp([vf("137", 1080, "avc1"),
                          af("251-en", 160, "opus", lang_pref=10, lang="en"),
                          af("251-pt", 158, "opus", lang_pref=-1, lang="pt")])
        info = youfish.resolve("vid")["info"]
        self.assertEqual(info["audio_itag"], "251-pt")               # started on the remembered dub

    def test_resolve_error_surfaces(self):
        def fail_run(cmd, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm")
        youfish.subprocess.run = fail_run
        res = youfish.resolve("vid")
        self.assertFalse(res.get("ok"))
        self.assertIn("Sign in", res.get("error", ""))

    def test_final_error_skips_the_widen_retry(self):
        # A removed video is YouTube's final word: one extraction, not the widen re-run too.
        calls = []
        def fail_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(
                returncode=1, stdout="",
                stderr="WARNING: [youtube] vid: some warning\n"
                       "ERROR: [youtube] vid: Video unavailable. This video has been removed "
                       "by the uploader")
        youfish.subprocess.run = fail_run
        res = youfish.resolve("vid")
        self.assertFalse(res.get("ok"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["error"], "Video unavailable. This video has been removed by the uploader")

    def test_unclear_error_still_widens(self):
        calls = []
        def fail_run(cmd, **kwargs):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="ERROR: [youtube] vid: Sign in to confirm you're not a bot")
        youfish.subprocess.run = fail_run
        youfish.resolve("vid")
        self.assertEqual(len(calls), 2)

    def test_ytdlp_err_keeps_the_reason_only(self):
        e = youfish._ytdlp_err
        self.assertEqual(e("ERROR: [youtube] Tq92D6wQ1mg: Sign in to confirm your age. Use "
                           "--cookies-from-browser or --cookies for the authentication. See  "
                           "https://github.com/yt-dlp/yt-dlp/wiki/FAQ for how", "x"),
                         "Sign in to confirm your age.")
        self.assertEqual(e("ERROR: Unable to download webpage: HTTP Error 429", "x"),
                         "Unable to download webpage: HTTP Error 429")
        self.assertEqual(e("", "resolve failed"), "resolve failed")
        self.assertFalse(youfish._final_error("Video unavailable. This content isn't available, "
                                              "try again later."))


class BinaryResolution(unittest.TestCase):
    """Binary resolution after the 2026-08-28 reversal: a managed copy WINS, but with none present
    the app DOES fall back to a user/system yt-dlp/ffmpeg (PATH / ~/.local/bin / … via
    _system_binary) — it is no longer managed-only. Both halves are pinned so neither regresses.
    The extra FinTune-only candidate layers (_CANDIDATE_PATHS / _FINTUBE_DATA_DIR) are neutralised
    so the same test is deterministic in both apps."""
    def setUp(self):
        self._mo, self._mf, self._which = (youfish._managed_ytdlp, youfish._managed_ffmpeg,
                                           youfish.shutil.which)
        self._tmp = tempfile.mkdtemp(prefix="binres-")
        self._sys = {}
        for name in ("yt-dlp", "ffmpeg"):               # a discoverable user/system copy
            p = os.path.join(self._tmp, name)
            with open(p, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(p, 0o755)
            self._sys[name] = p
        self.which_calls = []

        def _spy(name):
            self.which_calls.append(name)
            return self._sys.get(name)                  # only yt-dlp/ffmpeg resolve; deno → None

        youfish.shutil.which = _spy
        youfish._managed_ytdlp = lambda: os.path.join(self._tmp, "absent", "yt-dlp")
        youfish._managed_ffmpeg = lambda: os.path.join(self._tmp, "absent", "ffmpeg")
        # Neutralise FinTune's extra fallback layers so both apps go straight managed → system.
        self._cand = getattr(youfish, "_CANDIDATE_PATHS", None)
        if self._cand is not None:
            youfish._CANDIDATE_PATHS = ()
        self._fdd = getattr(youfish, "_FINTUBE_DATA_DIR", None)
        if self._fdd is not None:
            youfish._FINTUBE_DATA_DIR = os.path.join(self._tmp, "no-fintube")

    def tearDown(self):
        youfish._managed_ytdlp, youfish._managed_ffmpeg, youfish.shutil.which = (
            self._mo, self._mf, self._which)
        if self._cand is not None:
            youfish._CANDIDATE_PATHS = self._cand
        if self._fdd is not None:
            youfish._FINTUBE_DATA_DIR = self._fdd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_ytdlp_falls_back_to_system(self):
        # No managed copy, but a user/system copy is discoverable → resolved (NOT None), and the
        # system-fallback path was actually taken (which consulted for yt-dlp).
        self.assertEqual(youfish._ytdlp_path(), self._sys["yt-dlp"])
        self.assertIn("yt-dlp", self.which_calls)

    def test_ffmpeg_falls_back_to_system(self):
        self.assertEqual(youfish._ffmpeg_path(), self._sys["ffmpeg"])

    def test_managed_wins_over_system(self):
        managed = os.path.join(self._tmp, "managed-yt-dlp")
        with open(managed, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(managed, 0o755)
        youfish._managed_ytdlp = lambda: managed
        self.assertEqual(youfish._ytdlp_path(), managed)
        self.assertNotIn("yt-dlp", self.which_calls)    # managed short-circuits the fallback


class NewPipeImport(unittest.TestCase):
    """import_newpipe against a synthetic NewPipe backup: subscriptions, watch history + resume,
    local playlists, and bookmarked YouTube playlists — YouTube-only, merged, idempotent. The
    watch-history section is store-guarded, so this also passes in FinTune (no history store)."""
    def setUp(self):
        self._dd = youfish._data_dir
        self._tmp = tempfile.mkdtemp(prefix="npimp-")
        youfish._data_dir = lambda: self._tmp
        self.db = os.path.join(self._tmp, "newpipe.db")
        self._build(self.db)

    def tearDown(self):
        youfish._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _build(self, path):
        con = sqlite3.connect(path)
        c = con.cursor()
        c.execute("CREATE TABLE streams(uid INTEGER PRIMARY KEY, service_id INTEGER, url TEXT, "
                  "title TEXT, duration INTEGER, uploader TEXT, thumbnail_url TEXT)")
        c.execute("CREATE TABLE subscriptions(uid INTEGER PRIMARY KEY, service_id INTEGER, "
                  "url TEXT, name TEXT, avatar_url TEXT)")
        c.execute("CREATE TABLE stream_history(stream_id INTEGER, access_date INTEGER, "
                  "repeat_count INTEGER)")
        c.execute("CREATE TABLE stream_state(stream_id INTEGER PRIMARY KEY, progress_time INTEGER)")
        c.execute("CREATE TABLE playlists(uid INTEGER PRIMARY KEY, name TEXT, display_index INTEGER)")
        c.execute("CREATE TABLE playlist_stream_join(playlist_id INTEGER, stream_id INTEGER, "
                  "join_index INTEGER)")
        c.execute("CREATE TABLE remote_playlists(uid INTEGER PRIMARY KEY, service_id INTEGER, "
                  "name TEXT, url TEXT)")
        c.executemany("INSERT INTO streams(uid,service_id,url,title,duration,uploader,thumbnail_url)"
                      " VALUES(?,?,?,?,?,?,?)", [
                          (1, 0, "https://www.youtube.com/watch?v=AAAAAAAAAAA", "One", 600, "A", ""),
                          (2, 0, "https://youtu.be/BBBBBBBBBBB", "Two", 300, "B", ""),
                          (3, 1, "https://soundcloud.com/x", "SC", 100, "SC", ""),   # non-YT → dropped
                      ])
        c.executemany("INSERT INTO subscriptions(uid,service_id,url,name,avatar_url) "
                      "VALUES(?,?,?,?,?)", [
                          (1, 0, "https://www.youtube.com/channel/UC11111111111111111111", "A", ""),
                          (2, 0, "https://www.youtube.com/@handle", "H", ""),  # no id → skipped
                          (3, 1, "https://soundcloud.com/c", "SC", ""),        # non-YT → dropped
                      ])
        c.execute("INSERT INTO stream_state VALUES(1, 120000)")               # 20% → resume point
        c.execute("INSERT INTO stream_history VALUES(1, 1700000000000, 0)")
        c.execute("INSERT INTO playlists VALUES(1, 'Mix', 0)")
        c.executemany("INSERT INTO playlist_stream_join VALUES(?,?,?)", [(1, 2, 0), (1, 1, 1)])
        c.execute("INSERT INTO remote_playlists VALUES"
                  "(1, 0, 'Cool', 'https://www.youtube.com/playlist?list=PLxyz')")
        con.commit()
        con.close()

    def test_full_import_and_idempotent(self):
        res = youfish.import_newpipe(self.db)
        self.assertTrue(res["ok"])
        self.assertEqual(res["added"], 1)         # UC1 only (@handle skipped, SoundCloud dropped)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["resume"], 1)        # stream 1 at 20%
        self.assertEqual(res["playlists"], 1)     # local "Mix"
        self.assertEqual(res["remote"], 1)        # bookmarked "Cool"
        has_history = hasattr(youfish, "_load_watch_history")
        self.assertEqual(res["history"], 1 if has_history else 0)
        pls = json.load(open(os.path.join(self._tmp, "playlists.json")))
        mix = next(p for p in pls if p["title"] == "Mix")
        self.assertEqual([it["id"] for it in mix["items"]], ["BBBBBBBBBBB", "AAAAAAAAAAA"])  # join order
        cool = next(p for p in pls if p["title"] == "Cool")
        self.assertEqual((cool["kind"], cool["yt_id"], len(cool["items"])), ("youtube", "PLxyz", 0))
        res2 = youfish.import_newpipe(self.db)    # nothing new on a second run
        self.assertEqual((res2["added"], res2["resume"], res2["playlists"], res2["remote"]),
                         (0, 0, 0, 0))

    def test_rejects_non_database(self):
        junk = os.path.join(self._tmp, "junk.db")
        with open(junk, "wb") as f:
            f.write(b"not a database")
        self.assertFalse(youfish.import_newpipe(junk)["ok"])


class PotTag(unittest.TestCase):
    """The PO-token provider version is no longer a hardcoded dead-end: a stored override wins,
    else the pinned known-good default. (The GitHub 'latest' lookup needs the network, so it
    isn't exercised offline here.)"""
    def setUp(self):
        self._gs = youfish.get_settings

    def tearDown(self):
        youfish.get_settings = self._gs

    def test_defaults_to_pinned_when_no_override(self):
        youfish.get_settings = lambda: {}
        self.assertEqual(youfish._pot_effective_tag(), youfish._POT_TAG)

    def test_override_wins(self):
        youfish.get_settings = lambda: {"pot_tag": "1.4.0"}
        self.assertEqual(youfish._pot_effective_tag(), "1.4.0")

    def test_blank_override_ignored(self):
        youfish.get_settings = lambda: {"pot_tag": "   "}
        self.assertEqual(youfish._pot_effective_tag(), youfish._POT_TAG)


class HideWatched(unittest.TestCase):
    """The hide-watched feed filter drops w=1 videos when the setting is on. Guard-aware: FinTune
    has no watch-history store, so the filter is a safe no-op there (still respects the setting)."""
    def setUp(self):
        self._gs = youfish.get_settings
        self._wh = getattr(youfish, "_load_watch_history", None)

    def tearDown(self):
        youfish.get_settings = self._gs
        if self._wh is not None:
            youfish._load_watch_history = self._wh

    def test_filters_when_on(self):
        youfish.get_settings = lambda: {"hide_watched": True}
        items = [{"id": "AAA"}, {"id": "BBB"}, {"id": "CCC"}]
        if hasattr(youfish, "_load_watch_history"):
            youfish._load_watch_history = lambda: {"AAA": {"w": 1}, "BBB": {"w": 0}}
            self.assertEqual(youfish._feed_hide_watched(items), [{"id": "BBB"}, {"id": "CCC"}])
        else:
            self.assertEqual(youfish._feed_hide_watched(items), items)   # no store → no-op

    def test_noop_when_off(self):
        youfish.get_settings = lambda: {"hide_watched": False}
        items = [{"id": "AAA"}, {"id": "BBB"}]
        self.assertEqual(youfish._feed_hide_watched(items), items)


class SetWatched(unittest.TestCase):
    """set_watched marks a video watched/unwatched from the long-press menu, writing the same
    store the History page + hide-watched read. FinTube-only (FinTune has no watch store)."""
    def setUp(self):
        if not hasattr(youfish, "set_watched"):
            self.skipTest("no watch-history store (FinTune)")
        self._dd = youfish._data_dir
        self._tmp = tempfile.mkdtemp(prefix="setw-")
        youfish._data_dir = lambda: self._tmp

    def tearDown(self):
        if hasattr(self, "_dd"):
            youfish._data_dir = self._dd
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_mark_watched_creates_entry(self):
        res = youfish.set_watched("AAAAAAAAAAA", True, "Title", "Chan")
        self.assertEqual(res, {"ok": True, "watched": 1})
        e = youfish._load_watch_history()["AAAAAAAAAAA"]
        self.assertEqual(e["w"], 1)
        self.assertEqual(e["f"], 1.0)            # no prior progress → reads as fully played
        self.assertEqual(e["ti"], "Title")
        self.assertEqual(e["ch"], "Chan")

    def test_explicit_unwatch_clears_sticky_flag(self):
        youfish.set_watched("BBBBBBBBBBB", True)
        res = youfish.set_watched("BBBBBBBBBBB", False)
        self.assertEqual(res["watched"], 0)
        self.assertEqual(youfish._load_watch_history()["BBBBBBBBBBB"]["w"], 0)

    def test_unwatch_clears_the_progress_bar(self):
        youfish.record_watch("HHHHHHHHHHH", 300, 600)   # 50% → red bar at 0.5
        youfish.set_watched("HHHHHHHHHHH", True)         # → f=1.0 (full bar)
        youfish.set_watched("HHHHHHHHHHH", False)        # → bar cleared
        e = youfish._load_watch_history()["HHHHHHHHHHH"]
        self.assertEqual(e["w"], 0)
        self.assertEqual(e["f"], 0.0)                    # red bar gone
        self.assertEqual(e["p"], 0)

    def test_unwatch_unknown_is_noop(self):
        res = youfish.set_watched("CCCCCCCCCCC", False)
        self.assertEqual(res, {"ok": True, "watched": 0})
        self.assertNotIn("CCCCCCCCCCC", youfish._load_watch_history())   # no stub entry created

    def test_promote_marks_complete_and_keeps_meta(self):
        youfish.record_watch("DDDDDDDDDDD", 60, 600, "Vid", "By")        # 10% → not watched
        self.assertEqual(youfish._load_watch_history()["DDDDDDDDDDD"]["w"], 0)
        youfish.set_watched("DDDDDDDDDDD", True)                          # promote to watched
        e = youfish._load_watch_history()["DDDDDDDDDDD"]
        self.assertEqual(e["w"], 1)
        self.assertEqual(e["f"], 1.0)           # red bar reads complete
        self.assertEqual(e["p"], 600)           # position moved to the end
        self.assertEqual(e["d"], 600)           # duration preserved
        self.assertEqual(e["ti"], "Vid")        # meta preserved
        self.assertEqual(e["ch"], "By")

    def test_watched_id_is_hidden_by_hide_watched(self):
        youfish.set_watched("EEEEEEEEEEE", True)
        self.assertIn("EEEEEEEEEEE", youfish._watched_ids())

    def test_marking_watched_clears_resume_point(self):
        youfish.set_position("FFFFFFFFFFF", 120)
        self.assertEqual(youfish.get_position("FFFFFFFFFFF"), 120)
        youfish.set_watched("FFFFFFFFFFF", True)
        self.assertEqual(youfish.get_position("FFFFFFFFFFF"), 0)      # seek memory forgotten

    def test_unwatch_leaves_resume_point(self):
        youfish.record_watch("GGGGGGGGGGG", 90, 600)                  # entry + resume=90
        youfish.set_watched("GGGGGGGGGGG", False)                     # unwatch keeps the resume point
        self.assertEqual(youfish.get_position("GGGGGGGGGGG"), 90)


class FeedShortsAndDurations(unittest.TestCase):
    """Persistent duration + Shorts-membership caches and the feed Shorts filter. Shared engine, so
    this passes in both apps (FinTune has the same functions, just no QML consumer)."""
    def setUp(self):
        self._dd, self._gs, self._ls = youfish._data_dir, youfish.get_settings, youfish.list_subscriptions
        self._fd, self._fc = youfish._feed_durations_cache, youfish._feed_cache
        self._tmp = tempfile.mkdtemp(prefix="feeddur-")
        youfish._data_dir = lambda: self._tmp
        youfish._feed_durations_cache = {"ts": 0.0, "map": {}, "shorts": set()}
        youfish._feed_cache = {}                             # per-channel: {cid: {"ts", "rows": [...]}}

    def tearDown(self):
        youfish._data_dir, youfish.get_settings, youfish.list_subscriptions = self._dd, self._gs, self._ls
        youfish._feed_durations_cache, youfish._feed_cache = self._fd, self._fc
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_durations_cache_roundtrip(self):
        youfish._save_durations({"a": 10, "b": 200})
        self.assertEqual(youfish._load_saved_durations(), {"a": 10, "b": 200})

    def test_shorts_cache_roundtrip(self):
        youfish._save_shorts({"x", "y"})
        self.assertEqual(youfish._load_saved_shorts(), {"x", "y"})

    def test_shorts_filter_drops_known_shorts(self):
        youfish.get_settings = lambda: {"hide_shorts": True}
        youfish._feed_durations_cache["shorts"] = {"S1"}
        items = [{"id": "V1"}, {"id": "S1"}, {"id": "V2"}]
        self.assertEqual(youfish._feed_shorts_filter(items), [{"id": "V1"}, {"id": "V2"}])

    def test_shorts_filter_noop_when_off(self):
        youfish.get_settings = lambda: {"hide_shorts": False}
        youfish._feed_durations_cache["shorts"] = {"S1"}
        items = [{"id": "V1"}, {"id": "S1"}]
        self.assertEqual(youfish._feed_shorts_filter(items), items)

    def test_feed_durations_all_classified_is_instant(self):
        cid = "UC00000000000000000000AA"                      # fully-classified feed → no yt-dlp needed
        youfish.list_subscriptions = lambda: [{"id": cid, "url": "", "name": "A"}]
        youfish._feed_cache = {cid: {"ts": 0.0, "rows": [
            [3, {"id": "LONG", "channel_id": cid}],
            [2, {"id": "OTHER", "channel_id": cid}],
            [1, {"id": "SHORT", "channel_id": cid}],
        ]}}
        youfish._feed_durations_cache = {"ts": 0.0, "map": {"LONG": 300, "OTHER": 42}, "shorts": {"SHORT"}}
        res = youfish.feed_durations()
        self.assertEqual(res["durations"], {"LONG": 300, "OTHER": 42})   # SHORT has no /videos duration
        self.assertEqual(res["shorts"], ["SHORT"])

    def test_feed_with_durations_fills_cached_lengths(self):
        youfish._feed_durations_cache = {"ts": 0.0, "map": {"A": 120}, "shorts": set()}
        items = [{"id": "A", "duration": 0}, {"id": "C", "duration": 0}]
        out = youfish._feed_with_durations(items)
        self.assertEqual(out[0]["duration"], 120)   # cached → shows immediately with the feed
        self.assertEqual(out[1]["duration"], 0)      # uncached → filled later by feed_durations


class ResumeIdleTTL(unittest.TestCase):
    """Resume points carry a last-touched stamp, expire after _RESUME_TTL idle, and old int-format
    entries are grandfathered. Shared engine → passes in both apps."""
    def setUp(self):
        self._dd = youfish._data_dir
        self._tmp = tempfile.mkdtemp(prefix="pos-")
        youfish._data_dir = lambda: self._tmp

    def tearDown(self):
        youfish._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_set_get_roundtrip_and_forget(self):
        youfish.set_position("AAAAAAAAAAA", 120)
        self.assertEqual(youfish.get_position("AAAAAAAAAAA"), 120)
        youfish.set_position("AAAAAAAAAAA", 0)
        self.assertEqual(youfish.get_position("AAAAAAAAAAA"), 0)

    def test_stale_entry_expires_on_read(self):
        old = time.time() - (youfish._RESUME_TTL + 3600)
        with open(youfish._positions_path(), "w") as f:
            json.dump({"OLD": {"s": 90, "t": old}}, f)
        self.assertEqual(youfish.get_position("OLD"), 0)          # idle past TTL → gone

    def test_stale_swept_on_write(self):
        old = time.time() - (youfish._RESUME_TTL + 3600)
        with open(youfish._positions_path(), "w") as f:
            json.dump({"OLD": {"s": 90, "t": old}, "KEEP": {"s": 30, "t": time.time()}}, f)
        youfish.set_position("NEW", 50)
        d = youfish._load_positions()
        self.assertNotIn("OLD", d)                                # swept
        self.assertIn("KEEP", d)
        self.assertEqual(youfish.get_position("NEW"), 50)

    def test_old_int_format_grandfathered(self):
        with open(youfish._positions_path(), "w") as f:
            json.dump({"LEGACY": 77}, f)                          # pre-change plain-int format
        self.assertEqual(youfish.get_position("LEGACY"), 77)      # still readable
        youfish.set_position("OTHER", 10)                         # a write migrates LEGACY
        self.assertEqual(youfish._load_positions()["LEGACY"]["s"], 77)   # migrated + kept


class HistoryLimitSetting(unittest.TestCase):
    """Watch history is capped at the history_limit setting (FinTube-only store)."""
    def setUp(self):
        if not hasattr(youfish, "record_watch"):
            self.skipTest("no watch-history store (FinTune)")
        self._dd, self._gs = youfish._data_dir, youfish.get_settings
        self._tmp = tempfile.mkdtemp(prefix="histlim-")
        youfish._data_dir = lambda: self._tmp

    def tearDown(self):
        if hasattr(self, "_dd"):
            youfish._data_dir, youfish.get_settings = self._dd, self._gs
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_history_capped_at_setting(self):
        youfish.get_settings = lambda: {"history_limit": 50}     # 50 = the clamp floor
        for i in range(53):
            youfish.record_watch("VID%08d" % i, 30, 600)         # 5% → not watched, just recorded
        d = youfish._load_watch_history()
        self.assertEqual(len(d), 50)                             # capped to the setting
        self.assertIn("VID00000052", d)                         # newest kept
        self.assertNotIn("VID00000000", d)                      # oldest dropped

    def test_history_limit_clamped(self):
        youfish.get_settings = lambda: {"history_limit": 999999}
        self.assertEqual(youfish._history_limit(), 5000)         # clamp upper
        youfish.get_settings = lambda: {"history_limit": 1}
        self.assertEqual(youfish._history_limit(), 50)           # clamp lower


class CaptionTracks(unittest.TestCase):
    """_caption_tracks splits yt-dlp caption data into the short real list + the big
    auto-translate set; the ASR base (no tlang) is a real track, tlang entries are not."""
    def _fmts(self, url, name=None, ext="json3"):
        f = {"ext": ext, "url": url}
        if name is not None:
            f["name"] = name
        return [f]

    def test_manual_and_asr_are_tracks_translations_are_split(self):
        data = {
            "subtitles": {"en": self._fmts("http://x/en.json3?fmt=json3", "English")},
            "automatic_captions": {
                "en": self._fmts("http://x/asr.json3?fmt=json3&kind=asr", "English (auto)"),
                "de": self._fmts("http://x/t.json3?fmt=json3&tlang=de", "German"),
                "fr": self._fmts("http://x/t.json3?fmt=json3&tlang=fr", "French"),
            },
        }
        tracks, translations = youfish._caption_tracks(data)
        kinds = {t["lang"]: t["kind"] for t in tracks}
        self.assertEqual(kinds, {"en": "manual"})                    # manual wins the 'en' slot
        self.assertEqual({t["lang"] for t in translations}, {"de", "fr"})
        self.assertTrue(all("tlang=" in t["url"] for t in translations))

    def test_asr_kept_when_no_manual_for_that_lang(self):
        data = {"automatic_captions": {
            "es": self._fmts("http://x/asr.json3?fmt=json3", "Spanish"),        # ASR, no tlang
            "en": self._fmts("http://x/t.json3?fmt=json3&tlang=en", "English"), # translation
        }}
        tracks, translations = youfish._caption_tracks(data)
        self.assertEqual([(t["lang"], t["kind"]) for t in tracks], [("es", "asr")])
        self.assertEqual([t["lang"] for t in translations], ["en"])

    def test_non_json3_and_live_chat_ignored(self):
        data = {
            "subtitles": {
                "en": self._fmts("http://x/en.vtt", "English", ext="vtt"),   # no json3 -> skip
                "live_chat": self._fmts("http://x/lc.json3?fmt=json3"),      # not a subtitle
            },
            "automatic_captions": {},
        }
        tracks, translations = youfish._caption_tracks(data)
        self.assertEqual(tracks, [])
        self.assertEqual(translations, [])

    def test_translation_dropped_when_a_real_track_covers_that_lang(self):
        # YouTube offers auto-translate to EVERY language, so a manual 'ja' still gets a
        # translated 'ja' — the real track wins and the translation is dropped (real-data case).
        data = {
            "subtitles": {"ja": self._fmts("http://x/ja.json3?fmt=json3", "Japanese")},
            "automatic_captions": {
                "ja": self._fmts("http://x/t.json3?fmt=json3&tlang=ja", "Japanese"),
                "de": self._fmts("http://x/t.json3?fmt=json3&tlang=de", "German"),
            },
        }
        tracks, translations = youfish._caption_tracks(data)
        self.assertEqual([t["lang"] for t in tracks], ["ja"])
        self.assertEqual([t["lang"] for t in translations], ["de"])   # 'ja' translation dropped

    def test_translations_sorted_by_name(self):
        data = {"automatic_captions": {
            "z": self._fmts("http://x?tlang=z", "Zulu"),
            "a": self._fmts("http://x?tlang=a", "Afrikaans"),
        }}
        _, translations = youfish._caption_tracks(data)
        self.assertEqual([t["name"] for t in translations], ["Afrikaans", "Zulu"])


class ParseJson3(unittest.TestCase):
    """_parse_json3 turns json3 events into non-overlapping cues and tames rolling ASR."""
    def test_empty_and_whitespace_events_dropped(self):
        data = {"events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "hi"}]},
        ]}
        cues = youfish._parse_json3(data)
        self.assertEqual([c["text"] for c in cues], ["hi"])

    def test_multiple_segs_joined_and_newlines_collapsed(self):
        data = {"events": [{"tStartMs": 0, "dDurationMs": 2000,
                            "segs": [{"utf8": "hello"}, {"utf8": "\nworld"}]}]}
        cues = youfish._parse_json3(data)
        self.assertEqual(cues[0]["text"], "hello world")

    def test_overlapping_cues_clamped_to_next_start(self):
        data = {"events": [
            {"tStartMs": 0, "dDurationMs": 5000, "segs": [{"utf8": "a"}]},   # would run to 5s
            {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "b"}]},
        ]}
        cues = youfish._parse_json3(data)
        self.assertAlmostEqual(cues[0]["dur"], 2.0, places=2)               # clamped 5->2s
        self.assertAlmostEqual(cues[1]["start"], 2.0, places=2)

    def test_non_dict_body_returns_empty_not_crash(self):
        # A valid-JSON but non-object timedtext body (null / [] / a number) must not raise.
        for body in (None, [], 42, "text"):
            self.assertEqual(youfish._parse_json3(body), [])

    def test_repeated_line_merged_not_stacked(self):
        data = {"events": [
            {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "same"}]},
            {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "same"}]},
            {"tStartMs": 2000, "dDurationMs": 1000, "segs": [{"utf8": "next"}]},
        ]}
        cues = youfish._parse_json3(data)
        self.assertEqual([c["text"] for c in cues], ["same", "next"])       # one 'same', extended
        self.assertAlmostEqual(cues[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(cues[0]["dur"], 2.0, places=2)               # 0->2s merged


class SubscriptionFeed(unittest.TestCase):
    """subscription_feed is built from each channel's yt-dlp /videos tab (YouTube retired RSS):
    merged newest-first, durations inline, members-only dropped, cached briefly."""
    def setUp(self):
        self._saved = {n: getattr(youfish, n)
                       for n in ("_ytdlp_path", "list_subscriptions", "get_settings", "_data_dir")}
        self._run = youfish.subprocess.run
        self._cache, self._state = youfish._feed_cache, youfish._feed_state
        self._open = youfish.urllib.request.urlopen
        self._tmp = tempfile.mkdtemp(prefix="feed-")
        youfish._feed_cache = {}                        # per-channel: {cid: {"ts", "rows"}}
        youfish._feed_state = {"loaded": False}
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish.get_settings = lambda: {"hide_watched": False}
        youfish._data_dir = lambda: self._tmp          # disk feed-cache writes into a tempdir
        # RSS fallback network is inert by default (raises) so tests stay offline; a test that
        # exercises the fallback overrides urlopen itself.
        def _no_rss(req, timeout=0):
            raise youfish.urllib.error.URLError("no rss in test")
        youfish.urllib.request.urlopen = _no_rss

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run
        youfish._feed_cache, youfish._feed_state = self._cache, self._state
        youfish.urllib.request.urlopen = self._open
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mock(self, channels):
        """channels: {channel_id: [flat-entry, ...]}; fake_run dispatches on the URL's channel id."""
        def fake_run(cmd, **kw):
            url = cmd[-1]
            cid = next((c for c in channels if c in url), None)
            data = {"channel": cid, "entries": channels.get(cid, [])}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def test_a_dead_channel_does_not_take_the_feed_down(self):
        # A terminated channel's /videos answered "null" through the in-process tier; the
        # resulting AttributeError used to escape subscription_feed and the refresh never landed.
        now = int(time.time())
        self._mock({"UC_OK": [{"id": "okokokokok1", "title": "fine", "timestamp": now, "duration": 9}]})
        good = youfish.subprocess.run
        def run(cmd, **kw):
            if "UC_DEAD" in cmd[-1]:
                return types.SimpleNamespace(returncode=0, stdout="null", stderr="")
            return good(cmd, **kw)
        youfish.subprocess.run = run
        youfish.list_subscriptions = lambda: [{"id": "UC_OK"}, {"id": "UC_DEAD"}]
        res = youfish.subscription_feed(force=True)
        self.assertTrue(res["ok"])
        self.assertEqual([i["title"] for i in res["items"]], ["fine"])

    def test_merged_newest_first_durations_inline(self):
        now = int(time.time())
        self._mock({
            "UC_AAA": [{"id": "aaaaaaaaaa1", "title": "A new", "timestamp": now - 3600, "duration": 120},
                       {"id": "aaaaaaaaaa2", "title": "A old", "timestamp": now - 9 * 86400, "duration": 200}],
            "UC_BBB": [{"id": "bbbbbbbbbb1", "title": "B mid", "timestamp": now - 2 * 86400, "duration": 300}],
        })
        youfish.list_subscriptions = lambda: [{"id": "UC_AAA"}, {"id": "UC_BBB"}]
        res = youfish.subscription_feed(force=True)
        self.assertTrue(res["ok"])
        self.assertEqual([i["title"] for i in res["items"]], ["A new", "B mid", "A old"])  # cross-channel sort
        self.assertEqual(res["items"][0]["duration"], 120)          # duration inline, no 2nd pass
        self.assertEqual(res["items"][0]["channel_id"], "UC_AAA")

    def test_members_only_dropped(self):
        now = int(time.time())
        self._mock({"UC_AAA": [
            {"id": "okokokokok1", "title": "ok", "timestamp": now, "duration": 10},
            {"id": "memememem01", "title": "members", "timestamp": now, "duration": 10,
             "availability": "subscriber_only"}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_AAA"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["ok"])

    def test_cache_hit_skips_ytdlp(self):
        now = int(time.time())
        self._mock({"UC_AAA": [{"id": "cccccccccc1", "title": "c", "timestamp": now, "duration": 10}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_AAA"}]
        youfish.subscription_feed(force=True)                       # populate cache
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        res = youfish.subscription_feed(force=False)
        self.assertTrue(res.get("cached"))
        self.assertEqual(calls[0], 0)                               # served from cache, no spawn

    def test_no_subs_is_empty(self):
        youfish.list_subscriptions = lambda: []
        self.assertEqual(youfish.subscription_feed(force=True), {"ok": True, "items": []})

    def test_channel_failure_contributes_nothing(self):
        # yt-dlp fails AND the RSS fallback fails (urlopen raises, per setUp) → nothing from UC_BAD.
        now = int(time.time())
        def fake_run(cmd, **kw):
            if "UC_BAD" in cmd[-1]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="gone")
            data = {"channel": "UC_OK", "entries":
                    [{"id": "okokokokok1", "title": "ok", "timestamp": now, "duration": 10}]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run
        youfish.list_subscriptions = lambda: [{"id": "UC_BAD"}, {"id": "UC_OK"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["ok"])  # bad channel dropped, feed still built

    def test_rss_fallback_when_ytdlp_returns_nothing(self):
        # yt-dlp yields nothing for the channel → fall back to its RSS feed (dur/live absent).
        self._mock({"UC_R": []})              # yt-dlp returns an empty tab
        rss = ('<feed><entry><yt:videoId>rssvid00001</yt:videoId><title>RSS vid</title>'
               '<published>2026-08-30T12:00:00+00:00</published>'
               '<author><name>RSS Chan</name></author>'
               '<media:statistics views="42"/></entry></feed>')
        def fake_open(req, timeout=0):
            import io
            class R(io.BytesIO):
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return R(rss.encode())
        youfish.urllib.request.urlopen = fake_open
        youfish.list_subscriptions = lambda: [{"id": "UC_R"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["RSS vid"])
        it = res["items"][0]
        self.assertEqual(it["duration"], 0)          # RSS has no duration
        self.assertEqual(it["live"], 0)              # …nor live status
        self.assertEqual(it["views"], 42)

    def test_disk_cache_survives_worker_restart(self):
        # A cold launch (fresh in-memory cache) hydrates the per-channel cache from disk and shows
        # it WITHOUT spawning yt-dlp — the stale-while-revalidate win.
        now = int(time.time())
        self._mock({"UC_D": [{"id": "diskvid0001", "title": "disk", "timestamp": now, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_D"}]
        youfish.subscription_feed(force=True)                 # build + persist to disk
        self.assertTrue(os.path.exists(youfish._feed_cache_path()))
        youfish._feed_cache = {}                              # simulate an app relaunch...
        youfish._feed_state = {"loaded": False}               # ...forcing a fresh hydrate from disk
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        res = youfish.subscription_feed(force=False)
        self.assertTrue(res.get("cached"))
        self.assertEqual([i["title"] for i in res["items"]], ["disk"])   # served from disk
        self.assertEqual(calls[0], 0)                         # no yt-dlp spawn on cold launch

    def test_stale_flag_set_when_a_channel_is_due(self):
        now = int(time.time())
        self._mock({"UC_S": [{"id": "stalevid001", "title": "s", "timestamp": now, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_S"}]
        youfish.subscription_feed(force=True)                 # fresh, hot channel → not due
        self.assertFalse(youfish.subscription_feed(force=False)["stale"])
        youfish._feed_cache["UC_S"]["ts"] = time.time() - 3600   # age past the 10-min hot TTL
        res = youfish.subscription_feed(force=False)
        self.assertTrue(res["cached"] and res["stale"])       # served but flagged for bg refresh

    def test_background_refresh_skips_dormant_fetches_hot(self):
        now = int(time.time())
        self._mock({
            "UC_HOT": [{"id": "hotvideo001", "title": "hot", "timestamp": now, "duration": 9}],
            "UC_DORM": [{"id": "dormvideo01", "title": "dorm", "timestamp": now - 90 * 86400, "duration": 9}],
        })
        youfish.list_subscriptions = lambda: [{"id": "UC_HOT"}, {"id": "UC_DORM"}]
        youfish.subscription_feed(force=True)                 # build both
        # 40 min later: HOT (10-min TTL) is due, DORMANT (24-h TTL) is not.
        for cid in ("UC_HOT", "UC_DORM"):
            youfish._feed_cache[cid]["ts"] = time.time() - 40 * 60
        fetched = []
        prev = youfish.subprocess.run
        def spy(cmd, **kw):
            fetched.append(next((c for c in ("UC_HOT", "UC_DORM") if c in cmd[-1]), "?"))
            return prev(cmd, **kw)
        youfish.subprocess.run = spy
        youfish.subscription_feed(force=True)                 # background refresh (refresh_all=False)
        self.assertEqual(fetched, ["UC_HOT"])                 # only the hot channel re-fetched

    def test_refresh_all_refetches_even_dormant(self):
        now = int(time.time())
        self._mock({"UC_DORM": [{"id": "dormvideo01", "title": "d", "timestamp": now - 90 * 86400, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_DORM"}]
        youfish.subscription_feed(force=True)                 # build (dormant, not due)
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        youfish.subscription_feed(force=True, refresh_all=True)   # pull-to-refresh
        self.assertEqual(calls[0], 1)                         # dormant re-fetched anyway

    def test_failed_refresh_keeps_last_good(self):
        now = int(time.time())
        self._mock({"UC_K": [{"id": "keepvideo01", "title": "keep", "timestamp": now, "duration": 9}]})
        youfish.list_subscriptions = lambda: [{"id": "UC_K"}]
        youfish.subscription_feed(force=True)                 # cache a good entry
        youfish._feed_cache["UC_K"]["ts"] = time.time() - 3600   # make it due
        # Now both sources fail for this channel (yt-dlp rc!=0, RSS raises per setUp).
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="x")
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["keep"])   # last-good retained, not blanked

    def test_empty_channel_not_refetched_every_refresh(self):
        # A channel that returns nothing is stamped so it retries hourly, not on every refresh
        # (else it'd thrash yt-dlp and pin the feed 'stale' forever).
        self._mock({"UC_E": []})                              # yt-dlp empty; RSS also fails (setUp)
        youfish.list_subscriptions = lambda: [{"id": "UC_E"}]
        youfish.subscription_feed(force=True)                 # first fetch → empty, but stamped
        self.assertIn("UC_E", youfish._feed_cache)
        self.assertFalse(youfish.subscription_feed(force=False)["stale"])   # not perpetually stale
        calls = [0]
        prev = youfish.subprocess.run
        def counting(cmd, **kw):
            calls[0] += 1
            return prev(cmd, **kw)
        youfish.subprocess.run = counting
        youfish.subscription_feed(force=True)                 # a refresh moments later
        self.assertEqual(calls[0], 0)                         # within the 1-h retry window → skipped

    def test_live_stream_pinned_to_top(self):
        now = int(time.time())
        self._mock({"UC_L": [
            {"id": "olduploadd01", "title": "old upload", "timestamp": now - 30 * 86400, "duration": 100},
            {"id": "livestream01", "title": "LIVE now", "live_status": "is_live"},   # no timestamp
        ]})
        youfish.list_subscriptions = lambda: [{"id": "UC_L"}]
        res = youfish.subscription_feed(force=True)
        self.assertEqual([i["title"] for i in res["items"]], ["LIVE now", "old upload"])  # live pinned top
        self.assertEqual(res["items"][0]["live"], 1)


class CaptionCuesCache(unittest.TestCase):
    """caption_cues fetches once, caches on success, and leaves failures retryable."""
    def setUp(self):
        youfish._caption_cache.clear()
        self._open = youfish.urllib.request.urlopen
        self.calls = 0

    def tearDown(self):
        youfish.urllib.request.urlopen = self._open
        youfish._caption_cache.clear()

    def _mock(self, payload, fail=False):
        import io
        def fake(req, timeout=0):
            self.calls += 1
            if fail:
                raise youfish.urllib.error.URLError("boom")
            class R(io.BytesIO):
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return R(json.dumps(payload).encode())
        youfish.urllib.request.urlopen = fake

    def test_success_is_cached(self):
        self._mock({"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hi"}]}]})
        r1 = youfish.caption_cues("http://x/a")
        r2 = youfish.caption_cues("http://x/a")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertEqual(r1["cues"], r2["cues"])
        self.assertEqual(self.calls, 1)                 # second call served from cache

    def test_failure_not_cached_and_retryable(self):
        self._mock(None, fail=True)
        r = youfish.caption_cues("http://x/b")
        self.assertFalse(r["ok"])
        self.assertNotIn("http://x/b", youfish._caption_cache)
        self.assertEqual(self.calls, 1)                 # a later call would try again

    def test_empty_url(self):
        self.assertEqual(youfish.caption_cues(""), {"ok": False, "cues": []})


# --- YouTube login (cookies) + subscription import ------------------------------------------- #

def _fake_ytm(text):
    """Install a fake `ytm` module exposing netscape_cookies() → text, for the engine's lazy
    `import ytm`. Returns the previous sys.modules entry (or None) so the caller can restore it."""
    prev = sys.modules.get("ytm")
    m = types.ModuleType("ytm")
    m.netscape_cookies = lambda: text
    sys.modules["ytm"] = m
    return prev


class CookiesArgs(unittest.TestCase):
    def setUp(self):
        self._prev = sys.modules.get("ytm")

    def tearDown(self):
        if self._prev is not None:
            sys.modules["ytm"] = self._prev
        else:
            sys.modules.pop("ytm", None)

    def test_signed_out_yields_empty(self):
        _fake_ytm("")                               # netscape_cookies() == "" → no --cookies
        with youfish._cookies_args() as cargs:
            self.assertEqual(cargs, [])

    def test_signed_in_yields_ephemeral_file_removed_after(self):
        _fake_ytm("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tv\n")
        with youfish._cookies_args() as cargs:
            self.assertEqual(cargs[0], "--cookies")
            path = cargs[1]
            self.assertTrue(os.path.exists(path))   # exists during the with
            with open(path) as f:
                self.assertIn("SAPISID", f.read())
        self.assertFalse(os.path.exists(path))      # and is removed after


class ImportYoutubeSubs(unittest.TestCase):
    def setUp(self):
        self._ytdlp = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        self._ls = youfish.list_subscriptions
        self._save = youfish._save_subscriptions
        self._prev_ytm = sys.modules.get("ytm")
        self._store = []
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish.list_subscriptions = lambda: list(self._store)
        youfish._save_subscriptions = lambda subs: self._store.__setitem__(slice(None), subs)
        _fake_ytm("# ck\n")                          # signed in

    def tearDown(self):
        youfish._ytdlp_path = self._ytdlp
        youfish.subprocess.run = self._run
        youfish.list_subscriptions = self._ls
        youfish._save_subscriptions = self._save
        if self._prev_ytm is not None:
            sys.modules["ytm"] = self._prev_ytm
        else:
            sys.modules.pop("ytm", None)

    def _mock_feed(self, entries):
        data = {"entries": entries}
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(data), stderr="")

    def test_maps_dedupes_and_saves(self):
        self._store[:] = [{"id": "UChave", "name": "Have", "url": "", "thumbnail": ""}]
        self._mock_feed([
            {"id": "UCnew1", "url": "https://www.youtube.com/channel/UCnew1",
             "title": "New One", "thumbnails": [{"url": "t1"}]},
            {"id": "UChave", "url": "x", "title": "Have"},                       # already subscribed
            {"url": "https://www.youtube.com/channel/UCfromurl", "title": "URL Only"},  # id from url
            None,                                                                # malformed
        ])
        res = youfish.import_youtube_subscriptions()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["added"], 2)                # UCnew1 + UCfromurl (UChave deduped)
        ids = [s["id"] for s in self._store]
        self.assertEqual(ids, ["UChave", "UCnew1", "UCfromurl"])
        new1 = [s for s in self._store if s["id"] == "UCnew1"][0]
        self.assertEqual(new1["name"], "New One")
        self.assertEqual(new1["thumbnail"], "t1")
        self.assertEqual(new1["url"], "https://www.youtube.com/channel/UCnew1")

    def test_signed_out_is_a_clear_error(self):
        _fake_ytm("")                                    # not signed in
        self._mock_feed([{"id": "UCx", "title": "X"}])
        res = youfish.import_youtube_subscriptions()
        self.assertFalse(res["ok"])
        self.assertIn("signed in", res["error"].lower())

    def test_ytdlp_failure_surfaces(self):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=1, stdout="", stderr="feed error")
        res = youfish.import_youtube_subscriptions()
        self.assertFalse(res["ok"])


class ImportYoutubePlaylists(unittest.TestCase):
    def setUp(self):
        self._ytdlp = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        self._lp = youfish._load_playlists
        self._sp = youfish._save_playlists
        self._prev_ytm = sys.modules.get("ytm")
        self._store = []
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._load_playlists = lambda: list(self._store)
        youfish._save_playlists = lambda lst: self._store.__setitem__(slice(None), lst)
        _fake_ytm("# ck\n")

    def tearDown(self):
        youfish._ytdlp_path = self._ytdlp
        youfish.subprocess.run = self._run
        youfish._load_playlists = self._lp
        youfish._save_playlists = self._sp
        if self._prev_ytm is not None:
            sys.modules["ytm"] = self._prev_ytm
        else:
            sys.modules.pop("ytm", None)

    def _mock_feed(self, entries):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps({"entries": entries}), stderr="")

    def test_maps_dedupes_and_stores_empty_youtube_entries(self):
        self._store[:] = [{"id": "aaa", "title": "Old", "kind": "youtube",
                           "yt_id": "PLhave", "items": []}]
        self._mock_feed([
            {"id": "PLnew", "url": "https://www.youtube.com/playlist?list=PLnew", "title": "New PL"},
            {"id": "PLhave", "title": "Old"},                                  # dupe by yt_id
            {"url": "https://www.youtube.com/playlist?list=LL", "title": "Liked"},  # id from url
            None,
        ])
        res = youfish.import_youtube_playlists()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["added"], 2)                       # PLnew + LL (PLhave deduped)
        yt_ids = [p["yt_id"] for p in self._store]
        self.assertIn("PLnew", yt_ids)
        self.assertIn("LL", yt_ids)
        newpl = [p for p in self._store if p["yt_id"] == "PLnew"][0]
        self.assertEqual(newpl["kind"], "youtube")
        self.assertEqual(newpl["items"], [])                    # lazy — fetched on open
        self.assertEqual(newpl["title"], "New PL")

    def test_skips_channel_id_rows(self):
        # feed/playlists should only list playlists; if a channel-id (UC…) row slips in, it must
        # NOT be stored as a bogus playlist (opening it would build playlist?list=UC… and fail).
        self._mock_feed([
            {"id": "UCnotaplaylist", "title": "Not a playlist"},
            {"id": "PLreal", "title": "Real"},
        ])
        res = youfish.import_youtube_playlists()
        self.assertEqual(res["added"], 1)
        self.assertEqual([p["yt_id"] for p in self._store], ["PLreal"])

    def test_signed_out_is_a_clear_error(self):
        _fake_ytm("")
        self._mock_feed([{"id": "PLx", "title": "X"}])
        res = youfish.import_youtube_playlists()
        self.assertFalse(res["ok"])
        self.assertIn("signed in", res["error"].lower())


class ImportYoutubeAccount(unittest.TestCase):
    def setUp(self):
        self._ytdlp = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        self._ls = youfish.list_subscriptions
        self._save_s = youfish._save_subscriptions
        self._lp = youfish._load_playlists
        self._sp = youfish._save_playlists
        self._prev_ytm = sys.modules.get("ytm")
        self._subs, self._pls = [], []
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish.list_subscriptions = lambda: list(self._subs)
        youfish._save_subscriptions = lambda s: self._subs.__setitem__(slice(None), s)
        youfish._load_playlists = lambda: list(self._pls)
        youfish._save_playlists = lambda p: self._pls.__setitem__(slice(None), p)
        _fake_ytm("# ck\n")

        def fake(cmd, **kw):
            u = cmd[-1]                                          # the url is the last argv element
            if "feed/channels" in u:
                entries = [{"id": "UCa", "title": "Chan A",
                            "url": "https://www.youtube.com/channel/UCa"}]
            elif "feed/playlists" in u:
                entries = [{"id": "PLa", "title": "PL A",
                            "url": "https://www.youtube.com/playlist?list=PLa"}]
            else:
                entries = []
            return types.SimpleNamespace(returncode=0,
                                         stdout=json.dumps({"entries": entries}), stderr="")
        youfish.subprocess.run = fake

    def tearDown(self):
        youfish._ytdlp_path = self._ytdlp
        youfish.subprocess.run = self._run
        youfish.list_subscriptions = self._ls
        youfish._save_subscriptions = self._save_s
        youfish._load_playlists = self._lp
        youfish._save_playlists = self._sp
        if self._prev_ytm is not None:
            sys.modules["ytm"] = self._prev_ytm
        else:
            sys.modules.pop("ytm", None)

    def test_imports_both_and_combines_summary(self):
        res = youfish.import_youtube_account()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["subs_added"], 1)
        self.assertEqual(res["playlists_added"], 1)
        self.assertIn("subscription", res["summary"])
        self.assertIn("playlist", res["summary"])
        self.assertEqual([s["id"] for s in self._subs], ["UCa"])
        self.assertEqual([p["yt_id"] for p in self._pls], ["PLa"])

    def test_reimport_says_nothing_new(self):
        # Both halves succeed but add nothing (already imported) → clean "Nothing new" summary,
        # not "Imported 0 subscriptions and 0 playlists."
        self._subs[:] = [{"id": "UCa", "name": "Chan A", "url": "", "thumbnail": ""}]
        self._pls[:] = [{"id": "x", "title": "PL A", "kind": "youtube", "yt_id": "PLa", "items": []}]
        res = youfish.import_youtube_account()
        self.assertTrue(res["ok"])
        self.assertEqual(res["subs_added"], 0)
        self.assertEqual(res["playlists_added"], 0)
        self.assertEqual(res["summary"], "Nothing new to import.")

    def test_signed_out_fails_cleanly(self):
        _fake_ytm("")
        res = youfish.import_youtube_account()
        self.assertFalse(res["ok"])
        self.assertIn("signed in", res["error"].lower())
        self.assertEqual(res["subs_added"], 0)          # always present, even on the fail path
        self.assertEqual(res["playlists_added"], 0)


class YtmCookieModule(unittest.TestCase):
    def setUp(self):
        import ytm
        self.ytm = ytm
        self._dd = ytm._data_dir
        self._rj = ytm._read_cookie_jar
        self._paths = ytm._BROWSER_COOKIE_PATHS
        self._tmp = tempfile.mkdtemp()
        # Patch _data_dir (which _cookies_path derives from AND _save_cookies uses for its temp), so
        # the atomic os.replace stays within one filesystem instead of crossing /tmp <-> the real dir.
        ytm._data_dir = lambda: self._tmp

    def tearDown(self):
        self.ytm._data_dir = self._dd
        self.ytm._read_cookie_jar = self._rj
        self.ytm._BROWSER_COOKIE_PATHS = self._paths
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_netscape_from_rows_format(self):
        rows = [("SAPISID", "v", ".youtube.com", "/", 123, 1),
                ("X", "y", "youtube.com", "", 0, 0)]
        txt = self.ytm._netscape_from_rows(rows)
        self.assertTrue(txt.startswith("# Netscape HTTP Cookie File"))
        self.assertIn("\t".join([".youtube.com", "TRUE", "/", "TRUE", "123", "SAPISID", "v"]), txt)
        self.assertIn("\t".join(["youtube.com", "FALSE", "/", "FALSE", "0", "X", "y"]), txt)  # ""→"/"

    def test_status_roundtrip_and_logout(self):
        self.assertFalse(self.ytm.login_status()["logged_in"])
        self.assertEqual(self.ytm.netscape_cookies(), "")
        self.ytm._save_cookies({"ytdlp": "# ck\n", "count": 5,
                                "imported_at": 111, "source": "/jar"})
        st = self.ytm.login_status()
        self.assertTrue(st["logged_in"])
        self.assertEqual(st["count"], 5)
        self.assertEqual(self.ytm.netscape_cookies(), "# ck\n")
        self.ytm.logout()
        self.assertFalse(self.ytm.login_status()["logged_in"])
        self.assertEqual(self.ytm.netscape_cookies(), "")

    def test_import_rejects_jar_without_session(self):
        # cookies present but no login marker (no SAPISID/*PSID) → clear "not signed in" error.
        self.ytm._read_cookie_jar = lambda p: [("PREF", "x", ".youtube.com", "/", 0, 0)]
        self.ytm._BROWSER_COOKIE_PATHS = [self._tmp + "/cookies.sqlite"]
        open(self._tmp + "/cookies.sqlite", "w").close()   # make the candidate path exist
        res = self.ytm.import_browser_login()
        self.assertFalse(res["ok"])
        self.assertIn("signed-in", res["error"].lower())


class CommentsThreads(unittest.TestCase):
    """comments() nests replies under their parent thread and bounds the reply walk."""
    def setUp(self):
        self._path = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"

    def tearDown(self):
        youfish._ytdlp_path = self._path
        youfish.subprocess.run = self._run

    def _mock(self, payload):
        self.captured = {}
        def fake_run(cmd, **kw):
            self.captured["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        youfish.subprocess.run = fake_run

    def _xargs(self):
        cmd = self.captured["cmd"]
        return cmd[cmd.index("--extractor-args") + 1]

    def test_replies_nest_under_parent_in_order(self):
        self._mock({"comment_count": 99, "comments": [
            {"id": "a", "parent": "root", "text": "top A", "author": "Ann"},
            {"id": "a.1", "parent": "a", "text": "reply A1", "author": "Bo"},
            {"id": "a.2", "parent": "a", "text": "reply A2", "author": "Cy"},
            {"id": "b", "parent": "root", "text": "top B", "author": "Di"},
        ]})
        res = youfish.comments("vid")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["total"], 99)
        self.assertEqual([c["text"] for c in res["comments"]], ["top A", "top B"])
        a = res["comments"][0]
        self.assertEqual([r["text"] for r in a["replies"]], ["reply A1", "reply A2"])
        self.assertEqual(a["reply_count"], 2)
        self.assertEqual(res["comments"][1]["replies"], [])   # B has no replies
        self.assertEqual(res["comments"][1]["reply_count"], 0)

    def test_reply_count_prefers_youtube_total_when_larger(self):
        # yt-dlp reports 40 replies exist but we only fetched 1 (budget) → show the real 40.
        self._mock({"comments": [
            {"id": "a", "parent": "root", "text": "t", "reply_count": 40},
            {"id": "a.1", "parent": "a", "text": "r"},
        ]})
        c = youfish.comments("vid")["comments"][0]
        self.assertEqual(len(c["replies"]), 1)
        self.assertEqual(c["reply_count"], 40)

    def test_with_replies_flag_shapes_extractor_args(self):
        self._mock({"comments": []})
        youfish.comments("vid", limit=50, with_replies=True)
        on = self._xargs()
        self.assertIn("max_comments=%d,50,%d,%d"
                      % (50 + youfish._REPLY_BUDGET, youfish._REPLY_BUDGET,
                         youfish._REPLIES_PER_THREAD), on)
        youfish.comments("vid", limit=50, with_replies=False)
        self.assertIn("max_comments=50,50,0,0", self._xargs())
        self.assertNotIn("player_client=android", self._xargs())   # comments need the web client

    def test_lean_profile_skips_player_and_manifests(self):
        """The walk doesn't need player JS / n-sig (a Deno spawn!) or HLS/DASH manifests —
        skipping them was measured safe (identical comments) and saves seconds on-device."""
        self._mock({"comments": []})
        youfish.comments("vid")
        on = self._xargs()
        self.assertIn("player_skip=js,configs", on)
        self.assertIn("skip=hls,dash", on)
        cmd = self.captured["cmd"]
        self.assertIn("--ignore-no-formats-error", cmd)   # formats-less extraction must not error
        self.assertIn("--write-comments", cmd)

    def test_error_return(self):
        def fail(cmd, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="nope")
        youfish.subprocess.run = fail
        res = youfish.comments("vid")
        self.assertFalse(res["ok"])
        self.assertIn("nope", res["error"])


class VideoInfo(unittest.TestCase):
    """video_info() returns metadata even for an unplayable (formats-less) video."""
    def setUp(self):
        self._path = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"

    def tearDown(self):
        youfish._ytdlp_path = self._path
        youfish.subprocess.run = self._run

    def _mock(self, payload):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr="")

    def test_metadata_and_chapters_no_formats(self):
        self._mock({
            "title": "Hello", "uploader": "Chan", "channel_id": "UC1",
            "channel_url": "https://youtube.com/@chan", "description": "desc here",
            "duration": 212, "view_count": 12345, "like_count": 678,
            "upload_date": "20260101", "thumbnail": "http://t/x.jpg",
            "chapters": [{"start_time": 0, "title": "Intro"},
                         {"start_time": 60, "title": "Part 2"},
                         {"title": "no-start drop"}],
            # deliberately NO "formats" → resolve() would fail, video_info() must not.
        })
        res = youfish.video_info("vid")
        self.assertTrue(res["ok"], res)
        info = res["info"]
        self.assertEqual(info["title"], "Hello")
        self.assertEqual(info["uploader"], "Chan")
        self.assertEqual(info["view_count"], 12345)
        self.assertEqual(info["like_count"], 678)
        self.assertEqual(info["upload_date"], "20260101")
        self.assertEqual(len(info["chapters"]), 2)          # the no-start chapter is dropped
        self.assertEqual(info["chapters"][1]["title"], "Part 2")

    def test_uses_android_client_primary(self):
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"title": "X"}), stderr="")
        youfish.subprocess.run = fake
        youfish.video_info("vid")
        xargs = calls[0][calls[0].index("--extractor-args") + 1]
        self.assertIn("player_client=android", xargs)       # fast mobile-API client first
        self.assertEqual(len(calls), 1)                     # primary ok → no fallback call

    def test_falls_back_to_default_when_android_fails(self):
        calls = []
        def fake(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="android boom")
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"title": "Fell back"}), stderr="")
        youfish.subprocess.run = fake
        res = youfish.video_info("vid")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["info"]["title"], "Fell back")
        self.assertEqual(len(calls), 2)                     # android failed → default retry
        self.assertNotIn("--extractor-args", calls[1])      # fallback pins no client

    def test_error_return(self):
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=1, stdout="", stderr="blocked")
        res = youfish.video_info("vid")
        self.assertFalse(res["ok"])
        self.assertIn("blocked", res["error"])


class SearchDate(unittest.TestCase):
    """search() passes youtubetab:approximate_date so flat results carry a post date (`posted`)."""
    def setUp(self):
        self._path = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"

    def tearDown(self):
        youfish._ytdlp_path = self._path
        youfish.subprocess.run = self._run

    def test_passes_approximate_date_and_maps_posted(self):
        calls = []
        ts = int(time.time()) - 400 * 24 * 3600           # ~1.1 years ago
        def fake(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "entries": [{"id": "abc12345678", "title": "T", "channel": "C",
                             "view_count": 100, "timestamp": ts}]}))
        youfish.subprocess.run = fake
        res = youfish.search("query", n=3)
        self.assertTrue(res["ok"], res)
        # the extractor-arg that unlocks the date is present ...
        xargs = calls[0][calls[0].index("--extractor-args") + 1]
        self.assertIn("approximate_date", xargs)
        # ... and a flat entry's timestamp is surfaced as a human "posted" string
        self.assertTrue(res["items"][0]["posted"])
        self.assertEqual(res["items"][0]["views"], 100)


class SearchFilterSp(unittest.TestCase):
    """_search_filter_sp() reproduces YouTube's own sp protobuf codes."""
    @staticmethod
    def _dec(sp):
        return base64.b64decode(urllib.parse.unquote(sp))

    def test_matches_known_youtube_codes(self):
        # duration <4min alone → 12 02 18 01 (YouTube's EgIYAQ==)
        self.assertEqual(self._dec(youfish._search_filter_sp(dur=1)), b"\x12\x02\x18\x01")
        # sort by view count alone → 08 03 (YouTube's CAM=)
        self.assertEqual(self._dec(youfish._search_filter_sp(sort=3)), b"\x08\x03")
        # today + video-only → sub{ uploaded=2, type=1 } → 12 04 08 02 10 01
        self.assertEqual(self._dec(youfish._search_filter_sp(date=2, video_only=True)),
                         b"\x12\x04\x08\x02\x10\x01")
        # sort + date + dur together → top sort then sub{ date, dur }
        self.assertEqual(self._dec(youfish._search_filter_sp(sort=2, date=4, dur=2)),
                         b"\x08\x02\x12\x04\x08\x04\x18\x02")

    def test_empty_when_nothing_set(self):
        self.assertEqual(youfish._search_filter_sp(), "")


class SearchFiltersRouting(unittest.TestCase):
    """search() uses the sp= results URL only when a filter is set, else plain ytsearch."""
    def setUp(self):
        self._path = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        self.calls = []
        def fake(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"entries": []}))
        youfish.subprocess.run = fake

    def tearDown(self):
        youfish._ytdlp_path = self._path
        youfish.subprocess.run = self._run

    def _target(self):
        return self.calls[0][-1]        # after the "--" separator

    def test_unfiltered_uses_ytsearch(self):
        youfish.search("cats", n=5)
        self.assertTrue(self._target().startswith("ytsearch"))

    def test_filtered_uses_results_url_with_sp(self):
        youfish.search("cats", n=5, filters={"sort": 3})
        self.assertIn("/results?search_query=", self._target())
        self.assertIn("&sp=", self._target())


class SearchChannelsFallback(unittest.TestCase):
    """Channel search on the yt-dlp path (InnerTube unavailable) builds channel rows, keeping
    URL-only entries and dropping ones with neither id nor URL."""
    setUp = SearchFiltersRouting.setUp
    tearDown = SearchFiltersRouting.tearDown

    def test_channel_rows(self):
        entries = [{"id": "UC" + "a" * 22, "channel": "Alpha", "channel_follower_count": 12,
                    "channel_url": "https://www.youtube.com/channel/UC" + "a" * 22},
                   {"url": "https://www.youtube.com/@beta", "title": "Beta"},
                   {"title": "nothing to open"}]
        youfish.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({"entries": entries}))
        res = youfish.search("x", n=5, kind="channel")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["kind"], "channel")
        self.assertEqual([(i["type"], i["title"]) for i in res["items"]],
                         [("channel", "Alpha"), ("channel", "Beta")])
        self.assertEqual(res["items"][0]["subscribers"], 12)
        self.assertEqual(res["items"][1]["url"], "https://www.youtube.com/@beta")


class Related(unittest.TestCase):
    """related() pulls the RD mix flat and drops the seed video."""
    def setUp(self):
        self._path = youfish._ytdlp_path
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        self.calls = []

    def tearDown(self):
        youfish._ytdlp_path = self._path
        youfish.subprocess.run = self._run

    def test_builds_rd_url_and_drops_seed(self):
        def fake(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "entries": [
                    {"id": "SEED0000000", "title": "seed"},           # the seed → dropped
                    {"id": "aaa11111111", "title": "A", "view_count": 5},
                    {"id": "bbb22222222", "title": "B"},
                ]}))
        youfish.subprocess.run = fake
        res = youfish.related("SEED0000000", n=10)
        self.assertTrue(res["ok"], res)
        ids = [it["id"] for it in res["items"]]
        self.assertEqual(ids, ["aaa11111111", "bbb22222222"])         # seed dropped, order kept
        self.assertIn("list=RDSEED0000000", self.calls[0][-1])        # RD mix of the seed video

    def test_error_when_no_id(self):
        youfish.subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="{}", stderr="")
        self.assertFalse(youfish.related("")["ok"])


# --- fast resolve (in-process yt-dlp) --------------------------------------------------------- #

class ExtractorArgParse(unittest.TestCase):
    """_parse_extractor_args turns the yt-dlp argv fragment into YoutubeDL's extractor_args dict."""
    def test_empty(self):
        self.assertEqual(youfish._parse_extractor_args([]), {})

    def test_single_client(self):
        self.assertEqual(
            youfish._parse_extractor_args(["--extractor-args", "youtube:player_client=tv_embedded"]),
            {"youtube": {"player_client": ["tv_embedded"]}})

    def test_multi_client_and_fetch_pot(self):
        self.assertEqual(
            youfish._parse_extractor_args(
                ["--extractor-args", "youtube:player_client=tv,mweb;fetch_pot=always"]),
            {"youtube": {"player_client": ["tv", "mweb"], "fetch_pot": ["always"]}})

    def test_bare_key_and_missing_value_dont_crash(self):
        self.assertEqual(youfish._parse_extractor_args(["--extractor-args", "youtube:flag"]),
                         {"youtube": {"flag": []}})
        self.assertEqual(youfish._parse_extractor_args(["--extractor-args"]), {})   # value absent


class ZipappVersion(unittest.TestCase):
    """_zipapp_version reads yt_dlp/version.py's __version__ WITHOUT importing the zipapp (importing
    would pin the whole process to one yt_dlp version)."""
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zipapp-")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_reads_version_from_zip(self):
        import zipfile
        p = os.path.join(self._tmp, "yt-dlp.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("yt_dlp/__init__.py", "")
            z.writestr("yt_dlp/version.py", "__version__ = '2026.08.19'\n")
        self.assertEqual(youfish._zipapp_version(p), "2026.08.19")

    def test_non_zip_returns_blank(self):
        p = os.path.join(self._tmp, "junk")
        with open(p, "wb") as f:
            f.write(b"not a zip")
        self.assertEqual(youfish._zipapp_version(p), "")


class FastResolveStatusAndGate(unittest.TestCase):
    def setUp(self):
        self._zp, self._imp, self._pyok = (youfish._ytdlp_zipapp_path,
                                           youfish._import_yt_dlp,
                                           youfish._FAST_RESOLVE_PY_OK)
        self._tmp = tempfile.mkdtemp(prefix="frs-")

    def tearDown(self):
        (youfish._ytdlp_zipapp_path, youfish._import_yt_dlp,
         youfish._FAST_RESOLVE_PY_OK) = self._zp, self._imp, self._pyok
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_status_not_installed(self):
        youfish._ytdlp_zipapp_path = lambda: os.path.join(self._tmp, "absent.zip")
        st = youfish.fast_resolve_status()
        self.assertEqual((st["installed"], st["version"]), (False, ""))

    def test_status_installed_reports_version(self):
        import zipfile
        p = os.path.join(self._tmp, "yt-dlp.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("yt_dlp/__init__.py", "")
            z.writestr("yt_dlp/version.py", "__version__ = '2026.08.19'\n")
        youfish._ytdlp_zipapp_path = lambda: p
        st = youfish.fast_resolve_status()
        self.assertEqual((st["installed"], st["version"]), (True, "2026.08.19"))

    def test_ready_false_when_python_too_old_never_touches_zip(self):
        # The REAL _import_yt_dlp with the gate forced off: returns None before ever looking
        # at the zipapp (path mock would explode if consulted), and burns no import cache.
        youfish._FAST_RESOLVE_PY_OK = False
        youfish._ytdlp_zipapp_path = lambda: (_ for _ in ()).throw(AssertionError("touched zip"))
        self.assertIsNone(_REAL_IMPORT_YT_DLP())
        self.assertFalse(youfish._fast_resolve_ready())

    def test_ready_false_when_import_unavailable(self):
        youfish._import_yt_dlp = lambda: None
        self.assertFalse(youfish._fast_resolve_ready())

    def test_ready_true_when_importable(self):
        youfish._import_yt_dlp = lambda: object()
        self.assertTrue(youfish._fast_resolve_ready())

    def test_status_reports_device_python_gate(self):
        # The UI's honest why-not text keys off these; python_ok mirrors the module gate,
        # which itself reflects THIS interpreter (yt-dlp needs >= 3.10).
        youfish._ytdlp_zipapp_path = lambda: os.path.join(self._tmp, "absent.zip")
        st = youfish.fast_resolve_status()
        self.assertEqual(st["python_ok"], youfish._FAST_RESOLVE_PY_OK)
        self.assertEqual(st["python_ok"], sys.version_info >= (3, 10))
        self.assertEqual(st["python_version"], "%d.%d" % sys.version_info[:2])

    def test_fast_resolve_is_not_a_setting(self):
        # Deliberate: in-process yt-dlp is plumbing, not a preference — no defaults key
        # (a stale stored "fast_resolve" from older builds is ignored, never consulted).
        self.assertNotIn("fast_resolve", youfish._SETTINGS_DEFAULTS)


class ZipappAutofetch(unittest.TestCase):
    """_autofetch_zipapp: launch-time self-heal — fetch the missing copy exactly once per
    process, only on a capable python, only when the consented-to binary is already there."""

    def setUp(self):
        self._saved = dict(zp=youfish._ytdlp_zipapp_path, yp=youfish._ytdlp_path,
                           inst=youfish.install_ytdlp_zipapp, done=youfish._ZIPAPP_AUTOFETCH_DONE,
                           pyok=youfish._FAST_RESOLVE_PY_OK)
        youfish._ZIPAPP_AUTOFETCH_DONE = False
        youfish._FAST_RESOLVE_PY_OK = True
        self.calls = []
        youfish.install_ytdlp_zipapp = lambda: self.calls.append(1)
        self._tmp = tempfile.mkdtemp(prefix="zaf-")

    def tearDown(self):
        (youfish._ytdlp_zipapp_path, youfish._ytdlp_path, youfish.install_ytdlp_zipapp) = (
            self._saved["zp"], self._saved["yp"], self._saved["inst"])
        youfish._ZIPAPP_AUTOFETCH_DONE = self._saved["done"]
        youfish._FAST_RESOLVE_PY_OK = self._saved["pyok"]
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_fetches_once_when_missing_and_binary_present(self):
        youfish._ytdlp_zipapp_path = lambda: os.path.join(self._tmp, "absent.zip")
        youfish._ytdlp_path = lambda: os.path.join(self._tmp, "yt-dlp")
        youfish._autofetch_zipapp()
        youfish._autofetch_zipapp()      # second prewarm nudge in the same process
        self.assertEqual(len(self.calls), 1)

    def test_installer_refuses_when_python_too_old(self):
        # Engine-side belt for the QML gate: a too-old python must never even download the
        # zipapp (rides can race the async status load). Emits an honest done(False) event.
        import types as _t
        prev = sys.modules.get("pyotherside")
        events = []
        sys.modules["pyotherside"] = _t.SimpleNamespace(
            send=lambda *a: events.append(a))
        try:
            youfish._FAST_RESOLVE_PY_OK = False
            res = self._saved["inst"]()          # the REAL installer (setUp mocks the name)
            self.assertEqual(res, {"ok": False})
            self.assertTrue(events and events[0][0] == "ytdlp_zipapp_done"
                            and events[0][1] is False)
        finally:
            if prev is not None:
                sys.modules["pyotherside"] = prev
            else:
                sys.modules.pop("pyotherside", None)

    def test_skips_when_python_too_old(self):
        youfish._FAST_RESOLVE_PY_OK = False
        youfish._ytdlp_zipapp_path = lambda: os.path.join(self._tmp, "absent.zip")
        youfish._ytdlp_path = lambda: os.path.join(self._tmp, "yt-dlp")
        youfish._autofetch_zipapp()
        self.assertEqual(self.calls, [])

    def test_skips_when_already_present_or_no_binary(self):
        p = os.path.join(self._tmp, "have.zip")
        open(p, "wb").close()
        youfish._ytdlp_zipapp_path = lambda: p
        youfish._ytdlp_path = lambda: os.path.join(self._tmp, "yt-dlp")
        youfish._autofetch_zipapp()      # zipapp already there
        youfish._ZIPAPP_AUTOFETCH_DONE = False
        youfish._ytdlp_zipapp_path = lambda: os.path.join(self._tmp, "absent.zip")
        youfish._ytdlp_path = lambda: ""
        youfish._autofetch_zipapp()      # no binary yet (fresh install pre-consent)
        self.assertEqual(self.calls, [])


class _FakeJar:
    def clear(self):
        self.cleared = True

    def load(self, path):
        self.loaded = path


class _FakeYDL:
    """Stand-in YoutubeDL: records params + extract calls, returns canned info (or raises)."""
    instances = []
    raise_on_extract = False
    formats = []

    def __init__(self, params):
        self.params = dict(params)
        self.cookiejar = _FakeJar()
        self.extract_calls = []
        _FakeYDL.instances.append(self)

    def extract_info(self, url, download=False):
        self.extract_calls.append((url, download, dict(self.params.get("extractor_args") or {})))
        if _FakeYDL.raise_on_extract:
            raise RuntimeError("boom")
        return {"title": "T", "formats": list(_FakeYDL.formats), "duration": 100,
                "_internal": "strip-me"}

    def sanitize_info(self, info):
        info = dict(info)
        info.pop("_internal", None)          # the real sanitize_info strips yt-dlp internals
        return info


class FastResolveRouting(unittest.TestCase):
    """resolve() runs the token-free hot dump IN-PROCESS whenever the zipapp is importable, and
    falls back to the binary on any in-process failure or for the token (fetch_pot) path."""
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args", "_yt_extractor_args",
                     "_proxied", "get_settings", "_import_yt_dlp", "_pot_active"):
            self._saved[name] = getattr(youfish, name)
        self._run = youfish.subprocess.run
        self._tls = youfish._inproc_tls

        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._pot_active = lambda: False          # no probe / token path in the common case
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False}
        youfish._import_yt_dlp = lambda: types.SimpleNamespace(YoutubeDL=_FakeYDL)
        youfish._inproc_tls = youfish.threading.local()   # fresh warm-instance store per test
        youfish.invalidate_resolve_cache()

        _FakeYDL.instances = []
        _FakeYDL.raise_on_extract = False
        _FakeYDL.formats = [vf("137", 1080, "avc1"), af("251", 160, "opus", lang_pref=10)]

        self.binary_calls = []

        def fake_run(cmd, **kw):
            self.binary_calls.append(cmd)
            data = {"title": "BIN", "duration": 100,
                    "formats": [vf("137", 1080, "avc1"), af("251", 160, "opus", lang_pref=10)]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(youfish, name, fn)
        youfish.subprocess.run = self._run
        youfish._inproc_tls = self._tls

    def test_token_free_dump_runs_in_process(self):
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: (
            ["--extractor-args", "youtube:player_client=mweb;fetch_pot=always"] if want_pot else
            ["--extractor-args", "youtube:player_client=tv_embedded"])
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "T")            # from the in-process fake, not "BIN"
        self.assertEqual(res["info"]["video_itag"], "137")
        self.assertEqual(res["info"]["audio_itag"], "251")
        self.assertEqual(self.binary_calls, [])                # binary NEVER spawned
        self.assertEqual(len(_FakeYDL.instances), 1)           # one warm instance built
        ydl = _FakeYDL.instances[0]
        self.assertEqual(ydl.params.get("source_address"), "0.0.0.0")          # == -4 (force IPv4)
        self.assertEqual(ydl.extract_calls[0][2], {"youtube": {"player_client": ["tv_embedded"]}})

    def test_warm_instance_reused_across_resolves(self):
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=tv_embedded"]
        youfish.resolve("vidA")
        youfish.resolve("vidB")
        self.assertEqual(len(_FakeYDL.instances), 1)           # same warm YoutubeDL, not rebuilt
        self.assertEqual(len(_FakeYDL.instances[0].extract_calls), 2)

    def test_inproc_failure_falls_back_to_binary(self):
        _FakeYDL.raise_on_extract = True
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=tv_embedded"]
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")          # binary served it
        self.assertTrue(self.binary_calls)                     # fallback actually ran

    def test_token_path_never_runs_in_process(self):
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=mweb;fetch_pot=always"]
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")
        self.assertEqual(_FakeYDL.instances, [])               # in-process path skipped entirely

    def test_no_zipapp_uses_binary(self):
        youfish._import_yt_dlp = lambda: None   # copy absent / not importable → binary path
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "BIN")
        self.assertEqual(_FakeYDL.instances, [])


class AnonymousPrimary(unittest.TestCase):
    """The token-free PRIMARY dump resolves WITHOUT cookies (anonymous) — YouTube gates authenticated
    token-free requests but not anonymous ones. Only the fallback re-runs WITH cookies (restricted
    content). Exercised on the binary path (zipapp pinned absent)."""
    def setUp(self):
        self._saved = {}
        for name in ("_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args", "_yt_extractor_args",
                     "_proxied", "get_settings", "_cookies_args", "_pot_active", "_import_yt_dlp"):
            self._saved[name] = getattr(youfish, name)
        self._run = youfish.subprocess.run
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._pot_active = lambda: False
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False}
        youfish._import_yt_dlp = lambda: None   # zipapp pinned absent → binary path

        import contextlib

        @contextlib.contextmanager
        def _ck():
            yield ["--cookies", "CKFILE"]
        youfish._cookies_args = _ck
        # primary (want_pot=False) → tv_embedded; fallback (want_pot=True) → the wider retry set
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: \
            ["--extractor-args", "youtube:player_client=" + (client_override or "tv_embedded")]
        youfish.invalidate_resolve_cache()
        self.calls = []

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run

    def _full(self):
        return {"title": "T", "duration": 100,
                "formats": [vf("137", 1080, "avc1"), af("251", 160, "opus", lang_pref=10)]}

    def test_public_primary_is_anonymous(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self._full()), stderr="")
        youfish.subprocess.run = run
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(self.calls), 1)                  # public → one dump, no fallback
        self.assertNotIn("--cookies", self.calls[0])          # PRIMARY carries no cookies

    def test_restricted_falls_back_with_cookies(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            if "tv_embedded" in " ".join(cmd):                # the anonymous primary FAILS (restricted)
                return types.SimpleNamespace(returncode=1, stdout="", stderr="Sign in to confirm")
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(self._full()), stderr="")
        youfish.subprocess.run = run
        res = youfish.resolve("vid")
        self.assertTrue(res.get("ok"), res)
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertNotIn("--cookies", self.calls[0])          # primary anonymous
        self.assertIn("--cookies", self.calls[1])             # fallback re-runs WITH cookies


class ReresolveAnonFirst(unittest.TestCase):
    """_reresolve mirrors resolve(): an anonymous token-free dump first (same client + auth
    posture as the primary that produced the playing URLs), the cookie'd + PO-token dump only
    when the anonymous pass didn't yield the wanted itag."""

    def setUp(self):
        self.calls = []
        self._saved = dict(path=youfish._ytdlp_path, run=youfish.subprocess.run,
                           pot=youfish._pot_active, ens=youfish._ensure_pot_server,
                           ck=youfish._write_cookies_temp, gs=youfish.get_settings,
                           imp=youfish._import_yt_dlp)
        youfish._import_yt_dlp = lambda: None   # zipapp pinned absent → binary path
        youfish._ytdlp_path = lambda: "/bin/yt-dlp"
        youfish._pot_active = lambda: True
        youfish._ensure_pot_server = lambda **kw: True
        youfish._write_cookies_temp = lambda: ""   # signed out → _cookies_args yields []
        youfish.get_settings = lambda: {}
        youfish._url_cache.clear()
        youfish._reresolve_spawns[:] = []

    def tearDown(self):
        youfish._ytdlp_path = self._saved["path"]
        youfish.subprocess.run = self._saved["run"]
        youfish._pot_active = self._saved["pot"]
        youfish._ensure_pot_server = self._saved["ens"]
        youfish._write_cookies_temp = self._saved["ck"]
        youfish.get_settings = self._saved["gs"]
        youfish._import_yt_dlp = self._saved["imp"]
        youfish._url_cache.clear()
        youfish._reresolve_spawns[:] = []

    @staticmethod
    def _dump_json(itags):
        return json.dumps({"formats": [{"format_id": i, "url": "https://g/" + i} for i in itags]})

    def test_anon_hit_spawns_once_no_token(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=self._dump_json(["303"]), stderr="")
        youfish.subprocess.run = run
        got = youfish._reresolve("vid", "303", "https://g/old")
        self.assertEqual(got, "https://g/303")
        self.assertEqual(len(self.calls), 1)                     # anon pass sufficed
        joined = " ".join(self.calls[0])
        self.assertNotIn("--cookies", joined)                    # cookie-free
        self.assertNotIn("fetch_pot=always", joined)             # token-free

    def test_missing_itag_falls_back_to_token_dump(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            first = len(self.calls) == 1
            return types.SimpleNamespace(
                returncode=0,
                stdout=self._dump_json(["136"] if first else ["136", "303"]), stderr="")
        youfish.subprocess.run = run
        got = youfish._reresolve("vid", "303", "https://g/old")
        self.assertEqual(got, "https://g/303")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("fetch_pot=always", " ".join(self.calls[0]))   # anon first
        self.assertIn("fetch_pot=always", " ".join(self.calls[1]))      # token safety net second
        self.assertEqual(len(youfish._reresolve_spawns), 2)             # both spawns rate-counted

    def test_fallback_respects_rate_limit(self):
        def run(cmd, **kw):
            self.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=self._dump_json(["136"]), stderr="")
        youfish.subprocess.run = run
        now = time.time()
        youfish._reresolve_spawns[:] = [now] * (youfish._RERESOLVE_BURST - 1)   # one slot left
        got = youfish._reresolve("vid", "303", "https://g/old")
        self.assertIsNone(got)                                   # itag never found
        self.assertEqual(len(self.calls), 1)                     # the anon spawn took the last slot;
                                                                 # the token fallback was NOT spawned


class _FakeHttpResp:
    """Minimal urllib-response stand-in: .status/.getcode(), .headers (dict), .read(n), .close()."""
    def __init__(self, code, data, headers=None):
        self.status = code
        self._data = data
        self._off = 0
        self.headers = dict(headers or {})

    def getcode(self):
        return self.status

    def read(self, n):
        b = self._data[self._off:self._off + n]
        self._off += len(b)
        return b

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class DirectFetchStreamer(unittest.TestCase):
    """_DirectFetch: the in-process stand-in for the yt-dlp streaming child. Chunked Range
    delivery (the burst-window behaviour), clean-EOF vs error semantics (both read as b"", like
    a child pipe closing), truncation self-heal, the kill() duck surface, and _spawn's
    direct-vs-binary chooser + byte-0 fallback flip."""

    DATA = bytes(range(25)) * 1                     # 25 known bytes

    def setUp(self):
        self._urlopen = youfish.urllib.request.urlopen
        self._chunk = youfish._DIRECT_CHUNK
        self._ipv4 = youfish._force_ipv4
        youfish._force_ipv4 = lambda: None          # leave the test process's resolver alone
        youfish._DIRECT_CHUNK = 10
        self.ranges = []                            # every Range header the fetcher sent

    def tearDown(self):
        youfish.urllib.request.urlopen = self._urlopen
        youfish._DIRECT_CHUNK = self._chunk
        youfish._force_ipv4 = self._ipv4

    def _serve(self, data, truncate_first=0):
        """Fake urlopen honouring bounded ranges over `data`; optionally truncate the first
        response after N bytes (server closed early) to exercise the reopen path."""
        state = {"first": True}

        def fake(req, timeout=None):
            rng = req.headers.get("Range", "")
            m = __import__("re").match(r"bytes=(\d+)-(\d+)", rng)
            a, b = int(m.group(1)), int(m.group(2))
            self.ranges.append((a, b))
            if a >= len(data):
                raise urllib.error.HTTPError(req.full_url, 416, "range", {}, None)
            body = data[a:b + 1]
            if state["first"] and truncate_first:
                state["first"] = False
                body = body[:truncate_first]
            return _FakeHttpResp(206, body,
                                 {"Content-Range": "bytes %d-%d/%d" % (a, min(b, len(data) - 1),
                                                                       len(data))})
        youfish.urllib.request.urlopen = fake

    def test_probe_asks_for_the_same_range_as_the_first_fetch(self):
        self._serve(self.DATA)
        for url in ("https://r.googlevideo.com/vp?clen=25", "https://r.googlevideo.com/vp?clen=7",
                    "https://r.googlevideo.com/vp"):
            del self.ranges[:]
            self.assertEqual(youfish._probe_status(url, "UA"), 206)
            youfish._DirectFetch(url, "UA", 0, youfish._clen(url)).read(1)
            self.assertEqual(self.ranges[0], self.ranges[1], url)
        self.assertEqual(self.ranges[0], (0, 9))

    @staticmethod
    def _drain(f, n=7):
        out = b""
        while True:
            b = f.read(n)
            if not b:
                return out
            out += b

    def test_chunked_ranges_full_delivery_clean_eof(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA)
        self.assertEqual(self.ranges, [(0, 9), (10, 19), (20, 24)])   # bounded chunks, clamped end
        self.assertEqual(f.poll(), 0)                                 # clean EOF, not an error

    def test_resume_at_offset(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 12, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA[12:])
        self.assertEqual(self.ranges[0][0], 12)                       # first chunk starts AT the offset

    def test_unknown_total_learned_and_416_ends_clean(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, None)
        self.assertEqual(self._drain(f), self.DATA)                   # total learned from Content-Range
        self.assertEqual(f.poll(), 0)

    def test_error_at_byte0_reads_as_pipe_death(self):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
        youfish.urllib.request.urlopen = fake
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, 100)
        self.assertEqual(f.read(7), b"")                              # like the child dying at byte 0
        self.assertEqual(f.poll(), 1)                                 # error, not clean EOF

    def _refuse_first(self, n, data=None):
        """Fake googlevideo that answers 403 to the first n requests, then serves (a fresh PO-token
        URL's warm-up), or 403s everything once data has been served (data=bytes: mid-stream)."""
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            a, b = map(int, req.headers["Range"][6:].split("-"))
            self.ranges.append((a, b))
            if calls["n"] <= n or (data is not None and a > 0):
                raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
            body = (data or self.DATA)[a:b + 1]
            return _FakeHttpResp(206, body, {"Content-Range": "bytes %d-%d/%d"
                                             % (a, a + len(body) - 1, len(data or self.DATA))})
        youfish.urllib.request.urlopen = fake
        self._step, self._warm = youfish._POT_WARMUP_STEP, youfish._POT_WARMUP
        youfish._POT_WARMUP_STEP = 0
        self.addCleanup(setattr, youfish, "_POT_WARMUP_STEP", self._step)
        self.addCleanup(setattr, youfish, "_POT_WARMUP", self._warm)
        return calls

    @staticmethod
    def _gv(expire_in, pot=True):
        return ("https://r.googlevideo.com/vp?expire=%d&itag=251%s"
                % (time.time() + expire_in, "&pot=Mlx" if pot else ""))

    def test_young_token_url_rides_out_its_warmup(self):
        calls = self._refuse_first(3)                 # refused for ~1.5 s on a phone, then served
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA)
        self.assertEqual(calls["n"], 6)               # 3 refusals + the 3 real chunks
        self.assertEqual(self.ranges[:4], [(0, 9)] * 4)   # the same first request, retried

    def test_token_url_refused_past_the_warmup_dies(self):
        self._refuse_first(10 ** 6)
        youfish._POT_WARMUP = 0.05
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        self.assertEqual(f.read(7), b"")
        self.assertEqual((f.poll(), f.err), (1, "HTTP 403"))   # into the re-resolve as before

    def test_only_young_token_urls_wait(self):
        for url in (self._gv(21599, pot=False),       # token-free: a 403 is a real refusal
                    self._gv(3600)):                  # issued hours ago: expired or gated, not warming
            calls = self._refuse_first(1)
            f = youfish._DirectFetch(url, "UA", 0, len(self.DATA))
            self.assertEqual(f.read(7), b"", url)
            self.assertEqual(calls["n"], 1, url)

    def test_refusal_after_the_first_bytes_never_waits(self):
        calls = self._refuse_first(0, data=self.DATA)  # chunk 1 served, chunk 2 refused
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA[:10])
        self.assertEqual((f.poll(), calls["n"]), (1, 2))

    def test_kill_during_warmup_stops_it(self):
        calls = self._refuse_first(10 ** 6)
        f = youfish._DirectFetch(self._gv(21599), "UA", 0, len(self.DATA))
        youfish._POT_WARMUP_STEP = 0.01
        t = _threading.Timer(0.05, f.kill)
        t.start()
        self.assertEqual(f.read(7), b"")
        t.join()
        self.assertEqual(f.poll(), 0)                  # killed, not an error
        self.assertLess(calls["n"], 100)

    def test_midchunk_truncation_self_heals(self):
        self._serve(self.DATA, truncate_first=4)                      # server closes after 4 bytes
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, len(self.DATA))
        self.assertEqual(self._drain(f), self.DATA)                   # no byte lost, no byte doubled
        self.assertIn((4, 13), self.ranges)                           # reopened FROM the truncation point

    def test_kill_finalises_and_unblocks(self):
        self._serve(self.DATA)
        f = youfish._DirectFetch("https://r.googlevideo.com/vp", "UA", 0, len(self.DATA))
        self.assertTrue(f.read(7))
        f.kill()
        self.assertEqual(f.read(7), b"")
        self.assertEqual(f.poll(), 0)
        f.wait(timeout=5)                                             # duck surface for _reap_proc

    def test_spawn_prefers_direct_and_honours_flip(self):
        self._serve(self.DATA)
        s = types.SimpleNamespace(url="https://r.googlevideo.com/vp", ua="UA", total=25,
                                  itag="136", use_binary=False)
        self.assertIsInstance(youfish._spawn(s, 0), youfish._DirectFetch)
        argvs = []
        saved_popen, saved_path = youfish.subprocess.Popen, youfish._ytdlp_path
        youfish.subprocess.Popen = lambda argv, **kw: argvs.append(argv) or types.SimpleNamespace(
            stdout=None, kill=lambda: None, poll=lambda: 0, wait=lambda **k: 0)
        youfish._ytdlp_path = lambda: "/bin/yt-dlp"
        try:
            s.use_binary = True                                       # the byte-0 flip happened
            youfish._spawn(s, 5)
        finally:
            youfish.subprocess.Popen, youfish._ytdlp_path = saved_popen, saved_path
        self.assertEqual(len(argvs), 1)                               # binary child took over
        self.assertIn("--http-chunk-size", argvs[0])
        self.assertIn("Range: bytes=5-", " ".join(argvs[0]))          # resume offset forwarded


class SweepSmallFixes(unittest.TestCase):
    def test_inproc_json_raises_when_extraction_returned_nothing(self):
        # yt-dlp's CLI default ignoreerrors returns None for a failed extraction; that must
        # fall through to the binary, not come back as a successful "null".
        class YDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False): return None
        mod = types.SimpleNamespace(
            parse_options=lambda argv: types.SimpleNamespace(urls=["u"], ydl_opts={}),
            YoutubeDL=YDL)
        with self.assertRaises(RuntimeError):
            youfish._inproc_json(mod, ["u"])

    def test_safe_name_fits_the_filename_byte_limit(self):
        name = youfish._safe_name("\u3042" * 200)
        self.assertLessEqual(len(name.encode("utf-8")), 150)
        self.assertTrue(name)

    def test_list_downloads_skips_damaged_entries(self):
        tmp = tempfile.mkdtemp(prefix="dls-")
        saved = youfish._downloads_path
        try:
            good = os.path.join(tmp, "a.m4a")
            open(good, "w").close()
            youfish._downloads_path = lambda: os.path.join(tmp, "downloads.json")
            with open(youfish._downloads_path(), "w") as f:
                json.dump([{"id": "a", "kind": "audio", "path": good}, 1, "x", None], f)
            self.assertEqual([d["id"] for d in youfish.list_downloads()], ["a"])
        finally:
            youfish._downloads_path = saved
            shutil.rmtree(tmp, ignore_errors=True)


class DownloadNeedsFfmpeg(unittest.TestCase):
    """Video downloads REFUSE with a clear message when ffmpeg is missing — the old <=360p
    muxed fallback (-f 22/18) died with YouTube's progressive formats (verified live
    2026-09-06), so running yt-dlp anyway only produced a cryptic error."""

    def setUp(self):
        self.events = []
        self._had_pyo = "pyotherside" in sys.modules
        if not self._had_pyo:
            sys.modules["pyotherside"] = types.SimpleNamespace(
                send=lambda *a: self.events.append(a))
        else:
            self._old_send = sys.modules["pyotherside"].send
            sys.modules["pyotherside"].send = lambda *a: self.events.append(a)
        self._saved = dict(ff=youfish._ffmpeg_dir, path=youfish._ytdlp_path)
        youfish._ffmpeg_dir = lambda: None
        youfish._ytdlp_path = lambda: "/bin/yt-dlp"

    def tearDown(self):
        youfish._ffmpeg_dir = self._saved["ff"]
        youfish._ytdlp_path = self._saved["path"]
        if not self._had_pyo:
            sys.modules.pop("pyotherside", None)
        else:
            sys.modules["pyotherside"].send = self._old_send

    def test_video_without_ffmpeg_refused_with_reason(self):
        res = youfish.download("vid123", "Title", "video")
        self.assertFalse(res.get("ok"))
        self.assertEqual(len(self.events), 1)
        ev = self.events[0]
        self.assertEqual(ev[0], "download_done")
        self.assertFalse(ev[3])                         # ok = False
        self.assertIn("ffmpeg", ev[4])                  # the message says WHAT is missing
        self.assertIn("Providers", ev[4])               # ...and WHERE to fix it


class PotPluginProbe(unittest.TestCase):
    """_pot_plugin_probe: one offline verbose run tells whether the installed yt-dlp resolves
    the bgutil plugin dir (the 2026-09-02 'Plugin directories: none' incident detector), plus
    yt-dlp's own JS-runtime view."""

    HEADER_OK = ("[debug] Command-line config: [...]\n"
                 "[debug] yt-dlp version stable@2026.08.19\n"
                 "[debug] JS runtimes: deno 2026.1\n"
                 "[debug] Plugin directories: /repo/plugin/yt_dlp_plugins\n"
                 "[debug] Loaded 1744 extractors\n")
    HEADER_NONE = ("[debug] yt-dlp version stable@2026.08.19\n"
                   "[debug] JS runtimes: none\n"
                   "[debug] Plugin directories: none\n")
    HEADER_OLD = "[debug] yt-dlp version stable@2023.01.01\n"

    def setUp(self):
        self._saved = dict(path=youfish._ytdlp_path, run=youfish.subprocess.run,
                           inst=youfish._pot_installed, pd=youfish._pot_plugin_dir,
                           rd=youfish._pot_repo_dir)
        youfish._ytdlp_path = lambda: "/bin/yt-dlp"
        youfish._pot_installed = lambda: True
        youfish._pot_plugin_dir = lambda: "/repo"
        youfish._pot_repo_dir = lambda: "/repo"
        self.argv = None

    def tearDown(self):
        youfish._ytdlp_path = self._saved["path"]
        youfish.subprocess.run = self._saved["run"]
        youfish._pot_installed = self._saved["inst"]
        youfish._pot_plugin_dir = self._saved["pd"]
        youfish._pot_repo_dir = self._saved["rd"]

    def _with_header(self, header):
        def run(cmd, **kw):
            self.argv = cmd
            return types.SimpleNamespace(returncode=1, stdout="", stderr=header)
        youfish.subprocess.run = run
        return youfish._pot_plugin_probe()

    def test_resolved_dir_reads_loaded(self):
        p = self._with_header(self.HEADER_OK)
        self.assertTrue(p["checked"])
        self.assertIs(p["loaded"], True)
        self.assertEqual(p["js_runtimes"], "deno 2026.1")
        self.assertIn("--no-plugin-dirs", self.argv)     # only OUR dir is probed
        self.assertIn("--simulate", self.argv)           # offline — fails before any network
        self.assertIn("-v", self.argv)                   # the header only prints verbose

    def test_none_reads_not_loaded(self):
        p = self._with_header(self.HEADER_NONE)
        self.assertIs(p["loaded"], False)                # the incident shape
        self.assertEqual(p["js_runtimes"], "none")

    def test_missing_line_is_undetermined_not_alarm(self):
        p = self._with_header(self.HEADER_OLD)
        self.assertIsNone(p["loaded"])

    def test_not_installed_skips(self):
        youfish._pot_installed = lambda: False
        self.assertFalse(youfish._pot_plugin_probe()["checked"])


class PotStatusDenoManaged(unittest.TestCase):
    """pot_status flags whether the Deno in use is the APP-MANAGED copy — the one with no other
    updater, which the UI offers an 'Update Deno' button for."""

    def setUp(self):
        self._saved = dict(dp=youfish._deno_path, md=youfish._managed_deno,
                           port=youfish._pot_ready_on_port, inst=youfish._pot_installed,
                           gs=youfish.get_settings)
        youfish._pot_ready_on_port = lambda timeout=0.25: False
        youfish._pot_installed = lambda: False
        youfish.get_settings = lambda: {}

    def tearDown(self):
        youfish._deno_path = self._saved["dp"]
        youfish._managed_deno = self._saved["md"]
        youfish._pot_ready_on_port = self._saved["port"]
        youfish._pot_installed = self._saved["inst"]
        youfish.get_settings = self._saved["gs"]

    def test_managed_and_system_deno(self):
        youfish._managed_deno = lambda: "/data/bin/deno"
        youfish._deno_path = lambda: "/data/bin/deno"
        self.assertTrue(youfish.pot_status()["deno_managed"])
        youfish._deno_path = lambda: "/usr/bin/deno"
        self.assertFalse(youfish.pot_status()["deno_managed"])
        youfish._deno_path = lambda: None
        self.assertFalse(youfish.pot_status()["deno_managed"])


class InprocComments(unittest.TestCase):
    """_inproc_comments: the warm-YoutubeDL comment walk. THE invariant: the per-call params
    (getcomments / ignore_no_formats_error) are restored afterwards — a leaked getcomments=True
    would make every later resolve on the thread silently pay a full comment walk."""

    class _StubYdl:
        def __init__(self, result=None, raise_=None, params=None):
            self.params = dict(params or {})
            self._result = result
            self._raise = raise_
            self.seen_params = None

        def extract_info(self, url, download=False):
            self.seen_params = dict(self.params)     # what was in effect DURING the walk
            if self._raise:
                raise self._raise
            return self._result

        def sanitize_info(self, info):
            return info

    def setUp(self):
        self._saved = dict(imp=youfish._import_yt_dlp, ydl=youfish._inproc_ydl,
                           ck=youfish._inproc_apply_cookies)
        youfish._import_yt_dlp = lambda: object()
        youfish._inproc_apply_cookies = lambda ydl, anon=False: None

    def tearDown(self):
        youfish._import_yt_dlp = self._saved["imp"]
        youfish._inproc_ydl = self._saved["ydl"]
        youfish._inproc_apply_cookies = self._saved["ck"]

    def test_params_set_during_walk_and_restored_after(self):
        stub = self._StubYdl(result={"comments": [{"id": "a", "parent": "root", "text": "t"}]})
        youfish._inproc_ydl = lambda mod: stub
        data = youfish._inproc_comments("https://u", ["--extractor-args", "youtube:comment_sort=top"])
        self.assertEqual(len(data["comments"]), 1)
        self.assertTrue(stub.seen_params["getcomments"])              # ON during the walk
        self.assertTrue(stub.seen_params["ignore_no_formats_error"])
        self.assertNotIn("getcomments", stub.params)                  # gone afterwards
        self.assertNotIn("ignore_no_formats_error", stub.params)

    def test_preexisting_values_restored_not_deleted(self):
        stub = self._StubYdl(result={}, params={"getcomments": False})
        youfish._inproc_ydl = lambda mod: stub
        youfish._inproc_comments("https://u", [])
        self.assertIs(stub.params["getcomments"], False)              # restored, not popped

    def test_restored_even_when_extraction_raises(self):
        stub = self._StubYdl(raise_=RuntimeError("boom"))
        youfish._inproc_ydl = lambda mod: stub
        with self.assertRaises(RuntimeError):
            youfish._inproc_comments("https://u", [])
        self.assertNotIn("getcomments", stub.params)                  # finally ran

    def test_comments_falls_back_to_binary_on_inproc_failure(self):
        saved = dict(ready=youfish._fast_resolve_ready, inp=youfish._inproc_comments,
                     path=youfish._ytdlp_path, run=youfish.subprocess.run)
        youfish._fast_resolve_ready = lambda: True
        youfish._inproc_comments = lambda url, extra: (_ for _ in ()).throw(RuntimeError("x"))
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {"comments": [{"id": "a", "parent": "root", "text": "t"}]}), stderr="")
        youfish.subprocess.run = fake_run
        try:
            res = youfish.comments("vid")
        finally:
            youfish._fast_resolve_ready = saved["ready"]
            youfish._inproc_comments = saved["inp"]
            youfish._ytdlp_path = saved["path"]
            youfish.subprocess.run = saved["run"]
        self.assertTrue(res["ok"], res)                               # binary rescued the walk
        self.assertEqual(len(calls), 1)


class ChannelAwareDownloads(unittest.TestCase):
    """Direct downloads (binary install, zipapp, SHA2-256SUMS) must follow the user's update
    channel — a nightly BINARY beside a stable ZIPAPP means the in-process fast path silently
    misses the breakage fix the user switched to nightly for."""

    def setUp(self):
        self._gs = youfish.get_settings
        self._open = youfish._https_open

    def tearDown(self):
        youfish.get_settings = self._gs
        youfish._https_open = self._open

    def test_release_base_follows_channel(self):
        youfish.get_settings = lambda: {"ytdlp_channel": "nightly"}
        self.assertIn("yt-dlp-nightly-builds", youfish._ytdlp_release_base())
        youfish.get_settings = lambda: {}
        self.assertIn("/yt-dlp/yt-dlp/", youfish._ytdlp_release_base())
        youfish.get_settings = lambda: {"ytdlp_channel": "weird"}   # unknown -> stable, never KeyError
        self.assertIn("/yt-dlp/yt-dlp/", youfish._ytdlp_release_base())

    def test_expected_sha_reads_channel_sums(self):
        seen = []

        class _Sums:
            def read(self):
                return b"abc123 *yt-dlp\ndef456  yt-dlp_linux_aarch64\n"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        youfish._https_open = lambda url, ctx, timeout=30: seen.append(url) or _Sums()
        youfish.get_settings = lambda: {"ytdlp_channel": "nightly"}
        self.assertEqual(youfish._expected_sha256(None, "yt-dlp"), "abc123")
        self.assertIn("yt-dlp-nightly-builds", seen[0])
        self.assertTrue(seen[0].endswith("SHA2-256SUMS"))


class PotBindLocalhost(unittest.TestCase):
    """_pot_bind_localhost against the exact bgutil 1.3.2 source shape: BOTH hardcoded bind
    hosts move to 127.0.0.1, and the hardcoded success-log address strings are corrected too
    (upstream prints "[::]:<port>" regardless of the actual bind — a recurring log red
    herring). Genuine error-path text is left untouched."""

    SRC = (
        'httpServer\n'
        '    .listen(\n'
        '        {\n'
        '            host: "::",\n'
        '            port: PORT_NUMBER,\n'
        '        },\n'
        '        (err) => {\n'
        '            if (err) {\n'
        '                console.error(\n'
        '                    `Could not listen on [::]:${PORT_NUMBER}, falling back to 0.0.0.0 '
        '(Caused by ${strerror(err)})`,\n'
        '                );\n'
        '            } else {\n'
        '                console.log(\n'
        '                    `Started POT server (v${VERSION}) on on address [::]:${PORT_NUMBER}`,\n'
        '                );\n'
        '            }\n'
        '        },\n'
        '    )\n'
        '    .on("error", () => {\n'
        '        httpServer.listen(\n'
        '            {\n'
        '                host: "0.0.0.0",\n'
        '                port: PORT_NUMBER,\n'
        '            },\n'
        '            (err) => {\n'
        '                console.log(\n'
        '                    `Started POT server (v${VERSION}) on address 0.0.0.0:${PORT_NUMBER}`,\n'
        '                );\n'
        '            },\n'
        '        );\n'
        '    });\n'
    )

    def _patch(self, src):
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        os.makedirs(os.path.join(td, "src"))
        p = os.path.join(td, "src", "main.ts")
        with open(p, "w") as f:
            f.write(src)
        saved = youfish._pot_server_dir
        youfish._pot_server_dir = lambda: td
        try:
            youfish._pot_bind_localhost()
        finally:
            youfish._pot_server_dir = saved
        with open(p) as f:
            return f.read()

    def test_binds_and_success_logs_localhost(self):
        out = self._patch(self.SRC)
        self.assertNotIn('host: "::"', out)
        self.assertNotIn('host: "0.0.0.0"', out)
        self.assertEqual(out.count('host: "127.0.0.1"'), 2)
        self.assertEqual(out.count('address 127.0.0.1:${PORT_NUMBER}'), 2)
        self.assertNotIn('address [::]:', out)
        self.assertNotIn('address 0.0.0.0:', out)
        self.assertIn('falling back to 0.0.0.0', out)            # error text untouched
        self.assertIn('Could not listen on [::]:', out)

    def test_idempotent_and_unknown_shape_noop(self):
        out1 = self._patch(self.SRC)
        out2 = self._patch(out1)
        self.assertEqual(out1, out2)                             # second run changes nothing
        odd = "serve({ hostname: cfg.host })"
        self.assertEqual(self._patch(odd), odd)                  # unknown shape → untouched


class ParseYoutubeUrl(unittest.TestCase):
    """The D-Bus link opener's classifier. The list-input cases are a regression guard: the
    URL dispatcher hands openUrl a ONE-ELEMENT ARRAY on a warm app (cold start masked it via
    QML string coercion), which used to crash on url.strip()."""

    def test_watch_and_short_forms(self):
        for u in ("https://www.youtube.com/watch?v=abcdefghijk",
                  "https://youtu.be/abcdefghijk",
                  "https://www.youtube.com/shorts/abcdefghijk"):
            r = youfish.parse_youtube_url(u)
            self.assertEqual((r["kind"], r["id"]), ("video", "abcdefghijk"), u)

    def test_video_wins_over_list_param(self):
        r = youfish.parse_youtube_url(
            "https://www.youtube.com/watch?v=abcdefghijk&list=PLxyz")
        self.assertEqual(r["kind"], "video")

    def test_playlist_and_channel(self):
        r = youfish.parse_youtube_url("https://www.youtube.com/playlist?list=PLxyz-123")
        self.assertEqual((r["kind"], r["id"]), ("playlist", "PLxyz-123"))
        r = youfish.parse_youtube_url("https://www.youtube.com/channel/UCabc_123")
        self.assertEqual((r["kind"], r["id"]), ("channel", "UCabc_123"))
        r = youfish.parse_youtube_url("https://www.youtube.com/@somecreator")
        self.assertEqual(r["kind"], "channel")

    def test_list_input_unwrapped(self):
        r = youfish.parse_youtube_url(["https://youtu.be/abcdefghijk"])
        self.assertEqual((r["kind"], r["id"]), ("video", "abcdefghijk"))

    def test_empty_inputs(self):
        for u in ("", None, [], ()):
            r = youfish.parse_youtube_url(u)
            self.assertEqual(r, {"kind": "", "id": "", "url": ""})


class PotEnsureBudget(unittest.TestCase):
    """_ensure_pot_server(wait=) must give up quickly when another thread owns an in-flight
    boot (holds the lock) — the resolve hot path passes a short grace instead of joining a
    slow boot (field log 2026-09-08: 40s waiting on a server that never came up)."""

    def test_budget_respected_while_boot_in_flight(self):
        saved = (youfish._pot_active, youfish._pot_ready_on_port)
        youfish._pot_active = lambda: True
        youfish._pot_ready_on_port = lambda timeout=0.25: False
        self.assertTrue(youfish._pot_lock.acquire(timeout=1))   # simulate prewarm mid-boot
        try:
            t0 = time.time()
            self.assertFalse(youfish._ensure_pot_server(wait=0.3))
            self.assertLess(time.time() - t0, 2.0)   # gave up within the grace, not 25s
        finally:
            youfish._pot_lock.release()
            youfish._pot_active, youfish._pot_ready_on_port = saved


class AccountSlotAuthUser(unittest.TestCase):
    """_with_authuser -> ?authuser=N on the yt-dlp fallback URLs, driven by the selected identity's
    authuser index (ytm.selected_account)."""

    def setUp(self):
        self._as = youfish._account_slot

    def tearDown(self):
        youfish._account_slot = self._as

    def test_slot_zero_is_noop(self):
        youfish._account_slot = lambda: 0
        self.assertEqual(youfish._with_authuser("https://www.youtube.com/feed/channels"),
                         "https://www.youtube.com/feed/channels")

    def test_slot_appends_authuser(self):
        youfish._account_slot = lambda: 2
        self.assertEqual(youfish._with_authuser("https://www.youtube.com/feed/channels"),
                         "https://www.youtube.com/feed/channels?authuser=2")

    def test_slot_uses_ampersand_when_query_present(self):
        youfish._account_slot = lambda: 1
        self.assertEqual(youfish._with_authuser("https://x/y?a=b"), "https://x/y?a=b&authuser=1")


class YtmInnerTube(unittest.TestCase):
    """FinTube's InnerTube layer (ytm.py): enumerate accounts, apply the identity, and parse the
    subscriptions/playlists browses — all off a mocked _innertube so it stays offline."""

    def setUp(self):
        import ytm
        self._saved = {n: getattr(ytm, n) for n in
                       ("_innertube", "is_logged_in", "_load_cookies", "_save_cookies")}
        ytm.is_logged_in = lambda: True

    def tearDown(self):
        import ytm
        for n, v in self._saved.items():
            setattr(ytm, n, v)

    @staticmethod
    def _switcher_for(authuser):
        # account/accounts_list returns only the account(s) for the probed authuser.
        if authuser == 0:
            return {"contents": [{"accountItemSectionRenderer": {"contents": [
                {"accountItem": {"accountName": {"simpleText": "Personal Me"},
                                 "channelHandle": {"simpleText": "@me"}, "isSelected": True,
                                 "serviceEndpoint": {"selectActiveIdentityEndpoint": {
                                     "supportedTokens": [
                                         {"accountStateToken": {"obfuscatedGaiaId": "gaia0"}}]}}}}]}}]}
        if authuser == 1:                    # second login: personal + a brand channel
            return {"contents": [{"accountItemSectionRenderer": {"contents": [
                {"accountItem": {"accountName": {"simpleText": "Second Me"},
                                 "serviceEndpoint": {"selectActiveIdentityEndpoint": {
                                     "supportedTokens": [
                                         {"accountStateToken": {"obfuscatedGaiaId": "gaia1"}}]}}}},
                {"accountItem": {"accountName": {"simpleText": "Brand A"},
                                 "serviceEndpoint": {"selectActiveIdentityEndpoint": {
                                     "supportedTokens": [
                                         {"pageIdToken": {"pageId": "UCbrandA"}},
                                         {"accountStateToken": {"obfuscatedGaiaId": "gaia1b"}}]}}}}]}}]}
        return {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}

    def test_list_accounts_probes_logins_and_brand_channels(self):
        import ytm
        ytm._innertube = lambda ep, body, **k: self._switcher_for(k.get("authuser", 0))
        ytm._load_cookies = lambda: {"authuser": "0", "page_id": ""}
        res = ytm.list_accounts()
        self.assertTrue(res["ok"])
        a = res["accounts"]
        self.assertEqual(len(a), 3)          # probed authuser 0 (1) + 1 (2), then 2 = logged out
        self.assertEqual((a[0]["authuser"], a[0]["page_id"], a[0]["name"]), ("0", "", "Personal Me"))
        self.assertEqual((a[1]["authuser"], a[1]["page_id"], a[1]["name"]), ("1", "", "Second Me"))
        self.assertEqual((a[2]["authuser"], a[2]["page_id"]), ("1", "UCbrandA"))

    def test_list_accounts_logged_out(self):
        import ytm
        ytm._innertube = lambda ep, body, **k: {
            "responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}
        res = ytm.list_accounts()
        self.assertFalse(res["ok"])
        self.assertEqual(res["accounts"], [])

    def test_selected_follows_stored_choice_not_browser(self):
        # The browser's active channel (isSelected) must NOT mark an account selected — only OUR
        # stored choice does. Here the personal channel is browser-active but we've chosen the brand.
        import ytm

        def sw(authuser):
            if authuser == 0:
                return {"contents": [{"accountItemSectionRenderer": {"contents": [
                    {"accountItem": {"accountName": {"simpleText": "Personal"}, "isSelected": True,
                        "serviceEndpoint": {"selectActiveIdentityEndpoint": {"supportedTokens": [
                            {"accountStateToken": {"obfuscatedGaiaId": "g0"}}]}}}},
                    {"accountItem": {"accountName": {"simpleText": "Brand"},
                        "serviceEndpoint": {"selectActiveIdentityEndpoint": {"supportedTokens": [
                            {"pageIdToken": {"pageId": "UCb"}}]}}}}]}}]}
            return {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}

        ytm._innertube = lambda ep, body, **k: sw(k.get("authuser", 0))
        ytm._load_cookies = lambda: {"authuser": "0", "page_id": "UCb"}   # chose the brand channel
        res = ytm.list_accounts()
        sel = {x["name"]: x["selected"] for x in res["accounts"]}
        self.assertFalse(sel["Personal"])   # browser-active, but not our choice
        self.assertTrue(sel["Brand"])       # our stored choice

    def test_selected_identity_feeds_headers(self):
        import ytm
        # _innertube reads _selected_identity() for the X-Goog-AuthUser / X-Goog-PageId headers.
        ytm._load_cookies = lambda: {"authuser": "3", "page_id": "UCxyz", "selected_name": "Brand"}
        ident = ytm._selected_identity()
        self.assertEqual((ident["authuser"], ident["page_id"]), ("3", "UCxyz"))

    def test_subscriptions_parse(self):
        import ytm
        resp = {"contents": {"x": [
            {"channelRenderer": {"channelId": "UC111", "title": {"simpleText": "Chan One"},
                                 "thumbnail": {"thumbnails": [{"url": "//img/1.jpg"}]}}},
            {"gridChannelRenderer": {"channelId": "UC222",
                                     "title": {"runs": [{"text": "Chan Two"}]}}}]}}
        ytm._innertube = lambda ep, body, **k: dict(resp)
        r = ytm.subscriptions_innertube()
        self.assertTrue(r["ok"])
        ids = [c["id"] for c in r["channels"]]
        self.assertEqual(ids, ["UC111", "UC222"])
        self.assertEqual(r["channels"][0]["thumbnail"], "https://img/1.jpg")

    def test_playlists_parse_skips_channels(self):
        import ytm
        resp = {"contents": {"x": [
            {"gridPlaylistRenderer": {"playlistId": "PLaaa", "title": {"simpleText": "Mix"}}},
            {"playlistRenderer": {"playlistId": "UCnope", "title": {"simpleText": "not a playlist"}}},
            {"lockupViewModel": {"contentId": "PLbbb",
                "metadata": {"lockupMetadataViewModel": {"title": {"content": "Faves"}}}}}]}}
        ytm._innertube = lambda ep, body, **k: dict(resp)
        r = ytm.playlists_innertube()
        self.assertTrue(r["ok"])
        self.assertEqual(sorted(p["yt_id"] for p in r["playlists"]), ["PLaaa", "PLbbb"])

    def test_select_and_selected_roundtrip(self):
        import ytm
        store = {"sapisid": "abc"}
        ytm._load_cookies = lambda: dict(store)
        ytm._save_cookies = lambda c: (store.clear(), store.update(c))
        res = ytm.select_account("2", "UCz", "ds", "Brand Z")
        self.assertTrue(res["ok"])
        self.assertEqual(store["authuser"], "2")
        self.assertEqual(ytm.selected_account(),
                         {"authuser": "2", "page_id": "UCz", "name": "Brand Z"})



# --- InnerTube fast path (innertube.py + the youfish wiring) ---------------------------------- #
# Canned responses mirror the shapes captured live from YouTube (2026-10): lockupViewModel channel
# grids, shortsLockupViewModel Shorts, pageHeaderViewModel headers, VISIONOS player responses.

CID = "UCBJycsmduvYEL83R_U4JriQ"
_REAL_TRANSPORT = innertube._transport


def _lockup(vid, title, dur="11:47", views="4.8M views", ago="19 hours ago", live=False,
            members=False):
    badge = {"thumbnailBadgeViewModel": {
        "text": "LIVE" if live else dur,
        "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_LIVE" if live else
                      "THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT"}}
    row = {"metadataParts": [{"text": {"content": views}}] +
                            ([{"text": {"content": ago}}] if ago else [])}
    if members:
        row["badges"] = [{"badgeViewModel": {"badgeText": "Members only",
                                             "badgeStyle": "BADGE_MEMBERS_ONLY"}}]
    return {"richItemRenderer": {"content": {"lockupViewModel": {
        "contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"overlays": [
            {"thumbnailBottomOverlayViewModel": {"badges": [badge]}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [row]}}}}}}}}


def _cont_item(token):
    return {"continuationItemRenderer": {"continuationEndpoint": {
        "continuationCommand": {"token": token}}}}


def _browse(items, tab="Videos", cid=CID, name="Marques Brownlee"):
    content = {"richGridRenderer": {"contents": items}} if tab else {"sectionListRenderer": {}}
    return {
        "metadata": {"channelMetadataRenderer": {
            "title": name, "externalId": cid,
            "channelUrl": "https://www.youtube.com/channel/" + cid,
            "avatar": {"thumbnails": [{"url": "https://yt3.example/av=s900-c-k", "width": 900,
                                       "height": 900}]}}},
        "header": {"pageHeaderRenderer": {"content": {"pageHeaderViewModel": {
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": "@mkbhd"}}]},
                {"metadataParts": [{"text": {"content": "21.4M subscribers"}},
                                   {"text": {"content": "1.8K videos"}}]}]}}}}}},
        "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [
            {"tabRenderer": {"title": "Home", "selected": not tab}},
            {"tabRenderer": {"title": tab or "Videos", "selected": bool(tab), "content": content}}]}},
    }


def _shorts_item(vid, title):
    return {"richItemRenderer": {"content": {"shortsLockupViewModel": {
        "entityId": "shorts-shelf-item-" + vid,
        "onTap": {"innertubeCommand": {"reelWatchEndpoint": {"videoId": vid}}},
        "overlayMetadata": {"primaryText": {"content": title},
                            "secondaryText": {"content": "7.9M views"}}}}}}


def _player(vid="dQw4w9WgXcQ", status="OK", reason=None, live=False, dubbed=False):
    def fmt(itag, mime, **kw):
        d = {"itag": itag, "mimeType": mime, "bitrate": 1000 * (itag % 300),
             "url": "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=%d&expire=%d"
                    % (itag, int(time.time()) + 20000),
             "contentLength": "1000"}
        d.update(kw)
        return d
    if dubbed:
        audio = [fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM",
                     audioTrack={"displayName": "English original", "id": "en.4",
                                 "audioIsDefault": True}),
                 fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM",
                     audioTrack={"displayName": "Japanese", "id": "ja.3",
                                 "audioIsDefault": False})]
    else:
        audio = [fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM"),
                 fmt(251, 'audio/webm; codecs="opus"', audioQuality="AUDIO_QUALITY_MEDIUM",
                     isDrc=True),
                 fmt(140, 'audio/mp4; codecs="mp4a.40.2"', audioQuality="AUDIO_QUALITY_MEDIUM")]
    video = [fmt(137, 'video/mp4; codecs="avc1.640028"', height=1080, width=1920, fps=30,
                 qualityLabel="1080p"),
             fmt(136, 'video/mp4; codecs="avc1.4d401f"', height=720, width=1280, fps=30,
                 qualityLabel="720p"),
             fmt(247, 'video/webm; codecs="vp9"', height=720, width=1280, fps=30,
                 qualityLabel="720p"),
             fmt(398, 'video/mp4; codecs="av01.0.05M.08"', height=720, width=1280, fps=30,
                 qualityLabel="720p"),
             {"itag": 299, "mimeType": 'video/mp4; codecs="avc1.64002a"', "height": 1080,
              "signatureCipher": "s=abc&url=https%3A%2F%2Fx"}]          # ciphered → dropped
    return {
        "playabilityStatus": {"status": status, "reason": reason},
        "videoDetails": {"videoId": vid, "title": "Never Gonna", "author": "Rick",
                         "channelId": CID, "lengthSeconds": "213", "viewCount": "1700000000",
                         "isLive": live,
                         "shortDescription": "0:00 Intro\n1:00 Verse\n2:30 Chorus\n"},
        "streamingData": {} if status != "OK" else {"adaptiveFormats": video + audio},
        "captions": {"playerCaptionsTracklistRenderer": {
            "captionTracks": [
                {"baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=en", "languageCode": "en",
                 "name": {"runs": [{"text": "English"}]}},
                {"baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=en&kind=asr",
                 "languageCode": "en", "kind": "asr",
                 "name": {"runs": [{"text": "English (auto-generated)"}]}}],
            "translationLanguages": [
                {"languageCode": "de", "languageName": {"runs": [{"text": "German"}]}},
                {"languageCode": "en", "languageName": {"runs": [{"text": "English"}]}}]}},
    }


def _cpage(comments, cont=None, total=None, replies_page=False):
    """A /next comments page: thread (or reply) items pointing at commentEntityPayloads.
    `comments` = [(id, text, likes, reply_count, replies_token)]."""
    items, muts = [], []
    if total is not None:
        items.append({"commentsHeaderRenderer": {
            "countText": {"runs": [{"text": "{:,}".format(total)}, {"text": " Comments"}]}}})
    for cid, txt, likes, rc, rtok in comments:
        key = "K-" + cid
        cvm = {"commentKey": key, "commentId": cid, "toolbarStateKey": "T-" + cid}
        muts.append({"entityKey": key, "payload": {"commentEntityPayload": {
            "key": key,
            "properties": {"commentId": cid, "content": {"content": txt},
                           "publishedTime": "2 days ago", "replyLevel": 1 if replies_page else 0},
            "author": {"displayName": "@" + cid, "avatarThumbnailUrl": "https://yt3.example/" + cid,
                       "isCreator": cid.endswith("up")},
            "toolbar": {"likeCountNotliked": likes, "replyCount": str(rc) if rc else ""}}}})
        muts.append({"entityKey": "T-" + cid,
                     "payload": {"engagementToolbarStateEntityPayload": {"key": "T-" + cid}}})
        if replies_page:
            items.append({"commentViewModel": cvm})
        else:
            th = {"commentViewModel": {"commentViewModel": cvm}}
            if rtok:
                th["replies"] = {"commentRepliesRenderer": {"contents": [{"continuationItemRenderer": {
                    "continuationEndpoint": {"continuationCommand": {"token": rtok}}}}]}}
            items.append({"commentThreadRenderer": th})
    if cont:
        items.append({"continuationItemRenderer": {"button": {"buttonRenderer": {"command": {
            "continuationCommand": {"token": cont}}}}} if replies_page else
            {"continuationEndpoint": {"continuationCommand": {"token": cont}}}})
    act = "appendContinuationItemsAction" if replies_page or total is None else \
        "reloadContinuationItemsCommand"
    return {"onResponseReceivedEndpoints": [{act: {"continuationItems": items}}],
            "frameworkUpdates": {"entityBatchUpdate": {"mutations": muts}}}


class _FakeTransport:
    """Stand-in for innertube._transport: answers by endpoint (+ browse params / continuation)."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, body=None, headers=None, timeout=10):
        ep = url.split("/youtubei/v1/")[1].split("?")[0]
        req = json.loads(body.decode()) if body else {}
        self.calls.append((ep, req, headers or {}))
        key = ep
        if ep == "next" and req.get("continuation"):
            key = "next:" + req["continuation"]
        if ep == "browse":
            key = "browse:" + (req.get("continuation") or
                               "%s/%s" % (req.get("browseId"), req.get("params")))
        ans = self.routes.get(key)
        if ans is None:
            return 404, b"{}"
        if isinstance(ans, Exception):
            raise ans
        return 200, json.dumps(ans).encode()


class InnertubeParsing(unittest.TestCase):
    def setUp(self):
        self._t = innertube._transport
        innertube._visitor.update({"data": "", "ts": 0.0})
        innertube._cid_cache.clear()

    def tearDown(self):
        innertube._transport = self._t

    def _route(self, routes):
        innertube._transport = _FakeTransport(routes)
        return innertube._transport

    def test_text_and_count_parsers(self):
        self.assertEqual(innertube.text({"runs": [{"text": "a"}, {"text": "b"}]}), "ab")
        self.assertEqual(innertube.text({"simpleText": "x"}), "x")
        self.assertEqual(innertube.text({"content": "y"}), "y")
        self.assertEqual(innertube.parse_count("4.8M views"), 4800000)
        self.assertEqual(innertube.parse_count("1,234 views"), 1234)
        self.assertEqual(innertube.parse_count("21.4M subscribers"), 21400000)
        self.assertEqual(innertube.parse_count("1.8K videos"), 1800)
        self.assertEqual(innertube.parse_count("No views"), 0)
        self.assertEqual(innertube.parse_duration("11:47"), 707)
        self.assertEqual(innertube.parse_duration("1:02:03"), 3723)
        self.assertEqual(innertube.parse_duration("LIVE"), 0)
        self.assertEqual(innertube.parse_ago("19 hours ago"), 19 * 3600)
        self.assertEqual(innertube.parse_ago("Streamed 2 days ago"), 2 * 86400)
        self.assertIsNone(innertube.parse_ago("Scheduled for 10/20/26"))

    def test_channel_tab_lockups_header_and_continuation(self):
        params = innertube.urllib.parse.unquote(innertube.TAB_PARAMS["videos"])
        t = self._route({"browse:%s/%s" % (CID, params): _browse([
            _lockup("aaaaaaaaaa1", "New one"),
            _lockup("aaaaaaaaaa2", "On air", live=True, views="1.2K watching", ago=None),
            _lockup("aaaaaaaaaa3", "Members", members=True),
            {"richItemRenderer": {"content": {"videoRenderer": {   # legacy renderer still parses
                "videoId": "aaaaaaaaaa4", "title": {"runs": [{"text": "Legacy"}]},
                "lengthText": {"simpleText": "1:02:03"}, "viewCountText": {"simpleText": "12 views"},
                "publishedTimeText": {"simpleText": "3 weeks ago"}}}}},
            _cont_item("TOKEN1")])})
        res = innertube.channel_tab(CID, "videos")
        items = {i["id"]: i for i in res["items"]}
        self.assertEqual(list(items), ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3", "aaaaaaaaaa4"])
        self.assertEqual(items["aaaaaaaaaa1"]["duration"], 707)
        self.assertEqual(items["aaaaaaaaaa1"]["views"], 4800000)
        self.assertEqual(items["aaaaaaaaaa1"]["ago"], 19 * 3600)
        self.assertTrue(items["aaaaaaaaaa2"]["live"])
        self.assertTrue(items["aaaaaaaaaa3"]["members"])
        self.assertEqual(items["aaaaaaaaaa4"]["duration"], 3723)
        self.assertEqual(res["continuation"], "TOKEN1")
        ch = res["channel"]
        self.assertEqual((ch["id"], ch["name"], ch["subscribers"], ch["video_count"]),
                         (CID, "Marques Brownlee", 21400000, 1800))
        self.assertTrue(ch["avatars"])
        self.assertEqual(t.calls[0][2]["X-YouTube-Client-Name"], "1")   # browse = WEB client

    def test_missing_tab_raises(self):
        params = innertube.urllib.parse.unquote(innertube.TAB_PARAMS["videos"])
        self._route({"browse:%s/%s" % (CID, params): _browse([], tab=None)})
        with self.assertRaises(innertube.InnertubeError):
            innertube.channel_tab(CID, "videos")

    def test_shorts_and_continuation(self):
        params = innertube.urllib.parse.unquote(innertube.TAB_PARAMS["shorts"])
        self._route({
            "browse:%s/%s" % (CID, params): _browse([_shorts_item("sssssssss01", "A short")],
                                                    tab="Shorts"),
            "browse:TOKEN1": {"onResponseReceivedActions": [{"appendContinuationItemsAction": {
                "continuationItems": [_lockup("bbbbbbbbbb1", "Older"), _cont_item("TOKEN2")]}}]}})
        sh = innertube.channel_tab(CID, "shorts")["items"]
        self.assertEqual([(i["id"], i["short"], i["title"]) for i in sh],
                         [("sssssssss01", True, "A short")])
        c = innertube.continuation("TOKEN1")
        self.assertEqual([i["id"] for i in c["items"]], ["bbbbbbbbbb1"])
        self.assertEqual(c["continuation"], "TOKEN2")

    def test_resolve_channel(self):
        t = self._route({"navigation/resolve_url": {"endpoint": {"browseEndpoint": {"browseId": CID}}}})
        self.assertEqual(innertube.resolve_channel(CID), CID)                       # no request
        self.assertEqual(innertube.resolve_channel("https://www.youtube.com/channel/%s/videos" % CID), CID)
        self.assertEqual(len(t.calls), 0)
        self.assertEqual(innertube.resolve_channel("https://www.youtube.com/@mkbhd/videos"), CID)
        self.assertEqual(innertube.resolve_channel("@mkbhd"), CID)                  # cached now
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.calls[0][1]["url"], "https://www.youtube.com/@mkbhd")

    def test_player_info_shapes_formats_like_ytdlp(self):
        t = self._route({"visitor_id": {"responseContext": {"visitorData": "VD1"}},
                         "player": _player()})
        info = innertube.player_info("dQw4w9WgXcQ")
        fmts = {f["format_id"]: f for f in info["formats"]}
        self.assertIn("251", fmts)
        self.assertIn("251-drc", fmts)                         # DRC variant named like yt-dlp
        self.assertNotIn("299", fmts)                          # ciphered: yt-dlp's job, not ours
        v = fmts["137"]
        self.assertEqual((v["ext"], v["vcodec"], v["acodec"], v["height"], v["fps"]),
                         ("mp4", "avc1.640028", "none", 1080, 30))
        a = fmts["140"]
        self.assertEqual((a["ext"], a["vcodec"], a["acodec"]), ("m4a", "none", "mp4a.40.2"))
        self.assertEqual(a["format_note"], "medium")
        self.assertTrue(v["http_headers"]["User-Agent"])
        self.assertEqual(info["channel_url"], "https://www.youtube.com/channel/" + CID)
        self.assertEqual([c["start_time"] for c in info["chapters"]], [0, 60, 150])
        self.assertEqual(info["live_status"], "not_live")
        self.assertEqual(list(info["subtitles"]), ["en"])
        self.assertIn("fmt=json3", info["subtitles"]["en"][0]["url"])
        self.assertIn("de", info["automatic_captions"])
        self.assertIn("tlang=de", info["automatic_captions"]["de"][0]["url"])
        self.assertNotIn("tlang", info["automatic_captions"]["en"][0]["url"])   # the real ASR track
        # The visitor id is minted once and rides on the player call.
        self.assertEqual([c[0] for c in t.calls], ["visitor_id", "player"])
        self.assertEqual(t.calls[1][2]["X-Goog-Visitor-Id"], "VD1")
        # ...in the header only: the player body mirrors yt-dlp's (hl + UTC, no gl, html5 pref).
        req = t.calls[1][1]
        self.assertEqual(req["context"]["client"]["clientName"], "VISIONOS")
        self.assertNotIn("gl", req["context"]["client"])
        self.assertNotIn("visitorData", req["context"]["client"])
        self.assertEqual(req["context"]["client"]["timeZone"], "UTC")
        self.assertEqual(req["playbackContext"]["contentPlaybackContext"]["html5Preference"],
                         "HTML5_PREF_WANTS")
        self.assertTrue(req["contentCheckOk"] and req["racyCheckOk"])
        innertube.player_info("dQw4w9WgXcQ")
        self.assertEqual([c[0] for c in t.calls], ["visitor_id", "player", "player"])

    def test_player_info_dubbed_tracks(self):
        self._route({"visitor_id": {"responseContext": {"visitorData": "VD1"}},
                     "player": _player(dubbed=True)})
        fmts = {f["format_id"]: f for f in innertube.player_info("dQw4w9WgXcQ")["formats"]}
        en, ja = fmts["251-en.4"], fmts["251-ja.3"]
        self.assertEqual((en["language"], en["language_preference"]), ("en", 10))
        self.assertEqual((ja["language"], ja["language_preference"]), ("ja", -1))
        self.assertEqual(youfish._audio_lang_name(en), "English")
        self.assertEqual(youfish._audio_lang_name(ja), "Japanese")
        self.assertEqual(youfish._pick_audio(list(fmts.values()))["format_id"], "251-en.4")

    def test_bot_wall_is_flagged_and_burns_the_visitor(self):
        self._route({"visitor_id": {"responseContext": {"visitorData": "VD1"}},
                     "player": _player(status="LOGIN_REQUIRED",
                                       reason="Sign in to confirm you're not a bot")})
        with self.assertRaises(innertube.Unplayable) as cm:
            innertube.player_info("dQw4w9WgXcQ")
        self.assertTrue(cm.exception.bot)
        self.assertEqual(innertube._visitor["data"], "")       # re-minted on the next call

    def test_chapters_rules(self):
        f = innertube.chapters_from_description
        self.assertEqual(f("0:00 a\n0:05 b\n1:00 c"), [])           # < 10s apart → not chapters
        self.assertEqual(f("1:00 a\n2:00 b\n3:00 c"), [])           # must start at 0:00
        self.assertEqual(f("0:00 a\n1:00 b"), [])                   # need at least three
        self.assertEqual([c["title"] for c in f("Intro - 0:00\nMiddle - 2:00\nEnd - 1:00:00")],
                         ["Intro", "Middle", "End"])
        # an emoji + VS16 + "(" before the digits (freeCodeCamp's "⌨️ (0:00) Introduction")
        self.assertEqual([c["start_time"] for c in f("⌨️ (0:00) Intro\n⌨️ (1:45) Setup\n"
                                                      "⌨️ (6:40) Hello")], [0, 105, 400])

    def test_comments_page_and_replies_page(self):
        innertube._transport = _FakeTransport({
            "next:C1": _cpage([("c1up", "first!", "8.5K", 55, "R1"), ("c2", "hi", "", 0, None)],
                              cont="C2", total=2459208),
            "next:R1": _cpage([("c1up.r1", "me too", "42", 0, None)], cont="R2", replies_page=True)})
        try:
            p = innertube.comments("C1")
            self.assertEqual(p["total"], 2459208)
            self.assertEqual(p["continuation"], "C2")
            self.assertEqual([(c["id"], c["likes"], c["reply_count"], c["replies_token"],
                               c["is_uploader"]) for c in p["comments"]],
                             [("c1up", 8500, 55, "R1", True), ("c2", 0, 0, None, False)])
            c = p["comments"][0]
            self.assertEqual((c["author"], c["text"], c["time"], c["thumbnail"]),
                             ("@c1up", "first!", "2 days ago", "https://yt3.example/c1up"))
            r = innertube.comments("R1")
            self.assertEqual([x["text"] for x in r["comments"]], ["me too"])
            self.assertEqual((r["continuation"], r["total"]), ("R2", None))
        finally:
            innertube._transport = _REAL_TRANSPORT

    def test_configure_from_ytdlp_follows_jsless_default(self):
        class Policy:
            def __init__(self, required):
                self.required = required
        base = types.ModuleType("yt_dlp.extractor.youtube._base")
        base.INNERTUBE_CLIENTS = {
            "web": {"INNERTUBE_CONTEXT": {"client": {"clientName": "WEB",
                                                     "clientVersion": "2.20990101.00.00"}}},
            "needs_js": {"REQUIRE_JS_PLAYER": True},
            "needs_pot": {"REQUIRE_JS_PLAYER": False, "INNERTUBE_CONTEXT_CLIENT_NAME": 5,
                          "GVS_PO_TOKEN_POLICY": {"https": Policy(True)},
                          "INNERTUBE_CONTEXT": {"client": {"clientName": "X", "clientVersion": "1",
                                                           "userAgent": "UA-X"}}},
            "newclient": {"REQUIRE_JS_PLAYER": False, "INNERTUBE_CONTEXT_CLIENT_NAME": 77,
                          "GVS_PO_TOKEN_POLICY": {"https": Policy(False)},
                          "INNERTUBE_CONTEXT": {"client": {"clientName": "NEWCLIENT",
                                                           "clientVersion": "9.9",
                                                           "userAgent": "UA-NEW", "hl": "en"}}},
        }
        video = types.ModuleType("yt_dlp.extractor.youtube._video")
        video.YoutubeIE = type("YoutubeIE", (), {
            "_DEFAULT_JSLESS_CLIENTS": ("needs_js", "needs_pot", "newclient")})
        names = ["yt_dlp", "yt_dlp.extractor", "yt_dlp.extractor.youtube",
                 "yt_dlp.extractor.youtube._base", "yt_dlp.extractor.youtube._video"]
        saved_mods = {n: sys.modules.get(n) for n in names}
        saved_player, saved_web = dict(innertube._PLAYER), dict(innertube._WEB)
        pkg = types.ModuleType("yt_dlp.extractor.youtube")
        pkg._base, pkg._video = base, video
        for n, m in zip(names, [types.ModuleType("yt_dlp"), types.ModuleType("yt_dlp.extractor"),
                                pkg, base, video]):
            sys.modules[n] = m
        try:
            innertube._visitor.update({"data": "OLD", "ts": time.time()})
            self.assertEqual(innertube.configure_from_ytdlp(None), "newclient")
            self.assertEqual(innertube.player_client(), ("newclient", "NEWCLIENT", "UA-NEW"))
            self.assertEqual(innertube._WEB["clientVersion"], "2.20990101.00.00")
            self.assertEqual(innertube._visitor["data"], "")     # new client → new visitor id
        finally:
            for n, m in saved_mods.items():
                if m is None:
                    sys.modules.pop(n, None)
                else:
                    sys.modules[n] = m
            innertube._PLAYER.clear(); innertube._PLAYER.update(saved_player)
            innertube._WEB.clear(); innertube._WEB.update(saved_web)


class _FakeIT:
    """A stand-in innertube module for the youfish wiring tests."""
    InnertubeError = innertube.InnertubeError

    def __init__(self):
        self.player = None           # dict or Exception
        self.tabs = {}               # (cid, tab) -> {"items", "continuation", "channel"} or Exception
        self.conts = {}              # token -> {"items", "continuation"}
        self.live = None             # live_info answer: dict or Exception
        self.searches = {}           # (query, params) or continuation token -> search() answer
        self.nexts = {}              # video id -> watch_next answer: dict or Exception
        self.http = {}               # url -> (status, bytes) for _transport
        self.cpages = {}             # token -> innertube.comments answer
        self.calls = []

    def player_client(self):
        return ("visionos", "VISIONOS", "UA-V")

    def player_info(self, vid):
        self.calls.append(("player", vid))
        if isinstance(self.player, Exception):
            raise self.player
        return json.loads(json.dumps(self.player))

    def resolve_channel(self, ref):
        self.calls.append(("resolve", ref))
        return CID if (ref == CID or "mkbhd" in ref or CID in ref) else "UC" + "x" * 22

    def channel_tab(self, cid, tab="videos"):
        self.calls.append(("tab", cid, tab))
        ans = self.tabs.get((cid, tab))
        if ans is None:
            raise innertube.InnertubeError("no %s tab" % tab)
        if isinstance(ans, Exception):
            raise ans
        return json.loads(json.dumps(ans))

    def continuation(self, token):
        self.calls.append(("cont", token))
        return json.loads(json.dumps(self.conts[token]))

    def live_info(self, vid):
        self.calls.append(("live", vid))
        if self.live is None or isinstance(self.live, Exception):
            raise self.live or innertube.InnertubeError("not live")
        return json.loads(json.dumps(self.live))

    def search(self, query="", params="", continuation=None):
        key = continuation or (query, params)
        self.calls.append(("search", key))
        ans = self.searches.get(key)
        if ans is None or isinstance(ans, Exception):
            raise ans or innertube.InnertubeError("no results")
        return json.loads(json.dumps(ans))

    def watch_next(self, vid):
        self.calls.append(("next", vid))
        ans = self.nexts.get(vid)
        if ans is None or isinstance(ans, Exception):
            raise ans or innertube.InnertubeError("no next")
        return json.loads(json.dumps(ans))

    def comments(self, token):
        self.calls.append(("comments", token))
        ans = self.cpages.get(token)
        if ans is None or isinstance(ans, Exception):
            raise ans or innertube.InnertubeError("no comments page")
        return json.loads(json.dumps(ans))

    def _transport(self, method, url, body, headers, timeout):
        self.calls.append(("http", url))
        if url not in self.http:
            raise IOError("offline")
        return self.http[url]


def _it_item(vid, dur=100, **kw):
    d = {"id": vid, "title": "T " + vid, "duration": dur, "views": 5, "posted": "2 days ago",
         "ago": 2 * 86400, "live": False, "upcoming": False, "members": False, "short": False}
    d.update(kw)
    return d


def _it_chan(name="Chan"):
    return {"id": CID, "name": name, "url": "https://www.youtube.com/channel/" + CID,
            "avatars": [{"url": "https://yt3.example/a=s900-c", "width": 900, "height": 900}],
            "subscribers": 1000, "video_count": 42}


class InnertubeEngineWiring(unittest.TestCase):
    def setUp(self):
        self._saved = {n: getattr(youfish, n) for n in (
            "_it", "_ytdlp_path", "_ensure_pot_server", "_pot_ytdlp_args", "_yt_extractor_args",
            "_proxied", "get_settings", "_import_yt_dlp", "_data_dir", "list_subscriptions",
            "_feed_durations_cache", "_feed_cache", "_feed_state", "_classify_tried",
            "_ytdlp_formats", "_url_cache", "_probe_status")}
        self._run = youfish.subprocess.run
        self._tmp = tempfile.mkdtemp(prefix="it-")
        self.fake = _FakeIT()
        youfish._it = lambda: self.fake
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._yt_extractor_args = lambda client_override=None, want_pot=False, cookies=False: []
        youfish._proxied = lambda url, *a, **k: url
        youfish.get_settings = lambda: {"default_quality": 0, "hw_decode": False,
                                        "hide_shorts": True, "hide_watched": False}
        youfish._import_yt_dlp = lambda: None
        youfish._data_dir = lambda: self._tmp
        youfish._feed_durations_cache = {"ts": 0.0, "map": {}, "shorts": set()}
        youfish._feed_cache = {}
        youfish._feed_state = {"loaded": True}
        youfish._classify_tried = {}
        youfish._url_cache = {}
        youfish._fast_player_state.update({"bot_fails": 0, "off_until": 0.0, "strikes": []})
        self.probed = []

        def probe(url, ua, timeout=3):
            self.probed.append(url)
            return 206
        youfish._probe_status = probe
        youfish._chan_pages.clear()
        youfish._search_pages.clear()
        youfish._next_cache.clear()
        youfish._avatar_cache.clear()
        youfish._avatar_state["loaded"] = True
        youfish.invalidate_resolve_cache()
        self.spawns = []

        def no_spawn(cmd, **kw):
            self.spawns.append(cmd)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="spawned in a fast-path test")
        youfish.subprocess.run = no_spawn

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run
        youfish._fast_player_state.update({"bot_fails": 0, "off_until": 0.0, "strikes": []})
        youfish._chan_pages.clear()
        youfish._search_pages.clear()
        youfish._next_cache.clear()
        youfish._avatar_cache.clear()
        youfish._avatar_state["loaded"] = False
        youfish.invalidate_resolve_cache()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _fast_data(self, **kw):
        innertube._transport = _FakeTransport({"visitor_id": {"responseContext": {"visitorData": "V"}},
                                               "player": _player(**kw)})
        try:
            innertube._visitor.update({"data": "", "ts": 0.0})
            return innertube.player_info("dQw4w9WgXcQ")
        finally:
            innertube._transport = _REAL_TRANSPORT

    # --- resolve ---
    def test_resolve_uses_fast_path_without_ytdlp(self):
        self.fake.player = self._fast_data()
        res = youfish.resolve("dQw4w9WgXcQ")
        self.assertTrue(res.get("ok"), res)
        info = res["info"]
        self.assertEqual(info["video_itag"], "137")              # best avc1 (hw decode off)
        self.assertEqual(info["audio_itag"], "251")              # opus, non-DRC wins the tie
        self.assertEqual(info["video_ext"], "mp4")
        self.assertEqual([c["title"] for c in info["chapters"]], ["Intro", "Verse", "Chorus"])
        self.assertTrue(info["tracks"])
        self.assertEqual(self.spawns, [])                        # no yt-dlp at all
        self.assertEqual(sorted(self.probed), sorted(               # the played pair was probed
            [f["url"] for f in self.fake.player["formats"] if f["format_id"] in ("137", "251")]))

    def test_refused_fast_urls_fall_back_and_trip_the_breaker(self):
        self.fake.player = self._fast_data()
        youfish._probe_status = lambda url, ua, timeout=3: 403 if "itag=251" in url else 206

        def fake_run(cmd, **kw):
            self.spawns.append(cmd)
            data = {"title": "Y", "duration": 10,
                    "formats": [vf("137", 1080, "avc1"), af("251", 160, "opus")]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run
        res = youfish.resolve("dQw4w9WgXcQ")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "Y")               # yt-dlp's answer, not the fast one
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(len(youfish._fast_player_state["strikes"]), 1)
        youfish.invalidate_resolve_cache()
        youfish.resolve("dQw4w9WgXcQ")                            # second refusal opens the breaker
        self.assertGreater(youfish._fast_player_state["off_until"], time.time())
        n = len([c for c in self.fake.calls if c[0] == "player"])
        youfish.invalidate_resolve_cache()
        youfish.resolve("dQw4w9WgXcQ")
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "player"]), n)   # not asked

    def test_network_failure_in_probe_is_no_verdict(self):
        self.fake.player = self._fast_data()
        youfish._probe_status = lambda url, ua, timeout=3: None
        self.assertIsNotNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        self.assertEqual(youfish._fast_player_state["strikes"], [])

    def test_resolve_falls_back_when_fast_path_fails(self):
        self.fake.player = innertube.Unplayable("Sign in to confirm your age")
        def fake_run(cmd, **kw):
            self.spawns.append(cmd)
            data = {"title": "Y", "duration": 10,
                    "formats": [vf("137", 1080, "avc1"), af("251", 160, "opus")]}
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        youfish.subprocess.run = fake_run
        res = youfish.resolve("dQw4w9WgXcQ")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["info"]["title"], "Y")
        self.assertEqual(len(self.spawns), 1)

    def test_user_client_skips_fast_path(self):
        self.fake.player = self._fast_data()
        youfish.get_settings = lambda: {"player_client": "tv", "default_quality": 0}
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))     # explicit client honoured
        self.assertEqual(self.fake.calls, [])

    def test_bot_wall_trips_the_circuit_breaker(self):
        self.fake.player = innertube.Unplayable("Sign in to confirm you're not a bot", bot=True)
        for _ in range(youfish._FAST_PLAYER_BOT_LIMIT):
            self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        n = len(self.fake.calls)
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        self.assertEqual(len(self.fake.calls), n)                        # breaker open: no request

    def test_reresolve_asks_the_fast_client_first(self):
        self.fake.player = self._fast_data()
        youfish._ytdlp_formats = lambda vid, anon=False: self.fail("yt-dlp re-resolve not needed")
        fresh = youfish._reresolve("dQw4w9WgXcQ", "137",
                                   "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=137&old=1")
        self.assertIn("itag=137", fresh)

    def test_reresolve_refused_fast_url_goes_to_ytdlp(self):
        self.fake.player = self._fast_data()
        youfish._probe_status = lambda url, ua, timeout=3: 403
        youfish._ytdlp_formats = lambda vid, anon=False: {"137": "https://new/137"}
        fresh = youfish._reresolve("dQw4w9WgXcQ", "137",
                                   "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=137&old=1")
        self.assertEqual(fresh, "https://new/137")
        self.assertEqual(len(youfish._fast_player_state["strikes"]), 1)

    def test_reresolve_after_a_refusal_skips_the_fast_client(self):
        self.fake.player = self._fast_data()
        old = "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&old=1"
        youfish._url_cache = {"dQw4w9WgXcQ": {"ts": time.time(), "fmts": {
            "251": "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&cached=1"}}}
        youfish._ytdlp_formats = lambda vid, anon=False: {"251": "https://new/251"}
        fresh = youfish._reresolve("dQw4w9WgXcQ", "251", old, refused=True)
        self.assertEqual(fresh, "https://new/251")
        self.assertEqual([c for c in self.fake.calls if c[0] == "player"], [])

    def test_reresolve_finds_a_fast_path_dub_track_under_ytdlp_naming(self):
        # The fast path calls a dub "251-es.3"; yt-dlp numbers it "251-8" and keys it "251@es".
        youfish._ytdlp_formats = lambda vid, anon=False: {"251-8": "https://new/es",
                                                          "251@es": "https://new/es"}
        old = "https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251&old=1"
        self.assertEqual(youfish._reresolve("dQw4w9WgXcQ", "251-es.3", old, refused=True),
                         "https://new/es")
        self.assertEqual(youfish._track_alias("251-drc-en-US.4"), "251-drc@en-US")
        self.assertIsNone(youfish._track_alias("251-8"))

    def test_drop_resolved_forgets_only_that_video(self):
        youfish._resolve_cache.update({
            "aaaaaaaaaaa\x1fvisionos\x1f0": {"payload": {}, "good_until": time.time() + 99, "ts": 0},
            "aaaaaaaaaaa\x1fvisionos\x1f1": {"payload": {}, "good_until": time.time() + 99, "ts": 0},
            "bbbbbbbbbbb\x1fvisionos\x1f0": {"payload": {}, "good_until": time.time() + 99, "ts": 0}})
        youfish._drop_resolved("aaaaaaaaaaa")
        self.assertEqual(list(youfish._resolve_cache), ["bbbbbbbbbbb\x1fvisionos\x1f0"])

    def test_reresolve_foreign_client_goes_to_ytdlp(self):
        youfish._ytdlp_formats = lambda vid, anon=False: {"137": "https://new/137"}
        fresh = youfish._reresolve("dQw4w9WgXcQ", "137", "https://rr1.googlevideo.com/x?c=TVHTML5&itag=137")
        self.assertEqual(fresh, "https://new/137")
        self.assertEqual(self.fake.calls, [])

    # --- channel pages ---
    def test_channel_videos_pages_via_continuation(self):
        self.fake.tabs[(CID, "videos")] = {
            "items": [_it_item("v%010d" % i) for i in range(30)], "continuation": "T1",
            "channel": _it_chan()}
        self.fake.conts["T1"] = {"items": [_it_item("w%010d" % i, dur=0, live=True) for i in range(30)],
                                 "continuation": None}
        p1 = youfish.channel_videos("https://www.youtube.com/@mkbhd", 1, 30)
        self.assertTrue(p1["ok"])
        self.assertEqual(len(p1["items"]), 30)
        self.assertTrue(p1["has_more"])
        self.assertEqual(p1["channel"]["id"], CID)
        self.assertEqual(p1["channel"]["video_count"], 42)
        self.assertIn("=s176", p1["channel"]["thumbnail"])
        self.assertEqual(p1["items"][0]["posted"], "2 days ago")
        p2 = youfish.channel_videos("https://www.youtube.com/@mkbhd", 31, 30)
        self.assertEqual(p2["items"][0]["id"], "w0000000000")
        self.assertEqual(p2["items"][0]["live"], 1)
        self.assertFalse(p2["has_more"])
        self.assertEqual([c for c in self.fake.calls if c[0] in ("tab", "cont")],
                         [("tab", CID, "videos"), ("cont", "T1")])        # page 1 fetched once
        self.assertEqual(youfish._feed_durations_cache["map"].get("v0000000000"), 100)  # learned
        self.assertEqual(self.spawns, [])
        # The avatar came along with page 1 — opening a video of this channel is now free.
        self.assertEqual(youfish.channel_avatar(CID)["id"], CID)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "tab"]), 1)

    def test_channel_videos_falls_back_to_ytdlp(self):
        self.fake.tabs[(CID, "videos")] = innertube.InnertubeError("no videos tab")
        res = youfish.channel_videos(CID, 1, 30)
        self.assertFalse(res["ok"])                                  # the yt-dlp error surfaces
        self.assertEqual(len(self.spawns), 1)

    def test_members_only_dropped_from_channel_page(self):
        self.fake.tabs[(CID, "videos")] = {"items": [_it_item("aaaaaaaaaa1"),
                                                     _it_item("aaaaaaaaaa2", members=True)],
                                           "continuation": None, "channel": _it_chan()}
        res = youfish.channel_videos(CID, 1, 30)
        self.assertEqual([i["id"] for i in res["items"]], ["aaaaaaaaaa1"])

    # --- feed ---
    def test_feed_durations_classify_via_innertube(self):
        youfish.list_subscriptions = lambda: [{"id": CID, "url": "", "name": "A"}]
        youfish._feed_cache = {CID: {"ts": time.time(), "rows": [
            [3, {"id": "LONG0000001", "channel_id": CID}],
            [2, {"id": "SHORT000001", "channel_id": CID}],
            [1, {"id": "LIVE0000001", "channel_id": CID}]]}}
        self.fake.tabs[(CID, "videos")] = {"items": [_it_item("LONG0000001", dur=300)],
                                           "continuation": None, "channel": _it_chan()}
        self.fake.tabs[(CID, "shorts")] = {"items": [_it_item("SHORT000001", dur=0, short=True)],
                                           "continuation": None, "channel": _it_chan()}
        # no streams tab → LIVE0000001 stays unclassified
        res = youfish.feed_durations()
        self.assertEqual(res["durations"], {"LONG0000001": 300})
        self.assertEqual(res["shorts"], ["SHORT000001"])
        self.assertEqual(self.spawns, [])
        tabs = [c[2] for c in self.fake.calls if c[0] == "tab"]
        self.assertEqual(tabs, ["videos", "shorts", "streams"])
        # The unclassifiable id isn't re-asked on the next (non-forced) call...
        self.fake.calls[:] = []
        youfish.feed_durations()
        self.assertEqual(self.fake.calls, [])
        # ...but an explicit refresh does.
        youfish.feed_durations(force=True)
        self.assertTrue(self.fake.calls)
        # And it all persisted.
        self.assertEqual(youfish._load_saved_durations(), {"LONG0000001": 300})
        self.assertEqual(youfish._load_saved_shorts(), {"SHORT000001"})

    def test_feed_durations_skip_shorts_tab_when_videos_tab_suffices(self):
        youfish.list_subscriptions = lambda: [{"id": CID, "url": "", "name": "A"}]
        youfish._feed_cache = {CID: {"ts": time.time(), "rows": [[3, {"id": "LONG0000001",
                                                                     "channel_id": CID}]]}}
        self.fake.tabs[(CID, "videos")] = {"items": [_it_item("LONG0000001", dur=300)],
                                           "continuation": None, "channel": _it_chan()}
        youfish.feed_durations()
        self.assertEqual([c[2] for c in self.fake.calls if c[0] == "tab"], ["videos"])

    def test_feed_durations_fall_back_per_channel(self):
        youfish.list_subscriptions = lambda: [{"id": CID, "url": "", "name": "A"}]
        youfish._feed_cache = {CID: {"ts": time.time(), "rows": [[3, {"id": "LONG0000001",
                                                                     "channel_id": CID}]]}}
        self.fake.tabs[(CID, "videos")] = OSError("network down")

        def fake_run(cmd, **kw):
            self.spawns.append(cmd)
            entries = [{"id": "LONG0000001", "duration": 300}] if cmd[-1].endswith("/videos") else []
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"entries": entries}),
                                         stderr="")
        youfish.subprocess.run = fake_run
        res = youfish.feed_durations()
        self.assertEqual(res["durations"], {"LONG0000001": 300})
        self.assertEqual(len(self.spawns), 2)                         # the old /videos + /shorts pair

    def test_feed_channel_falls_back_from_rss_to_innertube(self):
        self.fake.tabs[(CID, "videos")] = {"items": [_it_item("aaaaaaaaaa1", dur=61),
                                                     _it_item("aaaaaaaaaa2", members=True)],
                                           "continuation": None, "channel": _it_chan("Chan")}
        saved = youfish._feed_from_rss
        youfish._feed_from_rss = lambda cid, n: []
        try:
            rows = youfish._feed_fetch_channel("/fake/yt-dlp", CID, 15)
        finally:
            youfish._feed_from_rss = saved
        self.assertEqual([r[1]["id"] for r in rows], ["aaaaaaaaaa1"])
        self.assertEqual(rows[0][1]["duration"], 61)
        self.assertEqual(rows[0][1]["channel_id"], CID)
        self.assertEqual(rows[0][1]["uploader"], "Chan")
        self.assertAlmostEqual(rows[0][0], time.time() - 2 * 86400, delta=60)
        self.assertEqual(self.spawns, [])

    # --- live ---
    _MASTER = "\n".join([
        "#EXTM3U",
        '#EXT-X-STREAM-INF:BANDWIDTH=1500000,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=1280x720,'
        'FRAME-RATE=30',
        "https://manifest.googlevideo.com/api/manifest/hls_playlist/id/x/itag/95/index.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.4d401e,mp4a.40.2",RESOLUTION=854x480,'
        'FRAME-RATE=30',
        "https://manifest.googlevideo.com/api/manifest/hls_playlist/id/x/itag/94/index.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=4000000,CODECS="avc1.640028",RESOLUTION=1920x1080,'
        'AUDIO="233"',
        "https://manifest.googlevideo.com/api/manifest/hls_playlist/id/x/itag/270/index.m3u8",
    ])

    def _live_data(self, master=None):
        return {"id": "dQw4w9WgXcQ", "title": "Live now", "uploader": "Chan", "channel": "Chan",
                "channel_id": CID, "channel_url": "", "description": "", "duration": 0,
                "view_count": 7, "is_live": True, "live_status": "is_live", "has_hls": True,
                "formats": innertube._hls_variants(master or self._MASTER, "UA-VR"),
                "subtitles": {}, "automatic_captions": {}, "chapters": []}

    def test_live_resolves_through_the_live_client(self):
        self.fake.player = self._fast_data(live=True)
        self.fake.live = self._live_data()
        res = youfish.resolve("dQw4w9WgXcQ")
        self.assertTrue(res.get("ok"), res)
        info = res["info"]
        self.assertTrue(info["is_live"])
        self.assertEqual(info["muxed_itag"], "95")
        self.assertIn("/itag/95/", info["muxed_url"])           # HLS stays unproxied
        self.assertEqual(info["muxed_proto"], "m3u8_native")
        self.assertEqual(info["video_url"], "")
        self.assertEqual(info["http_ua"], "UA-VR")
        self.assertEqual(self.spawns, [])                       # no yt-dlp, no widen dumps
        self.assertEqual(self.probed, [])                       # HLS isn't probed

    def test_live_without_a_muxed_rung_goes_to_ytdlp(self):
        self.fake.player = self._fast_data(live=True)
        self.fake.live = self._live_data(master="#EXTM3U\n")
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        self.fake.live = innertube.InnertubeError("no HLS manifest")
        self.assertIsNone(youfish._fast_player_data("dQw4w9WgXcQ"))
        self.assertEqual([c[0] for c in self.fake.calls], ["player", "live", "player", "live"])

    # --- search ---
    def _svid(self, n, **kw):
        d = _it_item("vid%08d" % n, channel="Up %d" % n)
        d.update(kind="video", **kw)
        return d

    def test_search_pages_by_continuation(self):
        sp = youfish._search_filter_sp(video_only=True)
        self.fake.searches[("cats", sp)] = {
            "items": [self._svid(i) for i in range(1, 5)] + [
                self._svid(5, members=True), self._svid(6, short=True)],
            "continuation": "T2"}
        self.fake.searches["T2"] = {"items": [self._svid(i) for i in range(7, 11)],
                                    "continuation": None}
        r = youfish.search("cats", n=6)
        self.assertTrue(r["ok"])
        self.assertEqual([e["id"] for e in r["items"]], ["vid%08d" % i for i in range(1, 5)])
        self.assertEqual(r["filtered_members"], 1)
        self.assertTrue(r["has_more"])
        row = r["items"][0]
        self.assertEqual((row["type"], row["uploader"], row["duration"], row["posted"]),
                         ("video", "Up 1", 100, "2 days ago"))
        self.assertEqual([c[1] for c in self.fake.calls], [("cats", sp)])
        r2 = youfish.search("cats", n=6, start=7)               # page 2: only the new page fetched
        self.assertEqual([e["id"] for e in r2["items"]], ["vid%08d" % i for i in range(7, 11)])
        self.assertFalse(r2["has_more"])
        self.assertEqual([c[1] for c in self.fake.calls], [("cats", sp), "T2"])
        youfish.search("cats", n=6)                             # a fresh page 1 is reused
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.spawns, [])

    def test_search_filters_and_channels_use_their_sp(self):
        sp = youfish._search_filter_sp(sort=2, video_only=True)
        self.fake.searches[("cats", sp)] = {"items": [self._svid(1)], "continuation": None}
        r = youfish.search("cats", filters={"sort": 2})
        self.assertEqual([e["id"] for e in r["items"]], ["vid00000001"])
        self.assertFalse(r["has_more"])
        self.fake.searches[("mkbhd", youfish._SEARCH_SP["channel"])] = {"items": [
            {"kind": "channel", "id": CID, "title": "MKBHD", "handle": "@mkbhd",
             "subscribers": 19000000, "url": "https://www.youtube.com/channel/" + CID,
             "avatars": [{"url": "https://yt3.example/a=s88-c", "width": 88, "height": 88}]},
            self._svid(2)], "continuation": None}
        r = youfish.search("mkbhd", kind="channel")
        self.assertEqual(len(r["items"]), 1)
        ch = r["items"][0]
        self.assertEqual((ch["type"], ch["id"], ch["title"], ch["subscribers"]),
                         ("channel", CID, "MKBHD", 19000000))
        self.assertTrue(ch["thumbnail"].startswith("https://yt3.example/"))

    def test_search_falls_back_to_ytdlp(self):
        self.fake.searches[("cats", youfish._search_filter_sp(video_only=True))] = \
            innertube.InnertubeError("HTTP 500")
        r = youfish.search("cats")
        self.assertFalse(r["ok"])                                 # the stub yt-dlp "failed"
        self.assertEqual(len(self.spawns), 1)
        self.assertIn("ytsearch15:cats", self.spawns[0])

    # --- watch-next: related + video_info ---
    def test_related_from_watch_next(self):
        self.fake.nexts["dQw4w9WgXcQ"] = {"related": [
            _it_item("dQw4w9WgXcQ", channel="Self"), _it_item("aaaaaaaaaa1", channel="A"),
            _it_item("aaaaaaaaaa2", members=True), _it_item("aaaaaaaaaa3", dur=30),
            _it_item("aaaaaaaaaa4", channel="B")]}
        r = youfish.related("https://www.youtube.com/watch?v=dQw4w9WgXcQ", n=5)
        self.assertEqual([e["id"] for e in r["items"]], ["aaaaaaaaaa1", "aaaaaaaaaa4"])
        self.assertEqual(r["items"][0]["uploader"], "A")
        youfish.related("dQw4w9WgXcQ")                            # cached
        self.assertEqual(self.fake.calls, [("next", "dQw4w9WgXcQ")])
        self.assertEqual(self.spawns, [])

    def test_related_falls_back_to_the_mix(self):
        self.fake.nexts["dQw4w9WgXcQ"] = innertube.InnertubeError("HTTP 500")
        youfish.related("dQw4w9WgXcQ")
        self.assertEqual(len(self.spawns), 1)
        self.assertTrue(any("list=RDdQw4w9WgXcQ" in a for a in self.spawns[0]))

    def test_video_info_from_player_and_next(self):
        self.fake.player = self._fast_data()
        self.fake.nexts["dQw4w9WgXcQ"] = {"related": [], "likes": 1234,
                                          "upload_date": "20091024"}
        r = youfish.video_info("https://youtu.be/dQw4w9WgXcQ")
        self.assertTrue(r["ok"], r)
        info = r["info"]
        self.assertEqual((info["like_count"], info["upload_date"]), (1234, "20091024"))
        self.assertEqual(info["title"], self.fake.player["title"])
        self.assertEqual([c["title"] for c in info["chapters"]], ["Intro", "Verse", "Chorus"])
        self.assertEqual(info["chapters"][1]["start"], self.fake.player["chapters"][1]["start_time"])
        self.assertIn("dQw4w9WgXcQ", info["thumbnail"])
        self.assertEqual(self.spawns, [])

    def test_video_info_unplayable_falls_back(self):
        self.fake.player = innertube.Unplayable("Sign in to confirm your age")
        youfish.video_info("dQw4w9WgXcQ")
        self.assertTrue(self.spawns)                               # yt-dlp's android → default dumps

    # --- small HTTP helpers ---
    def test_suggestions_pooled_and_cached(self):
        url = ("https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=%s"
               % "cat%20vid")
        self.fake.http[url] = (200, json.dumps(["cat vid", ["cat videos", 3, "cat vids"]]).encode())
        youfish._sugg_cache.clear()
        try:
            r = youfish.search_suggestions(" cat vid ")
            self.assertEqual(r, {"ok": True, "suggestions": ["cat videos", "cat vids"]})
            youfish.search_suggestions("cat vid")
            self.assertEqual(self.fake.calls, [("http", url)])
            self.fake.http.clear()
            self.assertFalse(youfish.search_suggestions("dog")["ok"])
            self.assertNotIn("dog", youfish._sugg_cache)          # failures aren't cached
        finally:
            youfish._sugg_cache.clear()

    def test_sponsor_segments_cached_on_success_only(self):
        answers = [{"ok": False, "segments": []},
                   {"ok": True, "segments": [{"start": 1.0, "end": 2.0, "category": "sponsor"}]}]
        saved = youfish._sponsor_segments_fetch
        youfish._sponsor_segments_fetch = lambda vid: answers.pop(0)
        youfish._sb_cache.clear()
        try:
            self.assertFalse(youfish.sponsor_segments("dQw4w9WgXcQ")["ok"])
            self.assertEqual(len(youfish.sponsor_segments("dQw4w9WgXcQ")["segments"]), 1)
            self.assertEqual(len(youfish.sponsor_segments("dQw4w9WgXcQ")["segments"]), 1)
            self.assertEqual(answers, [])                          # third call was a cache hit
        finally:
            youfish._sponsor_segments_fetch = saved
            youfish._sb_cache.clear()

    # --- layout drift: an answer that parses to nothing goes to yt-dlp ---
    def test_search_parsed_empty_goes_to_ytdlp_and_is_counted(self):
        youfish._it_fallbacks.clear()
        youfish._INNERTUBE["mod"] = innertube        # what _it_note checks the error type against
        self.fake.searches[("cats", youfish._search_filter_sp(video_only=True))] = {
            "items": [], "continuation": "T"}
        youfish.search("cats")
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(youfish._search_pages, {})                # nothing cached
        self.assertEqual(youfish._it_fallbacks["search"][0], 1)
        self.assertIn("parsed empty", "\n".join(youfish._it_fallback_report()))
        youfish._it_fallbacks.clear()

    def test_unplayable_and_network_errors_are_not_drift(self):
        youfish._it_fallbacks.clear()
        youfish._INNERTUBE["mod"] = innertube
        youfish._it_note("player", innertube.Unplayable("Sign in to confirm your age"))
        youfish._it_note("search", IOError("timed out"))
        self.assertEqual(youfish._it_fallbacks, {})
        self.assertIn("no layout fallbacks", "\n".join(youfish._it_fallback_report()))

    def test_empty_videos_tab_goes_to_ytdlp(self):
        self.fake.tabs[(CID, "videos")] = {"items": [], "continuation": None,
                                           "channel": _it_chan()}
        youfish.channel_videos(CID)
        self.assertEqual(len(self.spawns), 1)

    def test_feed_lengths_parsed_without_durations_go_to_ytdlp(self):
        youfish.list_subscriptions = lambda: [{"id": CID, "url": "", "name": "A"}]
        youfish._feed_cache = {CID: {"ts": time.time(), "rows": [[3, {"id": "LONG0000001",
                                                                     "channel_id": CID}]]}}
        self.fake.tabs[(CID, "videos")] = {"items": [_it_item("LONG0000001", dur=0)],
                                           "continuation": None, "channel": _it_chan()}
        youfish.feed_durations()
        self.assertEqual([c[-1].rsplit("/", 1)[-1] for c in self.spawns], ["videos", "shorts"])

    # --- comments ---
    def _cm(self, cid, rc=0, rtok=None):
        return {"id": cid, "author": "@" + cid, "text": "t " + cid, "likes": 1, "time": "1 day ago",
                "thumbnail": "", "is_uploader": False, "reply_count": rc, "replies_token": rtok}

    def test_comments_page_through_and_fetch_replies(self):
        self.fake.nexts["dQw4w9WgXcQ"] = {"related": [], "comments_token": "C1"}
        self.fake.cpages["C1"] = {"comments": [self._cm("a", 30, "Ra"), self._cm("b")],
                                  "continuation": "C2", "total": 1234}
        self.fake.cpages["C2"] = {"comments": [self._cm("c", 2, "Rc"), self._cm("d")],
                                  "continuation": "C3", "total": None}
        self.fake.cpages["Ra"] = {"comments": [self._cm("a.%d" % i) for i in range(10)],
                                  "continuation": "Ra2", "total": None}
        self.fake.cpages["Rc"] = {"comments": [self._cm("c.1"), self._cm("c.2")],
                                  "continuation": None, "total": None}
        r = youfish.comments("dQw4w9WgXcQ", 3, True)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["count"], r["total"]), (3, 1234))
        self.assertEqual([c["id"] for c in r["comments"]], ["a", "b", "c"])  # C3 never fetched
        a = r["comments"][0]
        self.assertEqual((len(a["replies"]), a["reply_count"]), (10, 30))
        self.assertNotIn("replies_token", a)
        self.assertEqual(set(a["replies"][0]), {"id", "author", "text", "likes", "time",
                                                "thumbnail", "is_uploader"})
        self.assertEqual(len(r["comments"][2]["replies"]), 2)
        self.assertNotIn(("comments", "C3"), self.fake.calls)
        self.assertEqual(self.spawns, [])
        # without replies: no reply pages at all
        self.fake.calls[:] = []
        youfish.comments("dQw4w9WgXcQ", 2, False)
        self.assertEqual([c[1] for c in self.fake.calls if c[0] == "comments"], ["C1"])

    def test_comments_parsed_empty_or_no_token_go_to_ytdlp(self):
        youfish._it_fallbacks.clear()
        youfish._INNERTUBE["mod"] = innertube
        self.fake.nexts["dQw4w9WgXcQ"] = {"related": [], "comments_token": "C1"}
        self.fake.cpages["C1"] = {"comments": [], "continuation": "C2", "total": 99}
        youfish.comments("dQw4w9WgXcQ", 5, True)
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(youfish._it_fallbacks["comments"][0], 1)
        youfish._it_fallbacks.clear()
        youfish._next_cache.clear()
        self.fake.nexts["dQw4w9WgXcQ"] = {"related": [], "comments_token": None}   # comments off?
        youfish.comments("dQw4w9WgXcQ", 5, True)
        self.assertEqual(len(self.spawns), 2)

    def test_watch_next_is_single_flight(self):
        import threading as _th
        gate = _th.Event()
        real = self.fake.watch_next

        def slow(vid):
            gate.wait(2)
            return real(vid)
        self.fake.watch_next = slow
        self.fake.nexts["dQw4w9WgXcQ"] = {"related": [], "likes": 7}
        out = []
        ts = [_th.Thread(target=lambda: out.append(youfish._watch_next("dQw4w9WgXcQ")))
              for _ in range(3)]
        for t in ts:
            t.start()
        time.sleep(0.1)
        gate.set()
        for t in ts:
            t.join(5)
        self.assertEqual([o["likes"] for o in out], [7, 7, 7])
        self.assertEqual(self.fake.calls, [("next", "dQw4w9WgXcQ")])

    def test_token_free_client_follows_innertube(self):
        saved = youfish._pot_active
        try:
            youfish._pot_active = lambda: True
            self.assertEqual(youfish._token_free_client(), "visionos")
            self.assertEqual(youfish._default_client(), "visionos")
            youfish._it = lambda: None
            self.assertEqual(youfish._token_free_client(), "visionos")
            youfish._pot_active = lambda: False
            self.assertEqual(youfish._default_client(), "")
        finally:
            youfish._pot_active = saved


class YtdlpVersionCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ver-")
        self.bin = os.path.join(self._tmp, "yt-dlp")
        with open(self.bin, "w") as f:
            f.write("x")
        self._saved = (youfish._ytdlp_path, youfish._data_dir, youfish.subprocess.run)
        youfish._ytdlp_path = lambda: self.bin
        youfish._data_dir = lambda: self._tmp
        self.runs = 0

        def run(cmd, **kw):
            self.runs += 1
            return types.SimpleNamespace(returncode=0, stdout="2026.08.19\n", stderr="")
        youfish.subprocess.run = run

    def tearDown(self):
        youfish._ytdlp_path, youfish._data_dir, youfish.subprocess.run = self._saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_version_remembered_per_binary(self):
        self.assertEqual(youfish.ytdlp_version(), "2026.08.19")
        self.assertEqual(youfish.ytdlp_version(), "2026.08.19")
        self.assertEqual(self.runs, 1)                        # second launch: no spawn
        youfish.ytdlp_version(fresh=True)
        self.assertEqual(self.runs, 2)                        # install proves the binary runs
        with open(self.bin, "w") as f:
            f.write("updated binary")                         # an update changes the stat
        youfish.ytdlp_version()
        self.assertEqual(self.runs, 3)


class YtdlpInprocessTier(unittest.TestCase):
    """_ytdlp_run: the listing argv runs in-process through parse_options, else the binary."""

    def setUp(self):
        self._saved = (youfish._import_yt_dlp, youfish.subprocess.run)
        self.spawns = []

        def run(cmd, **kw):
            self.spawns.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout='{"from": "binary"}', stderr="")
        youfish.subprocess.run = run

    def tearDown(self):
        youfish._import_yt_dlp, youfish.subprocess.run = self._saved

    def _fake_mod(self, fail=False):
        seen = self.seen = {}

        class YDL:
            def __init__(self, opts):
                seen["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                seen["closed"] = True

            def extract_info(self, url, download=True):
                if fail:
                    raise RuntimeError("extractor broke")
                seen["url"], seen["download"] = url, download
                return {"entries": [{"id": "x"}]}

            def post_extract(self, info):
                seen["post_extract"] = True

            def sanitize_info(self, info, remove_private_keys=False):
                seen["stripped"] = remove_private_keys
                return dict(info)

        def parse_options(argv):
            seen["argv"] = argv
            return types.SimpleNamespace(
                urls=[argv[-1]],
                ydl_opts={"dump_single_json": True, "simulate": True, "quiet": True,
                          "playlist_items": "1:5", "clean_infojson": True, "socket_timeout": None})
        return types.SimpleNamespace(parse_options=parse_options, YoutubeDL=YDL)

    def test_in_process_answers_without_a_spawn(self):
        youfish._import_yt_dlp = lambda: self._fake_mod()
        argv = ["-4", "--flat-playlist", "--playlist-items", "1:5", "--dump-single-json", "--",
                "ytsearch5:cats"]
        proc = youfish._ytdlp_run("/fake/yt-dlp", argv, 90)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {"entries": [{"id": "x"}]})
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.seen["argv"], argv)                 # the binary's argv, verbatim
        self.assertEqual((self.seen["url"], self.seen["download"]), ("ytsearch5:cats", False))
        self.assertNotIn("dump_single_json", self.seen["opts"])
        self.assertEqual(self.seen["opts"]["playlist_items"], "1:5")
        self.assertEqual(self.seen["opts"]["socket_timeout"], 20)
        self.assertTrue(self.seen["closed"])
        self.assertTrue(self.seen["post_extract"])                # what the CLI does for -J...
        self.assertFalse(self.seen["stripped"])                   # ...without dropping "entries"

    def test_in_process_failure_runs_the_binary(self):
        youfish._import_yt_dlp = lambda: self._fake_mod(fail=True)
        proc = youfish._ytdlp_run("/fake/yt-dlp", ["--dump-single-json", "--", "u"], 90)
        self.assertEqual(json.loads(proc.stdout), {"from": "binary"})
        self.assertEqual(self.spawns, [["/fake/yt-dlp", "--dump-single-json", "--", "u"]])

    def test_no_zipapp_runs_the_binary(self):
        youfish._import_yt_dlp = lambda: None
        youfish._ytdlp_run("/fake/yt-dlp", ["--dump-single-json", "--", "u"], 90)
        self.assertEqual(len(self.spawns), 1)



class SignedInClientArgs(unittest.TestCase):
    """yt-dlp drops clients that don't support cookies (visionos among them) and fails with none
    left, so a signed-in run must not name the automatic token-free client."""

    def setUp(self):
        self._saved = {n: getattr(youfish, n) for n in (
            "get_settings", "_pot_active", "_write_cookies_temp", "_ytdlp_path",
            "_ensure_pot_server", "_pot_ytdlp_args", "_set_pdeathsig")}
        self._run = youfish.subprocess.run
        self.settings = {}
        youfish.get_settings = lambda: self.settings
        youfish._pot_active = lambda: True
        self._tmp = tempfile.mkdtemp(prefix="authed-")

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(youfish, n, f)
        youfish.subprocess.run = self._run
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_signed_in_run_leaves_the_client_to_ytdlp(self):
        self.assertIn("player_client=visionos", youfish._yt_extractor_args(want_pot=True)[1])
        args = youfish._yt_extractor_args(want_pot=True, cookies=True)
        self.assertEqual(args, ["--extractor-args", "youtube:fetch_pot=always"])

    def test_user_client_and_override_still_win_when_signed_in(self):
        self.assertIn("player_client=mweb",
                      youfish._yt_extractor_args(client_override="mweb", cookies=True)[1])
        self.settings = {"player_client": "tv"}
        self.assertIn("player_client=tv", youfish._yt_extractor_args(cookies=True)[1])

    def test_cookied_reresolve_dump_names_no_client(self):
        ck = os.path.join(self._tmp, "c.txt")
        youfish._write_cookies_temp = lambda: open(ck, "w").close() or ck
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._set_pdeathsig = lambda: None
        cmds = []

        def run(cmd, **kw):
            cmds.append(cmd)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        youfish.subprocess.run = run
        youfish._ytdlp_formats("dQw4w9WgXcQ")
        self.assertIn("--cookies", cmds[0])
        self.assertFalse([a for a in cmds[0] if "player_client=" in a], cmds[0])

    def test_signed_in_audio_download_names_no_client(self):
        ck = os.path.join(self._tmp, "c.txt")
        youfish._write_cookies_temp = lambda: open(ck, "w").close() or ck
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._ensure_pot_server = lambda **kw: True
        youfish._pot_ytdlp_args = lambda: []
        youfish._set_pdeathsig = lambda: None
        cmds, done = [], youfish.threading.Event()

        def popen(cmd, **kw):
            cmds.append(cmd)
            raise RuntimeError("stop here")                # the thread reports download_done
        saved = (youfish.subprocess.Popen, sys.modules.get("pyotherside"), youfish._downloads_dir)
        youfish.subprocess.Popen = popen
        youfish._downloads_dir = lambda: self._tmp
        sys.modules["pyotherside"] = types.SimpleNamespace(
            send=lambda *a: done.set() if a[0] == "download_done" else None)
        try:
            youfish.download("dQw4w9WgXcQ", "Song", "audio")
            self.assertTrue(done.wait(5), "download thread never finished")
        finally:
            youfish.subprocess.Popen, prev, youfish._downloads_dir = saved
            if prev is not None:
                sys.modules["pyotherside"] = prev
            else:
                sys.modules.pop("pyotherside", None)
        self.assertIn("--cookies", cmds[0])
        self.assertEqual(cmds[0][cmds[0].index("-f") + 1], "140")
        self.assertFalse([a for a in cmds[0] if "player_client=" in a], cmds[0])


class AccountSelection(unittest.TestCase):
    """ytm.select_account (the account/brand-channel picker) persists the choice and returns. It
    used to self-deadlock on the store lock, which hung the main Python lane for good."""

    def setUp(self):
        import importlib
        self._prev = sys.modules.pop("ytm", None)            # other tests install a fake ytm
        self.ytm = importlib.import_module("ytm")
        self._tmp = tempfile.mkdtemp(prefix="acct-")
        self._dd = self.ytm._data_dir
        self.ytm._data_dir = lambda: self._tmp
        with open(self.ytm._cookies_path(), "w") as f:
            json.dump({"sapisid": "S", "ytdlp": "# Netscape", "cookie": "SAPISID=S"}, f)

    def tearDown(self):
        self.ytm._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._prev is not None:
            sys.modules["ytm"] = self._prev
        else:
            sys.modules.pop("ytm", None)

    def test_select_account_returns_and_persists(self):
        res = {}
        t = youfish.threading.Thread(
            target=lambda: res.update(self.ytm.select_account("1", "PAGE", "DS", "Brand")),
            daemon=True)
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive(), "select_account deadlocked")
        self.assertEqual(res, {"ok": True, "name": "Brand"})
        self.assertEqual(self.ytm.selected_account(),
                         {"authuser": "1", "page_id": "PAGE", "name": "Brand"})
        with open(self.ytm._cookies_path()) as f:
            self.assertEqual(json.load(f)["sapisid"], "S")   # the login itself is untouched

class RssPooled(unittest.TestCase):
    """_rss_get: the feed's RSS goes through innertube's keep-alive pool when the module is there,
    a refusal raises (so the channel counts as failed, as with urlopen), and a redirect is left to
    urllib, which follows it."""
    XML = "<feed><entry><yt:videoId>abc</yt:videoId></entry></feed>"

    def setUp(self):
        self.saved_it = youfish._it
        self.saved_open = youfish.urllib.request.urlopen
        self.calls = []

    def tearDown(self):
        youfish._it = self.saved_it
        youfish.urllib.request.urlopen = self.saved_open

    def _pool(self, status, body=b""):
        def request(method, url, body_=None, headers=None, timeout=10):
            self.calls.append((method, url, headers))
            return status, body, None
        youfish._it = lambda: types.SimpleNamespace(request=request)

    def _urllib(self, text):
        import io

        class R(io.BytesIO):
            def __enter__(s): return s
            def __exit__(s, *a): return False
        youfish.urllib.request.urlopen = lambda req, timeout=0: R(text.encode())

    def test_pooled_get(self):
        self._pool(200, self.XML.encode())
        self._urllib("<feed>WRONG</feed>")
        self.assertEqual(youfish._rss_get("UC_X"), self.XML)
        method, url, headers = self.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/feeds/videos.xml?channel_id=UC_X"))
        self.assertIn("User-Agent", headers)

    def test_refusal_raises(self):
        self._pool(404)
        with self.assertRaises(IOError):
            youfish._rss_get("UC_GONE")
        self.assertEqual(youfish._feed_from_rss("UC_GONE", 5), [])

    def test_redirect_and_no_module_use_urllib(self):
        self._pool(302)
        self._urllib(self.XML)
        self.assertEqual(youfish._rss_get("UC_X"), self.XML)
        youfish._it = lambda: None
        self.assertEqual(youfish._rss_get("UC_X"), self.XML)


class Arm32Userland(unittest.TestCase):
    """32-bit ARM: yt-dlp is the stable one-directory armv7l zip, unpacked behind a bin/yt-dlp
    symlink and updated by downloading again; Deno and the PO-token provider are refused."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.saved = dict((n, getattr(youfish, n)) for n in (
            "_data_dir", "_ARM32", "_YTDLP_ASSET", "ytdlp_version", "_ytdlp_latest_tag",
            "_install_ytdlp_sync", "get_settings"))
        youfish._data_dir = lambda: self._tmp
        youfish._ARM32 = True
        youfish._YTDLP_ASSET = "yt-dlp_linux_armv7l.zip"
        os.makedirs(os.path.join(self._tmp, "bin"))
        self.sent = []
        self.prev_po = sys.modules.get("pyotherside")
        sys.modules["pyotherside"] = types.SimpleNamespace(send=lambda *a: self.sent.append(a))

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(youfish, n, v)
        if self.prev_po is not None:
            sys.modules["pyotherside"] = self.prev_po
        else:
            sys.modules.pop("pyotherside", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _zip(self, version, extra=None):
        import zipfile
        path = os.path.join(self._tmp, "y.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("yt-dlp_linux_armv7l", "#!/bin/sh\necho %s\n" % version)
            z.writestr("_internal/base_library.zip", version)
            for name in (extra or []):
                z.writestr(name, "x")
        return path

    def test_unpack_points_bin_ytdlp_at_the_tree(self):
        youfish._unpack_ytdlp_dir(self._zip("2026.01.01"))
        exe = youfish._managed_ytdlp()
        self.assertTrue(os.path.islink(exe))
        self.assertTrue(os.access(exe, os.X_OK))
        self.assertEqual(os.readlink(exe), os.path.join("yt-dlp-armv7l", "yt-dlp_linux_armv7l"))
        out = youfish.subprocess.run([exe], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "2026.01.01")

    def test_reinstall_swaps_and_parks_the_old_tree(self):
        youfish._unpack_ytdlp_dir(self._zip("old"))
        youfish._unpack_ytdlp_dir(self._zip("new"))
        bindir = os.path.join(self._tmp, "bin")
        with open(os.path.join(bindir, "yt-dlp-armv7l", "_internal", "base_library.zip")) as f:
            self.assertEqual(f.read(), "new")
        with open(os.path.join(bindir, "yt-dlp-armv7l.old", "_internal", "base_library.zip")) as f:
            self.assertEqual(f.read(), "old")
        self.assertFalse(os.path.exists(os.path.join(bindir, "yt-dlp-armv7l.new")))

    def test_unsafe_archive_is_refused_and_leaves_the_install(self):
        youfish._unpack_ytdlp_dir(self._zip("good"))
        with self.assertRaises(ValueError):
            youfish._unpack_ytdlp_dir(self._zip("evil", extra=["../../escape"]))
        self.assertFalse(os.path.exists(os.path.join(self._tmp, "escape")))
        out = youfish.subprocess.run([youfish._managed_ytdlp()], capture_output=True,
                                     text=True).stdout.strip()
        self.assertEqual(out, "good")

    def test_channel_is_pinned_to_stable(self):
        youfish.get_settings = lambda: {"ytdlp_channel": "nightly"}
        self.assertEqual(youfish._ytdlp_channel(), "stable")
        youfish._ARM32 = False
        self.assertEqual(youfish._ytdlp_channel(), "nightly")

    def test_update_downloads_only_when_behind(self):
        calls = []
        youfish._install_ytdlp_sync = lambda progress=None: (calls.append(1) or
                                                             (True, "Installed yt-dlp 2", "2"))
        youfish.ytdlp_version = lambda fresh=False: "1"
        youfish._ytdlp_latest_tag = lambda: "1"
        self.assertTrue(youfish._ytdlp_update_by_download()["ok"])
        self.assertEqual(calls, [])                       # up to date: nothing fetched
        youfish._ytdlp_latest_tag = lambda: ""
        res = youfish._ytdlp_update_by_download()
        self.assertFalse(res["ok"])                       # can't tell: don't pull 40 MB blind
        self.assertEqual(calls, [])
        youfish._ytdlp_latest_tag = lambda: "2"
        res = youfish._ytdlp_update_by_download()
        self.assertEqual((res["ok"], res["version"], calls), (True, "2", [1]))

    def test_deno_and_provider_are_refused(self):
        self.assertFalse(youfish.install_deno()["ok"])
        self.assertFalse(youfish.install_pot_provider()["ok"])
        self.assertEqual([e[0] for e in self.sent], ["deno_install_done", "pot_install_done"])
        self.assertTrue(all(e[1] is False for e in self.sent))



# --- Release-safety net: features that had no offline test (see TESTING.md) ------------------ #

class _TmpDataDir(unittest.TestCase):
    """Base: every store lands in a throwaway data dir."""
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="stores-")
        self._dd = youfish._data_dir
        youfish._data_dir = lambda: self._tmp

    def tearDown(self):
        youfish._data_dir = self._dd
        shutil.rmtree(self._tmp, ignore_errors=True)


class AudioTrackMemory(_TmpDataDir):
    """The Audio picker's choice, as the next video sees it (the 1.5.x "original audio" bug)."""

    def test_original_track_remembers_original_not_its_language(self):
        self.assertEqual(youfish.set_audio_track("pl", True)["audio_lang"], "")

    def test_dub_is_remembered_by_language(self):
        self.assertEqual(youfish.set_audio_track("es-419", False)["audio_lang"], "es-419")

    def test_next_video_after_picking_a_polish_original_plays_its_own_original(self):
        youfish.set_audio_track("pl", True)
        english_video = [af("251-en", 160, "opus", lang_pref=10, lang="en"),   # original
                         af("251-pl", 158, "opus", lang_pref=-1, lang="pl")]   # Polish dub
        want = youfish.get_settings().get("audio_lang") or ""
        self.assertEqual(youfish._pick_audio(english_video, want)["format_id"], "251-en")

    def test_after_picking_a_dub_the_next_video_starts_on_that_dub(self):
        youfish.set_audio_track("pl", False)
        english_video = [af("251-en", 160, "opus", lang_pref=10, lang="en"),
                         af("251-pl", 158, "opus", lang_pref=-1, lang="pl")]
        want = youfish.get_settings().get("audio_lang") or ""
        self.assertEqual(youfish._pick_audio(english_video, want)["format_id"], "251-pl")


class SettingsStore(_TmpDataDir):
    def test_defaults_then_persist(self):
        self.assertEqual(youfish.get_settings()["default_quality"], "720")
        youfish.set_setting("hide_shorts", False)
        self.assertIs(youfish.get_settings()["hide_shorts"], False)
        self.assertEqual(oct(os.stat(youfish._settings_path()).st_mode & 0o777), "0o600")

    def test_output_settings_drop_cached_resolves_others_dont(self):
        calls = []
        saved = youfish.invalidate_resolve_cache
        youfish.invalidate_resolve_cache = lambda: calls.append(1)
        try:
            for key in youfish._RESOLVE_OUTPUT_KEYS:
                youfish.set_setting(key, "x")
            self.assertEqual(len(calls), len(youfish._RESOLVE_OUTPUT_KEYS))
            youfish.set_setting("caption_lang", "de")
            youfish.set_setting("sponsorblock", False)
            self.assertEqual(len(calls), len(youfish._RESOLVE_OUTPUT_KEYS))
        finally:
            youfish.invalidate_resolve_cache = saved

    def test_corrupt_settings_file_falls_back_to_defaults(self):
        with open(youfish._settings_path(), "w") as f:
            f.write("{not json")
        self.assertEqual(youfish.get_settings()["history_limit"], 500)

    def test_qml_fallbacks_match_engine_defaults(self):
        # Backend.qml repeats some defaults as `s.key || <literal>`; a drift between the two means
        # the UI shows one value while the engine uses another.
        qml = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "qml",
                                "Backend.qml")).read()
        found = re.findall(r"=\s*s\.(\w+)\s*\|\|\s*(\"[^\"]*\"|[\d.]+)", qml)
        found += re.findall(r"\(s\.(\w+)\s*===\s*undefined\)\s*\?\s*(\"[^\"]*\"|[\d.]+|true|false)",
                            qml)
        self.assertTrue(found)
        for key, lit in found:
            if key not in youfish._SETTINGS_DEFAULTS:
                continue
            want = youfish._SETTINGS_DEFAULTS[key]
            got = json.loads(lit)
            if want in ("", 0, None) and want is not False:   # `|| ""` just guards a missing key
                continue
            self.assertEqual(got, want, "Backend.qml fallback for %s" % key)


class ResolveKeyInputs(_TmpDataDir):
    """Invariant 6: every setting that changes resolve's answer is part of the cache key."""
    def test_each_output_input_changes_the_key(self):
        base = youfish._resolve_key("vid")
        for key, val in (("default_quality", "1080"), ("audio_lang", "de"), ("hw_decode", True)):
            youfish.set_setting(key, val)
            changed = youfish._resolve_key("vid")
            self.assertNotEqual(changed, base, key)
            base = changed
        saved = youfish._signed_in
        youfish._signed_in = lambda: True
        try:
            self.assertNotEqual(youfish._resolve_key("vid"), base)
        finally:
            youfish._signed_in = saved

    def test_display_only_settings_keep_the_key(self):
        base = youfish._resolve_key("vid")
        youfish.set_setting("caption_lang", "de")
        youfish.set_setting("hide_shorts", False)
        self.assertEqual(youfish._resolve_key("vid"), base)


class LocalPlaylists(_TmpDataDir):
    def test_create_rename_add_remove_delete(self):
        pid = youfish.create_playlist("  Mix  ")["id"]
        self.assertEqual(youfish.list_playlists()[0]["title"], "Mix")
        youfish.rename_playlist(pid, "Chill")
        youfish.add_to_playlist(pid, "a", title="A")
        youfish.add_to_playlist(pid, "a", title="A again")     # no duplicates
        youfish.add_to_playlist(pid, "b")
        items = youfish.get_playlist(pid)["playlist"]["items"]
        self.assertEqual([i["id"] for i in items], ["a", "b"])
        self.assertEqual(youfish.list_playlists()[0]["title"], "Chill")
        self.assertEqual(youfish.list_playlists()[0]["count"], 2)
        self.assertEqual([i["id"] for i in youfish.remove_from_playlist(pid, "a")["playlist"]["items"]],
                         ["b"])
        youfish.delete_playlist(pid)
        self.assertEqual(youfish.list_playlists(), [])
        self.assertFalse(youfish.get_playlist(pid)["ok"])

    def test_blank_title_gets_a_name(self):
        youfish.create_playlist("   ")
        self.assertEqual(youfish.list_playlists()[0]["title"], "New playlist")


class SubscriptionsStore(_TmpDataDir):
    def test_toggle_on_and_off(self):
        r = youfish.toggle_subscription("UC1", name="One")
        self.assertTrue(r["subscribed"])
        self.assertTrue(youfish.is_subscribed("UC1"))
        self.assertEqual(youfish.list_subscriptions()[0]["name"], "One")
        self.assertFalse(youfish.toggle_subscription("UC1")["subscribed"])
        self.assertEqual(youfish.list_subscriptions(), [])

    def test_empty_id_is_refused(self):
        self.assertFalse(youfish.toggle_subscription("")["ok"])


class WatchHistoryPages(_TmpDataDir):
    def test_newest_first_paging_and_clear(self):
        for i in range(5):
            youfish.record_watch("v%d" % i, 10, 100, title="T%d" % i)
        self.assertEqual([r["id"] for r in youfish.watch_history(1, 2)], ["v4", "v3"])
        self.assertEqual([r["id"] for r in youfish.watch_history(3, 2)], ["v2", "v1"])
        youfish.record_watch("v1", 20, 100)                     # a replay moves it to the top
        self.assertEqual(youfish.watch_history(1, 1)[0]["id"], "v1")
        youfish.clear_watch_history()
        self.assertEqual(youfish.watch_history(), [])


class DownloadFolder(_TmpDataDir):
    def test_pick_reset_and_refuse(self):
        target = os.path.join(self._tmp, "Videos")
        r = youfish.set_download_dir("file://" + target)
        self.assertTrue(r["ok"])
        self.assertTrue(r["custom"])
        self.assertEqual(r["effective"], target)
        blocker = os.path.join(self._tmp, "file")
        open(blocker, "w").close()
        r = youfish.set_download_dir(os.path.join(blocker, "sub"))   # can't create under a file
        self.assertFalse(r["ok"])
        self.assertEqual(youfish.download_location()["configured"], target)   # previous kept
        self.assertFalse(youfish.set_download_dir("")["custom"])

    def test_delete_download_removes_file_and_entry(self):
        f, g = os.path.join(self._tmp, "a.m4a"), os.path.join(self._tmp, "a.mp4")
        open(f, "w").close()
        open(g, "w").close()
        youfish._save_downloads([{"id": "a", "kind": "audio", "path": f},
                                 {"id": "a", "kind": "video", "path": g}])
        left = youfish.delete_download("a", "audio")["downloads"]
        self.assertFalse(os.path.exists(f))
        self.assertTrue(os.path.exists(g))
        self.assertEqual([(d["id"], d["kind"]) for d in left], [("a", "video")])


class AtomicJsonWrite(_TmpDataDir):
    def test_failed_write_keeps_the_old_file(self):
        p = os.path.join(self._tmp, "x.json")
        youfish._atomic_write_json(p, {"a": 1})
        with self.assertRaises(Exception):
            youfish._atomic_write_json(p, {"bad": object()})     # not serialisable
        with open(p) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertEqual(sorted(os.listdir(self._tmp)), ["x.json"])   # no temp file left behind


class ProxyAllowList(unittest.TestCase):
    """Invariant 1's SSRF guard: the proxy only refetches https Google media hosts."""
    def test_allowed(self):
        for u in ("https://rr1---sn-x.googlevideo.com/videoplayback?a=1",
                  "https://i.ytimg.com/vi/x/hq.jpg", "https://www.youtube.com/x"):
            self.assertTrue(youfish._proxy_url_ok(u), u)

    def test_refused(self):
        for u in ("http://rr1.googlevideo.com/x", "https://127.0.0.1/x", "file:///etc/passwd",
                  "https://evil.com/googlevideo.com", "https://googlevideo.com.evil.com/x",
                  "https://evilgooglevideo.com/x", ""):
            self.assertFalse(youfish._proxy_url_ok(u), u)


class MediaProxyEndToEnd(_TmpDataDir):
    """A real request through the localhost proxy: playback's own path minus the network. The
    fake googlevideo serves known bytes; the proxy must hand back exactly the asked range."""
    DATA = bytes(range(256)) * 40          # 10240 bytes

    def setUp(self):
        super().setUp()
        self._saved = {n: getattr(youfish, n) for n in ("_force_ipv4", "_ytdlp_path", "_DIRECT_CHUNK")}
        self._urlopen = youfish.urllib.request.urlopen
        youfish._force_ipv4 = lambda: None
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"
        youfish._DIRECT_CHUNK = 4096
        data = self.DATA

        def fake(req, timeout=None):
            a, b = map(int, re.match(r"bytes=(\d+)-(\d+)", req.headers["Range"]).groups())
            if a >= len(data):
                raise urllib.error.HTTPError(req.full_url, 416, "range", {}, None)
            return _FakeHttpResp(206, data[a:b + 1], {"Content-Range": "bytes %d-%d/%d" % (
                a, min(b, len(data) - 1), len(data))})
        youfish.urllib.request.urlopen = fake
        youfish._ensure_proxy()
        self._sdir = youfish._STREAM_DIR
        youfish._STREAM_DIR = self._tmp

    def tearDown(self):
        youfish.release_playback("pv", [])
        youfish.urllib.request.urlopen = self._urlopen
        youfish._STREAM_DIR = self._sdir
        for n, v in self._saved.items():
            setattr(youfish, n, v)
        super().tearDown()

    def _get(self, url, rng=None):
        import http.client
        u = urllib.parse.urlsplit(url)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.request("GET", u.path + "?" + u.query, headers={"Range": rng} if rng else {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def test_range_requests_get_the_exact_bytes(self):
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
        url = youfish._proxied(src, "pv", "251", "UA/1")
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        st, h, body = self._get(url, "bytes=0-")
        self.assertEqual(st, 206)
        self.assertEqual(body, self.DATA)
        st, h, body = self._get(url, "bytes=5000-")          # a seek
        self.assertEqual((st, h["Content-Range"]), (206, "bytes 5000-10239/10240"))
        self.assertEqual(body, self.DATA[5000:])

    def test_seek_past_the_end_is_a_clean_416(self):
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d" % len(self.DATA)
        st, h, _ = self._get(youfish._proxied(src, "pv", "251"), "bytes=10240-")
        self.assertEqual((st, h["Content-Range"]), (416, "bytes */10240"))

    def test_non_google_target_is_refused(self):
        st, _, _ = self._get(youfish._proxied("https://example.com/x?clen=10", "pv", "251"))
        self.assertEqual(st, 403)

    def test_plays_without_yt_dlp_installed(self):
        """A fast-path resolve needs no yt-dlp, and neither does the direct fetch. The proxy used
        to refuse such a stream (503 "no stream capacity"), so nothing played on a phone where
        yt-dlp wasn't installed or couldn't run (the first 32-bit report)."""
        youfish._ytdlp_path = lambda: None
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
        st, _, body = self._get(youfish._proxied(src, "pv", "251", "UA/1"), "bytes=0-")
        self.assertEqual((st, body), (206, self.DATA))

    def test_byte0_refusal_without_yt_dlp_retries_direct(self):
        """A stream refused at byte 0 flips to the yt-dlp child, unless there's no yt-dlp: then
        the direct fetch tries again (it used to crash the reader and fail the stream)."""
        youfish._ytdlp_path = lambda: None
        serve, calls = youfish.urllib.request.urlopen, []

        def once_refused(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
            return serve(req, timeout)
        youfish.urllib.request.urlopen = once_refused
        src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
        st, _, body = self._get(youfish._proxied(src, "pv", "251", "UA/1"), "bytes=0-")
        self.assertEqual((st, body), (206, self.DATA))
        self.assertGreater(len(calls), 1)

    def test_refusal_says_why(self):
        free = youfish._free_bytes
        youfish._free_bytes = lambda path: 120 << 20
        try:
            src = "https://rr1.googlevideo.com/videoplayback?clen=%d&itag=251" % len(self.DATA)
            u = urllib.parse.urlsplit(youfish._proxied(src, "pv", "251"))
            import http.client
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
            c.request("GET", u.path + "?" + u.query)
            r = c.getresponse()
            r.read()
            c.close()
        finally:
            youfish._free_bytes = free
        self.assertEqual(r.status, 503)
        self.assertIn("not enough free storage", r.reason)
        self.assertIn("120 MB free", r.reason)



class ChannelNoVideosTab(unittest.TestCase):
    """A channel without uploads: the page opens its playlists from a machine field, not by
    matching yt-dlp's wording."""
    def setUp(self):
        self._saved = {n: getattr(youfish, n) for n in
                       ("_channel_videos_innertube", "_ytdlp_path", "_ytdlp_run")}
        youfish._channel_videos_innertube = lambda *a, **k: None
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(youfish, n, v)

    def _fail(self, stderr):
        youfish._ytdlp_run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        return youfish.channel_videos("UCx")

    def test_no_videos_tab_is_flagged(self):
        r = self._fail("ERROR: [youtube:tab] UCx: This channel does not have a videos tab")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_videos_tab")
        self.assertIn("videos tab", r["error"])

    def test_other_errors_carry_no_reason(self):
        r = self._fail("ERROR: [youtube:tab] UCx: HTTP Error 404: Not Found")
        self.assertFalse(r["ok"])
        self.assertNotIn("reason", r)


# --- Gap closers: functions no test ran before (python3 tests/coverage_report.py) ------------ #

class _InlineThreading:
    """Stands in for a module's `threading`: Thread/Timer are recorded and, when `run`, their target
    runs inline at start(); `fail` makes start() raise like a thread/FD-exhausted process."""
    def __init__(self, run=True, fail=False):
        self.run, self.fail, self.made = run, fail, []

    def __getattr__(self, name):
        return getattr(_threading, name)

    def Thread(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        return self._make(name, target, args, kwargs, None)

    def Timer(self, interval, function, args=None, kwargs=None):
        return self._make(None, function, args or (), kwargs, interval)

    def _make(self, name, target, args, kwargs, interval):
        rec = types.SimpleNamespace(name=name, target=target, interval=interval, daemon=None)

        def start():
            if self.fail:
                raise RuntimeError("can't start new thread")
            if self.run:
                target(*args, **(kwargs or {}))
        rec.start = start
        self.made.append(rec)
        return rec


class _Download(io.BytesIO):
    """A download response for _https_open fakes: .headers, read(n), a context manager. `fail`
    is raised by the read after the first one (a connection dropping mid-download)."""
    def __init__(self, data, fail=None):
        io.BytesIO.__init__(self, data)
        self.headers = {"Content-Length": str(len(data))}
        self._fail = fail

    def read(self, n=-1):
        if self._fail is not None and self.tell() > 0:
            raise self._fail
        return io.BytesIO.read(self, n)


def _script(path, body):
    """A fake binary: a small executable shell script."""
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


class _Restore(unittest.TestCase):
    """Base: SAVE lists youfish attributes put back after each test; also restores pyotherside,
    urllib's urlopen and subprocess.run, which several of these tests replace."""
    SAVE = ()

    def setUp(self):
        self._saved = {n: getattr(youfish, n) for n in self.SAVE}
        self._urlopen = urllib.request.urlopen
        self._run = youfish.subprocess.run
        self.sent = []
        self._prev_po = sys.modules.get("pyotherside")
        sys.modules["pyotherside"] = types.SimpleNamespace(send=lambda *a: self.sent.append(a))
        self._tmp = tempfile.mkdtemp(prefix="gap-")

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(youfish, n, v)
        urllib.request.urlopen = self._urlopen
        youfish.subprocess.run = self._run
        if self._prev_po is not None:
            sys.modules["pyotherside"] = self._prev_po
        else:
            sys.modules.pop("pyotherside", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def tmp_data(self):
        """Point every store at the throwaway dir."""
        youfish._data_dir = lambda: self._tmp


class FfmpegInstallIntegrity(_Restore):
    """install_ffmpeg: the .md5 and pinned SHA-256 gates. A mismatch never replaces a working ffmpeg,
    an archive path can't escape bin/, nothing staged is left behind, and every outcome reaches the
    UI as one ffmpeg_install_done event."""
    SAVE = ("_data_dir", "_force_ipv4", "_https_open", "_FFMPEG_SHA256", "threading")
    GOOD = "echo 'ffmpeg version 6.0-static https://johnvansickle.com/ffmpeg/'\n"

    def setUp(self):
        super().setUp()
        self.tmp_data()
        youfish._force_ipv4 = lambda: None
        youfish.threading = _InlineThreading()
        self.bin = os.path.join(self._tmp, "bin")
        self.served = {}

        def https_open(url, ctx, timeout=60):
            v = self.served[url]
            if isinstance(v, Exception):
                raise v
            return v if isinstance(v, _Download) else _Download(v)
        youfish._https_open = https_open

    @staticmethod
    def _sha(text):
        return hashlib.sha256(("#!/bin/sh\n" + text).encode()).hexdigest()

    def _serve(self, members, md5=None, fail=None):
        """Serve a .tar.xz of {arcname: script body} (+ its .md5; md5=b"" serves an empty one)."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            for name, text in members.items():
                data = ("#!/bin/sh\n" + text).encode()
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(data), 0o755
                tf.addfile(ti, io.BytesIO(data))
        blob = buf.getvalue()
        self.served[youfish._FFMPEG_URL] = _Download(blob, fail) if fail else blob
        self.served[youfish._FFMPEG_MD5_URL] = (
            (hashlib.md5(blob).hexdigest().upper() + "  ffmpeg-release-arm64-static.tar.xz\n").encode()
            if md5 is None else md5)

    def _done(self):
        done = [e for e in self.sent if e[0] == "ffmpeg_install_done"]
        self.assertEqual(len(done), 1, self.sent)
        return done[0]

    def test_pinned_build_installs_verified(self):
        self._serve({"ffmpeg-7.0-arm64-static/ffmpeg": self.GOOD,
                     "ffmpeg-7.0-arm64-static/ffprobe": "exit 0\n"})
        youfish._FFMPEG_SHA256 = self._sha(self.GOOD).upper()
        self.assertEqual(youfish.install_ffmpeg(), {"ok": True})
        self.assertEqual(self._done(), ("ffmpeg_install_done", True,
                                        "Installed ffmpeg 6.0-static (SHA-256 verified — pinned build)",
                                        "6.0-static"))
        self.assertTrue(any(e[0] == "ffmpeg_install_progress" for e in self.sent))
        self.assertEqual(sorted(os.listdir(self.bin)), ["ffmpeg", "ffprobe"])   # nothing staged left
        self.assertEqual(youfish._sha256_file(os.path.join(self.bin, "ffmpeg")), self._sha(self.GOOD))
        self.assertEqual(youfish.ffmpeg_version(), "6.0-static")

    def test_md5_mismatch_installs_nothing(self):
        self._serve({"x/ffmpeg": self.GOOD}, md5=b"0" * 32)
        youfish._FFMPEG_SHA256 = self._sha(self.GOOD)
        youfish.install_ffmpeg()
        self.assertEqual(self._done(), ("ffmpeg_install_done", False,
                                        "Checksum mismatch — download discarded, nothing installed", ""))
        self.assertEqual(os.listdir(self.bin), [])

    def test_pin_mismatch_keeps_the_working_ffmpeg(self):
        os.makedirs(self.bin)
        old = _script(os.path.join(self.bin, "ffmpeg"), "echo 'ffmpeg version 5.1'\n")
        self._serve({"x/ffmpeg": self.GOOD, "x/ffprobe": "exit 0\n"})
        youfish._FFMPEG_SHA256 = "0" * 64
        youfish.install_ffmpeg()
        ev = self._done()
        self.assertEqual(ev[1:2] + ev[3:], (False, "", True))   # 5th arg: offer "install unverified"
        self.assertIn("NOT installed", ev[2])
        self.assertEqual(os.listdir(self.bin), ["ffmpeg"])      # staged .new files discarded
        self.assertEqual(youfish._sha256_file(old), self._sha("echo 'ffmpeg version 5.1'\n"))

    def test_user_accepts_unpinned_build(self):
        self._serve({"x/ffmpeg": self.GOOD})
        youfish._FFMPEG_SHA256 = "0" * 64
        youfish.update_ffmpeg(allow_unpinned=True)
        self.assertEqual(self._done()[1:3], (True, "Installed ffmpeg 6.0-static "
                                                   "(unverified build — accepted by you)"))

    def test_no_pin_and_no_md5_says_unverified(self):
        youfish._FFMPEG_SHA256 = ""
        self._serve({"x/ffmpeg": self.GOOD}, md5=b"")
        youfish.install_ffmpeg()
        self.assertEqual(self._done()[1:3], (True, "Installed ffmpeg 6.0-static "
                                                   "(checksum unavailable, not verified)"))

    def test_archive_without_ffmpeg(self):
        self._serve({"x/ffprobe": "exit 0\n", "x/README": "hi\n"})
        youfish.install_ffmpeg()
        self.assertEqual(self._done()[1:3], (False, "Archive didn't contain an ffmpeg binary"))
        self.assertEqual(os.listdir(self.bin), [])

    def test_archive_paths_cannot_escape_bin(self):
        self._serve({"../../../escape/ffmpeg": self.GOOD, "../../etc/README": "x\n"})
        youfish._FFMPEG_SHA256 = self._sha(self.GOOD)
        youfish.install_ffmpeg()
        self.assertTrue(self._done()[1])
        self.assertEqual(os.listdir(self.bin), ["ffmpeg"])     # basename only, README ignored
        self.assertEqual(sorted(os.listdir(self._tmp)), ["bin"])

    def test_binary_that_wont_run(self):
        self._serve({"x/ffmpeg": "exit 1\n"})
        youfish._FFMPEG_SHA256 = self._sha("exit 1\n")
        youfish.install_ffmpeg()
        self.assertEqual(self._done()[1:3], (False, "Unpacked, but the binary won't run here"))

    def test_dropped_download_leaves_no_part_file(self):
        self._serve({"x/ffmpeg": self.GOOD}, fail=ConnectionResetError("connection reset"))
        youfish.install_ffmpeg()
        self.assertEqual(self._done(), ("ffmpeg_install_done", False, "connection reset", ""))
        self.assertEqual(os.listdir(self.bin), [])

    def test_ffmpeg_version_edges(self):
        saved = youfish._ffmpeg_path
        try:
            youfish._ffmpeg_path = lambda: None
            self.assertEqual(youfish.ffmpeg_version(), "")
            youfish._ffmpeg_path = lambda: _script(os.path.join(self._tmp, "ff"), "echo odd build\n")
            self.assertEqual(youfish.ffmpeg_version(), "odd build")
        finally:
            youfish._ffmpeg_path = saved


class YtdlpInstallIntegrity(_Restore):
    """_install_ytdlp_sync / install_ytdlp: the release's SHA2-256SUMS gate, a binary that won't run
    isn't reported installed, a failure keeps the old binary and leaves no .part, and every
    download refuses plain HTTP, also after a redirect."""
    SAVE = ("_data_dir", "_force_ipv4", "_https_open", "_YTDLP_ASSET", "_ARM32", "threading",
            "_ytdlp_path")
    GOOD = "echo 2026.10.01\n"

    def setUp(self):
        super().setUp()
        self.tmp_data()
        youfish._force_ipv4 = lambda: None
        youfish._ARM32 = False
        youfish._YTDLP_ASSET = "yt-dlp_linux_aarch64"
        youfish._ytdlp_path = youfish._managed_ytdlp
        youfish.threading = _InlineThreading()
        self.served = {}

        def https_open(url, ctx, timeout=60):
            v = self.served[url]
            return v if isinstance(v, _Download) else _Download(v)
        youfish._https_open = https_open
        self.dest = youfish._managed_ytdlp()

    def _serve(self, body, listed_sha=None, fail=None):
        base = youfish._ytdlp_release_base()
        blob = ("#!/bin/sh\n" + body).encode()
        sha = hashlib.sha256(blob).hexdigest() if listed_sha is None else listed_sha
        sums = "%s  yt-dlp\n" % ("1" * 64)
        if sha:
            sums += "%s  yt-dlp_linux_aarch64\n" % sha.upper()
        self.served[base + "SHA2-256SUMS"] = sums.encode()
        self.served[base + "yt-dlp_linux_aarch64"] = _Download(blob, fail) if fail else blob

    def _keep_old(self):
        os.makedirs(os.path.dirname(self.dest), exist_ok=True)
        _script(self.dest, "echo 2025.01.01\n")

    def _assert_old_kept(self):
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.dest))), ["yt-dlp"])   # no .part
        self.assertEqual(youfish.ytdlp_version(fresh=True), "2025.01.01")

    def test_verified_install(self):
        self._serve(self.GOOD)
        pct = []
        self.assertEqual(youfish._install_ytdlp_sync(pct.append),
                         (True, "Installed yt-dlp 2026.10.01", "2026.10.01"))
        self.assertEqual(pct[-1], 100.0)
        self.assertTrue(os.access(self.dest, os.X_OK))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_checksum_mismatch_keeps_the_old_binary(self):
        self._keep_old()
        self._serve(self.GOOD, listed_sha="2" * 64)
        self.assertEqual(youfish._install_ytdlp_sync(),
                         (False, "Checksum mismatch — download discarded, nothing installed", ""))
        self._assert_old_kept()

    def test_unlisted_asset_says_unverified(self):
        self._serve(self.GOOD, listed_sha="")
        ok, msg, _ = youfish._install_ytdlp_sync()
        self.assertTrue(ok)
        self.assertEqual(msg, "Installed yt-dlp 2026.10.01 (checksum unavailable, not verified)")

    def test_binary_that_wont_run_is_not_installed_ok(self):
        self._serve("exit 1\n")
        ok, msg, ver = youfish._install_ytdlp_sync()
        self.assertEqual((ok, ver), (False, ""))
        self.assertTrue(msg.startswith("Downloaded + checksum OK, but the binary won't run here"))

    def test_dropped_download_keeps_the_old_binary(self):
        self._keep_old()
        self._serve(self.GOOD, fail=ConnectionResetError("connection reset"))
        self.assertEqual(youfish._install_ytdlp_sync(), (False, "connection reset", ""))
        self._assert_old_kept()

    def test_install_ytdlp_reports_to_the_ui(self):
        self._serve(self.GOOD)
        self.assertEqual(youfish.install_ytdlp(), {"ok": True})
        self.assertEqual(self.sent[0][0], "ytdlp_install_progress")
        self.assertEqual(self.sent[-1], ("ytdlp_install_done", True, "Installed yt-dlp 2026.10.01",
                                         "2026.10.01"))

    def test_https_open_refuses_plain_http(self):
        https_open = self._saved["_https_open"]
        opened = []
        urllib.request.urlopen = lambda req, timeout=0, context=None: opened.append(req)
        with self.assertRaises(ValueError):
            https_open("http://github.com/x", None)
        self.assertEqual(opened, [])
        closed = []
        resp = types.SimpleNamespace(geturl=lambda: "http://evil.example/x",
                                     close=lambda: closed.append(1))
        urllib.request.urlopen = lambda req, timeout=0, context=None: resp
        with self.assertRaises(ValueError):
            https_open("https://github.com/x", None)
        self.assertEqual(closed, [1])
        resp.geturl = lambda: "https://objects.githubusercontent.com/x"
        self.assertIs(https_open("https://github.com/x", None), resp)

    def test_latest_tag(self):
        urllib.request.urlopen = lambda req, timeout=0, context=None: _Download(
            b'{"tag_name": " 2026.10.01 "}')
        self.assertEqual(youfish._ytdlp_latest_tag(), "2026.10.01")

        def down(req, timeout=0, context=None):
            raise urllib.error.URLError("offline")
        urllib.request.urlopen = down
        self.assertEqual(youfish._ytdlp_latest_tag(), "")

    def test_zipapp_lives_in_bin(self):
        self.assertEqual(youfish._ytdlp_zipapp_path(), os.path.join(self._tmp, "bin", "yt-dlp.zip"))


class YtdlpUpdate(_Restore):
    """ytdlp_update: the self-updater runs on the chosen channel; its output, a timeout and a missing
    binary all come back as a result the Providers page can show; 32-bit ARM downloads instead."""
    SAVE = ("_ytdlp_path", "_ARM32", "_managed_ytdlp", "ytdlp_version", "get_settings",
            "_ytdlp_update_by_download")

    def setUp(self):
        super().setUp()
        youfish._ytdlp_path = lambda: "/b/yt-dlp"
        youfish._managed_ytdlp = lambda: "/b/yt-dlp"
        youfish._ARM32 = False
        youfish.ytdlp_version = lambda fresh=False: "2026.10.01"
        youfish.get_settings = lambda: {"ytdlp_channel": "nightly"}
        self.argv = []

    def _proc(self, rc, out="", err=""):
        def run(cmd, **kw):
            self.argv.append(cmd)
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        youfish.subprocess.run = run

    def test_no_binary(self):
        youfish._ytdlp_path = lambda: None
        self.assertEqual(youfish.ytdlp_update(), {"ok": False, "error": "yt-dlp not found",
                                                  "version": ""})

    def test_updates_on_the_chosen_channel(self):
        self._proc(0, "Updated yt-dlp to nightly@2026.10.01\n")
        r = youfish.ytdlp_update()
        self.assertEqual(r, {"ok": True, "output": "Updated yt-dlp to nightly@2026.10.01",
                             "version": "2026.10.01", "channel": "nightly"})
        self.assertEqual(self.argv[0][0], "/b/yt-dlp")
        self.assertEqual(self.argv[0][-2:], ["--update-to", "nightly@latest"])

    def test_failure_output_is_kept_short(self):
        self._proc(1, "", "x" * 1000 + "ERROR: can't update a pip install")
        r = youfish.ytdlp_update()
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["output"]), 400)
        self.assertTrue(r["output"].endswith("can't update a pip install"))
        self._proc(1)
        self.assertEqual(youfish.ytdlp_update()["output"], "yt-dlp reported nothing")

    def test_timeout_and_errors(self):
        def slow(cmd, **kw):
            raise youfish.subprocess.TimeoutExpired(cmd, 300)
        youfish.subprocess.run = slow
        self.assertEqual(youfish.ytdlp_update(), {"ok": False, "error": "update timed out",
                                                  "version": "2026.10.01"})

        def gone(cmd, **kw):
            raise OSError("exec format error")
        youfish.subprocess.run = gone
        self.assertEqual(youfish.ytdlp_update()["error"], "exec format error")

    def test_arm32_downloads_instead_of_self_updating(self):
        youfish._ARM32 = True
        youfish._ytdlp_update_by_download = lambda: {"ok": True, "by": "download"}
        self._proc(0)
        self.assertEqual(youfish.ytdlp_update(), {"ok": True, "by": "download"})
        self.assertEqual(self.argv, [])
        youfish._ytdlp_path = lambda: "/usr/bin/yt-dlp"      # a system yt-dlp updates itself
        self.assertTrue(youfish.ytdlp_update()["ok"])
        self.assertEqual(len(self.argv), 1)


class PotSandboxFlags(_Restore):
    """_pot_server_flags: the Deno sidecar runs default-deny. No write, no subprocess, reads jailed
    to its own tree, and FFI granted only to node-canvas's one addon, and only when asked for."""
    SAVE = ("_data_dir", "_deno_path", "get_settings")

    def setUp(self):
        super().setUp()
        self.tmp_data()
        youfish._deno_path = lambda: "/d/deno"
        self.settings = {}
        youfish.get_settings = lambda: self.settings
        self.srv = youfish._pot_server_dir()

    def _addon(self):
        """Deno's npm layout: the package under node_modules/.deno/, linked as node_modules/canvas."""
        nm = os.path.join(self.srv, "node_modules")
        pkg = os.path.join(nm, ".deno", "canvas@2.11.2", "node_modules", "canvas")
        os.makedirs(os.path.join(pkg, "build", "Release"))
        open(os.path.join(pkg, "build", "Release", "canvas.node"), "w").close()
        os.symlink(os.path.join(".deno", "canvas@2.11.2", "node_modules", "canvas"),
                   os.path.join(nm, "canvas"))
        return os.path.join(nm, "canvas", "build", "Release", "canvas.node")

    def _assert_no_broad_grant(self, flags):
        for bad in ("--allow-write", "--allow-run", "--allow-all", "-A", "--allow-ffi",
                    "--allow-read", "--allow-sys"):
            self.assertNotIn(bad, flags)          # bare = unrestricted

    def test_default_deny(self):
        flags = youfish._pot_server_flags()
        self.assertEqual(flags, [
            "/d/deno", "run", "--allow-net", "--allow-env", "--allow-read=" + self.srv,
            "--deny-write", "--deny-run", "--v8-flags=--max-old-space-size=8192", "--deny-ffi",
            os.path.join(self.srv, "src", "main.ts")])
        self.assertTrue(self.srv.startswith(self._tmp))
        self._assert_no_broad_grant(flags)

    def test_ffi_wanted_but_no_addon_stays_denied(self):
        self.settings["pot_needs_ffi"] = True
        self.assertEqual(youfish._canvas_node_path(), "")
        self.assertIn("--deny-ffi", youfish._pot_server_flags())

    def test_addon_is_ignored_unless_asked_for(self):
        self._addon()
        self.assertIn("--deny-ffi", youfish._pot_server_flags())

    def test_ffi_only_to_the_canvas_addon(self):
        addon = self._addon()
        self.settings["pot_needs_ffi"] = True
        self.assertEqual(youfish._canvas_node_path(), addon)
        flags = youfish._pot_server_flags()
        self.assertIn("--allow-ffi=" + addon, flags)
        self.assertNotIn("--deny-ffi", flags)
        self._assert_no_broad_grant(flags)


class PotServerHelpers(_Restore):
    """The PO-token sidecar's small helpers: the WebGPU crash shim, the server log tail and its
    once-per-launch rotation, the token label in debug logs, stop/enable, deno/git lookup and the
    explicit provider update."""
    SAVE = ("_data_dir", "_pot_log_rotated", "_pot_proc", "_ensure_pot_server", "stop_pot_server",
            "pot_status", "_force_ipv4", "install_pot_provider", "_deno_path")

    def setUp(self):
        super().setUp()
        self.tmp_data()
        youfish._force_ipv4 = lambda: None
        self.pot = youfish._pot_dir()
        self._which = youfish.shutil.which

    def tearDown(self):
        youfish.shutil.which = self._which
        super().tearDown()

    def test_webgpu_shim_is_prepended_once(self):
        main = os.path.join(youfish._pot_server_dir(), "src", "main.ts")
        youfish._pot_disable_webgpu()                      # no clone yet: a no-op, not a crash
        self.assertFalse(os.path.exists(main))
        os.makedirs(os.path.dirname(main))
        with open(main, "w") as f:
            f.write("serve();\n")
        youfish._pot_disable_webgpu()
        youfish._pot_disable_webgpu()
        with open(main) as f:
            src = f.read()
        self.assertTrue(src.startswith("/* youfish:no-webgpu */"))
        self.assertEqual(src.count("/* youfish:no-webgpu */"), 1)
        self.assertIn("requestAdapter", src)
        self.assertTrue(src.endswith("serve();\n"))

    def test_log_tail(self):
        self.assertEqual(youfish._pot_server_log_tail(), "")
        os.makedirs(self.pot)
        with open(os.path.join(self.pot, "server.log"), "w") as f:
            f.write("a\n\n  \nb\nc\n")
        self.assertEqual(youfish._pot_server_log_tail(2), "b\nc")
        self.assertEqual(youfish._pot_server_log_tail(0), "c")
        self.assertEqual(youfish._pot_server_log_tail(), "a\nb\nc")

    def test_log_rotates_once_per_launch(self):
        youfish._pot_log_rotated = False
        os.makedirs(self.pot)
        log = os.path.join(self.pot, "server.log")
        with open(log, "w") as f:
            f.write("last session\n")
        youfish._pot_rotate_log()
        with open(log + ".prev") as f:
            self.assertEqual(f.read(), "last session\n")
        self.assertFalse(os.path.exists(log))
        with open(log, "w") as f:
            f.write("this session\n")
        youfish._pot_rotate_log()                          # second call in the same process: no-op
        self.assertTrue(os.path.exists(log))

    def test_pot_label_never_leaks_the_token(self):
        self.assertEqual(youfish._pot_of(""), "no-url")
        self.assertEqual(youfish._pot_of("https://r1.googlevideo.com/videoplayback?c=WEB"), "no-pot")
        self.assertEqual(youfish._pot_of("https://r1.googlevideo.com/videoplayback?pot=SECRETTOKEN123"),
                         "len=14 pfx=SECRETTO")

    def test_stop_pot_server(self):
        calls = []

        def proc(alive=True, wait_hangs=False):
            def wait(timeout=None):
                calls.append("wait")
                if wait_hangs:
                    raise youfish.subprocess.TimeoutExpired("deno", timeout)
            return types.SimpleNamespace(poll=lambda: None if alive else 0, wait=wait,
                                         terminate=lambda: calls.append("term"),
                                         kill=lambda: calls.append("kill"))
        youfish._pot_proc = None
        youfish.stop_pot_server()                          # nothing running: fine
        youfish._pot_proc = proc()
        youfish.stop_pot_server()
        self.assertEqual((calls, youfish._pot_proc), (["term", "wait"], None))
        del calls[:]
        youfish._pot_proc = proc(wait_hangs=True)
        youfish.stop_pot_server()
        self.assertEqual(calls, ["term", "wait", "kill"])
        del calls[:]
        youfish._pot_proc = proc(alive=False)
        youfish.stop_pot_server()
        self.assertEqual(calls, [])

    def test_set_pot_enabled(self):
        calls = []
        youfish._ensure_pot_server = lambda wait=25.0: calls.append("start")
        youfish.stop_pot_server = lambda: calls.append("stop")
        youfish.pot_status = lambda: {"status": 1}
        self.assertEqual(youfish.set_pot_enabled(True), {"status": 1})
        self.assertIs(youfish.get_settings()["pot_provider"], True)
        youfish.set_pot_enabled(False)
        self.assertIs(youfish.get_settings()["pot_provider"], False)
        self.assertEqual(calls, ["start", "stop"])

    def test_update_pot_provider(self):
        installs = []
        youfish.install_pot_provider = lambda tag=None, persist_tag=False: (
            installs.append((tag, persist_tag)) or {"ok": True})

        def down(req, timeout=0, context=None):
            raise urllib.error.URLError("offline")
        urllib.request.urlopen = down
        self.assertEqual(youfish._pot_latest_tag(), "")
        self.assertEqual(youfish.update_pot_provider(), {"ok": False})
        self.assertEqual(self.sent[-1][:2], ("pot_install_done", False))
        self.assertEqual(installs, [])
        urllib.request.urlopen = lambda req, timeout=0, context=None: _Download(b'{"tag_name":"1.4.0"}')
        self.assertEqual(youfish.update_pot_provider(), {"ok": True})
        self.assertEqual(installs, [("1.4.0", True)])     # the tag is persisted only by the install

    def test_deno_version(self):
        youfish._deno_path = lambda: None
        self.assertEqual(youfish.deno_version(), "")
        deno = _script(os.path.join(self._tmp, "deno"),
                       "echo 'deno 2.1.4 (stable, release, aarch64-unknown-linux-gnu)'\necho 'v8 13'\n")
        youfish._deno_path = lambda: deno
        self.assertEqual(youfish.deno_version(), "2.1.4")

    def test_git_path(self):
        youfish.shutil.which = lambda name: "/opt/bin/git" if name == "git" else None
        self.assertEqual(youfish._git_path(), "/opt/bin/git")
        youfish.shutil.which = lambda name: None
        self.assertIn(youfish._git_path(), (None, "/usr/bin/git", "/usr/local/bin/git",
                                            os.path.expanduser("~/.local/bin/git")))


class PotPortAndPing(unittest.TestCase):
    """_pot_ready_on_port / _pot_http_ping against real localhost sockets: a bound port is
    'running', only an HTTP answer (even an error status) is 'responding', a wedged server that
    accepts but never answers is not, and /ping's version is read."""

    def setUp(self):
        self._port = youfish._POT_PORT

    def tearDown(self):
        youfish._POT_PORT = self._port

    def _serve(self, status, body):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(h):
                h.send_response(status)
                h.send_header("Content-Length", str(len(body)))
                h.end_headers()
                h.wfile.write(body)

            def log_message(h, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        _threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        youfish._POT_PORT = srv.server_address[1]

    def test_answering_server(self):
        self._serve(200, b'{"version": "1.3.2"}')
        self.assertTrue(youfish._pot_ready_on_port())
        self.assertEqual(youfish._pot_http_ping(), {"ok": True, "version": "1.3.2"})

    def test_error_status_still_counts_as_alive(self):
        self._serve(500, b"boom")
        self.assertEqual(youfish._pot_http_ping(), {"ok": True, "version": ""})

    def test_non_json_body(self):
        self._serve(200, b"pong")
        self.assertEqual(youfish._pot_http_ping(), {"ok": True, "version": ""})

    def test_wedged_and_closed_ports(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)                                   # accepts, never answers
        self.addCleanup(s.close)
        youfish._POT_PORT = s.getsockname()[1]
        self.assertTrue(youfish._pot_ready_on_port())
        self.assertEqual(youfish._pot_http_ping(timeout=0.3), {"ok": False, "version": ""})
        s.close()
        self.assertFalse(youfish._pot_ready_on_port())
        self.assertEqual(youfish._pot_http_ping(), {"ok": False, "version": ""})


class PotDiagnostics(_Restore):
    """pot_diagnostics, the copy-paste health report: its verdict, a crashed sidecar's exit code,
    a failed restart and a plugin yt-dlp doesn't load must all show in it."""
    SAVE = ("_pot_installed", "get_settings", "_pot_ready_on_port", "_ensure_pot_server",
            "_deno_path", "_git_path", "_ytdlp_path", "_ffmpeg_path", "_pot_http_ping",
            "_pot_plugin_probe", "deno_version", "ytdlp_version", "ffmpeg_version",
            "_it_fallback_report", "_pot_server_log_tail", "_pot_proc", "_pot_last_error",
            "_pot_effective_tag", "_data_dir")

    def setUp(self):
        super().setUp()
        self.port, self.restarts, self.installed = [True], [], [True]
        self.settings = {"pot_provider": True}
        self.probe = {"checked": True, "loaded": True, "detail": "/repo", "js_runtimes": "deno 2.1",
                      "bgutil_lines": ""}
        youfish._pot_installed = lambda: self.installed[0]
        youfish.get_settings = lambda: self.settings
        youfish._pot_ready_on_port = lambda timeout=0.25: self.port[0]
        youfish._ensure_pot_server = lambda wait=25.0: self.restarts.append(1) and False
        youfish._deno_path = lambda: "/d/deno"
        youfish._git_path = lambda: "/usr/bin/git"
        youfish._ytdlp_path = lambda: "/d/yt-dlp"
        youfish._ffmpeg_path = lambda: None
        youfish._pot_http_ping = lambda timeout=1.5: {"ok": True, "version": "1.3.2"}
        youfish._pot_plugin_probe = lambda timeout=30: self.probe
        youfish.deno_version = lambda: "2.1.4"
        youfish.ytdlp_version = lambda fresh=False: "2026.10.01"
        youfish.ffmpeg_version = lambda: ""
        youfish._it_fallback_report = lambda: ["InnerTube fast path: player client VISIONOS"]
        youfish._pot_server_log_tail = lambda n=30: "Started POT server"
        youfish._pot_proc = None
        youfish._pot_last_error = ""
        youfish._pot_effective_tag = lambda: "1.3.2"
        youfish._data_dir = lambda: "/data"

    def test_working(self):
        d = youfish.pot_diagnostics()
        r = d["report"]
        for line in ("Deno   : /d/deno  (v2.1.4)", "yt-dlp : /d/yt-dlp  (2026.10.01)",
                     "ffmpeg : not installed", "InnerTube fast path: player client VISIONOS",
                     "provider installed : yes (1.3.2)", "answering HTTP     : yes (server v1.3.2)",
                     "plugin in yt-dlp   : loads (/repo)", "verdict            : working",
                     "--- server.log (last lines) ---\nStarted POT server",
                     "server process     : not started in this app session"):
            self.assertIn(line, r)
        self.assertEqual(self.restarts, [])
        self.assertEqual((d["responding"], d["plugin_loaded"], d["ffmpeg"], d["prev_exit"]),
                         (True, True, False, None))

    def test_unloaded_plugin_is_not_working(self):
        self.probe.update(loaded=False, detail="none")
        r = youfish.pot_diagnostics()["report"]
        self.assertIn("plugin in yt-dlp   : NOT LOADED — none", r)
        self.assertIn("verdict            : NOT working", r)

    def test_crashed_sidecar_and_failed_restart(self):
        self.port[0] = False
        youfish._pot_proc = types.SimpleNamespace(poll=lambda: -11)
        youfish._pot_last_error = "NotCapable: net"
        d = youfish.pot_diagnostics()
        self.assertEqual(self.restarts, [1])
        self.assertEqual(d["prev_exit"], -11)
        self.assertIn("STARTED, then EXITED (code -11)", d["report"])
        self.assertIn("restart attempt    : FAILED — NotCapable: net", d["report"])
        self.assertIn("verdict            : NOT working", d["report"])
        self.assertIn("last error         : NotCapable: net", d["report"])

    def test_switched_off_and_not_set_up(self):
        self.settings.clear()
        self.port[0] = False
        self.assertIn("verdict            : installed but switched off",
                      youfish.pot_diagnostics()["report"])
        self.assertEqual(self.restarts, [])               # off: no restart attempt
        self.installed[0] = False
        youfish._pot_plugin_probe = lambda timeout=30: self.fail("probe without an install")
        d = youfish.pot_diagnostics()
        self.assertIn("verdict            : not set up", d["report"])
        self.assertIsNone(d["plugin_loaded"])


class PrewarmWiring(_Restore):
    """prewarm (the first py call at launch): arms the delayed yt-dlp import ONCE, mints the
    InnerTube visitor id off-thread, and starts the PO-token sidecar only when the provider is on;
    the delayed import and the debug spawn-tax probe behave."""
    SAVE = ("threading", "_ytdlp_warm_armed", "_autofetch_zipapp", "_pot_rotate_log", "_pot_active",
            "_ensure_pot_server", "_import_yt_dlp", "_DEBUG", "_spawn_tax_probe", "_it",
            "_force_ipv4", "_tlog", "_plog", "_ytdlp_path")

    def setUp(self):
        super().setUp()
        self.calls = []
        youfish.threading = _InlineThreading(run=False)
        youfish._ytdlp_warm_armed = False
        youfish._autofetch_zipapp = lambda: self.calls.append("autofetch")
        youfish._pot_rotate_log = lambda: self.calls.append("rotate")
        youfish._pot_active = lambda: False
        youfish._ensure_pot_server = lambda wait=25.0: None
        youfish._force_ipv4 = lambda: None
        self.logs = []
        youfish._tlog = youfish._plog = self.logs.append

    def test_import_is_armed_once(self):
        youfish.prewarm()
        youfish.prewarm()
        made = youfish.threading.made
        timers = [m for m in made if m.interval is not None]
        self.assertEqual(len(timers), 1)
        self.assertEqual((timers[0].interval, timers[0].target, timers[0].daemon),
                         (youfish._YTDLP_WARM_DELAY, youfish._delayed_ytdlp_warm, True))
        self.assertEqual([m.name for m in made if m.interval is None],
                         ["innertube-prewarm", "innertube-prewarm"])
        self.assertEqual(self.calls, ["autofetch", "rotate"] * 2)

    def test_sidecar_starts_only_when_active(self):
        youfish._pot_active = lambda: True
        youfish.prewarm()
        pot = [m for m in youfish.threading.made if m.name == "pot-prewarm"]
        self.assertEqual(len(pot), 1)
        self.assertIs(pot[0].target, youfish._ensure_pot_server)

    def test_delayed_warm(self):
        youfish._import_yt_dlp = lambda: self.calls.append("import")
        youfish._spawn_tax_probe = lambda: self.calls.append("tax")
        youfish._DEBUG = False
        youfish._delayed_ytdlp_warm()
        youfish._DEBUG = True
        youfish._delayed_ytdlp_warm()
        self.assertEqual(self.calls, ["import", "import", "tax"])

    def test_innertube_prewarm(self):
        youfish._it = lambda: None
        youfish._innertube_prewarm()                       # FinTune / no module: nothing to do
        minted = []
        youfish._it = lambda: types.SimpleNamespace(visitor_data=lambda: minted.append(1))
        youfish._innertube_prewarm()
        self.assertEqual(minted, [1])

        def offline():
            raise OSError("offline")
        youfish._it = lambda: types.SimpleNamespace(visitor_data=offline)
        youfish._innertube_prewarm()                       # a failure is logged, never raised
        self.assertTrue(any("prewarm failed" in m for m in self.logs))

    def test_spawn_tax_probe(self):
        youfish._DEBUG = False
        youfish._ytdlp_path = lambda: self.fail("probe ran without YOUFISH_DEBUG")
        youfish._spawn_tax_probe()
        youfish._DEBUG = True
        youfish._ytdlp_path = lambda: None
        youfish._spawn_tax_probe()
        self.assertIn("spawn_tax: yt-dlp not found", self.logs)
        ytdlp = _script(os.path.join(self._tmp, "yt-dlp"), "echo 2026.10.01\n")
        youfish._ytdlp_path = lambda: ytdlp
        youfish._spawn_tax_probe()
        self.assertTrue(self.logs[-1].startswith("spawn_tax "))
        self.assertTrue(self.logs[-1].endswith("yt-dlp 2026.10.01"))


@unittest.skipUnless(sys.platform.startswith("linux"), "prctl is Linux-only")
class PotDeathSignal(unittest.TestCase):
    """_set_pdeathsig really arms PR_SET_PDEATHSIG = SIGKILL (the Deno sidecar's preexec_fn, so it
    dies with the app). Checked on this process via PR_GET_PDEATHSIG, then put back."""

    def test_arms_sigkill(self):
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        before = ctypes.c_int()
        libc.prctl(2, ctypes.byref(before))               # PR_GET_PDEATHSIG
        try:
            youfish._set_pdeathsig()
            now = ctypes.c_int()
            libc.prctl(2, ctypes.byref(now))
            self.assertEqual(now.value, int(youfish.signal.SIGKILL))
        finally:
            libc.prctl(1, before.value)                   # PR_SET_PDEATHSIG back to what it was

class InnertubeModuleLoad(unittest.TestCase):
    """_it: imports innertube once, pins IPv4 BEFORE its pool can open a socket, and a missing
    module reads as None (yt-dlp only) without retrying the import on every call."""

    def setUp(self):
        self.state = dict(youfish._INNERTUBE)
        self._f, self._p = youfish._force_ipv4, youfish._plog
        youfish._INNERTUBE.update(mod=None, tried=False)
        self.forced = []
        youfish._force_ipv4 = lambda: self.forced.append(1)
        youfish._plog = lambda m: None

    def tearDown(self):
        youfish._INNERTUBE.clear()
        youfish._INNERTUBE.update(self.state)
        youfish._force_ipv4, youfish._plog = self._f, self._p
        sys.modules["innertube"] = innertube

    def test_loads_once_after_pinning_ipv4(self):
        self.assertIs(_REAL_IT(), innertube)
        self.assertIs(_REAL_IT(), innertube)
        self.assertEqual(self.forced, [1])

    def test_missing_module_is_none(self):
        sys.modules["innertube"] = None                   # import innertube -> ImportError
        self.assertIsNone(_REAL_IT())
        sys.modules["innertube"] = innertube
        self.assertIsNone(_REAL_IT())                     # not retried
        self.assertTrue(youfish._INNERTUBE["tried"])


class PrefetchResolve(_Restore):
    """prefetch_resolve: one background resolve per video (deduped), at most 2 at once (over the cap
    it is dropped, not queued), and a prefetch thread never starts the PO-token sidecar."""
    SAVE = ("_resolve_key", "_resolve_cache_get", "_resolve_and_cache", "_pot_active",
            "_pot_ready_on_port", "prewarm", "threading", "_prefetch_sema")

    def setUp(self):
        super().setUp()
        self._pending = set(youfish._prefetch_pending)
        youfish._prefetch_pending.clear()
        self.resolved = []
        youfish._resolve_key = lambda vid: "k:" + vid
        youfish._resolve_cache_get = lambda key: None
        youfish._resolve_and_cache = lambda vid, key, speculative=False: self.resolved.append(
            (vid, key, speculative))
        youfish._pot_active = lambda: False
        youfish.threading = _InlineThreading(run=False)
        youfish._prefetch_sema = _threading.BoundedSemaphore(2)

    def tearDown(self):
        youfish._prefetch_pending.clear()
        youfish._prefetch_pending.update(self._pending)
        super().tearDown()

    def _run_last(self):
        youfish.threading.made[-1].target()

    def test_empty_and_cached(self):
        self.assertEqual(youfish.prefetch_resolve(""), {"ok": True, "queued": False})
        youfish._resolve_cache_get = lambda key: {"payload": {}}
        self.assertEqual(youfish.prefetch_resolve("abc"),
                         {"ok": True, "queued": False, "cached": True})
        self.assertEqual(youfish.threading.made, [])

    def test_deduped_while_in_flight(self):
        self.assertEqual(youfish.prefetch_resolve("abc", speculative=True),
                         {"ok": True, "queued": True})
        self.assertEqual(youfish.prefetch_resolve("abc"),
                         {"ok": True, "queued": False, "inflight": True})
        self.assertEqual(len(youfish.threading.made), 1)
        self._run_last()
        self.assertEqual(self.resolved, [("abc", "k:abc", True)])
        self.assertEqual(youfish._prefetch_pending, set())
        self.assertTrue(youfish.prefetch_resolve("abc")["queued"])     # done: may run again

    def test_over_the_cap_is_dropped(self):
        youfish._prefetch_sema = _threading.BoundedSemaphore(1)
        youfish._prefetch_sema.acquire()
        youfish.prefetch_resolve("abc")
        self._run_last()
        self.assertEqual(self.resolved, [])
        self.assertEqual(youfish._prefetch_pending, set())

    def test_failed_resolve_frees_its_slot(self):
        def boom(*a, **k):
            raise RuntimeError("yt-dlp died")
        youfish._resolve_and_cache = boom
        youfish._prefetch_sema = _threading.BoundedSemaphore(1)
        youfish.prefetch_resolve("abc")
        self._run_last()
        self.assertTrue(youfish._prefetch_sema.acquire(blocking=False))
        self.assertEqual(youfish._prefetch_pending, set())

    def test_never_starts_the_sidecar_itself(self):
        youfish._pot_active = lambda: True
        youfish._pot_ready_on_port = lambda timeout=0.25: False
        warmed = []
        youfish.prewarm = lambda: warmed.append(1)
        youfish.prefetch_resolve("abc")
        self._run_last()
        self.assertEqual((warmed, self.resolved, youfish._prefetch_pending), ([1], [], set()))

    def test_thread_start_failure(self):
        youfish.threading = _InlineThreading(fail=True)
        self.assertEqual(youfish.prefetch_resolve("abc"),
                         {"ok": False, "queued": False, "error": "spawn failed"})
        self.assertEqual(youfish._prefetch_pending, set())


class ProxyReapIdle(unittest.TestCase):
    """_reap_one_idle_locked frees a proxy slot by dropping the least-recently-active stream no
    connection holds, never one that is playing; _reap_proc never raises on a stuck child."""

    def setUp(self):
        self._streams, self._rl = youfish._streams, youfish._reap_locked
        self.reaped = []
        youfish._reap_locked = lambda s: self.reaped.append(s.vid)

    def tearDown(self):
        youfish._streams, youfish._reap_locked = self._streams, self._rl

    @staticmethod
    def _s(vid, refs, last_active):
        return types.SimpleNamespace(vid=vid, refs=refs, last_active=last_active,
                                     cond=_threading.Condition())

    def test_reaps_oldest_idle_only(self):
        youfish._streams = {("a", 1): self._s("a", 0, 50), ("b", 1): self._s("b", 1, 10),
                            ("c", 1): self._s("c", 0, 20)}
        youfish._reap_one_idle_locked()
        self.assertEqual(self.reaped, ["c"])                  # b is older but playing
        youfish._reap_one_idle_locked()
        youfish._reap_one_idle_locked()
        self.assertEqual(self.reaped, ["c", "a"])
        self.assertEqual(list(youfish._streams), [("b", 1)])

    def test_reap_proc(self):
        waits = []
        youfish._reap_proc(None)
        youfish._reap_proc(types.SimpleNamespace(wait=lambda timeout=None: waits.append(timeout)))

        def stuck(timeout=None):
            raise youfish.subprocess.TimeoutExpired("yt-dlp", timeout)
        youfish._reap_proc(types.SimpleNamespace(wait=stuck))
        self.assertEqual(waits, [5])


class ProbeUrlOk(_Restore):
    """_probe_url_ok (the token-free → token escalation trigger): only a definite 403 says no; a
    timeout or another status never makes resolve pay for the slower client. Plus _url_shape,
    the identity-free URL summary in debug logs."""
    SAVE = ("_force_ipv4",)

    def setUp(self):
        super().setUp()
        youfish._force_ipv4 = lambda: None
        self.reqs = []

    def _answer(self, outcome):
        def fake(req, timeout=None):
            self.reqs.append(req)
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeHttpResp(206, b"ab")
        urllib.request.urlopen = fake

    def test_serving_url(self):
        self._answer(None)
        self.assertTrue(youfish._probe_url_ok("https://r1.googlevideo.com/v", "UA/1"))
        self.assertEqual(self.reqs[0].get_header("Range"), "bytes=0-1")
        self.assertEqual(self.reqs[0].get_header("User-agent"), "UA/1")
        youfish._probe_url_ok("https://r1.googlevideo.com/v", "")
        self.assertEqual(self.reqs[1].get_header("User-agent"), youfish._BROWSER_UA)

    def test_only_403_says_no(self):
        def err(code):
            return urllib.error.HTTPError("https://r1.googlevideo.com/v", code, "x", {}, None)
        self._answer(err(403))
        self.assertFalse(youfish._probe_url_ok("https://r1.googlevideo.com/v", "UA"))
        for maybe in (err(404), err(503), urllib.error.URLError("dns"), socket.timeout("slow")):
            self._answer(maybe)
            self.assertTrue(youfish._probe_url_ok("https://r1.googlevideo.com/v", "UA"))

    def test_no_url_is_not_probed(self):
        self._answer(None)
        self.assertTrue(youfish._probe_url_ok("", "UA"))
        self.assertEqual(self.reqs, [])

    def test_url_shape(self):
        exp = int(time.time()) + 600
        s = youfish._url_shape("https://r1.googlevideo.com/videoplayback?c=VISIONOS&ip=203.0.113.1"
                               "&n=abc&expire=%d&sparams=expire,ip" % exp)
        m = re.match(r"c=VISIONOS ip=v4 n=y pot=- exp=\+(\d+)s sparams=expire,ip$", s)
        self.assertTrue(m, s)
        self.assertTrue(590 <= int(m.group(1)) <= 600)
        self.assertIn("ip=v6", youfish._url_shape("https://x/v?ip=2001:db8::1&pot=T"))
        self.assertEqual(youfish._url_shape("https://x/v"), "c=? ip=- n=- pot=- exp=+0s sparams=")


class WatchStateStore(_TmpDataDir):
    """watch_state: the whole watch map the thumbnails read, straight from watch_history.json."""

    def test_reads_the_history_store(self):
        self.assertEqual(youfish.watch_state(), {})
        youfish.set_watched("dQw4w9WgXcQ", True, "T", "C")
        st = youfish.watch_state()
        self.assertEqual(st["dQw4w9WgXcQ"]["w"], 1)
        self.assertEqual(st, youfish._load_watch_history())


class SponsorSegmentsFetch(_Restore):
    """_sponsor_segments_fetch: SponsorBlock's answer → sorted, sane segments. No segments (404) is
    a valid empty answer; an outage is ok:False, so it isn't cached as 'nothing to skip'."""
    SAVE = ("_force_ipv4",)

    def setUp(self):
        super().setUp()
        youfish._force_ipv4 = lambda: None
        self.urls = []

    def _answer(self, outcome):
        def fake(req, timeout=None):
            self.urls.append(req.full_url)
            if isinstance(outcome, Exception):
                raise outcome
            return _Download(json.dumps(outcome).encode())
        urllib.request.urlopen = fake

    def test_segments(self):
        self._answer([{"segment": [30, 40.5], "category": "outro"},
                      {"segment": [5, 12], "category": "sponsor"},
                      {"segment": [20, 20], "category": "intro"},       # empty: dropped
                      {"segment": [1], "category": "selfpromo"}])       # malformed: dropped
        r = youfish._sponsor_segments_fetch("a b&c")
        self.assertEqual(r, {"ok": True, "segments": [
            {"start": 5.0, "end": 12.0, "category": "sponsor"},
            {"start": 30.0, "end": 40.5, "category": "outro"}]})
        self.assertIn("videoID=a%20b%26c&categories=", self.urls[0])
        self.assertTrue(self.urls[0].startswith("https://sponsor.ajay.app/api/skipSegments?"))

    def test_none_vs_outage(self):
        def err(code):
            return urllib.error.HTTPError("https://sponsor.ajay.app", code, "x", {}, None)
        self._answer(err(404))
        self.assertEqual(youfish._sponsor_segments_fetch("v"), {"ok": True, "segments": []})
        for bad in (err(500), urllib.error.URLError("offline")):
            self._answer(bad)
            self.assertEqual(youfish._sponsor_segments_fetch("v"), {"ok": False, "segments": []})
        self._answer({"message": "odd"})
        self.assertEqual(youfish._sponsor_segments_fetch("v"), {"ok": True, "segments": []})


class YoutubeAccountWrappers(_Restore):
    """The youtube_* QML wrappers over ytm: they pass ytm's answers through, fall back to a safe
    signed-out shape when ytm fails or is missing, and every login/account change drops the
    resolve cache (signed-in extraction differs)."""
    SAVE = ("invalidate_resolve_cache",)

    def setUp(self):
        super().setUp()
        self._ytm = sys.modules.get("ytm")
        self.inval = []
        youfish.invalidate_resolve_cache = lambda: self.inval.append(1)
        self.calls = []
        c = self.calls
        sys.modules["ytm"] = types.SimpleNamespace(
            login_status=lambda: {"logged_in": True, "count": 9},
            import_browser_login=lambda: c.append("import") or {"ok": True, "count": 9},
            logout=lambda: c.append("logout") or {"logged_in": False},
            list_accounts=lambda: {"ok": True, "accounts": [{"name": "Me"}]},
            select_account=lambda *a: c.append(a) or {"ok": True, "name": a[3]},
            selected_account=lambda: {"authuser": "1", "page_id": "P", "name": "Brand"})

    def tearDown(self):
        if self._ytm is not None:
            sys.modules["ytm"] = self._ytm
        else:
            sys.modules.pop("ytm", None)
        super().tearDown()

    def test_pass_through(self):
        self.assertEqual(youfish.youtube_login_status(), {"logged_in": True, "count": 9})
        self.assertEqual(youfish.youtube_import_login(), {"ok": True, "count": 9})
        self.assertEqual(youfish.youtube_list_accounts()["accounts"], [{"name": "Me"}])
        self.assertEqual(youfish.youtube_select_account("1", "P", "D", "Brand"),
                         {"ok": True, "name": "Brand"})
        self.assertEqual(youfish.youtube_selected_account()["name"], "Brand")
        self.assertEqual(youfish.youtube_logout(), {"logged_in": False})
        self.assertEqual(self.calls, ["import", ("1", "P", "D", "Brand"), "logout"])
        self.assertEqual(len(self.inval), 3)              # import, select, logout

    def test_failures_fall_back(self):
        def boom(*a):
            raise RuntimeError("cookie jar locked")
        sys.modules["ytm"] = types.SimpleNamespace(**{n: boom for n in (
            "login_status", "import_browser_login", "logout", "list_accounts", "select_account",
            "selected_account")})
        self.assertEqual(youfish.youtube_login_status(),
                         {"logged_in": False, "count": 0, "imported_at": 0, "source": ""})
        self.assertEqual(youfish.youtube_import_login(),
                         {"ok": False, "count": 0, "error": "cookie jar locked"})
        self.assertEqual(youfish.youtube_list_accounts(),
                         {"ok": False, "accounts": [], "error": "cookie jar locked"})
        self.assertEqual(youfish.youtube_select_account(),
                         {"ok": False, "error": "cookie jar locked"})
        self.assertEqual(youfish.youtube_selected_account(),
                         {"authuser": "0", "page_id": "", "name": ""})
        self.assertEqual(youfish.youtube_logout(), {"logged_in": False})
        self.assertEqual(len(self.inval), 1)              # only logout clears after a failure
        sys.modules["ytm"] = None                         # module missing altogether
        self.assertFalse(youfish.youtube_login_status()["logged_in"])


class AvatarCacheLoad(_TmpDataDir):
    """_load_avatar_cache: avatars survive a restart, minus entries past the TTL and anything
    malformed; a corrupt file is an empty cache, not a crash."""

    def _write(self, obj):
        with open(youfish._avatar_cache_path(), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_keeps_only_fresh_wellformed(self):
        now = time.time()
        self._write({"fresh": {"ts": now - 60, "res": {"thumbnail": "t"}},
                     "old": {"ts": now - youfish._AVATAR_CACHE_TTL - 1, "res": {"thumbnail": "t"}},
                     "bad_res": {"ts": now, "res": "t"}, "not_dict": [1]})
        self.assertEqual(youfish._load_avatar_cache(),
                         {"fresh": {"ts": now - 60, "res": {"thumbnail": "t"}}})

    def test_missing_corrupt_or_wrong_type(self):
        self.assertEqual(youfish._load_avatar_cache(), {})
        self._write("{truncated")
        self.assertEqual(youfish._load_avatar_cache(), {})
        self._write([1, 2])
        self.assertEqual(youfish._load_avatar_cache(), {})


class ChannelFeedRss(_TmpDataDir):
    """channel_feed_rss: the channel page's instant first paint from RSS. Only a UC id or
    /channel/UC… URL can use it; anything else (or a failed fetch) says ok:False so the page falls
    back to channel_videos; the hide-Shorts setting applies."""
    XML = ("<feed><title>Chan &amp; Co</title>"
           "<entry><yt:videoId>aaaaaaaaaaa</yt:videoId><title>One &amp; two</title>"
           "<published>2026-10-01T00:00:00+00:00</published><author><name></name></author>"
           '<link rel="alternate" href="https://www.youtube.com/watch?v=aaaaaaaaaaa"/>'
           '<media:statistics views="12"/></entry>'
           "<entry><yt:videoId>bbbbbbbbbbb</yt:videoId><title>Short</title>"
           "<author><name>Other</name></author>"
           '<link rel="alternate" href="https://www.youtube.com/shorts/bbbbbbbbbbb"/></entry></feed>')

    def setUp(self):
        super().setUp()
        self._saved = {n: getattr(youfish, n) for n in ("_rss_get", "_hide_shorts")}
        self.fetched = []
        youfish._rss_get = lambda cid: self.fetched.append(cid) or self.XML
        youfish._hide_shorts = lambda: True

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(youfish, n, v)
        super().tearDown()

    def test_first_paint(self):
        r = youfish.channel_feed_rss("UCabc")
        self.assertTrue(r["ok"] and r["has_more"])
        self.assertEqual(r["channel"]["name"], "Chan & Co")
        self.assertEqual(r["channel"]["url"], "https://www.youtube.com/channel/UCabc")
        self.assertEqual([i["id"] for i in r["items"]], ["aaaaaaaaaaa"])     # Short hidden
        it = r["items"][0]
        self.assertEqual((it["title"], it["uploader"], it["views"], it["live"]),
                         ("One & two", "Chan & Co", 12, 0))
        self.assertTrue(it["posted"])

    def test_shorts_shown_when_not_hidden(self):
        youfish._hide_shorts = lambda: False
        r = youfish.channel_feed_rss("https://www.youtube.com/channel/UCabc/videos")
        self.assertEqual(self.fetched, ["UCabc"])
        self.assertEqual([i["uploader"] for i in r["items"]], ["Chan & Co", "Other"])

    def test_needs_a_channel_id(self):
        for ref in ("@handle", "https://www.youtube.com/@handle", "", None, "UCx/videos"):
            self.assertEqual(youfish.channel_feed_rss(ref), {"ok": False, "items": []})
        self.assertEqual(self.fetched, [])

        def fail(cid):
            raise IOError("HTTP 404")
        youfish._rss_get = fail
        self.assertEqual(youfish.channel_feed_rss("UCabc"), {"ok": False, "items": []})


class YoutubePlaylists(_TmpDataDir):
    """youtube_playlist / save_youtube_playlist / refresh_playlist / channel_playlists: the yt-dlp
    listing calls behind the library's YouTube playlists. Saving dedupes by list id, a failed
    refresh keeps the stored items, and a channel without a Playlists tab falls back to Releases."""
    PL = {"id": "PLx", "title": "Mix", "uploader": "Someone", "entries": [
        {"id": "aaaaaaaaaaa", "title": "A", "duration": 61, "channel": "Ch"},
        {"title": "no id"},
        {"id": "bbbbbbbbbbb", "live_status": "is_live"}]}

    def setUp(self):
        super().setUp()
        self._saved = {n: getattr(youfish, n) for n in ("_ytdlp_path", "_ytdlp_run", "_cookies_args")}
        youfish._ytdlp_path = lambda: "/fake/yt-dlp"

        @contextlib.contextmanager
        def no_cookies():
            yield []
        youfish._cookies_args = no_cookies
        self.answers, self.runs = {}, []

        def run(path, argv, timeout):
            self.runs.append(argv)
            a = self.answers.get(argv[-1], "ERROR: [youtube:tab] not found")
            if isinstance(a, dict):
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(a), stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr=a)
        youfish._ytdlp_run = run

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(youfish, n, v)
        super().tearDown()

    URL = "https://www.youtube.com/playlist?list=PLx"

    def test_playlist(self):
        self.answers[self.URL] = self.PL
        r = youfish.youtube_playlist("PLx")
        self.assertEqual((r["ok"], r["title"], r["uploader"], r["yt_id"]), (True, "Mix", "Someone", "PLx"))
        self.assertEqual([(i["id"], i["title"], i["uploader"], i["duration"], i["live"])
                          for i in r["items"]],
                         [("aaaaaaaaaaa", "A", "Ch", 61, 0), ("bbbbbbbbbbb", "(untitled)", "", 0, 1)])
        argv = self.runs[0]
        self.assertEqual(argv[argv.index("--playlist-end") + 1], "200")
        self.assertEqual(argv[-2:], ["--", self.URL])

    def test_playlist_errors(self):
        self.answers[self.URL] = "ERROR: [youtube:tab] PLx: The playlist does not exist."
        r = youfish.youtube_playlist(self.URL)
        self.assertFalse(r["ok"])
        self.assertIn("does not exist", r["error"])
        youfish._ytdlp_path = lambda: None
        self.assertEqual(youfish.youtube_playlist("PLx"), {"ok": False, "error": "yt-dlp not found"})

    def test_save_dedupes_and_refresh_updates(self):
        self.answers[self.URL] = self.PL
        self.assertTrue(youfish.save_youtube_playlist("PLx")["ok"])
        self.answers[self.URL] = dict(self.PL, title="Mix (renamed)")
        youfish.save_youtube_playlist(self.URL)
        lst = youfish._load_playlists()
        self.assertEqual(len(lst), 1)
        self.assertEqual((lst[0]["kind"], lst[0]["yt_id"], lst[0]["title"]),
                         ("youtube", "PLx", "Mix (renamed)"))
        pid = lst[0]["id"]
        self.answers[self.URL] = dict(self.PL, entries=[{"id": "ccccccccccc", "title": "New"}])
        r = youfish.refresh_playlist(pid)
        self.assertEqual([i["id"] for i in r["playlist"]["items"]], ["ccccccccccc"])
        self.answers[self.URL] = "ERROR: HTTP Error 503"
        self.assertFalse(youfish.refresh_playlist(pid)["ok"])
        self.assertEqual([i["id"] for i in youfish.get_playlist(pid)["playlist"]["items"]],
                         ["ccccccccccc"])                  # a failed refresh keeps the items
        self.assertFalse(youfish.save_youtube_playlist("PLgone")["ok"])
        self.assertEqual(len(youfish._load_playlists()), 1)

    def test_refresh_leaves_local_playlists_alone(self):
        local = youfish.create_playlist("Mine")
        pid = next(p["id"] for p in youfish._load_playlists())
        self.assertEqual(youfish.refresh_playlist(pid)["playlist"]["kind"], "local")
        self.assertEqual(self.runs, [])
        self.assertIsNotNone(local)

    def test_channel_playlists(self):
        base = "https://www.youtube.com/@x"
        self.answers[base + "/playlists"] = {"entries": [
            {"id": "PL1", "title": "P", "playlist_count": 3}, {"title": "no id"}, {"id": "PL2"}]}
        r = youfish.channel_playlists(base + "/videos")
        self.assertEqual([(i["yt_id"], i["title"], i["count"]) for i in r["items"]],
                         [("PL1", "P", 3), ("PL2", "(playlist)", 0)])
        self.assertEqual(len(self.runs), 1)

    def test_channel_playlists_falls_back_to_releases(self):
        base = "https://www.youtube.com/channel/UCm"
        self.answers[base + "/playlists"] = {"entries": []}
        self.answers[base + "/releases"] = {"entries": [{"id": "OLAK5", "title": "Album"}]}
        r = youfish.channel_playlists("UCm")
        self.assertEqual([i["yt_id"] for i in r["items"]], ["OLAK5"])
        self.assertEqual([a[-1] for a in self.runs], [base + "/playlists", base + "/releases"])
        self.assertEqual(youfish.channel_playlists("UCnothing"), {"ok": True, "items": []})
        youfish._ytdlp_path = lambda: None
        self.assertFalse(youfish.channel_playlists("UCm")["ok"])


class InprocDebugHooks(_Restore):
    """The YOUFISH_DEBUG instrumentation of in-process yt-dlp: its logger stays silent unless
    debugging, and the Deno-share probe counts only deno spawns, only on a thread that armed it,
    and wraps Popen once."""
    SAVE = ("_DEBUG", "_tlog", "_orig_popen")

    def setUp(self):
        super().setUp()
        self._popen = youfish.subprocess.Popen
        self.logs = []
        youfish._tlog = self.logs.append

    def tearDown(self):
        youfish.subprocess.Popen = self._popen
        if hasattr(youfish._inproc_tls, "deno"):
            del youfish._inproc_tls.deno
        super().tearDown()

    def test_logger(self):
        lg = youfish._InprocLogger()
        youfish._DEBUG = False
        for m in (lg.debug, lg.info, lg.warning, lg.error):
            m("noise")
        self.assertEqual(self.logs, [])
        youfish._DEBUG = True
        lg.warning("still quiet")
        lg.error("x" * 300)
        self.assertEqual(self.logs, ["inproc yt-dlp error: " + "x" * 200])

    def test_deno_probe_counts_only_armed_deno_spawns(self):
        deno = _script(os.path.join(self._tmp, "deno"), "exit 0\n")
        other = _script(os.path.join(self._tmp, "ffprobe"), "exit 0\n")
        youfish._orig_popen = None
        youfish._inproc_install_deno_probe()
        wrapped = youfish.subprocess.Popen
        self.assertIsNot(wrapped, self._popen)
        youfish._inproc_install_deno_probe()
        self.assertIs(youfish.subprocess.Popen, wrapped)       # idempotent: wrapped once
        youfish.subprocess.run([deno])                         # not armed on this thread
        acc = [0, 0.0]
        youfish._inproc_tls.deno = acc
        self.assertEqual(youfish.subprocess.run([deno], capture_output=True).returncode, 0)
        youfish.subprocess.run([other])
        p = youfish.subprocess.Popen([deno])
        p.wait()
        p.wait()                                               # one process counts once
        self.assertEqual(acc[0], 2)
        self.assertGreaterEqual(acc[1], 0.0)


class _PoolConn:
    """A fake http.client.HTTPSConnection for innertube's pool. `fail` makes the next request()
    raise (a keep-alive socket the server already closed)."""
    def __init__(self, test, host, timeout=None):
        self.host, self.timeout, self.test = host, timeout, test
        self.sock = types.SimpleNamespace(settimeout=lambda t: setattr(self, "sock_timeout", t))
        self.sock_timeout, self.closed, self.fail, self.sent = None, False, False, []
        test.conns.append(self)

    def request(self, method, path, body=None, headers=None):
        if self.fail:
            self.fail = False
            raise ConnectionResetError("peer closed")
        self.sent.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        status, data, hdrs, will_close = self.test.reply
        return types.SimpleNamespace(status=status, read=lambda: data, will_close=will_close,
                                     getheader=lambda n, d=None: hdrs.get(n, d), msg=hdrs)

    def close(self):
        self.closed = True


class InnertubePool(unittest.TestCase):
    """innertube.request, the keep-alive HTTPS pool every InnerTube call (and the feed's RSS) rides:
    reuse, idle expiry, one retry on a dead reused socket (never on a fresh one), will_close,
    the per-host cap, gunzip, and the env-proxy path through urllib."""
    URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"

    def setUp(self):
        self._pool = dict(innertube._pool)
        innertube._pool.clear()
        self._conn = http.client.HTTPSConnection
        self._proxies = urllib.request.getproxies
        self._urlopen = urllib.request.urlopen
        self.conns = []
        http.client.HTTPSConnection = lambda host, timeout=None: _PoolConn(self, host, timeout)
        urllib.request.getproxies = lambda: {}
        self.reply = (200, b"ok", {}, False)

    def tearDown(self):
        http.client.HTTPSConnection = self._conn
        urllib.request.getproxies = self._proxies
        urllib.request.urlopen = self._urlopen
        innertube._pool.clear()
        innertube._pool.update(self._pool)

    def test_reuses_one_connection(self):
        st, data, hdrs = innertube.request("POST", self.URL, b"{}", {"X": "1"}, timeout=7)
        self.assertEqual((st, data), (200, b"ok"))
        innertube.request("GET", "https://www.youtube.com/feeds/videos.xml?channel_id=UC1", timeout=3)
        self.assertEqual(len(self.conns), 1)
        c = self.conns[0]
        self.assertEqual((c.host, c.timeout, c.sock_timeout), ("www.youtube.com", 7, 3))
        self.assertEqual(c.sent[0][:3], ("POST", "/youtubei/v1/browse?prettyPrint=false", b"{}"))
        self.assertEqual(c.sent[0][3], {"X": "1", "Accept-Encoding": "gzip"})
        self.assertEqual(c.sent[1][1], "/feeds/videos.xml?channel_id=UC1")

    def test_idle_connection_expires(self):
        innertube.request("GET", self.URL)
        old = self.conns[0]
        host = "www.youtube.com"
        innertube._pool[host] = [(old, time.time() - innertube._POOL_IDLE_MAX - 1)]
        innertube.request("GET", self.URL)
        self.assertTrue(old.closed)
        self.assertEqual(len(self.conns), 2)

    def test_dead_reused_socket_is_retried_once(self):
        innertube.request("GET", self.URL)
        self.conns[0].fail = True
        self.assertEqual(innertube.request("GET", self.URL)[1], b"ok")
        self.assertTrue(self.conns[0].closed)
        self.assertEqual(len(self.conns), 2)

    def test_fresh_connection_failure_raises(self):
        orig = http.client.HTTPSConnection

        def failing(host, timeout=None):
            c = orig(host, timeout)
            c.fail = True
            return c
        http.client.HTTPSConnection = failing
        with self.assertRaises(ConnectionResetError):
            innertube.request("GET", self.URL)
        self.assertEqual(len(self.conns), 1)              # no retry on a fresh socket
        self.assertTrue(self.conns[0].closed)
        self.assertEqual(innertube._pool.get("www.youtube.com", []), [])

    def test_will_close_is_not_pooled(self):
        self.reply = (404, b"", {}, True)
        self.assertEqual(innertube.request("GET", self.URL)[0], 404)   # statuses come back, not raised
        self.assertTrue(self.conns[0].closed)
        self.assertEqual(innertube._pool.get("www.youtube.com", []), [])

    def test_gzip_body(self):
        self.reply = (200, gzip.compress(b'{"a": 1}'), {"Content-Encoding": "gzip"}, False)
        self.assertEqual(innertube.request("GET", self.URL)[1], b'{"a": 1}')
        self.assertEqual(_REAL_TRANSPORT("GET", self.URL), (200, b'{"a": 1}'))

    def test_per_host_cap(self):
        conns = [_PoolConn(self, "h") for _ in range(innertube._POOL_PER_HOST + 1)]
        for c in conns:
            innertube._put_conn("h", c)
        self.assertEqual(len(innertube._pool["h"]), innertube._POOL_PER_HOST)
        self.assertEqual([c.closed for c in conns].count(True), 1)
        self.assertTrue(conns[-1].closed)

    def test_env_proxy_goes_through_urllib(self):
        urllib.request.getproxies = lambda: {"https": "http://proxy:3128"}
        seen = []

        def fake(req, timeout=None):
            seen.append(req)
            r = _Download(gzip.compress(b"hello"))
            r.status, r.headers = 200, {"Content-Encoding": "gzip"}
            return r
        urllib.request.urlopen = fake
        self.assertEqual(innertube.request("POST", self.URL, b"{}")[:2], (200, b"hello"))
        self.assertEqual((seen[0].get_method(), seen[0].get_header("Accept-encoding")),
                         ("POST", "gzip"))

        def refused(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, io.BytesIO(b"later"))
        urllib.request.urlopen = refused
        self.assertEqual(innertube.request("GET", self.URL)[:2], (429, b"later"))
        self.assertEqual(self.conns, [])

    def test_reel_items(self):
        it = innertube._from_reel({"videoId": "aaaaaaaaaaa", "headline": {"simpleText": "Reel"},
                                   "viewCountText": {"simpleText": "1.2K views"}})
        self.assertEqual((it["id"], it["title"], it["short"], it["views"]),
                         ("aaaaaaaaaaa", "Reel", True, 1200))
        self.assertIsNone(innertube._from_reel({"videoId": "short"}))
        self.assertIsNone(innertube._from_reel({}))


# --- ytm.py (cookie login): the cookie jar, SAPISIDHASH auth and the InnerTube identity ------- #

class _FreshYtm(unittest.TestCase):
    """Base: a freshly imported ytm.py over a throwaway data dir (other tests install a fake ytm).
    Puts back the global socket/urllib/tempfile hooks the ytm tests replace."""

    def setUp(self):
        import importlib
        self._prev = sys.modules.pop("ytm", None)
        self.ytm = importlib.import_module("ytm")
        self._tmp = tempfile.mkdtemp(prefix="ytm-")
        self.ytm._data_dir = lambda: self._tmp
        self._urlopen, self._gai, self._tempdir = (urllib.request.urlopen, socket.getaddrinfo,
                                                   tempfile.tempdir)
        self.logs = []
        self.ytm._log = self.logs.append

    def tearDown(self):
        urllib.request.urlopen, socket.getaddrinfo, tempfile.tempdir = (self._urlopen, self._gai,
                                                                        self._tempdir)
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._prev is not None:
            sys.modules["ytm"] = self._prev
        else:
            sys.modules.pop("ytm", None)

    def store(self, **c):
        with open(self.ytm._cookies_path(), "w") as f:
            json.dump(c, f)


class YtmCookieJar(_FreshYtm):
    """_read_cookie_jar: reads only google/youtube rows from the browser's cookies.sqlite, sees
    rows still in a running browser's WAL, and leaves no copy of the live cookies behind."""
    ROWS = [("SAPISID", "s1", ".youtube.com", "/", 2000000000, 1),
            ("SID", "s2", ".google.com", "/", 0, 1),
            ("other", "x", ".example.com", "/", 0, 0)]

    def _jar(self, wal=False):
        path = os.path.join(self._tmp, "cookies.sqlite")
        con = sqlite3.connect(path)
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE moz_cookies (name, value, host, path, expiry, isSecure)")
        con.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?)", self.ROWS)
        con.commit()
        return path, con

    def test_google_rows_only_and_no_copy_left(self):
        path, con = self._jar()
        con.close()
        scratch = os.path.join(self._tmp, "scratch")
        os.mkdir(scratch)
        tempfile.tempdir = scratch
        self.assertEqual(sorted(self.ytm._read_cookie_jar(path)), sorted(self.ROWS[:2]))
        self.assertEqual(os.listdir(scratch), [])

    def test_reads_a_running_browsers_wal(self):
        path, con = self._jar(wal=True)                  # still open: the rows live in -wal
        try:
            self.assertTrue(os.path.exists(path + "-wal"))
            self.assertEqual(len(self.ytm._read_cookie_jar(path)), 2)
        finally:
            con.close()

    def test_missing_or_broken_jar(self):
        self.assertEqual(self.ytm._read_cookie_jar(os.path.join(self._tmp, "nope.sqlite")), [])
        bad = os.path.join(self._tmp, "bad.sqlite")
        with open(bad, "w") as f:
            f.write("not a database")
        self.assertEqual(self.ytm._read_cookie_jar(bad), [])


class YtmAuth(_FreshYtm):
    """The signed-in request's credentials: SAPISIDHASH, the Cookie header + SAPISID (stored, or
    derived from an older import's Netscape text with only the auth cookies), is_logged_in, the
    gzip body reader and the IPv4 pin."""

    def test_sapisidhash(self):
        h = self.ytm._sapisidhash("SECRET", "https://www.youtube.com")
        m = re.match(r"SAPISIDHASH (\d+)_([0-9a-f]{40})$", h)
        self.assertTrue(m, h)
        self.assertLessEqual(abs(int(m.group(1)) - time.time()), 5)
        self.assertEqual(m.group(2), hashlib.sha1(
            ("%s SECRET https://www.youtube.com" % m.group(1)).encode()).hexdigest())

    def test_cookie_and_sapisid(self):
        self.assertEqual(self.ytm._cookie_and_sapisid(), ("", ""))
        self.assertFalse(self.ytm.is_logged_in())
        rows = [("YSC", "p", ".youtube.com", "/", 0, 1), ("SAPISID", "plain", ".youtube.com", "/", 0, 1),
                ("__Secure-3PAPISID", "s3", ".youtube.com", "/", 0, 1), ("SID", "sid", ".google.com", "/", 0, 1)]
        self.store(ytdlp=self.ytm._netscape_from_rows(rows))
        self.assertTrue(self.ytm.is_logged_in())
        header, sapisid = self.ytm._cookie_and_sapisid()
        self.assertEqual(sapisid, "s3")                  # the 3P one wins
        self.assertNotIn("YSC", header)                  # not an auth cookie
        vals = {"__Secure-3PAPISID": "s3", "SAPISID": "plain", "SID": "sid"}
        self.assertEqual(header.split("; "), ["%s=%s" % (n, vals[n])
                                              for n in self.ytm._AUTH_COOKIE_NAMES if n in vals])
        self.assertIn("SAPISID=plain", header.split("; "))
        self.store(cookie="A=1", sapisid="S", ytdlp="x")
        self.assertEqual(self.ytm._cookie_and_sapisid(), ("A=1", "S"))

    def test_read_body(self):
        def resp(data, hdrs):
            return types.SimpleNamespace(read=lambda: data, headers=hdrs)
        self.assertEqual(self.ytm._read_body(resp(gzip.compress(b"x"), {"Content-Encoding": "GZIP"})), b"x")
        self.assertEqual(self.ytm._read_body(resp(b"plain", {"Content-Encoding": "gzip"})), b"plain")
        self.assertEqual(self.ytm._read_body(resp(b"plain", None)), b"plain")

    def test_force_ipv4(self):
        seen = []
        socket.getaddrinfo = lambda host, port, family=0, *a: seen.append(family) or []
        self.ytm._force_ipv4()
        self.ytm._force_ipv4()                           # once: never wraps the wrapper
        socket.getaddrinfo("www.youtube.com", 443)
        self.assertEqual(seen, [socket.AF_INET])


class YtmIdentityConfig(_FreshYtm):
    """The self-healing InnerTube key + client version: the cached config and its default, the
    background re-scrape when stale (at most every 10 min, committed only with a sane version)."""

    def test_load_and_cache(self):
        cfg = self.ytm._yt_cfg_load()
        self.assertEqual(cfg, {"key": self.ytm._DEFAULT_INNERTUBE_KEY,
                               "version": self.ytm._DEFAULT_CLIENT_VERSION, "ts": 0})
        warms = []
        self.ytm._warm_yt_config = lambda: warms.append(1)
        self.assertEqual(self.ytm.innertube_identity()["source"], "default")
        self.assertEqual(warms, [1])                      # default = stale: re-scrape
        with open(self.ytm._yt_config_path(), "w") as f:
            json.dump({"version": "2.20261001.00.00", "ts": time.time()}, f)
        self.ytm._yt_cfg_cache = None
        self.assertEqual(self.ytm.innertube_identity(),
                         {"version": "2.20261001.00.00", "source": "live"})
        self.assertEqual(warms, [1])                      # fresh: no scrape
        self.assertEqual(self.ytm._context()["client"]["clientVersion"], "2.20261001.00.00")
        self.assertEqual(self.ytm._yt_config()["key"], self.ytm._DEFAULT_INNERTUBE_KEY)
        with open(self.ytm._yt_config_path(), "w") as f:
            f.write("{")
        self.assertEqual(self.ytm._yt_cfg_load()["ts"], 0)

    def _page(self, html):
        self.reqs = []

        def fake(req, timeout=None):
            self.reqs.append(req)
            r = _Download(gzip.compress(html.encode()))
            r.headers = {"Content-Encoding": "gzip"}
            return r
        urllib.request.urlopen = fake

    def test_fetch_identity(self):
        self.ytm._force_ipv4 = lambda: None
        self._page('ytcfg.set({"INNERTUBE_API_KEY": "KEY1", "INNERTUBE_CLIENT_VERSION": "2.20261001.01.00"})')
        self.assertEqual(self.ytm._fetch_yt_identity(), ("KEY1", "2.20261001.01.00"))
        self.assertIn("SOCS=", self.reqs[0].get_header("Cookie"))      # skips the EU consent page
        self._page('"INNERTUBE_API_KEY": "KEY1", "INNERTUBE_CLIENT_VERSION": "garbage"')
        self.assertEqual(self.ytm._fetch_yt_identity(), ("KEY1", None))

        def down(req, timeout=None):
            raise urllib.error.URLError("offline")
        urllib.request.urlopen = down
        self.assertEqual(self.ytm._fetch_yt_identity(), (None, None))

    def _warm(self):
        self.ytm._warm_yt_config()
        deadline = time.time() + 5
        while self.ytm._yt_cfg_warming and time.time() < deadline:
            time.sleep(0.01)

    def test_warm_commits_only_a_real_version(self):
        fetches = []
        self.ytm._fetch_yt_identity = lambda: fetches.append(1) or (None, None)
        self._warm()
        self.assertIsNone(self.ytm._yt_cfg_cache)
        self.assertFalse(os.path.exists(self.ytm._yt_config_path()))
        self._warm()                                      # within 10 min of the last try: no fetch
        self.assertEqual(fetches, [1])
        self.ytm._yt_cfg_last_try = 0
        self.ytm._fetch_yt_identity = lambda: (None, "2.20261001.00.00")
        self._warm()
        self.assertEqual(self.ytm._yt_cfg_cache["version"], "2.20261001.00.00")
        self.assertEqual(self.ytm._yt_cfg_cache["key"], self.ytm._DEFAULT_INNERTUBE_KEY)
        self.assertEqual(self.ytm._yt_cfg_load()["version"], "2.20261001.00.00")


class YtmInnertubeCall(_FreshYtm):
    """ytm._innertube, every signed-in InnerTube request: SAPISIDHASH auth, the chosen account's
    AuthUser/PageId (overridable when probing logins), refuses to run signed out, and a 400
    triggers an identity re-scrape."""

    def setUp(self):
        super().setUp()
        self.ytm._force_ipv4 = lambda: None
        self.warms = []
        self.ytm._warm_yt_config = lambda: self.warms.append(1)
        self.ytm._yt_cfg_cache = {"key": "K", "version": "2.20261001.00.00", "ts": time.time()}
        self.reqs = []

    def _answer(self, outcome):
        def fake(req, timeout=None):
            self.reqs.append(req)
            if isinstance(outcome, Exception):
                raise outcome
            r = _Download(gzip.compress(json.dumps(outcome).encode()))
            r.headers = {"Content-Encoding": "gzip"}
            return r
        urllib.request.urlopen = fake

    def test_signed_out_refuses(self):
        self._answer({})
        with self.assertRaises(RuntimeError):
            self.ytm._innertube("browse", {"browseId": "FEsubscriptions"})
        self.assertEqual(self.reqs, [])

    def test_authed_request(self):
        self.store(cookie="SAPISID=S; SID=x", sapisid="S", authuser="2", page_id="PAGE")
        self._answer({"contents": 1})
        self.assertEqual(self.ytm._innertube("browse", {"browseId": "FEsubscriptions"}), {"contents": 1})
        req = self.reqs[0]
        self.assertEqual(req.full_url, self.ytm._INNERTUBE + "browse?key=K&prettyPrint=false")
        self.assertTrue(req.get_header("Authorization").startswith("SAPISIDHASH "))
        self.assertEqual((req.get_header("Cookie"), req.get_header("X-goog-authuser"),
                          req.get_header("X-goog-pageid")), ("SAPISID=S; SID=x", "2", "PAGE"))
        body = json.loads(req.data)
        self.assertEqual((body["browseId"], body["context"]["client"]["clientName"]),
                         ("FEsubscriptions", "WEB"))
        self.ytm._innertube("account/accounts_list", {}, authuser=0, page_id="")
        self.assertEqual(self.reqs[1].get_header("X-goog-authuser"), "0")
        self.assertIsNone(self.reqs[1].get_header("X-goog-pageid"))

    def test_400_rescrapes_identity(self):
        self.store(cookie="SAPISID=S", sapisid="S")
        for code, warmed in ((500, []), (400, [1])):
            self._answer(urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"bad")))
            with self.assertRaises(urllib.error.HTTPError):
                self.ytm._innertube("browse", {})
            self.assertEqual(self.warms, warmed)


class YtmDebugShapes(_FreshYtm):
    """The debug-only response dumps (_skeleton, values-free so logs are safe to paste;
    _dump_shape, silent unless YOUFISH_DEBUG) and _account_sections' in-order walk."""

    def test_skeleton_has_no_values(self):
        sk = self.ytm._skeleton({"a": 1, "b": [{"c": "secret"}, {"d": 2}], "e": [], "f": None})
        self.assertEqual(sk, {"a": "int", "b": [{"c": "str"}], "e": [], "f": "NoneType"})
        deep = {}
        cur = deep
        for _ in range(12):
            cur["k"] = {}
            cur = cur["k"]
        self.assertIn("…", json.dumps(self.ytm._skeleton(deep), ensure_ascii=False))

    def test_dump_shape_only_when_debugging(self):
        data = {"contents": [{"videoRenderer": {"title": "t"}}],
                "continuationItemRenderer": {}, "accountName": "x"}
        self.ytm._DEBUG = False
        self.ytm._dump_shape("browse", data)
        self.assertEqual(self.logs, [])
        self.ytm._DEBUG = True
        self.ytm._dump_shape("browse", data)
        self.assertEqual(len(self.logs), 3)
        self.assertIn("'videoRenderer'", self.logs[1])
        self.assertIn("'continuationItemRenderer'", self.logs[1])
        self.ytm._dump_shape("bad", {"x": {1, 2}})         # not JSON-serialisable: logged, no raise
        self.assertIn("dump failed", self.logs[-1])

    def test_account_sections_in_order(self):
        tree = {"a": [{"accountItemSectionRenderer": {"n": 0}},
                      {"x": {"accountItemSectionRenderer": {"n": 1}}}],
                "b": {"accountItemSectionRenderer": "not a dict"}}
        out = []
        self.ytm._account_sections(tree, out)
        self.assertEqual(out, [{"n": 0}, {"n": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
