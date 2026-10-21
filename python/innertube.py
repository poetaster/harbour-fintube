"""Hand-rolled InnerTube client — the NewPipe-style fast path in front of yt-dlp.

Why this exists: every channel listing, feed classification and resolve used to go through yt-dlp,
which on-device means either a frozen-binary spawn (~1.3s tax before any work) or an in-process
extraction that downloads + regex-parses the ~1 MB watch/channel HTML on a slow ARM core. NewPipe
is fast because it talks to InnerTube directly: one small JSON POST per question. This module does
the same for the few questions FinTube asks constantly:

  - channel_tab()     one `browse` POST → a channel tab's videos (title, length, views, age) + header
  - continuation()    the next page of that tab
  - resolve_channel() @handle / /c/ / /user/ URL → UC id (one `navigation/resolve_url` POST)
  - player_info()     one `player` POST (+ a cached visitor id) → a yt-dlp-SHAPED info dict, so
                      youfish's format selection runs on it unchanged

It is strictly a FAST PATH: every function raises on anything unexpected, and every caller in
youfish falls back to the yt-dlp path that existed before. YouTube reshapes these responses every
few months (videoRenderer → lockupViewModel in 2025), so the parsers accept both generations and
anything else simply fails over. Pure stdlib, Python 3.8-safe (SFOS <= 5.0 ships 3.8).

Network: requests go through a small keep-alive pool (one TLS handshake per host instead of one
per request — the handshake dominates a small POST on mobile). IPv4 forcing is youfish's job
(_force_ipv4 patches getaddrinfo process-wide); googlevideo URLs are bound to the requesting IP, so
the player call MUST go out on the same family the proxy later streams on.
"""

import gzip
import http.client
import json
import re
import threading
import time
import urllib.parse
import urllib.request

_HOST = "www.youtube.com"

# Browse/navigation client. clientVersion tracks yt-dlp's (refreshed from the imported zipapp by
# configure_from_ytdlp); YouTube accepts a months-old web version for these read-only endpoints.
_WEB = {"clientName": "WEB", "clientVersion": "2.20260708.00.00", "hl": "en", "gl": "US"}
_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# Player client: a JS-less client whose formats carry plain, n-param-free URLs that need no PO
# token (yt-dlp 2026.08's own JS-less default). configure_from_ytdlp() swaps in whatever the
# installed yt-dlp currently uses, so a YouTube-forced client change rides in with a yt-dlp update.
_PLAYER = {
    "name": "visionos",
    "number": 101,
    "context": {"clientName": "VISIONOS", "clientVersion": "1.02",
                "deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
                "userAgent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
                              "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"),
                "osName": "visionOS", "osVersion": "26.5.23O471"},
}
# Live client: a running livestream is HLS-only, and the JS-less player client's master playlist
# splits audio into an EXT-X-MEDIA group the player can't join; ANDROID_VR (also JS-less, and HLS
# needs no PO token) serves classic muxed variants (itags 91-96) GStreamer plays directly.
_LIVE = {
    "name": "android_vr",
    "number": 28,
    "context": {"clientName": "ANDROID_VR", "clientVersion": "1.65.10",
                "deviceMake": "Oculus", "deviceModel": "Quest 3", "androidSdkVersion": 32,
                "userAgent": ("com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; "
                              "Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"),
                "osName": "Android", "osVersion": "12L"},
}
_cfg_lock = threading.Lock()

# Channel tab `params` (protobuf, as sent by the web app).
TAB_PARAMS = {
    "videos": "EgZ2aWRlb3PyBgQKAjoA",
    "shorts": "EgZzaG9ydHPyBgUKA5oBAA%3D%3D",
    "streams": "EgdzdHJlYW1z8gYECgJ6AA%3D%3D",
}


class InnertubeError(Exception):
    pass


class Unplayable(InnertubeError):
    """The player said no (login/age/bot wall, private, live-only, ...). `bot` = a bot check, the
    one failure that says the CLIENT is burned rather than this video being restricted."""
    def __init__(self, msg, bot=False):
        InnertubeError.__init__(self, msg)
        self.bot = bot


# --------------------------------------------------------------------------- #
# Transport: keep-alive HTTPS pool.
# --------------------------------------------------------------------------- #

_POOL_IDLE_MAX = 45.0        # drop a parked connection older than this (servers close idle ones)
_POOL_PER_HOST = 8
_pool = {}                   # host -> [(conn, parked_at), ...]
_pool_lock = threading.Lock()


def _get_conn(host, timeout):
    now = time.time()
    with _pool_lock:
        lst = _pool.get(host) or []
        while lst:
            conn, ts = lst.pop()
            if now - ts < _POOL_IDLE_MAX:
                try:
                    if conn.sock is not None:
                        conn.sock.settimeout(timeout)
                except Exception:
                    pass
                return conn, True
            try:
                conn.close()
            except Exception:
                pass
    return http.client.HTTPSConnection(host, timeout=timeout), False


def _put_conn(host, conn):
    with _pool_lock:
        lst = _pool.setdefault(host, [])
        if len(lst) < _POOL_PER_HOST:
            lst.append((conn, time.time()))
            return
    try:
        conn.close()
    except Exception:
        pass


def request(method, url, body=None, headers=None, timeout=10):
    """One HTTP request on the keep-alive pool → (status, body bytes, response headers). The body
    comes back gunzipped and the headers as an http.client.HTTPMessage (.get / .get_all). Any
    status is returned, not raised, and redirects are not followed. A reused keep-alive connection
    that turns out to be dead is retried once on a fresh one. An env proxy (rare on a phone)
    disables pooling and goes through urllib, which honours it. Public so other callers on the
    same hosts (FinTune's ytm, the subscription feed's RSS) skip a TLS handshake per request too."""
    p = urllib.parse.urlsplit(url)
    hdrs = dict(headers or {})
    hdrs.setdefault("Accept-Encoding", "gzip")
    if urllib.request.getproxies().get(p.scheme):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data, status, rh = r.read(), r.status, r.headers
        except urllib.error.HTTPError as ex:
            data, status, rh = ex.read() or b"", ex.code, ex.headers
        enc = rh.get("Content-Encoding", "") if rh is not None else ""
        return status, (gzip.decompress(data) if enc == "gzip" and data else data), rh
    path = p.path + ("?" + p.query if p.query else "")
    for attempt in (0, 1):
        conn, reused = _get_conn(p.netloc, timeout)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            if reused and attempt == 0:
                continue                      # stale keep-alive socket: one retry on a fresh one
            raise
        if resp.will_close:
            conn.close()
        else:
            _put_conn(p.netloc, conn)
        if (resp.getheader("Content-Encoding") or "") == "gzip" and data:
            data = gzip.decompress(data)
        return resp.status, data, resp.msg
    raise InnertubeError("unreachable")


def _transport(method, url, body=None, headers=None, timeout=10):
    """request() without the headers: (status, body bytes). The one seam this module's own calls
    go through, so tests can swap it."""
    status, data, _ = request(method, url, body, headers, timeout)
    return status, data


def _post(endpoint, body, context_client, ua, client_number, visitor=None, timeout=10,
          player=False):
    ctx = {"client": dict(context_client)}
    ctx["client"].setdefault("hl", "en")
    if player:
        # Player-client calls mirror yt-dlp's request byte for byte (hl + UTC, no gl, visitor id in
        # the header only): the stream URLs they mint are the ones googlevideo then has to accept,
        # so this is the one request shape that must not drift from the known-good reference.
        ctx["client"].update({"timeZone": "UTC", "utcOffsetMinutes": 0})
    else:
        ctx["client"].setdefault("gl", "US")
        if visitor:
            ctx["client"]["visitorData"] = visitor
    payload = dict(body)
    payload["context"] = ctx
    headers = {
        "Content-Type": "application/json",
        "User-Agent": ua,
        "Origin": "https://www.youtube.com",
        "X-YouTube-Client-Name": str(client_number),
        "X-YouTube-Client-Version": context_client.get("clientVersion", ""),
    }
    if visitor:
        headers["X-Goog-Visitor-Id"] = visitor
    status, data = _transport(
        "POST", "https://%s/youtubei/v1/%s?prettyPrint=false" % (_HOST, endpoint),
        body=json.dumps(payload).encode("utf-8"), headers=headers, timeout=timeout)
    if status != 200:
        raise InnertubeError("%s HTTP %s" % (endpoint, status))
    try:
        out = json.loads(data.decode("utf-8", "replace"))
    except Exception as ex:
        raise InnertubeError("%s: bad JSON (%s)" % (endpoint, ex))
    if not isinstance(out, dict):
        raise InnertubeError("%s: unexpected response" % endpoint)
    return out


def _web_post(endpoint, body, timeout=10):
    with _cfg_lock:
        web = dict(_WEB)
    return _post(endpoint, body, web, _WEB_UA, 1, timeout=timeout)


# --------------------------------------------------------------------------- #
# Client config: follow the installed yt-dlp.
# --------------------------------------------------------------------------- #

def configure_from_ytdlp(mod):
    """Adopt the WEB clientVersion and the JS-less default player client from an imported yt_dlp
    module. Best-effort and validating: a client that needs the JS player or a GVS PO token can't
    work without yt-dlp's machinery, so it's never adopted (the built-in default stays). Returns the
    player client name in use."""
    try:
        from yt_dlp.extractor.youtube import _base as ytb      # noqa: F401 (zipapp on sys.path)
        clients = ytb.INNERTUBE_CLIENTS
    except Exception:
        try:
            clients = mod.extractor.youtube._base.INNERTUBE_CLIENTS
        except Exception:
            return _PLAYER["name"]
    try:
        web = clients.get("web", {}).get("INNERTUBE_CONTEXT", {}).get("client", {})
        if web.get("clientVersion"):
            with _cfg_lock:
                _WEB["clientVersion"] = web["clientVersion"]
    except Exception:
        pass
    try:
        from yt_dlp.extractor.youtube._video import YoutubeIE
        names = list(getattr(YoutubeIE, "_DEFAULT_JSLESS_CLIENTS", ()) or ())
    except Exception:
        names = []
    for name in names:
        cfg = clients.get(name) or {}
        if cfg.get("REQUIRE_JS_PLAYER", True) or cfg.get("REQUIRE_AUTH"):
            continue
        if cfg.get("PLAYER_PARAMS") or cfg.get("INNERTUBE_HOST", _HOST) != _HOST:
            continue
        try:
            pol = (cfg.get("GVS_PO_TOKEN_POLICY") or {})
            if any(getattr(v, "required", False) for v in pol.values()):
                continue
        except Exception:
            continue
        ctx = dict(((cfg.get("INNERTUBE_CONTEXT") or {}).get("client")) or {})
        num = cfg.get("INNERTUBE_CONTEXT_CLIENT_NAME")
        if not (ctx.get("clientName") and ctx.get("clientVersion") and ctx.get("userAgent") and num):
            continue
        ctx.pop("hl", None)
        with _cfg_lock:
            changed = _PLAYER["name"] != name or _PLAYER["context"] != ctx
            if changed:
                _PLAYER.update({"name": name, "number": int(num), "context": ctx})
        if changed:                       # visitor ids are per-client (lock order: visitor → cfg)
            with _visitor_lock:
                _visitor["data"], _visitor["ts"] = "", 0.0
        break
    try:
        cfg = clients.get(_LIVE["name"]) or {}
        ctx = dict(((cfg.get("INNERTUBE_CONTEXT") or {}).get("client")) or {})
        num = cfg.get("INNERTUBE_CONTEXT_CLIENT_NAME")
        if ctx.get("clientName") and ctx.get("clientVersion") and ctx.get("userAgent") and num \
                and not cfg.get("REQUIRE_JS_PLAYER", True):
            ctx.pop("hl", None)
            with _cfg_lock:
                changed = _LIVE["context"] != ctx
                if changed:
                    _LIVE.update({"number": int(num), "context": ctx})
            if changed:
                with _visitor_lock:
                    _live_visitor["data"], _live_visitor["ts"] = "", 0.0
    except Exception:
        pass
    return _PLAYER["name"]


def player_client():
    """(name, clientName as it appears in googlevideo's `c=` URL param, User-Agent)."""
    with _cfg_lock:
        return (_PLAYER["name"], _PLAYER["context"].get("clientName", ""),
                _PLAYER["context"].get("userAgent", ""))


# --------------------------------------------------------------------------- #
# Visitor id: the player call is bot-walled without one. One tiny POST, reused for hours (NewPipe
# does the same), so a resolve is normally a SINGLE request.
# --------------------------------------------------------------------------- #

_VISITOR_TTL = 6 * 3600
_visitor = {"data": "", "ts": 0.0}           # for _PLAYER
_live_visitor = {"data": "", "ts": 0.0}      # for _LIVE
_visitor_lock = threading.Lock()


def _mint_visitor(cache, cfg, refresh, timeout):
    with _visitor_lock:
        if not refresh and cache["data"] and time.time() - cache["ts"] < _VISITOR_TTL:
            return cache["data"]
        with _cfg_lock:
            ctx, num = dict(cfg["context"]), cfg["number"]
        res = _post("visitor_id", {}, ctx, ctx.get("userAgent", ""), num, timeout=timeout,
                    player=True)
        vd = (res.get("responseContext") or {}).get("visitorData") or ""
        if not vd:
            raise InnertubeError("no visitorData")
        cache["data"], cache["ts"] = vd, time.time()
        return vd


def visitor_data(refresh=False, timeout=8):
    return _mint_visitor(_visitor, _PLAYER, refresh, timeout)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

def text(o):
    """InnerTube text in any of its shapes: {"simpleText"}, {"runs": [...]}, {"content"} or str."""
    if isinstance(o, str):
        return o
    if not isinstance(o, dict):
        return ""
    if isinstance(o.get("simpleText"), str):
        return o["simpleText"]
    if isinstance(o.get("content"), str):
        return o["content"]
    runs = o.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return ""


_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "THOUSAND": 1e3, "MILLION": 1e6, "BILLION": 1e9}


def parse_count(s):
    """'4.8M views' / '1,234 views' / '21.4M subscribers' / '1.8K videos' / 'No views' → int."""
    s = (s or "").strip()
    m = re.search(r"([\d][\d,.]*)\s*(thousand|million|billion|[KMB])?\b", s, re.I)
    if not m:
        return 0
    num = m.group(1).replace(",", "")
    try:
        v = float(num)
    except ValueError:
        return 0
    if m.group(2):
        v *= _MULT[m.group(2).upper()]
    return int(v)


def parse_duration(s):
    """'11:47' → 707, '1:02:03' → 3723; anything else → 0."""
    s = (s or "").strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){1,2}", s):
        return 0
    secs = 0
    for part in s.split(":"):
        secs = secs * 60 + int(part)
    return secs


_AGO_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
              "month": 30 * 86400, "year": 365 * 86400}


def parse_ago(s):
    """'19 hours ago' / 'Streamed 2 days ago' / 'Premiered 3 weeks ago' → seconds ago, else None."""
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", s or "", re.I)
    if not m:
        return None
    return int(m.group(1)) * _AGO_UNITS[m.group(2).lower()]


def _walk(o, key, out, depth=0):
    """Collect every dict value stored under `key` anywhere inside `o` (bounded depth)."""
    if depth > 14:
        return out
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key and isinstance(v, dict):
                out.append(v)
            if isinstance(v, (dict, list)):
                _walk(v, key, out, depth + 1)
    elif isinstance(o, list):
        for v in o:
            if isinstance(v, (dict, list)):
                _walk(v, key, out, depth + 1)
    return out


# --------------------------------------------------------------------------- #
# Channel tabs
# --------------------------------------------------------------------------- #

def _item(vid, title, duration=0, views=0, posted="", live=False, upcoming=False,
          members=False, short=False, channel="", channel_id=""):
    return {"id": vid, "title": title or "(untitled)", "duration": int(duration or 0),
            "views": int(views or 0), "posted": posted or "", "ago": parse_ago(posted),
            "live": bool(live), "upcoming": bool(upcoming), "members": bool(members),
            "short": bool(short), "channel": channel or "", "channel_id": channel_id or ""}


def _browse_id(o):
    """The first channel (UC…) browseId linked anywhere inside `o`, or ""."""
    for be in _walk(o, "browseEndpoint", []):
        bid = str(be.get("browseId") or "")
        if bid.startswith("UC"):
            return bid
    return ""


def _from_lockup(lv):
    if lv.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
        return None                                    # playlists / mixes / podcasts
    vid = lv.get("contentId") or ""
    if not re.fullmatch(r"[\w-]{11}", vid):
        return None
    md = (lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}
    title = text(md.get("title"))
    views, posted, live, upcoming, channel = 0, "", False, False, ""
    rows = (((md.get("metadata") or {}).get("contentMetadataViewModel") or {})
            .get("metadataRows") or [])
    for row in rows:
        for part in (row.get("metadataParts") or []):
            t = text(part.get("text"))
            # Watch-next/search lockups abbreviate ("6.3M", "1mo ago") and spell it out only in the
            # accessibility label — classify (and read dates) from the label, counts from the text.
            label = part.get("accessibilityLabel") or t
            low = label.lower()
            if "watching" in low:
                live = True
                views = views or parse_count(t)
            elif "view" in low:
                views = parse_count(t)
            elif " ago" in low:
                posted = label
            elif low.startswith(("scheduled for", "premieres")):
                upcoming = True
            elif t and not (channel or views or posted):
                channel = t                  # the byline row leads when the listing mixes channels
    duration = 0
    for badge in _walk(lv.get("contentImage") or {}, "thumbnailBadgeViewModel", []):
        bt = (badge.get("text") or "").strip()
        style = badge.get("badgeStyle") or ""
        if "LIVE" in style or bt.upper() == "LIVE":
            live = True
        elif bt.upper() in ("UPCOMING", "PREMIERE"):
            upcoming = True
        elif parse_duration(bt):
            duration = parse_duration(bt)
    members = False
    for b in _walk(md, "badgeViewModel", []):
        if "member" in (str(b.get("badgeText") or "") + str(b.get("badgeStyle") or "")).lower():
            members = True
    return _item(vid, title, duration, views, posted, live, upcoming, members,
                 channel=channel, channel_id=_browse_id(md) if channel else "")


def _from_video_renderer(vr):
    vid = vr.get("videoId") or ""
    if not re.fullmatch(r"[\w-]{11}", vid):
        return None
    live = upcoming = members = False
    for ov in (vr.get("thumbnailOverlays") or []):
        st = (ov.get("thumbnailOverlayTimeStatusRenderer") or {}).get("style") or ""
        if st == "LIVE":
            live = True
        elif st == "UPCOMING":
            upcoming = True
    for b in (vr.get("badges") or []):
        st = ((b.get("metadataBadgeRenderer") or {}).get("style") or "")
        if "MEMBERS" in st:
            members = True
        elif "LIVE" in st:
            live = True
    if vr.get("upcomingEventData"):
        upcoming = True
    vc = text(vr.get("viewCountText"))
    byline = vr.get("ownerText") or vr.get("longBylineText") or vr.get("shortBylineText") or {}
    return _item(vid, text(vr.get("title")), parse_duration(text(vr.get("lengthText"))),
                 parse_count(vc) if "no views" not in vc.lower() else 0,
                 text(vr.get("publishedTimeText")), live, upcoming, members,
                 channel=text(byline), channel_id=_browse_id(byline))


def _from_shorts_lockup(sl):
    vid = ((((sl.get("onTap") or {}).get("innertubeCommand") or {})
            .get("reelWatchEndpoint") or {}).get("videoId")) or ""
    if not vid:
        m = re.search(r"([\w-]{11})$", sl.get("entityId") or "")
        vid = m.group(1) if m else ""
    if not re.fullmatch(r"[\w-]{11}", vid):
        return None
    om = sl.get("overlayMetadata") or {}
    return _item(vid, text(om.get("primaryText")), 0, parse_count(text(om.get("secondaryText"))),
                 short=True)


def _from_reel(rr):
    vid = rr.get("videoId") or ""
    if not re.fullmatch(r"[\w-]{11}", vid):
        return None
    return _item(vid, text(rr.get("headline")), 0, parse_count(text(rr.get("viewCountText"))),
                 short=True)


def _parse_items(contents):
    """A list of grid/list entries → (items, continuation token or None)."""
    items, cont = [], None
    for c in contents or []:
        if not isinstance(c, dict):
            continue
        inner = c
        if "richItemRenderer" in c:
            inner = (c["richItemRenderer"] or {}).get("content") or {}
        it = None
        if "lockupViewModel" in inner:
            it = _from_lockup(inner["lockupViewModel"] or {})
        elif "videoRenderer" in inner:
            it = _from_video_renderer(inner["videoRenderer"] or {})
        elif "gridVideoRenderer" in inner:
            it = _from_video_renderer(inner["gridVideoRenderer"] or {})
        elif "shortsLockupViewModel" in inner:
            it = _from_shorts_lockup(inner["shortsLockupViewModel"] or {})
        elif "reelItemRenderer" in inner:
            it = _from_reel(inner["reelItemRenderer"] or {})
        elif "continuationItemRenderer" in c:
            cont = ((((c["continuationItemRenderer"] or {}).get("continuationEndpoint") or {})
                     .get("continuationCommand") or {}).get("token")) or cont
        if it:
            items.append(it)
    return items, cont


def _channel_header(res):
    cm = (res.get("metadata") or {}).get("channelMetadataRenderer") or {}
    ch = {"id": cm.get("externalId") or "", "name": cm.get("title") or "",
          "url": cm.get("channelUrl") or "", "handle_url": cm.get("vanityChannelUrl") or "",
          "avatars": list(((cm.get("avatar") or {}).get("thumbnails")) or []),
          "subscribers": 0, "video_count": 0}
    hdr = res.get("header") or {}
    phv = ((hdr.get("pageHeaderRenderer") or {}).get("content") or {}).get("pageHeaderViewModel")
    if isinstance(phv, dict):
        rows = (((phv.get("metadata") or {}).get("contentMetadataViewModel") or {})
                .get("metadataRows") or [])
        for row in rows:
            for part in (row.get("metadataParts") or []):
                t = text(part.get("text")).lower()
                if "subscriber" in t:
                    ch["subscribers"] = parse_count(t)
                elif "video" in t:
                    ch["video_count"] = parse_count(t)
        if not ch["avatars"]:
            for img in _walk(phv.get("image") or {}, "image", []):
                if isinstance(img.get("sources"), list):
                    ch["avatars"] = list(img["sources"])
    c4 = hdr.get("c4TabbedHeaderRenderer")
    if isinstance(c4, dict):
        ch["subscribers"] = ch["subscribers"] or parse_count(text(c4.get("subscriberCountText")))
        ch["video_count"] = ch["video_count"] or parse_count(text(c4.get("videosCountText")))
        ch["name"] = ch["name"] or c4.get("title") or ""
        ch["id"] = ch["id"] or c4.get("channelId") or ""
        if not ch["avatars"]:
            ch["avatars"] = list(((c4.get("avatar") or {}).get("thumbnails")) or [])
    return ch


def channel_tab(cid, tab="videos", timeout=10):
    """First page of a channel tab → {"items", "continuation", "channel"}. Raises InnertubeError
    when the channel has no such tab (YouTube then answers with Home selected) or the shape is
    unrecognised — the caller falls back to yt-dlp."""
    if not re.fullmatch(r"UC[\w-]{22}", cid or ""):
        raise InnertubeError("not a channel id: %r" % cid)
    res = _web_post("browse", {"browseId": cid,
                               "params": urllib.parse.unquote(TAB_PARAMS[tab])}, timeout=timeout)
    tabs = (((res.get("contents") or {}).get("twoColumnBrowseResultsRenderer") or {})
            .get("tabs")) or []
    grid = None
    for t in tabs:
        tr = (t or {}).get("tabRenderer") or {}
        if tr.get("selected"):
            grid = (tr.get("content") or {}).get("richGridRenderer")
            break
    if not isinstance(grid, dict):
        raise InnertubeError("channel %s has no %s tab" % (cid, tab))
    items, cont = _parse_items(grid.get("contents"))
    ch = _channel_header(res)
    ch["id"] = ch["id"] or cid
    return {"items": items, "continuation": cont, "channel": ch}


def continuation(token, timeout=10):
    """Next page of a tab → {"items", "continuation"}."""
    res = _web_post("browse", {"continuation": token}, timeout=timeout)
    contents = []
    for a in (res.get("onResponseReceivedActions") or []) + \
             (res.get("onResponseReceivedEndpoints") or []):
        for key in ("appendContinuationItemsAction", "reloadContinuationItemsCommand"):
            ci = (a.get(key) or {}).get("continuationItems")
            if isinstance(ci, list):
                contents.extend(ci)
    if not contents:
        raise InnertubeError("empty continuation")
    items, cont = _parse_items(contents)
    return {"items": items, "continuation": cont}


_cid_cache = {}
_cid_lock = threading.Lock()


def resolve_channel(ref, timeout=8):
    """A channel reference (UC id, /channel/UC… URL, @handle, /c/…, /user/… URL) → UC id. Cached
    for the session. Raises when it can't be resolved."""
    ref = (ref or "").strip()
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return ref
    m = re.search(r"/channel/(UC[\w-]{22})", ref)
    if m:
        return m.group(1)
    if ref.startswith("@"):
        ref = "https://www.youtube.com/" + ref
    if "://" not in ref:
        raise InnertubeError("unrecognised channel ref %r" % ref)
    key = re.sub(r"/(videos|shorts|streams|featured|playlists|about)/?$", "", ref.rstrip("/"))
    with _cid_lock:
        if key in _cid_cache:
            return _cid_cache[key]
    res = _web_post("navigation/resolve_url", {"url": key}, timeout=timeout)
    bid = (((res.get("endpoint") or {}).get("browseEndpoint") or {}).get("browseId")) or ""
    if not re.fullmatch(r"UC[\w-]{22}", bid):
        raise InnertubeError("resolve_url gave no channel for %r" % ref)
    with _cid_lock:
        if len(_cid_cache) > 256:
            _cid_cache.clear()
        _cid_cache[key] = bid
    return bid


# --------------------------------------------------------------------------- #
# Player → yt-dlp-shaped info
# --------------------------------------------------------------------------- #

_MIME_RE = re.compile(r'(\w+)/([\w.+-]+)(?:;\s*codecs="([^"]*)")?')
_EXT = {("video", "mp4"): "mp4", ("video", "webm"): "webm", ("audio", "mp4"): "m4a",
        ("audio", "webm"): "webm", ("video", "3gpp"): "3gp"}


def _lang_pref(track, orig_lang, default_lang):
    """yt-dlp's get_language_code_and_preference + its original/default post-pass, mirrored so the
    dict looks exactly like the one yt-dlp would have produced for the same stream."""
    name = (track.get("displayName") or "").lower()
    code = (track.get("id") or "").split(".")[0] or None
    if "descriptive" in name:
        return (code + "-desc" if code else "desc"), -10
    if "original" in name or (code and code == orig_lang):
        return code, 10
    if track.get("audioIsDefault") or (code and code == default_lang):
        return code, 5
    return code, -1


def _formats(streaming, ua):
    """streamingData → list of yt-dlp-shaped format dicts. Only entries with a plain `url` (a
    signatureCipher/`n` challenge needs the JS player → that's yt-dlp's job, so such a response
    simply yields fewer formats and the caller falls back). Duplicate itags (dubbed audio: one
    itag per language) get a deterministic `-<track id>` suffix; DRC variants `-drc` like yt-dlp."""
    raw = [f for f in ((streaming.get("formats") or []) + (streaming.get("adaptiveFormats") or []))
           if isinstance(f, dict) and f.get("url") and f.get("itag") is not None]
    raw = [f for f in raw if f.get("type") != "FORMAT_STREAM_TYPE_OTF" and not f.get("drmFamilies")
           and not f.get("targetDurationSec")]
    orig_lang = default_lang = None
    for f in raw:
        tr = f.get("audioTrack") or {}
        code = (tr.get("id") or "").split(".")[0] or None
        if code and "original" in (tr.get("displayName") or "").lower() and not orig_lang:
            orig_lang = code
        if code and tr.get("audioIsDefault") and not default_lang:
            default_lang = code
    counts = {}
    for f in raw:
        key = (str(f["itag"]), bool(f.get("isDrc")))
        counts[key] = counts.get(key, 0) + 1
    out, seen = [], set()
    for f in raw:
        itag = str(f["itag"])
        tr = f.get("audioTrack") or {}
        fid = itag + ("-drc" if f.get("isDrc") else "")
        if counts[(itag, bool(f.get("isDrc")))] > 1 and tr.get("id"):
            fid += "-" + re.sub(r"[^\w.-]", "", tr["id"])
        if fid in seen:
            continue
        seen.add(fid)
        m = _MIME_RE.match(f.get("mimeType") or "")
        kind, sub, codecs = (m.group(1), m.group(2), m.group(3) or "") if m else ("", "", "")
        cl = [c.strip() for c in codecs.split(",") if c.strip()]
        if kind == "audio":
            vcodec, acodec = "none", (cl[0] if cl else "")
        elif len(cl) >= 2:
            vcodec, acodec = cl[0], cl[1]
        else:
            vcodec, acodec = (cl[0] if cl else ""), "none"
        lang, lpref = (None, None)
        if tr:
            lang, lpref = _lang_pref(tr, orig_lang, default_lang)
        quality = f.get("qualityLabel") or (f.get("audioQuality") or "").replace(
            "AUDIO_QUALITY_", "").lower()
        note_lang = " ".join(x for x in (tr.get("displayName") or "",
                                         "(default)" if tr.get("audioIsDefault") else "") if x)
        note = ", ".join(x for x in (note_lang, quality, "DRC" if f.get("isDrc") else "") if x)
        if lang and lang == orig_lang and "original" not in note.lower():
            note += " (original)"
        if itag in ("616", "235"):
            note += " Premium"
        tbr = f.get("averageBitrate") or f.get("bitrate")
        fps = f.get("fps")
        try:
            clen = int(f.get("contentLength")) if f.get("contentLength") else None
        except (TypeError, ValueError):
            clen = None
        out.append({
            "format_id": fid,
            "url": f["url"],
            "protocol": "https",
            "ext": _EXT.get((kind, sub), sub),
            "vcodec": vcodec,
            "acodec": acodec,
            "width": f.get("width"),
            "height": f.get("height"),
            "fps": fps if fps and fps > 1 else None,
            "tbr": (tbr / 1000.0) if tbr else None,
            "abr": (tbr / 1000.0) if (tbr and kind == "audio") else None,
            "asr": int(f["audioSampleRate"]) if str(f.get("audioSampleRate") or "").isdigit() else None,
            "audio_channels": f.get("audioChannels"),
            "filesize": clen,
            "language": lang,
            "language_preference": lpref,
            "format_note": note,
            "http_headers": {"User-Agent": ua},
        })
    return out


def _captions(pr):
    """captions → (subtitles, automatic_captions) in yt-dlp's {lang: [{ext, url, name}]} shape,
    json3 only (all the player parses). Translations hang off the ASR track (else the first)."""
    rend = ((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer")) or {}
    tracks = rend.get("captionTracks") or []
    subs, auto = {}, {}
    base_for_tl = None
    for t in tracks:
        url = t.get("baseUrl") or ""
        code = t.get("languageCode") or ""
        if not url or not code:
            continue
        url = re.sub(r"&fmt=[^&]*", "", url)
        if url.startswith("/"):
            url = "https://www.youtube.com" + url
        entry = [{"ext": "json3", "url": url + "&fmt=json3", "name": text(t.get("name")) or code}]
        if t.get("kind") == "asr":
            auto.setdefault(code, entry)
            base_for_tl = url
        else:
            subs.setdefault(code, entry)
            if base_for_tl is None:
                base_for_tl = url
    if base_for_tl:
        for tl in (rend.get("translationLanguages") or []):
            code = tl.get("languageCode") or ""
            if code and code not in auto:
                auto[code] = [{"ext": "json3",
                               "url": base_for_tl + "&fmt=json3&tlang=" + urllib.parse.quote(code),
                               "name": text(tl.get("languageName")) or code}]
    return subs, auto


_TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})"
# Up to 8 non-word characters around the timestamp: "⌨️ (0:00) Intro" is keycap + VS16 + space +
# paren before the digits (freeCodeCamp's whole catalogue), and ZWJ emoji are longer still.
_CH_START = re.compile(r"^\W{0,8}" + _TS + r"\W{0,8}\s*(.+?)\s*$")
_CH_END = re.compile(r"^\s*(.+?)\s*\W{0,8}" + _TS + r"\W{0,8}$")


def chapters_from_description(desc, duration=0):
    """Creator chapters as YouTube defines them: description lines carrying a timestamp, the first
    at 0:00, at least three, strictly increasing, each >= 10s. Anything else → []."""
    out = []
    for line in (desc or "").splitlines():
        line = line.strip()
        m = _CH_START.match(line)
        if m:
            h, mi, s, title = m.groups()
        else:
            m = _CH_END.match(line)
            if not m:
                continue
            title, h, mi, s = m.groups()
        start = int(h or 0) * 3600 + int(mi) * 60 + int(s)
        title = title.strip(" -–—:|·•")
        if not title:
            continue
        out.append({"start_time": start, "title": title})
    if len(out) < 3 or out[0]["start_time"] != 0:
        return []
    for a, b in zip(out, out[1:]):
        if b["start_time"] - a["start_time"] < 10:
            return []
    if duration and out[-1]["start_time"] >= duration:
        return []
    return out


def player_info(video_id, timeout=10):
    """One InnerTube `player` request → a dict shaped like yt-dlp's --dump-single-json for the
    fields youfish reads (formats, title, uploader, channel_id/url, description, duration, live
    flags, chapters, subtitles, automatic_captions). Raises Unplayable / InnertubeError — the
    caller falls back to yt-dlp, which handles everything this deliberately doesn't (live/HLS,
    restricted content, ciphered formats)."""
    if not re.fullmatch(r"[\w-]{11}", video_id or ""):
        raise InnertubeError("bad video id")
    with _cfg_lock:
        ctx, num = dict(_PLAYER["context"]), _PLAYER["number"]
    ua = ctx.get("userAgent", "")
    body = {"videoId": video_id, "contentCheckOk": True, "racyCheckOk": True,
            "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}}}
    vd = visitor_data(timeout=timeout)
    pr = _post("player", body, ctx, ua, num, visitor=vd, timeout=timeout, player=True)
    det = _playable_details(pr, video_id, _visitor)
    streaming = pr.get("streamingData") or {}
    is_live = bool(det.get("isLive"))
    upcoming = bool(det.get("isUpcoming"))
    try:
        duration = int(det.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        duration = 0
    cid = det.get("channelId") or ""
    subs, auto = _captions(pr)
    desc = det.get("shortDescription") or ""
    return {
        "id": video_id,
        "title": det.get("title") or "",
        "uploader": det.get("author") or "",
        "channel": det.get("author") or "",
        "channel_id": cid,
        "channel_url": ("https://www.youtube.com/channel/" + cid) if cid else "",
        "description": desc,
        "duration": duration,
        "view_count": parse_count(det.get("viewCount") or ""),
        "is_live": is_live,
        "live_status": "is_live" if is_live else ("is_upcoming" if upcoming else "not_live"),
        "has_hls": bool(streaming.get("hlsManifestUrl")),
        "formats": _formats(streaming, ua),
        "subtitles": subs,
        "automatic_captions": auto,
        "chapters": chapters_from_description(desc, duration),
        "loudness": _loudness(pr),
    }


def _playable_details(pr, video_id, visitor_cache):
    """videoDetails of an OK player response; raises Unplayable (bot walls burn the visitor id
    so the next call mints a fresh one) or InnertubeError for an answer about another video."""
    ps = pr.get("playabilityStatus") or {}
    status = ps.get("status") or ""
    if status != "OK":
        reason = ps.get("reason") or text(((ps.get("errorScreen") or {})
                                           .get("playerErrorMessageRenderer") or {}).get("reason")) \
            or status or "unplayable"
        if "bot" in reason.lower():
            with _visitor_lock:
                visitor_cache["data"] = ""
        raise Unplayable(reason, bot="bot" in reason.lower())
    det = pr.get("videoDetails") or {}
    if det.get("videoId") and det.get("videoId") != video_id:
        raise InnertubeError("player answered for a different video")
    return det


def _loudness(pr):
    """The player's loudness metadata (for volume normalisation): {"db", "perceptual_db",
    "target_lkfs"}, each None when absent."""
    ac = (pr.get("playerConfig") or {}).get("audioConfig") or {}
    return {"db": ac.get("loudnessDb"), "perceptual_db": ac.get("perceptualLoudnessDb"),
            "target_lkfs": ac.get("loudnessTargetLkfs")}


# --------------------------------------------------------------------------- #
# Live: muxed HLS variants from the live client
# --------------------------------------------------------------------------- #

_HLS_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_VCODECS = ("avc", "vp9", "vp09", "av01", "hev", "hvc")
_ACODECS = ("mp4a", "opus", "ac-3", "ec-3")


def _hls_variants(m3u8, ua):
    """Muxed (audio+video) variants of an HLS master playlist as yt-dlp-shaped formats keyed by
    the itag in each variant URL. Video-only variants (those pointing at an AUDIO group) are
    skipped — on their own they'd play silent."""
    out, attrs = [], None
    for line in (m3u8 or "").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = {k: v.strip('"') for k, v in _HLS_ATTR.findall(line.split(":", 1)[1])}
            continue
        if not line or line.startswith("#") or attrs is None:
            continue
        a, attrs = attrs, None
        m = re.search(r"/itag/(\d+)/", line)
        codecs = [c.strip() for c in a.get("CODECS", "").split(",") if c.strip()]
        vc = next((c for c in codecs if c.lower().startswith(_VCODECS)), "none")
        ac = next((c for c in codecs if c.lower().startswith(_ACODECS)), "none")
        if not m or "AUDIO" in a or vc == "none" or ac == "none":
            continue
        w, _, h = a.get("RESOLUTION", "").partition("x")
        try:
            fps = float(a.get("FRAME-RATE") or 0) or None
        except ValueError:
            fps = None
        try:
            tbr = int(a.get("BANDWIDTH") or 0) / 1000.0 or None
        except ValueError:
            tbr = None
        out.append({"format_id": m.group(1), "url": line, "protocol": "m3u8_native", "ext": "mp4",
                    "width": int(w) if w.isdigit() else None,
                    "height": int(h) if h.isdigit() else None,
                    "fps": fps, "vcodec": vc, "acodec": ac, "tbr": tbr,
                    "http_headers": {"User-Agent": ua}})
    return out


def live_info(video_id, timeout=10):
    """A RUNNING livestream as muxed HLS variants (player_info's dict shape; formats are
    protocol m3u8_native). Raises for anything that isn't live-now with such a playlist."""
    if not re.fullmatch(r"[\w-]{11}", video_id or ""):
        raise InnertubeError("bad video id")
    with _cfg_lock:
        ctx, num = dict(_LIVE["context"]), _LIVE["number"]
    ua = ctx.get("userAgent", "")
    body = {"videoId": video_id, "contentCheckOk": True, "racyCheckOk": True,
            "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}}}
    vd = _mint_visitor(_live_visitor, _LIVE, False, timeout)
    pr = _post("player", body, ctx, ua, num, visitor=vd, timeout=timeout, player=True)
    det = _playable_details(pr, video_id, _live_visitor)
    if not det.get("isLive"):
        raise InnertubeError("not live")
    hls = (pr.get("streamingData") or {}).get("hlsManifestUrl") or ""
    if not hls:
        raise InnertubeError("no HLS manifest")
    status, data = _transport("GET", hls, None, {"User-Agent": ua}, timeout)
    if status != 200:
        raise InnertubeError("HLS manifest HTTP %s" % status)
    formats = _hls_variants(data.decode("utf-8", "replace"), ua)
    if not formats:
        raise InnertubeError("no muxed HLS variants")
    cid = det.get("channelId") or ""
    return {
        "id": video_id,
        "title": det.get("title") or "",
        "uploader": det.get("author") or "",
        "channel": det.get("author") or "",
        "channel_id": cid,
        "channel_url": ("https://www.youtube.com/channel/" + cid) if cid else "",
        "description": det.get("shortDescription") or "",
        "duration": 0,
        "view_count": parse_count(det.get("viewCount") or ""),
        "is_live": True,
        "live_status": "is_live",
        "has_hls": True,
        "formats": formats,
        "subtitles": {},
        "automatic_captions": {},
        "chapters": [],
        "loudness": _loudness(pr),
    }


# --------------------------------------------------------------------------- #
# Search and watch-next (WEB client, like the channel tabs)
# --------------------------------------------------------------------------- #

def _from_channel_renderer(cr):
    cid = cr.get("channelId") or ""
    if not cid.startswith("UC"):
        return None
    # YouTube swaps these two fields on search results: the "subscriberCountText" holds the
    # @handle and "videoCountText" the subscriber count. Read both by content, not by name.
    texts = [text(cr.get(k)) for k in ("videoCountText", "subscriberCountText")]
    thumbs = []
    for t in ((cr.get("thumbnail") or {}).get("thumbnails") or []):
        u = t.get("url") or ""
        if u:
            thumbs.append(dict(t, url=("https:" + u) if u.startswith("//") else u))
    return {"kind": "channel", "id": cid, "title": text(cr.get("title")),
            "handle": next((t for t in texts if t.startswith("@")), ""),
            "subscribers": next((parse_count(t) for t in texts if "subscriber" in t.lower()), 0),
            "avatars": thumbs, "url": "https://www.youtube.com/channel/" + cid}


def _search_sections(sections):
    items, cont = [], None
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        if "continuationItemRenderer" in sec:
            cont = ((((sec["continuationItemRenderer"] or {}).get("continuationEndpoint") or {})
                     .get("continuationCommand") or {}).get("token")) or cont
            continue
        for c in ((sec.get("itemSectionRenderer") or {}).get("contents") or []):
            if not isinstance(c, dict):
                continue
            if "channelRenderer" in c:
                ch = _from_channel_renderer(c["channelRenderer"] or {})
                if ch:
                    items.append(ch)
                continue
            got, _ = _parse_items([c])          # videos; shelves / ads / promos parse to nothing
            for g in got:
                g["kind"] = "video"
                items.append(g)
    return items, cont


def search(query="", params="", continuation=None, timeout=10):
    """One page of search results: {"items": [video / channel dicts, "kind" set], "continuation"}.
    `params` is the (percent-encoded) sp= filter; `continuation` fetches the next page."""
    if continuation:
        res = _web_post("search", {"continuation": continuation}, timeout=timeout)
        sections = []
        for cmd in (res.get("onResponseReceivedCommands") or []):
            sections += ((cmd.get("appendContinuationItemsAction") or {})
                         .get("continuationItems") or [])
    else:
        body = {"query": query}
        if params:
            body["params"] = urllib.parse.unquote(params)
        res = _web_post("search", body, timeout=timeout)
        slr = ((((res.get("contents") or {}).get("twoColumnSearchResultsRenderer") or {})
                .get("primaryContents") or {}).get("sectionListRenderer"))
        if slr is None:
            raise InnertubeError("no search results container")
        sections = slr.get("contents") or []
    items, cont = _search_sections(sections)
    return {"items": items, "continuation": cont}


_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                                       "Sep", "Oct", "Nov", "Dec"), 1)}


def _upload_date(date_text):
    """'Oct 24, 2009' / 'Streamed live on Oct 1, 2025' / 'Premiered Mar 3, 2024' → 'YYYYMMDD'.
    Parsed by hand: strptime's %b follows the process locale, which Qt sets from the device."""
    m = re.search(r"\b([A-Z][a-z]{2})[a-z]* (\d{1,2}), (\d{4})", date_text or "")
    if not m or m.group(1) not in _MONTHS:
        return ""
    return "%s%02d%02d" % (m.group(3), _MONTHS[m.group(1)], int(m.group(2)))


def _comment_entities(res):
    """commentEntityPayload by entity key: the comment bodies of a /next comments page, which the
    thread items only point at (commentViewModel.commentKey)."""
    out = {}
    for m in (((res.get("frameworkUpdates") or {}).get("entityBatchUpdate") or {})
              .get("mutations") or []):
        p = (m.get("payload") or {}).get("commentEntityPayload")
        if isinstance(p, dict):
            out[m.get("entityKey") or p.get("key")] = p
    return out


def _comment(cvm, ents):
    p = ents.get((cvm or {}).get("commentKey"))
    if not p:
        return None
    pr, au, tb = p.get("properties") or {}, p.get("author") or {}, p.get("toolbar") or {}
    return {"id": pr.get("commentId") or cvm.get("commentId") or "",
            "author": au.get("displayName") or "",
            "text": (pr.get("content") or {}).get("content") or "",
            "likes": parse_count(tb.get("likeCountNotliked") or ""),
            "time": pr.get("publishedTime") or "",
            "thumbnail": au.get("avatarThumbnailUrl") or "",
            "is_uploader": bool(au.get("isCreator")),
            "reply_count": parse_count(tb.get("replyCount") or "")}


def _cont_token(o):
    for cc in _walk(o, "continuationCommand", []):
        if cc.get("token"):
            return cc["token"]
    return None


def comments(token, timeout=10):
    """One page of comments from a continuation token — watch_next's comments_token, a page's own
    continuation, or a thread's replies_token: {"comments", "continuation", "total"}. Top-level
    comments carry "replies_token" (None without replies); total is the header count (first page
    of a video only, else None). Default order is YouTube's "Top"."""
    res = _web_post("next", {"continuation": token}, timeout=timeout)
    ents = _comment_entities(res)
    items = []
    for ep in (res.get("onResponseReceivedEndpoints") or []):
        for act in ("reloadContinuationItemsCommand", "appendContinuationItemsAction"):
            items += (ep.get(act) or {}).get("continuationItems") or []
    out, cont, total = [], None, None
    for it in items:
        if not isinstance(it, dict):
            continue
        if "commentsHeaderRenderer" in it:
            total = parse_count(text((it["commentsHeaderRenderer"] or {}).get("countText")))
        elif "commentThreadRenderer" in it:
            th = it["commentThreadRenderer"] or {}
            c = _comment((th.get("commentViewModel") or {}).get("commentViewModel"), ents)
            if c:
                c["replies_token"] = _cont_token(th.get("replies") or {})
                out.append(c)
        elif "commentViewModel" in it:                 # a replies page
            c = _comment(it["commentViewModel"], ents)
            if c:
                out.append(c)
        elif "continuationItemRenderer" in it:
            cont = _cont_token(it) or cont
    return {"comments": out, "continuation": cont, "total": total}


def watch_next(video_id, timeout=10):
    """The watch page's /next answer: related videos (+ their continuation), upload date, likes,
    the uploader's subscriber count and the comments' first continuation token."""
    if not re.fullmatch(r"[\w-]{11}", video_id or ""):
        raise InnertubeError("bad video id")
    res = _web_post("next", {"videoId": video_id, "contentCheckOk": True, "racyCheckOk": True},
                    timeout=timeout)
    tc = (res.get("contents") or {}).get("twoColumnWatchNextResults")
    if not tc:
        raise InnertubeError("no watch-next results")
    flat = []
    for c in ((((tc.get("secondaryResults") or {}).get("secondaryResults") or {})
               .get("results")) or []):
        if isinstance(c, dict) and "itemSectionRenderer" in c:
            flat += (c["itemSectionRenderer"] or {}).get("contents") or []
        else:
            flat.append(c)
    related, rel_cont = _parse_items(flat)
    prim = (((tc.get("results") or {}).get("results") or {}).get("contents")) or []
    pi, si, ctok = {}, {}, None
    for c in prim:
        if not isinstance(c, dict):
            continue
        pi = c.get("videoPrimaryInfoRenderer") or pi
        si = c.get("videoSecondaryInfoRenderer") or si
        isr = c.get("itemSectionRenderer") or {}
        if isr.get("sectionIdentifier") == "comment-item-section":
            for cc in (isr.get("contents") or []):
                ctok = ((((cc.get("continuationItemRenderer") or {}).get("continuationEndpoint")
                          or {}).get("continuationCommand") or {}).get("token")) or ctok
    m = re.search(r"along with ([\d,]+) other", json.dumps(pi))
    owner = (si.get("owner") or {}).get("videoOwnerRenderer") or {}
    date_text = text(pi.get("dateText"))
    return {
        "related": related,
        "related_continuation": rel_cont,
        "date_text": date_text,
        "upload_date": _upload_date(date_text),
        "likes": (int(m.group(1).replace(",", "")) + 1) if m else 0,
        "subscribers": parse_count(text(owner.get("subscriberCountText"))),
        "comments_token": ctok,
    }
