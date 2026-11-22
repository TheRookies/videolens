"""TubeLens: YouTube research and video understanding for MCP clients. No API key.

Reads metadata, comments, transcripts; searches and researches topics; "watches" videos through
frames (even or scene-based) and OCR; transcribes silent-caption videos with local Whisper.
Built on yt-dlp, a bundled ffmpeg (imageio-ffmpeg) and optional faster-whisper / rapidocr.
"""
import concurrent.futures as cf
import datetime as dt
import functools
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
mcp = FastMCP("tubelens")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # bundled ffmpeg, no system install needed
PROTOCOLS = "https,http,tls,tcp,crypto"

# --------------------------------------------------------------------------- security
_YT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtube-nocookie.com",
}


def _extra_hosts() -> set:
    """Opt-in extra video hosts: TUBELENS_EXTRA_HOSTS="vimeo.com,www.vimeo.com". Empty by default."""
    raw = os.environ.get("TUBELENS_EXTRA_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _check_url(url: str, hosts: set | None = None) -> str:
    """Only http(s) URLs on allowed hosts. Stops a hostile link (e.g. planted in a comment)
    from steering yt-dlp/ffmpeg at arbitrary hosts or local files."""
    url = url.strip()
    if hosts is None and re.fullmatch(r"[A-Za-z0-9_-]{11}", url):  # bare video id
        return f"https://www.youtube.com/watch?v={url}"
    allowed = hosts if hosts is not None else (_YT_HOSTS | _extra_hosts())
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in allowed:
        raise ValueError("URL host not allowed (YouTube by default; see TUBELENS_EXTRA_HOSTS).")
    return url


_FRIENDLY = (
    ("private video", "This video is private."),
    ("confirm your age", "Age-restricted video: needs a signed-in account, which this server never uses."),
    ("members-only", "Members-only video: not accessible without a login."),
    ("video unavailable", "Video unavailable (removed, region-blocked or wrong link)."),
    ("this live event", "Live stream has not started or has no recording yet."),
    ("429", "YouTube is rate-limiting this IP. Wait a few minutes and retry."),
    ("sign in to confirm you", "YouTube asked for bot verification on this IP. Retry later or from another network."),
)


def tool(fn):
    """Register an MCP tool and turn raw yt-dlp/network failures into short, actionable messages."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except ValueError:
            raise
        except (yt_dlp.utils.DownloadError, urllib.error.URLError, subprocess.TimeoutExpired) as ex:
            msg = str(ex)
            for needle, text in _FRIENDLY:
                if needle in msg.lower():
                    raise RuntimeError(text) from None
            if isinstance(ex, subprocess.TimeoutExpired):
                raise RuntimeError("Timed out reading the video stream. Narrow start/end and retry.") from None
            raise RuntimeError(msg.replace("ERROR: ", "")[:300]) from None
    return mcp.tool()(wrapper)


# --------------------------------------------------------------------------- helpers
def _ydl(**opts):
    base = {"quiet": True, "no_warnings": True, "skip_download": True}
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


_CACHE: dict = {}
_TTL = 600  # seconds


def _cached(key, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    if len(_CACHE) > 200:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:50]:
            _CACHE.pop(k, None)
    return val


def _info(url: str, fmt: str | None = None) -> dict:
    def go():
        with _ydl(**({"format": fmt} if fmt else {})) as y:
            return y.extract_info(url, download=False)
    return _cached(("info", url, fmt), go)


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_time(t) -> float | None:
    """Accept seconds, 'mm:ss' or 'hh:mm:ss'."""
    if t is None or t == "":
        return None
    if isinstance(t, (int, float)):
        return float(t)
    out = 0.0
    for p in str(t).split(":"):
        out = out * 60 + float(p)
    return out


def _num(n) -> str:
    if n is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(int(n))


def _age_days(upload_date: str | None) -> int | None:
    if not upload_date or len(upload_date) != 8:
        return None
    try:
        d = dt.datetime.strptime(upload_date, "%Y%m%d")
    except ValueError:
        return None
    return max(0, (dt.datetime.now() - d).days)


# --------------------------------------------------------------------------- transcripts
def _transcript_events(info: dict, lang: str):
    """Return (lang_used, [(start_sec, text)]) from manual or auto captions."""
    for source in ("subtitles", "automatic_captions"):
        tracks = info.get(source) or {}
        keys = [k for k in tracks if k == lang or k.startswith(lang + "-") or k.startswith(lang + ".")]
        if not keys and source == "automatic_captions":
            keys = [k for k in tracks if k.split("-")[0] == lang.split("-")[0]]
        for k in keys:
            fmts = [f for f in tracks[k] if f.get("ext") == "json3"]
            if not fmts:
                continue
            with urllib.request.urlopen(fmts[0]["url"], timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            events = []
            for ev in data.get("events", []):
                segs = ev.get("segs")
                if not segs:
                    continue
                text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
                if text:
                    events.append((ev.get("tStartMs", 0) / 1000.0, text))
            return k, events
    return None, []


def _pick_lang(info: dict, lang: str | None) -> str:
    """lang='auto' -> the video's spoken language (YouTube marks it as '<code>-orig' or 'language')."""
    if lang and lang != "auto":
        return lang
    auto = info.get("automatic_captions") or {}
    subs = info.get("subtitles") or {}
    cands = [info.get("language")] + [k for k in auto if k.endswith("-orig")] + list(subs) + ["en"]
    for c in cands:
        if c:
            return c.split("-")[0]
    return "en"


def _events(url: str, lang: str):
    info = _info(url)
    real = _pick_lang(info, lang)
    return _cached(("ev", url, real), lambda: _transcript_events(info, real))


def _chunk(events, window: int = 20):
    """Merge caption fragments into ~window-second lines and drop repeats (saves many tokens)."""
    if window <= 0:
        return list(events)
    out, cur_t, cur, last = [], None, [], None
    for t, x in events:
        if x == last:
            continue
        last = x
        if cur_t is None:
            cur_t = t
        elif t - cur_t >= window:
            out.append((cur_t, " ".join(cur)))
            cur_t, cur = t, []
        cur.append(x)
    if cur:
        out.append((cur_t, " ".join(cur)))
    return out


def _render(events, with_ts=True) -> str:
    if with_ts:
        return "\n".join(f"[{_fmt_time(t)}] {x}" for t, x in events)
    return " ".join(x for _, x in events)


def _cap(text: str, max_chars: int, hint: str = "use start/end to continue") -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    nl = cut.rfind("\n[")
    cut = cut[:nl] if nl > 0 else cut
    return cut + f"\n...[truncated at {len(cut)} of {len(text)} chars; {hint}]"


_STOP = set("""
about above after again against also because been before being below between both could did does
doing down during each from further have having here into itself just like make more most much only
other over really same should some such than that their them then there these they this those
through under until very want were what when where which while will with would your yours you're
don't it's i'm that's there's what's gonna going know yeah okay right think thing things well
actually para ama için gibi daha çok kadar sonra şimdi böyle bunu şunu olarak olan oldu değil yani
işte ben sen biz siz onlar bir bu şu ve ile veya ama fakat çünkü
""".split())


def _top_terms(text: str, k: int = 8) -> list:
    words = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü']{4,}", text.lower())
    return [w for w, _ in Counter(w for w in words if w not in _STOP).most_common(k)]


def _digest(events, head=500, mid=350, tail=350) -> str:
    text = _render(_chunk(events, 30), with_ts=False)
    if len(text) <= head + mid + tail + 100:
        return text
    m = len(text) // 2
    return f"{text[:head]} […] {text[m - mid // 2: m + mid // 2]} […] {text[-tail:]}"


# --------------------------------------------------------------------------- frames
def _stream(info: dict):
    fmt = (info.get("requested_formats") or [info])[0]
    headers = "".join(f"{k}: {v}\r\n" for k, v in (fmt.get("http_headers") or {}).items())
    return fmt["url"], headers


def _ffmpeg_base(headers: str) -> list:
    return [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", PROTOCOLS,
            "-headers", headers]


def _grab(stream: str, headers: str, times, width: int, tmp: Path) -> list:
    """Grab one frame per timestamp. Each seek opens its own connection (~5s), so run 4 in parallel."""
    def one(item):
        k, t = item
        f = tmp / f"f{k}.jpg"
        try:
            subprocess.run(
                _ffmpeg_base(headers) + ["-ss", f"{t:.2f}", "-i", stream, "-frames:v", "1",
                                         "-vf", f"scale={width}:-2", "-q:v", "6", str(f)],
                check=False, timeout=60)
        except subprocess.TimeoutExpired:
            return None
        return (t, f) if f.exists() and f.stat().st_size > 0 else None

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        return [r for r in ex.map(one, enumerate(times)) if r]


def _thumb(path: Path) -> list:
    with PILImage.open(path) as im:
        return list(im.convert("L").resize((24, 24)).getdata())


def _frames_for(stream, headers, s, e, n, mode, width, tmp):
    """Frames for a range. 'scenes' samples 2n evenly spaced moments and keeps the n that differ most
    from their predecessor (a fast stand-in for scene detection: reading the whole stream is too slow)."""
    if mode == "scenes":
        m = min(n * 2, 16)
        step = (e - s) / m
        got = _grab(stream, headers, [s + step * (k + 0.5) for k in range(m)], width, tmp)
        if len(got) > n:
            thumbs = [_thumb(f) for _, f in got]
            score = [1e9] + [sum(abs(a - b) for a, b in zip(thumbs[i], thumbs[i - 1])) / 576
                             for i in range(1, len(got))]
            keep = sorted(sorted(range(len(got)), key=lambda i: -score[i])[:n])
            got = [got[i] for i in keep]
        return got, f"most distinct moments out of {m} sampled"
    step = (e - s) / n
    got = _grab(stream, headers, [s + step * (k + 0.5) for k in range(n)], width, tmp)
    return got, f"even spacing, one every {step:.1f}s"


# --------------------------------------------------------------------------- info tools
@tool
def video_info(url: str, description_chars: int = 1500) -> str:
    """Get YouTube video metadata: title, channel, stats, upload date, tags, chapters, description."""
    url = _check_url(url)
    i = _info(url)
    desc = (i.get("description") or "")[:description_chars]
    chapters = "\n".join(f"  {_fmt_time(c['start_time'])} {c['title']}" for c in (i.get("chapters") or []))
    avail = sorted(set((i.get("subtitles") or {}).keys()))
    full = len(i.get("description") or "")
    more = f"\n...[{full - description_chars} more chars, raise description_chars]" if full > description_chars else ""
    return (
        f"Title: {i.get('title')}\n"
        f"Channel: {i.get('channel')} ({i.get('channel_follower_count')} subs)\n"
        f"Uploaded: {i.get('upload_date')}  Duration: {_fmt_time(i.get('duration') or 0)}\n"
        f"Views: {i.get('view_count')}  Likes: {i.get('like_count')}  Comments: {i.get('comment_count')}\n"
        f"Tags: {(i.get('tags') or [])[:10]}\n"
        f"Manual subs: {avail or 'none'}  Auto-captions: {'yes' if i.get('automatic_captions') else 'no'}\n"
        f"Chapters:\n{chapters or '  none'}\n\n"
        f"Description:\n{desc}{more}"
    )


@tool
def video_comments(
    url: str,
    max_comments: int = 30,
    sort: str = "top",
    replies: bool = False,
    max_chars_each: int = 300,
    query: str | None = None,
    scan: int = 300,
) -> str:
    """Read YouTube comments. sort: 'top' or 'new'. replies=True adds replies (3 per thread).
    query: comma-separated terms; scans up to `scan` comments and returns only those containing
    any term (case-insensitive), e.g. query="broke,refund,scam". Each comment is cut to max_chars_each."""
    url = _check_url(url)
    fetch = max(max_comments, scan) if query else max_comments
    rep, per = ("30", "3") if replies else ("0", "0")
    opts = {"getcomments": True, "extractor_args": {"youtube": {
        "comment_sort": [sort], "max_comments": [str(fetch), str(fetch), rep, per]}}}
    with _ydl(**opts) as y:
        i = y.extract_info(url, download=False)
    comments = i.get("comments") or []
    scanned = len(comments)
    try:
        total = _info(url).get("comment_count")
    except Exception:
        total = i.get("comment_count")
    if query:
        terms = [t.strip().lower() for t in query.split(",") if t.strip()]
        comments = [c for c in comments if any(t in (c.get("text") or "").lower() for t in terms)]
    comments = comments[:max_comments]
    head = f"{len(comments)} comments" + (f" matching '{query}' (scanned {scanned})" if query else "")
    lines = [f"{head} (video total: {total})"]
    for c in comments:
        indent = "    ↳ " if c.get("parent") not in (None, "root") else "- "
        text = " ".join((c.get("text") or "").split())
        if len(text) > max_chars_each:
            text = text[:max_chars_each] + "…"
        likes = c.get("like_count") or 0
        meta = (f"{likes}♥" if likes else "") + (" pinned" if c.get("is_pinned") else "")
        lines.append(f"{indent}{c.get('author')} {('(' + meta.strip() + ') ') if meta else ''}{text}")
    return "\n".join(lines)


@tool
def video_transcript(
    url: str,
    lang: str = "auto",
    with_timestamps: bool = True,
    max_chars: int = 12000,
    start: str | None = None,
    end: str | None = None,
    chunk_seconds: int = 20,
) -> str:
    """Get video transcript (manual subs first, then auto-captions). lang='auto' (default) uses the
    video's spoken language; or force e.g. 'en', 'tr'.
    Lines merge into ~chunk_seconds blocks (0 = raw) to save tokens. Use start/end ('1:30') to read
    one part of a long video. No captions? use transcribe_video."""
    url = _check_url(url)
    used, events = _events(url, lang)
    if not events:
        return f"No captions for lang '{lang}'. Try another lang (see video_info) or use transcribe_video."
    s, e = _parse_time(start), _parse_time(end)
    if s is not None or e is not None:
        events = [(t, x) for t, x in events if (s is None or t >= s) and (e is None or t <= e)]
    return f"(lang: {used})\n" + _cap(_render(_chunk(events, chunk_seconds), with_timestamps), max_chars)


@tool
def batch_transcripts(urls: list[str], lang: str = "auto", max_chars_each: int = 3000) -> str:
    """Transcripts of several videos in one call (max 8), each merged into 30s blocks and capped."""
    out = []
    for u in urls[:8]:
        try:
            u = _check_url(u)
            i = _info(u)
            used, ev = _events(u, lang)
            body = (_cap(_render(_chunk(ev, 30)), max_chars_each, "use video_transcript start/end")
                    if ev else "(no captions; use transcribe_video)")
            out.append(f"## {i.get('title')} | {i.get('channel')} | {u}\n{body}")
        except Exception as ex:  # keep going, report per-video failure
            out.append(f"## {u}\nERROR: {str(ex)[:150]}")
    return "\n\n".join(out)


@tool
def find_in_video(
    url: str, query: str, lang: str = "auto", max_hits: int = 6, frames: bool = False, width: int = 512
) -> list:
    """Find WHERE a topic is said in a video. query: comma-separated terms (any match). Returns timestamped
    caption lines (with a little context). frames=True also returns the video frame at each hit."""
    url = _check_url(url)
    used, ev = _events(url, lang)
    if not ev:
        return ["No captions; run transcribe_video first or try another lang."]
    terms = [t.strip().lower() for t in query.split(",") if t.strip()]
    hits, last = [], -999.0
    for k, (t, x) in enumerate(ev):
        if any(term in x.lower() for term in terms) and t - last > 12:
            ctx = " ".join(y for _, y in ev[max(0, k - 1): k + 3])
            hits.append((t, ctx))
            last = t
    if not hits:
        return [f"No match for '{query}' in {len(ev)} caption lines."]
    hits = hits[:max_hits]
    out: list = [f"{len(hits)} hit(s) for '{query}':"]
    tmp = Path(tempfile.mkdtemp(prefix="ytmcp_"))
    try:
        got = {}
        if frames:
            info = _info(url, "bv*[height<=480]/b[height<=480]/worst")
            stream, headers = _stream(info)
            got = dict(_grab(stream, headers, [t for t, _ in hits], max(256, min(width, 1024)), tmp))
        for t, ctx in hits:
            out.append(f"[{_fmt_time(t)}] {ctx[:300]}")
            if t in got:
                out.append(Image(data=got[t].read_bytes(), format="jpeg"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# --------------------------------------------------------------------------- search & research
def _search_entries(query: str, n: int, by_date: bool) -> list:
    if by_date:  # yt-dlp has no date-sorted search key; use YouTube's own sort filter (sp=upload date)
        target = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query) + "&sp=CAI%253D"
    else:
        target = f"ytsearch{n}:{query}"
    with _ydl(extract_flat=True, playlistend=n) as y:
        r = y.extract_info(target, download=False)
    return [e for e in (r.get("entries") or []) if e and e.get("id")][:n]


def _channel_url(url: str) -> str:
    """Accept '@handle', a channel URL or a playlist URL; channels default to their /videos tab."""
    url = url.strip()
    if url.startswith("@"):
        url = f"https://www.youtube.com/{url}"
    url = _check_url(url)
    path = urllib.parse.urlparse(url).path.rstrip("/")
    if path.startswith(("/@", "/channel/", "/c/", "/user/")) and not path.endswith(
            ("/videos", "/shorts", "/streams", "/playlists")):
        url = url.rstrip("/") + "/videos"
    return url


def _passes(e: dict, min_views, min_dur, max_dur) -> bool:
    v, d = e.get("view_count"), e.get("duration")
    if min_views and v is not None and v < min_views:
        return False
    if d is not None and ((min_dur and d < min_dur) or (max_dur and d > max_dur)):
        return False
    return True


@tool
def youtube_search(
    query: str,
    n: int = 8,
    sort: str = "relevance",
    min_views: int = 0,
    min_minutes: float = 0,
    max_minutes: float = 0,
) -> str:
    """Search YouTube. sort: 'relevance' or 'date' (newest first). Optional filters: min_views,
    min_minutes, max_minutes (0 = off). Returns title, channel, duration, views, URL."""
    filtered = bool(min_views or min_minutes or max_minutes)
    pool = _search_entries(query, min(n * 3, 50) if filtered else n, sort == "date")
    kept = [e for e in pool if _passes(e, min_views, min_minutes * 60, max_minutes * 60)][:n]
    out = [
        f"- {e.get('title')} | {e.get('channel')} | {_fmt_time(e.get('duration') or 0)} | "
        f"{_num(e.get('view_count'))} views | {e.get('url')}" for e in kept
    ]
    return "\n".join(out) or "no results"


def _enrich(entry: dict, lang: str, with_comments: bool, n_comments: int = 6):
    try:
        url = f"https://www.youtube.com/watch?v={entry['id']}"
        info = _info(url)
        used, ev = _events(url, lang)
        row = {"url": url, "info": info, "lang": used, "events": ev, "comments": []}
        if with_comments:
            try:
                opts = {"getcomments": True, "extractor_args": {"youtube": {
                    "comment_sort": ["top"], "max_comments": [str(n_comments), str(n_comments), "0", "0"]}}}
                with _ydl(**opts) as y:
                    row["comments"] = (y.extract_info(url, download=False).get("comments") or [])[:n_comments]
            except Exception:
                pass
        return row
    except Exception:
        return None


@tool
def youtube_research(
    topic: str,
    queries: list[str] | None = None,
    n_videos: int = 5,
    sort: str = "relevance",
    min_views: int = 0,
    max_age_days: int = 0,
    min_minutes: float = 2,
    max_minutes: float = 120,
    lang: str = "auto",
    with_comments: bool = True,
) -> str:
    """Research a topic on YouTube in one call. Runs several searches (your `queries`, or the topic plus
    'explained' and 'review' variants), merges and de-duplicates, ranks by views, recency and how many
    searches surfaced each video, then returns a compact brief per top video: stats, chapters, description,
    transcript excerpt (start/middle/end) with top terms, and top comments. Follow up with find_in_video,
    video_comments(query=...) or watch_video on the most promising ones. n_videos max 8."""
    n_videos = max(1, min(int(n_videos), 8))
    qs = (queries or [topic, f"{topic} explained", f"{topic} review"])[:5]
    by_date = sort == "date"
    pool: dict = {}
    with cf.ThreadPoolExecutor(max_workers=min(4, len(qs))) as ex:
        futs = [ex.submit(_search_entries, q, 15, by_date) for q in qs]
        for f in futs:
            try:
                res = f.result()
            except Exception:
                continue
            for rank, e in enumerate(res):
                if not _passes(e, min_views, min_minutes * 60, max_minutes * 60):
                    continue
                p = pool.setdefault(e["id"], {"e": e, "hits": 0, "best_rank": rank})
                p["hits"] += 1
                p["best_rank"] = min(p["best_rank"], rank)
    if not pool:
        return "No videos matched. Loosen min_views/min_minutes or change queries."

    def score(p):
        v = max(p["e"].get("view_count") or 1, 1)
        return math.log10(v) + 1.5 * (p["hits"] - 1) - 0.08 * p["best_rank"]

    ranked = sorted(pool.values(), key=score, reverse=True)[: n_videos + 3]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        rows = list(ex.map(lambda p: _enrich(p["e"], lang, with_comments), ranked))
    chosen = []
    for p, row in zip(ranked, rows):
        if not row:
            continue
        age = _age_days(row["info"].get("upload_date"))
        if max_age_days and age is not None and age > max_age_days:
            continue
        if min_views and (row["info"].get("view_count") or 0) < min_views:
            continue
        chosen.append((p, row))
    chosen = chosen[:n_videos]
    if not chosen:
        return "Videos found but none passed the age/views filters."

    out = [f"# YouTube research: {topic}",
           f"{len(pool)} unique candidates from {len(qs)} searches; showing top {len(chosen)}.\n"]
    for idx, (p, row) in enumerate(chosen, 1):
        i = row["info"]
        age = _age_days(i.get("upload_date"))
        out.append(
            f"## {idx}. {i.get('title')}\n"
            f"{i.get('channel')} ({_num(i.get('channel_follower_count'))} subs) | "
            f"{_num(i.get('view_count'))} views | {_num(i.get('like_count'))} likes | "
            f"{_fmt_time(i.get('duration') or 0)} | {i.get('upload_date')}"
            f"{f' ({age}d ago)' if age is not None else ''} | seen in {p['hits']}/{len(qs)} searches\n"
            f"{row['url']}"
        )
        chaps = [c["title"] for c in (i.get("chapters") or [])][:12]
        if chaps:
            out.append("Chapters: " + " · ".join(chaps))
        desc = " ".join((i.get("description") or "").split())[:350]
        if desc:
            out.append(f"Description: {desc}")
        if row["events"]:
            full = _render(_chunk(row["events"], 30), with_ts=False)
            out.append(f"Top terms: {', '.join(_top_terms(full))}")
            out.append(f"Transcript ({row['lang']}): {_digest(row['events'])}")
        else:
            out.append("Transcript: none (no captions; transcribe_video can create one)")
        for c in row["comments"][:4]:
            t = " ".join((c.get("text") or "").split())[:160]
            out.append(f"  comment ({_num(c.get('like_count'))}♥): {t}")
        out.append("")
    return "\n".join(out)


@tool
def channel_videos(url: str, n: int = 10) -> str:
    """List recent videos of a channel or playlist URL (e.g. https://www.youtube.com/@name/videos)."""
    url = _channel_url(url)
    with _ydl(extract_flat=True, playlistend=n) as y:
        r = y.extract_info(url, download=False)
    out = [f"{r.get('title')}"]
    for e in (r.get("entries") or [])[:n]:
        out.append(f"- {e.get('title')} | {_fmt_time(e.get('duration') or 0)} | "
                   f"{_num(e.get('view_count'))} views | {e.get('url')}")
    return "\n".join(out)


@tool
def channel_stats(url: str, n: int = 30) -> str:
    """Quick channel analysis over its latest n videos (max 50): average/median views, best videos,
    typical length and upload cadence. url like https://www.youtube.com/@name/videos"""
    url = _channel_url(url)
    n = max(5, min(int(n), 50))
    with _ydl(extract_flat=True, playlistend=n) as y:
        r = y.extract_info(url, download=False)
    es = [e for e in (r.get("entries") or []) if e and e.get("id")][:n]
    if not es:
        return "No videos found."
    views = sorted(e["view_count"] for e in es if e.get("view_count") is not None)
    durs = [e["duration"] for e in es if e.get("duration")]
    lines = [f"{r.get('title')} | analysed {len(es)} latest videos"]
    if views:
        lines.append(f"Views: mean {_num(sum(views) / len(views))}, median {_num(views[len(views) // 2])}, "
                     f"min {_num(views[0])}, max {_num(views[-1])}")
    if durs:
        lines.append(f"Length: median {_fmt_time(sorted(durs)[len(durs) // 2])}, "
                     f"shortest {_fmt_time(min(durs))}, longest {_fmt_time(max(durs))}")
    # listings carry no dates, so read the 6 newest videos' pages (parallel, cached) to estimate cadence
    def stamp(e):
        try:
            d = _info(f"https://www.youtube.com/watch?v={e['id']}").get("upload_date")
            return dt.datetime.strptime(d, "%Y%m%d") if d else None
        except Exception:
            return None

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        days = sorted((d for d in ex.map(stamp, es[:6]) if d), reverse=True)
    if len(days) >= 3:
        gap = (days[0] - days[-1]).days / (len(days) - 1)
        lines.append(f"Cadence: a video every {gap:.1f} days (from the {len(days)} newest)")
        lines.append(f"Last upload: {days[0].date()} ({(dt.datetime.now() - days[0]).days} days ago)")
    else:
        lines.append("Cadence: could not read upload dates")
    lines.append("Top by views:")
    for e in sorted(es, key=lambda e: e.get("view_count") or 0, reverse=True)[:5]:
        lines.append(f"- {e.get('title')} | {_num(e.get('view_count'))} views | {e.get('url')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- watching
@tool
def watch_video(
    url: str,
    n_frames: int = 6,
    start: str | None = None,
    end: str | None = None,
    width: int = 512,
    include_transcript: bool = True,
    lang: str = "auto",
    mode: str = "even",
) -> list:
    """'Watch' a video: returns frames (images, each labelled with timestamp) plus the transcript for the
    same range. mode='even' spaces frames evenly; mode='scenes' samples twice as many moments and keeps
    the ones that differ most (best for slides, demos, edited videos). Use start/end ('90', '1:30') to
    zoom in and call again for detail. n_frames max 24 (default 6); width 512 saves tokens, raise to
    ~768 only to read small on-screen text (or use ocr_frames)."""
    url = _check_url(url)
    n_frames = max(1, min(int(n_frames), 24))
    width = max(256, min(int(width), 1024))
    info = _info(url, "bv*[height<=480]/b[height<=480]/worst")
    dur = float(info.get("duration") or 0)
    s = _parse_time(start) or 0.0
    e = _parse_time(end) or dur
    e = min(e, dur) if dur else e
    if e <= s:
        return [f"Invalid range: start={s} end={e} duration={dur}"]
    stream, headers = _stream(info)
    tmp = Path(tempfile.mkdtemp(prefix="ytmcp_"))
    try:
        got, how = _frames_for(stream, headers, s, e, n_frames, mode, width, tmp)
        out: list = [f"{info.get('title')} — {_fmt_time(s)} to {_fmt_time(e)} (of {_fmt_time(dur)}), "
                     f"{len(got)} frames, {how}"]
        for t, f in got:
            out.append(f"Frame @ {_fmt_time(t)}")
            out.append(Image(data=f.read_bytes(), format="jpeg"))
        if include_transcript:
            used, events = _events(url, lang)
            seg = _chunk([(t, x) for t, x in events if s <= t <= e], 15)
            if seg:
                out.append(f"Transcript ({used}):\n"
                           + _cap(_render(seg), 5000, "narrow start/end or use video_transcript"))
            else:
                out.append("(no captions in range; transcribe_video can create a transcript)")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


@tool
def ocr_frames(
    url: str,
    n_frames: int = 6,
    start: str | None = None,
    end: str | None = None,
    mode: str = "scenes",
    max_chars: int = 4000,
) -> str:
    """Read the TEXT shown in a video (slides, code, UI, burned-in subtitles) without returning images,
    so it is cheap in tokens. Frames come from scene changes (default) or even spacing.
    Output: timestamp and recognised text per frame. Runs locally (rapidocr), no cloud."""
    url = _check_url(url)
    n_frames = max(1, min(int(n_frames), 16))
    info = _info(url, "bv*[height<=480]/b[height<=480]/worst")
    dur = float(info.get("duration") or 0)
    s = _parse_time(start) or 0.0
    e = min(_parse_time(end) or dur, dur) if dur else (_parse_time(end) or 0)
    if e <= s:
        return f"Invalid range: start={s} end={e}"
    stream, headers = _stream(info)
    tmp = Path(tempfile.mkdtemp(prefix="ytmcp_"))
    try:
        got, how = _frames_for(stream, headers, s, e, n_frames, mode, 960, tmp)
        engine = _ocr_engine()
        out, seen = [f"OCR of {len(got)} frames ({how})"], set()
        for t, f in got:
            res, _ = engine(str(f))
            lines = [r[1] for r in sorted(res or [], key=lambda r: (round(r[0][0][1] / 20), r[0][0][0]))]
            text = " | ".join(lines)
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(f"[{_fmt_time(t)}] {text}")
        if len(out) == 1:
            out.append("(no text recognised)")
        return _cap("\n".join(out), max_chars, "narrow start/end")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- whisper
_WHISPER: dict = {}


@tool
def transcribe_video(
    url: str,
    start: str | None = None,
    end: str | None = None,
    lang: str | None = None,
    model: str = "base",
    max_chars: int = 12000,
    chunk_seconds: int = 20,
) -> str:
    """Transcribe the SPEECH of a video locally with Whisper (faster-whisper, CPU) when it has no usable
    captions. model: tiny | base | small (bigger = slower, more accurate). lang: 'en', 'tr'... or empty to
    auto-detect. Max 30 min per call, so use start/end for long videos. First run downloads the model once."""
    url = _check_url(url)
    info = _info(url, "ba/b")
    dur = float(info.get("duration") or 0)
    s = _parse_time(start) or 0.0
    e = min(_parse_time(end) or dur, dur) if dur else (_parse_time(end) or 0)
    if e <= s:
        return f"Invalid range: start={s} end={e}"
    if e - s > 1800:
        return "Range too long (max 30 min per call). Use start/end to split the video."
    if model not in ("tiny", "base", "small"):
        return "model must be tiny, base or small."
    stream, headers = _stream(info)
    tmp = Path(tempfile.mkdtemp(prefix="ytmcp_"))
    try:
        wav = tmp / "a.wav"
        subprocess.run(
            _ffmpeg_base(headers) + ["-ss", f"{s:.2f}", "-i", stream, "-t", f"{e - s:.2f}",
                                     "-vn", "-ac", "1", "-ar", "16000", str(wav)],
            check=False, timeout=600)
        if not wav.exists() or wav.stat().st_size < 1000:
            return "Could not extract audio."
        if model not in _WHISPER:
            from faster_whisper import WhisperModel
            _WHISPER[model] = WhisperModel(model, device="cpu", compute_type="int8")
        # decode the 16 kHz mono wav ourselves: faster-whisper's own decoder breaks on newer PyAV
        import wave

        import numpy as np
        with wave.open(str(wav)) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        segs, meta = _WHISPER[model].transcribe(
            audio, language=lang or None, vad_filter=True, beam_size=1)
        events = [(s + sg.start, sg.text.strip()) for sg in segs if sg.text.strip()]
        if not events:
            return "No speech detected in this range."
        return (f"(whisper {model}, lang: {meta.language}, p={meta.language_probability:.2f})\n"
                + _cap(_render(_chunk(events, chunk_seconds)), max_chars))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- diagnostics
@tool
def doctor() -> str:
    """Self-check: versions, which optional features are installed, and whether YouTube is reachable.
    Run this first if any tool misbehaves."""
    def has(mod):
        try:
            __import__(mod)
            return "installed"
        except Exception:
            return "missing (pip install 'extras')"
    lines = [
        f"yt-dlp {yt_dlp.version.__version__}  (YouTube changes often; pip install -U yt-dlp if tools fail)",
        f"ffmpeg: {'ok' if Path(FFMPEG).exists() else 'MISSING'} ({Path(FFMPEG).name})",
        f"whisper (transcribe_video): {has('faster_whisper')}",
        f"ocr (ocr_frames): {has('rapidocr_onnxruntime')}",
        f"extra allowed hosts: {sorted(_extra_hosts()) or 'none (YouTube only)'}",
    ]
    try:
        t = time.time()
        _info("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        lines.append(f"YouTube reachable: yes ({time.time() - t:.1f}s)")
    except Exception as ex:
        lines.append(f"YouTube reachable: NO ({str(ex)[:120]})")
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
