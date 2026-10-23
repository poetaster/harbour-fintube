#!/usr/bin/env python3
"""The engine's yt-dlp paths against the REAL yt-dlp and live YouTube. Everything else in tests/
runs offline on stand-ins; this is the check that a yt-dlp release (or a YouTube change it tracks)
still works with the exact arguments and option mapping the app uses.

    python3 tests/live_ytdlp.py               # fresh temp data dir, downloads yt-dlp
    python3 tests/live_ytdlp.py --pot         # also Deno + the PO-token provider (slow)
    python3 tests/live_ytdlp.py --dir D       # keep the installs in D between runs

InnerTube is switched off, so every call takes its yt-dlp fallback. Each area runs on both forms
of yt-dlp the app has: the binary, and the zipapp in-process (whose silent fall-back to the binary
is watched, so a broken in-process path fails here instead of hiding).

YouTube often refuses datacenter IPs (GitHub's runners) with "confirm you're not a bot" or a rate
limit. That is not a failure of the app: such a check is SKIPPED and reported as inconclusive.
Every other error fails. From a home connection the wall is rare, so run this before a release.
"""
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))

VIDEO = "dQw4w9WgXcQ"          # long-lived, many formats, comments open
SHORT_VIDEO = "jNQXAC9IVRw"    # "Me at the zoo", 19 s: the audio download stays small
CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"   # Rick Astley; the app passes channel ids
QUERY = "lofi hip hop"

# YouTube's answers to an IP it distrusts. Anything else is a real failure.
BOT_WALL = re.compile(r"not a bot|HTTP Error 429|Too Many Requests|try again later|"
                      r"unusual traffic|rate.?limit", re.I)


class _Events:
    """Stands in for pyotherside: records what the engine sends so a test can wait for it."""
    def __init__(self):
        self.cond = threading.Condition()
        self.sent = []

    def send(self, event, *args):
        with self.cond:
            self.sent.append((event, args))
            self.cond.notify_all()

    def wait(self, event, timeout, match=lambda args: True):
        end = time.time() + timeout
        with self.cond:
            while True:
                for ev, args in self.sent:
                    if ev == event and match(args):
                        self.sent.remove((ev, args))
                        return args
                left = end - time.time()
                if left <= 0:
                    raise AssertionError("no %s event within %ds" % (event, timeout))
                self.cond.wait(left)


EVENTS = _Events()
sys.modules["pyotherside"] = types.SimpleNamespace(send=EVENTS.send)

import youfish  # noqa: E402

youfish._it = lambda: None            # InnerTube off: every area answers through yt-dlp
# The engine fetches the phone's aarch64 builds. CI runs on an arm64 runner, so it gets exactly
# those; on an x86-64 PC the same releases' x86-64 builds stand in.
if os.uname().machine == "x86_64":
    youfish._YTDLP_ASSET = "yt-dlp_linux"
    youfish._DENO_DOWNLOAD_URL = youfish._DENO_DOWNLOAD_URL.replace("aarch64", "x86_64")
    youfish._DENO_SUMS_URL = youfish._DENO_DOWNLOAD_URL + ".sha256sum"
_REAL_IMPORT = youfish._import_yt_dlp
OPTS = {"pot": False, "dir": None}
WALLS = []                            # (test, message) for the summary
INPROC_ERRS = []                      # in-process failures the engine fell back from


def _watch_inproc(name):
    real = getattr(youfish, name)

    def wrapped(*a, **kw):
        try:
            return real(*a, **kw)
        except Exception as ex:
            INPROC_ERRS.append("%s: %s" % (name, ex))
            raise
    setattr(youfish, name, wrapped)


for _n in ("_inproc_json", "_inproc_dump", "_inproc_comments"):
    _watch_inproc(_n)


def setUpModule():
    d = OPTS["dir"] or tempfile.mkdtemp(prefix="ytdlp-live-")
    os.makedirs(d, exist_ok=True)
    OPTS["data"] = d
    youfish._data_dir = lambda: d
    t = time.time()
    if not os.path.isfile(youfish._managed_ytdlp()):
        ok, msg, ver = youfish._install_ytdlp_sync()
        if not ok:
            raise RuntimeError("yt-dlp install failed: " + msg)
    if not os.path.isfile(youfish._ytdlp_zipapp_path()):
        youfish.install_ytdlp_zipapp()
        ok, msg, _ = EVENTS.wait("ytdlp_zipapp_done", 300)
        if not ok:
            raise RuntimeError("yt-dlp zipapp install failed: " + msg)
    if OPTS["pot"]:
        if not youfish._deno_path():
            youfish.install_deno()
            ok, msg, _ = EVENTS.wait("deno_install_done", 300)
            if not ok:
                raise RuntimeError("Deno install failed: " + msg)
        if not youfish._pot_installed():
            youfish.install_pot_provider()
            ok, msg = EVENTS.wait("pot_install_done", 900)[:2]
            if not ok:
                raise RuntimeError("PO-token provider install failed: " + msg)
        youfish.set_pot_enabled(True)
        if not youfish._ensure_pot_server(wait=60):
            raise RuntimeError("PO-token server didn't come up: %s" % youfish._pot_last_error)
    print("yt-dlp %s (binary) / %s (zipapp)%s, set up in %.0fs" % (
        youfish.ytdlp_version(fresh=True), youfish._zipapp_version(youfish._ytdlp_zipapp_path()),
        ", PO-token provider on" if OPTS["pot"] else "", time.time() - t))


def tearDownModule():
    if OPTS["pot"]:
        youfish.stop_pot_server()
    if not OPTS["dir"]:
        shutil.rmtree(OPTS["data"], ignore_errors=True)


class _Live:
    """One area of the engine on one form of yt-dlp (a mixin: the subclasses are the tests).
    Subclasses set TIER."""
    TIER = "binary"

    def setUp(self):
        youfish.invalidate_resolve_cache()
        youfish._AVATAR_CACHE_TTL = 0           # never answer from the avatar cache
        del INPROC_ERRS[:]
        if self.TIER == "binary":
            youfish._import_yt_dlp = lambda: None
        else:
            youfish._import_yt_dlp = _REAL_IMPORT
            if _REAL_IMPORT() is None:
                self.fail("the yt-dlp zipapp did not import (device Python >= 3.10 runs it)")

    def tearDown(self):
        youfish._import_yt_dlp = _REAL_IMPORT

    def wall(self, msg):
        """Skip (inconclusive) when YouTube refused us; otherwise return."""
        if BOT_WALL.search(msg or ""):
            WALLS.append((self.id(), msg))
            self.skipTest("YouTube bot wall / rate limit (inconclusive): " + msg[:160])

    def ok(self, res):
        """An engine answer that must have succeeded on THIS tier."""
        if not res.get("ok", True) or res.get("error"):
            err = res.get("error") or json.dumps(res)[:300]
            self.wall(err)
            self.fail(err)
        if self.TIER == "inproc" and INPROC_ERRS:
            for e in INPROC_ERRS:
                self.wall(e)
            self.fail("in-process yt-dlp failed and fell back to the binary: " + INPROC_ERRS[0])
        return res

    def fetch(self, url, n=65536):
        """The first n bytes through the app's localhost proxy, as the player's first request."""
        req = urllib.request.Request(url, headers={"Range": "bytes=0-%d" % (n - 1)})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                try:   # the proxy may answer 200 with the whole length: read only n
                    return r.status, r.read(n)
                except http.client.IncompleteRead as ex:   # googlevideo refused mid-answer
                    return r.status, ex.partial
        except urllib.error.HTTPError as ex:
            return ex.code, b""

    def check_resolve(self):
        """resolve() picks an HD pair, and both streams serve their first bytes through the
        proxy (the player's first request)."""
        res = self.ok(youfish.resolve(VIDEO))
        info = res["info"]
        self.assertTrue(info["video_url"] and info["audio_url"], "no HD video+audio pair picked")
        self.assertTrue(info["qualities"], "empty quality menu")
        self.assertGreater(info["duration"], 200)
        for kind in ("video", "audio"):
            status, body = self.fetch(info[kind + "_url"])
            self.assertIn(status, (200, 206), "%s stream refused (HTTP %s), itag %s"
                          % (kind, status, info[kind + "_itag"]))
            self.assertGreater(len(body), 1000, kind + " stream sent no bytes")
        return info


class _Areas(_Live):
    """Every area that has a yt-dlp path, on one tier."""

    def test_resolve_plays(self):
        info = self.check_resolve()
        PICKS[self.TIER] = (info["video_itag"], info["audio_itag"])

    def test_video_info(self):
        info = self.ok(youfish.video_info(VIDEO))["info"]
        self.assertIn("Never Gonna Give You Up", info["title"])

    def test_search(self):
        items = self.ok(youfish.search(QUERY, n=10))["items"]
        self.assertGreaterEqual(len(items), 5)
        for it in items:
            self.assertRegex(it["id"], r"^[\w-]{11}$")
            self.assertTrue(it["title"])

    def test_search_channels(self):
        items = self.ok(youfish.search("Rick Astley", n=5, kind="channel"))["items"]
        self.assertTrue(any(i["id"].startswith("UC") for i in items), items[:2])

    def test_related(self):
        items = self.ok(youfish.related(VIDEO))["items"]
        self.assertGreaterEqual(len(items), 5)
        self.assertNotIn(VIDEO, [i["id"] for i in items[:1]])

    def test_channel_videos(self):
        res = self.ok(youfish.channel_videos(CHANNEL, n=10))
        self.assertGreaterEqual(len(res["items"]), 5)
        for it in res["items"]:
            self.assertRegex(it["id"], r"^[\w-]{11}$")

    def test_channel_avatar(self):
        res = self.ok(youfish.channel_avatar(CHANNEL))
        self.assertEqual(res["id"], CHANNEL)
        self.assertRegex(res["thumbnail"], r"^https://")

    def test_channel_playlists_and_one_playlist(self):
        pls = self.ok(youfish.channel_playlists(CHANNEL))["items"]
        self.assertTrue(pls, "no playlists")
        pl = self.ok(youfish.youtube_playlist(pls[0]["yt_id"], limit=20))
        self.assertTrue(pl["items"], "playlist %s came back empty" % pls[0]["yt_id"])

    def test_comments(self):
        res = self.ok(youfish.comments(VIDEO, limit=10, with_replies=False))
        self.assertGreaterEqual(len(res["comments"]), 5)
        self.assertTrue(all(c.get("text") for c in res["comments"]))


PICKS = {}                            # tier -> (video itag, audio itag) resolve picked


class Binary(_Areas, unittest.TestCase):
    TIER = "binary"


class InProcess(_Areas, unittest.TestCase):
    TIER = "inproc"

    def test_zz_same_picks_as_binary(self):
        """The warm in-process resolve maps the app's options by hand (_parse_extractor_args);
        given the same yt-dlp it must pick what the binary picks."""
        if "binary" not in PICKS or "inproc" not in PICKS:
            self.skipTest("needs both resolve checks to have passed")
        self.assertEqual(PICKS["inproc"], PICKS["binary"],
                         "(video itag, audio itag): in-process vs binary")


class PoToken(_Live, unittest.TestCase):
    """--pot only: the token path resolve falls back to when YouTube gates the token-free client
    (mweb + a PO token minted by the Deno sidecar), forced here by picking the client."""
    TIER = "binary"

    def setUp(self):
        if not OPTS["pot"]:
            self.skipTest("run with --pot")
        super().setUp()
        youfish.set_setting("player_client", "mweb")
        self.raw = []                           # the googlevideo URLs resolve handed the proxy
        self._proxied = real = youfish._proxied

        def proxied(url, *a, **kw):
            self.raw.append(url)
            return real(url, *a, **kw)
        youfish._proxied = proxied

    def tearDown(self):
        youfish._proxied = self._proxied
        youfish.set_setting("player_client", "")
        super().tearDown()

    def test_resolve_plays(self):
        self.check_resolve()
        self.assertTrue(any("pot=" in u for u in self.raw), "no PO token in the stream URLs")


class Lockstep(unittest.TestCase):
    def test_binary_and_zipapp_same_version(self):
        self.assertEqual(youfish.ytdlp_version(fresh=True),
                         youfish._zipapp_version(youfish._ytdlp_zipapp_path()))


class Download(_Live, unittest.TestCase):
    """The download is always the binary (with the PO-token plugin when set up)."""
    TIER = "binary"

    def test_audio_download(self):
        youfish.download(SHORT_VIDEO, "Me at the zoo", "audio")
        _vid, _kind, ok, msg = EVENTS.wait("download_done", 300,
                                           lambda a: a[0] == SHORT_VIDEO)
        if not ok:
            self.wall(msg)
            self.fail(msg)
        d = youfish.list_downloads()[0]
        self.assertEqual(d["id"], SHORT_VIDEO)
        self.assertGreater(os.path.getsize(d["path"]), 50000)


def _summary(res):
    lines = ["", "yt-dlp live: %d run, %d failed, %d errors, %d skipped"
             % (res.testsRun, len(res.failures), len(res.errors), len(res.skipped))]
    if WALLS:
        lines.append("YouTube refused %d check(s) as a bot / rate limit — INCONCLUSIVE, not a "
                     "failure (common from datacenter IPs; run from home to verify):" % len(WALLS))
        lines += ["  %s: %s" % (t.split(".", 1)[1], m[:140]) for t, m in WALLS]
    text = "\n".join(lines)
    print(text)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write("```\n%s\n```\n" % text.strip())


def main(argv):
    OPTS["pot"] = "--pot" in argv
    if "--dir" in argv:
        OPTS["dir"] = os.path.abspath(argv[argv.index("--dir") + 1])
    rest = [a for i, a in enumerate(argv)
            if not a.startswith("--") and not (i and argv[i - 1] == "--dir")]
    res = unittest.main(argv=[sys.argv[0]] + rest, exit=False, verbosity=2,
                        warnings="ignore").result      # the engine's daemon threads leak fds
    _summary(res)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
