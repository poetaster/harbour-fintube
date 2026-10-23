#!/usr/bin/env python3
"""The InnerTube parsers against real YouTube answers.

innertube.py is the one part nobody upstream maintains, so its parsers are checked against
answers recorded from YouTube (tests/fixtures/*.json.gz), not hand-written samples. The checks
assert what the app needs (ids, titles, durations, a continuation, ...), not exact values, so
re-recording doesn't mean rewriting tests.

    python3 tests/test_parsers.py            # offline, against the recordings (test.sh and CI)
    python3 tests/test_parsers.py --live     # the same checks against YouTube right now
    python3 tests/test_parsers.py --record   # re-record from YouTube (saved only if checks pass)

When the app starts falling back to yt-dlp ("InnerTube fast path … fell back" in diagnostics),
run --live: the failing check names the parser that no longer understands YouTube's answer. Fix
it, then --record so the offline tests follow the new layout.

Recordings are scrubbed: your public IP (googlevideo URLs carry it) is replaced by 203.0.113.1,
and --record refuses to save if it still finds it.
"""
import base64
import gzip
import json
import os
import re
import sys
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))
import innertube  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
FAKE_IP = "203.0.113.1"          # TEST-NET-3, reserved for documentation
MODE = "replay"                  # replay | live | record

RICK = "dQw4w9WgXcQ"                         # long-lived, comments + chapters-free, huge counts
RICK_CHANNEL = "UCuAXFkgsw1L7xaCfnd5JJOw"
SHORTS_CHANNEL = "UCX6OQ3DkcsbYNE6H8uQQuVA"  # a channel with a Shorts tab
LIVE_FILTER = "EgJAAQ%3D%3D"                 # search sp= filter: Live


# --- record / replay seam ------------------------------------------------------------------ #

def _key(method, url, body):
    p = urllib.parse.urlsplit(url)
    if method == "GET":                       # HLS manifests: signed one-off paths
        return "GET " + p.netloc
    try:
        b = json.loads(body.decode("utf-8")) if body else {}
        b.pop("context", None)
        b = json.dumps(b, sort_keys=True)
    except Exception:
        b = ""
    return "%s %s%s %s" % (method, p.netloc, p.path, b)


def _reset_caches():
    for c in (innertube._visitor, innertube._live_visitor):
        c["data"], c["ts"] = "", 0.0
    innertube._cid_cache.clear()


class _Recorder:
    def __init__(self, real):
        self.real, self.calls = real, {}

    def __call__(self, method, url, body=None, headers=None, timeout=10):
        status, data = self.real(method, url, body, headers, timeout)
        self.calls.setdefault(_key(method, url, body), []).append(
            {"status": status, "data": data.decode("utf-8", "replace")})
        return status, data


class _Player:
    def __init__(self, calls):
        self.calls = {k: list(v) for k, v in calls.items()}

    def __call__(self, method, url, body=None, headers=None, timeout=10):
        k = _key(method, url, body)
        if k not in self.calls:
            raise AssertionError("no recording for %s — re-record (--record)" % k[:160])
        q = self.calls[k]
        r = q.pop(0) if len(q) > 1 else q[0]
        return r["status"], r["data"].encode("utf-8")


def _fixture_path(name):
    return os.path.join(FIXTURES, name + ".json.gz")


def _public_ips(calls):
    ips = set()
    for rs in calls.values():
        for r in rs:
            t = urllib.parse.unquote(r["data"])
            ips.update(re.findall(r"[?&/]ip[=/]([0-9a-fA-F.:]+)", t))
    return {ip for ip in ips if ip and ip != FAKE_IP}


# Where the recording was made shows through more than the IP: YouTube adds a "nearest event"
# ticket shelf (a city near you) to music videos, and googlevideo URLs name the CDN cache node
# that serves your ISP and area (sn-xxxxxxxx). Neither is something a parser reads.
_LOCAL_SHELVES = {"ticketShelfRenderer", "ticketEventRenderer", "merchandiseShelfRenderer"}
_CDN_NODE = re.compile(r"(?:(?<=---)|(?<=%2C)|(?<![A-Za-z0-9_-]))sn-[a-z0-9]{7,10}(?:-[a-z0-9]{4})?"
                       r"(?![A-Za-z0-9_-])")


# The anonymous visitor id YouTube hands out (visitorData, base64 protobuf) carries the country it
# placed you in: field 6 holds {1: <two-letter code>}. It's only ever echoed back in a header, so
# ZZ is safe.
_VISITOR = re.compile(r"Cg[st][A-Za-z0-9_-]{20,}(?:%3D|=)*")


def _visitor_country(m):
    v = m.group(0)
    raw = urllib.parse.unquote(v)
    try:
        b = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        return v
    if not b.startswith(b"\n\x0b"):
        return v                                     # not a visitor id
    b2 = re.sub(rb"(2.\n\x02)[A-Z]{2}", rb"\1ZZ", b, count=1, flags=re.S)
    if b2 == b:
        return v
    out = base64.urlsafe_b64encode(b2).decode()
    if raw.endswith("=") or "%3D" in v:
        return out.replace("=", "%3D") if "%3D" in v else out
    return out.rstrip("=")


# YouTube Music's home names your country in its chart shelves ("Top 100 Music Videos <country>").
_CHART_COUNTRY = re.compile(r"(?:Trending 20|Top 100 Music Videos|Top 100 Songs|Top \d+ Songs|"
                            r"Daily Top Music Videos -|Weekly Top Songs -|Top Songs -) "
                            r"([A-Z][A-Za-z.' -]{2,30}?)(?=\\?\")")
FAKE_COUNTRY = "Countryland"


def _drop_local(o):
    """o without the location-dependent shelves (and list items that were only such a shelf)."""
    if isinstance(o, dict):
        return {k: _drop_local(v) for k, v in o.items() if k not in _LOCAL_SHELVES}
    if isinstance(o, list):
        return [_drop_local(x) for x in o
                if not (isinstance(x, str) and x in _LOCAL_SHELVES)   # preload name lists
                and not (isinstance(x, dict) and x and set(x) <= _LOCAL_SHELVES)]
    return o


def _scrub(calls):
    """Remove what says where the recording was made: the public IP, the nearby-events shelves,
    the CDN node names, the visitor id's country and the country in chart names. Raises instead
    of saving any of them."""
    ips = _public_ips(calls)
    nodes = {}

    def node(m):
        return nodes.setdefault(m.group(0), "sn-%08d" % len(nodes))
    countries = {m.group(1) for rs in calls.values() for r in rs
                 for m in _CHART_COUNTRY.finditer(r["data"])}
    countries -= {FAKE_COUNTRY, "Global", "Worldwide"}
    out = {}
    for key, rs in calls.items():
        for r in rs:
            for ip in ips:
                for form in (ip, urllib.parse.quote(ip, safe=""), urllib.parse.quote(ip)):
                    r["data"] = r["data"].replace(form, FAKE_IP)
            try:
                r["data"] = json.dumps(_drop_local(json.loads(r["data"])))
            except ValueError:
                pass                                  # not JSON (an RSS feed, a JS file)
            r["data"] = _VISITOR.sub(_visitor_country, _CDN_NODE.sub(node, r["data"]))
            for c in countries:
                r["data"] = re.sub(r"\b%s\b" % re.escape(c), FAKE_COUNTRY, r["data"])
        out[_CDN_NODE.sub(node, key)] = rs
    leftover = _public_ips(out)
    blob = json.dumps(out)
    if leftover or any(ip in blob for ip in ips) or any(n in blob for n in _LOCAL_SHELVES) \
            or any(n in blob for n in nodes) or any(c in blob for c in countries) \
            or any(_visitor_country(m) != m.group(0) for m in _VISITOR.finditer(blob)):
        raise RuntimeError("location scrub failed, not saving")
    return out


def run_scenario(name, fn):
    """Run fn() with the transport recorded, replayed or live, per MODE."""
    _reset_caches()
    real = innertube._transport
    if MODE == "replay":
        with gzip.open(_fixture_path(name), "rt") as f:
            innertube._transport = _Player(json.load(f)["calls"])
    elif MODE == "record":
        innertube._transport = _Recorder(real)
    try:
        out = fn()
    finally:
        rec, innertube._transport = innertube._transport, real
    return out, (rec.calls if MODE == "record" else None)


# --- scenarios: each returns what the checks look at ---------------------------------------- #

def sc_search():
    first = innertube.search("lofi hip hop")
    return {"first": first, "next": innertube.search(continuation=first["continuation"])}


def sc_watch_next():
    wn = innertube.watch_next(RICK)
    top = innertube.comments(wn["comments_token"])
    tok = next((c["replies_token"] for c in top["comments"] if c.get("replies_token")), None)
    return {"next": wn, "comments": top, "replies": innertube.comments(tok) if tok else None}


def sc_channel():
    cid = innertube.resolve_channel("@RickAstleyYT")
    first = innertube.channel_tab(cid, "videos")
    return {"cid": cid, "first": first, "next": innertube.continuation(first["continuation"]),
            "shorts": innertube.channel_tab(SHORTS_CHANNEL, "shorts")}


def sc_player():
    return innertube.player_info(RICK)


def sc_live():
    found = innertube.search("lofi radio", params=LIVE_FILTER)
    live = [i for i in found["items"] if i.get("live")]
    return {"search": found, "info": innertube.live_info(live[0]["id"]) if live else None}


SCENARIOS = [("search", sc_search), ("watch_next", sc_watch_next), ("channel", sc_channel),
             ("player", sc_player), ("live", sc_live)]


# --- checks ------------------------------------------------------------------------------- #

class _Base(unittest.TestCase):
    name = None
    _cache = {}

    def data(self):
        if self.name not in self._cache:
            out, rec = run_scenario(self.name, dict(SCENARIOS)[self.name])
            self._cache[self.name] = out
            if rec is not None:
                _RECORDED[self.name] = rec
        return self._cache[self.name]

    def assertVideos(self, items, n, need_duration=0.7):
        vids = [i for i in items if i.get("kind", "video") == "video"]
        self.assertGreaterEqual(len(vids), n, "too few videos parsed")
        for v in vids:
            self.assertRegex(v["id"], r"^[\w-]{11}$")
            self.assertTrue(v["title"] and v["title"] != "(untitled)", v)
        timed = [v for v in vids if v["duration"] > 0 or v["live"] or v["upcoming"]]
        self.assertGreaterEqual(len(timed), need_duration * len(vids), "durations missing")
        return vids


class Search(_Base):
    name = "search"

    def test_first_page(self):
        first = self.data()["first"]
        vids = self.assertVideos(first["items"], 10)
        self.assertTrue(first["continuation"])
        with_views = [v for v in vids if v["views"] > 0 or v["live"]]
        self.assertGreaterEqual(len(with_views), 0.7 * len(vids), "view counts missing")
        self.assertTrue(all(re.match(r"^UC[\w-]{22}$", v["channel_id"]) for v in vids
                            if v.get("channel_id")))
        self.assertGreaterEqual(sum(1 for v in vids if v["channel"]), 0.9 * len(vids))

    def test_next_page(self):
        self.assertVideos(self.data()["next"]["items"], 5)


class WatchNext(_Base):
    name = "watch_next"

    def test_related_and_counts(self):
        wn = self.data()["next"]
        self.assertVideos(wn["related"], 5)
        self.assertRegex(wn["upload_date"], r"^2009\d{4}$")
        self.assertGreater(wn["likes"], 1000000)
        self.assertGreater(wn["subscribers"], 1000000)
        self.assertTrue(wn["comments_token"])

    def test_comments_and_replies(self):
        top = self.data()["comments"]
        self.assertGreaterEqual(len(top["comments"]), 5)
        self.assertGreater(top["total"], 1000)
        for c in top["comments"]:
            self.assertTrue(c["author"] and c["text"] and c["id"], c)
        self.assertTrue(top["continuation"])
        self.assertTrue(any(c["replies_token"] for c in top["comments"]))
        replies = self.data()["replies"]
        self.assertTrue(replies and replies["comments"], "reply page parsed nothing")


class Channel(_Base):
    name = "channel"

    def test_handle_resolves(self):
        self.assertEqual(self.data()["cid"], RICK_CHANNEL)

    def test_videos_tab_and_header(self):
        first = self.data()["first"]
        self.assertVideos(first["items"], 10)
        self.assertTrue(first["continuation"])
        self.assertEqual(first["channel"]["id"], RICK_CHANNEL)
        self.assertIn("Rick Astley", first["channel"].get("name") or first["channel"].get("title", ""))
        self.assertVideos(self.data()["next"]["items"], 5)

    def test_shorts_tab(self):
        shorts = self.data()["shorts"]["items"]
        self.assertGreaterEqual(len(shorts), 5)
        self.assertTrue(all(s["short"] for s in shorts))
        self.assertTrue(all(re.match(r"^[\w-]{11}$", s["id"]) for s in shorts))


class Player(_Base):
    name = "player"

    def test_details(self):
        p = self.data()
        self.assertIn("Never Gonna Give You Up", p["title"])
        self.assertEqual(p["channel_id"], RICK_CHANNEL)
        self.assertTrue(200 < p["duration"] < 230)
        self.assertEqual(p["live_status"], "not_live")
        self.assertGreater(p["view_count"], 1000000000)

    def test_formats_the_app_can_play(self):
        fs = self.data()["formats"]
        audio = [f for f in fs if f.get("vcodec") == "none" and f.get("url")]
        video = [f for f in fs if f.get("acodec") == "none" and f.get("url")]
        self.assertTrue(audio, "no audio-only formats with a URL")
        self.assertTrue(any(f.get("acodec", "").startswith(("opus", "mp4a")) for f in audio))
        self.assertTrue(any((f.get("vcodec") or "").startswith("avc1") and f.get("height")
                            for f in video), "no H.264 video-only format")
        for f in audio + video:
            self.assertTrue(f.get("format_id") and f.get("ext"), f)
            self.assertIn("googlevideo.com", f["url"])

    def test_loudness(self):
        # The app's normalization figure: loudnessDb, or perceptual minus target when the JS-less
        # client leaves loudnessDb out (youfish._loudness_db).
        loud = self.data()["loudness"]
        num = lambda k: isinstance(loud.get(k), (int, float))
        self.assertTrue(num("db") or (num("perceptual_db") and num("target_lkfs")), loud)


class Live(_Base):
    name = "live"

    def test_live_filter_finds_live_streams(self):
        self.assertTrue(any(i.get("live") for i in self.data()["search"]["items"]))

    def test_running_stream_has_muxed_hls(self):
        info = self.data()["info"]
        self.assertTrue(info, "no live stream to check")
        self.assertTrue(info["is_live"])
        self.assertGreaterEqual(len(info["formats"]), 2)
        for f in info["formats"]:
            self.assertEqual(f["protocol"], "m3u8_native")
            self.assertTrue(f.get("height") and f.get("url"), f)


class ScrubLocation(unittest.TestCase):
    """What a recording would give away about where it was made, and that _scrub removes it."""
    def test_scrub(self):
        visitor = base64.urlsafe_b64encode(b"\n\x0bAbcdefGhijk(\xd5\xad\xe5\xd6\x062(\n\x02QQ"
                                           b"\x12\"\x12\x1e\x12\x1c").decode().replace("=", "%3D")
        answer = {
            "visitorData": visitor,
            "url": "https://rr6---sn-abcd1234-wxyz.googlevideo.com/videoplayback?ip=198.51.100.7"
                   "&mn=sn-abcd1234-wxyz%2Csn-efgh5678&itag=251",
            "contents": [{"ticketShelfRenderer": {"title": "Nearest event · Springfield"}},
                         {"videoRenderer": {"videoId": "dQw4w9WgXcQ"}}],
            "preloadMessageNames": ["ticketShelfRenderer", "videoRenderer"],
            "shelf": {"title": "Top 100 Music Videos Freedonia"}, "note": "Freedonia"}
        calls = _scrub({"POST x {}": [{"status": 200, "data": json.dumps(answer)}]})
        out = json.loads(calls["POST x {}"][0]["data"])
        blob = json.dumps(out)
        for gone in ("198.51.100.7", "abcd1234", "efgh5678", "Springfield", "ticketShelf",
                     "Freedonia"):
            self.assertNotIn(gone, blob)
        self.assertEqual(out["contents"], [{"videoRenderer": {"videoId": "dQw4w9WgXcQ"}}])
        self.assertIn("https://rr6---sn-00000000.googlevideo.com/videoplayback?ip=" + FAKE_IP, blob)
        self.assertEqual(out["shelf"]["title"], "Top 100 Music Videos " + FAKE_COUNTRY)
        v = urllib.parse.unquote(out["visitorData"])
        raw = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
        self.assertIn(b"\n\x02ZZ", raw)
        self.assertTrue(raw.startswith(b"\n\x0bAbcdefGhijk"))     # the id itself is untouched


class UploadDate(unittest.TestCase):
    """Month names parsed by hand: strptime's %b follows the phone's locale (Qt sets it)."""
    def test_forms(self):
        d = innertube._upload_date
        self.assertEqual(d("Oct 24, 2009"), "20091024")
        self.assertEqual(d("Streamed live on Oct 1, 2025"), "20251001")
        self.assertEqual(d("Premiered Mar 3, 2024"), "20240303")
        self.assertEqual(d("September 9, 2021"), "20210909")
        self.assertEqual(d("3 days ago"), "")
        self.assertEqual(d(""), "")
        self.assertEqual(d(None), "")


_RECORDED = {}


def main(argv):
    global MODE
    if "--live" in argv:
        MODE = "live"
    elif "--record" in argv:
        MODE = "record"
    res = unittest.main(argv=[sys.argv[0]] + [a for a in argv if not a.startswith("--")],
                        exit=False, verbosity=1).result
    if MODE == "record":
        if not res.wasSuccessful() and "--force" not in argv:
            print("Checks failed: recordings NOT saved (a parser needs updating first, or "
                  "--record --force to save anyway).")
            return 1
        os.makedirs(FIXTURES, exist_ok=True)
        for name, calls in _RECORDED.items():
            with gzip.open(_fixture_path(name), "wt") as f:
                json.dump({"recorded": time.strftime("%Y-%m-%d"), "calls": _scrub(calls)}, f)
        print("saved %d recordings to %s" % (len(_RECORDED), FIXTURES))
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
