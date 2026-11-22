"""YouTube MCP server: info, comments, transcript, search, and frame-based video watching.

Uses yt-dlp (no API key) and a bundled ffmpeg (imageio-ffmpeg).
"""
import json
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("yt-mcp")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # bundled ffmpeg binary, no system install needed


_ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtube-nocookie.com",
}


def _check_url(url: str) -> str:
    """Only accept http(s) YouTube URLs. Stops a hostile link (e.g. one planted in a
    comment) from steering yt-dlp/ffmpeg at arbitrary hosts or local files."""
    u = urllib.parse.urlparse(url.strip())
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in _ALLOWED_HOSTS:
        raise ValueError("Only YouTube URLs (youtube.com, youtu.be) are allowed.")
    return url.strip()


def _ydl(**opts):
    base = {"quiet": True, "no_warnings": True, "skip_download": True}
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_time(t: str | float | int | None) -> float | None:
    """Accept seconds, 'mm:ss' or 'hh:mm:ss'."""
    if t is None or t == "":
        return None
    if isinstance(t, (int, float)):
        return float(t)
    parts = [float(p) for p in str(t).split(":")]
    out = 0.0
    for p in parts:
        out = out * 60 + p
    return out


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


def _chunk(events, window: int = 20):
    """Merge caption fragments into ~window-second lines and drop repeats.
    Raw captions are one line per ~2s with a timestamp each; merging cuts tokens a lot."""
    if window <= 0:
        return [(t, x) for t, x in events]
    out, cur_t, cur = [], None, []
    last = None
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


@mcp.tool()
def video_info(url: str, description_chars: int = 1500) -> str:
    """Get YouTube video metadata: title, channel, stats, upload date, tags, chapters, description."""
    url = _check_url(url)
    with _ydl() as y:
        i = y.extract_info(url, download=False)
    desc = (i.get("description") or "")[:description_chars]
    chapters = "\n".join(
        f"  {_fmt_time(c['start_time'])} {c['title']}" for c in (i.get("chapters") or [])
    )
    avail = sorted(set((i.get("subtitles") or {}).keys()))
    auto = (i.get("automatic_captions") or {})
    auto_note = "yes" if auto else "no"
    full = len(i.get("description") or "")
    more = f"\n...[{full - description_chars} more chars, raise description_chars]" if full > description_chars else ""
    return (
        f"Title: {i.get('title')}\n"
        f"Channel: {i.get('channel')} ({i.get('channel_follower_count')} subs)\n"
        f"Uploaded: {i.get('upload_date')}  Duration: {_fmt_time(i.get('duration') or 0)}\n"
        f"Views: {i.get('view_count')}  Likes: {i.get('like_count')}  Comments: {i.get('comment_count')}\n"
        f"Tags: {(i.get('tags') or [])[:10]}\n"
        f"Manual subs: {avail or 'none'}  Auto-captions: {auto_note}\n"
        f"Chapters:\n{chapters or '  none'}\n\n"
        f"Description:\n{desc}{more}"
    )


@mcp.tool()
def video_comments(
    url: str, max_comments: int = 30, sort: str = "top", replies: bool = False, max_chars_each: int = 300
) -> str:
    """Read YouTube comments. sort: 'top' or 'new'. replies=True also fetches replies (max 3 per thread).
    Each comment is cut to max_chars_each characters."""
    url = _check_url(url)
    # yt-dlp: max-comments, max-parents, max-replies, max-replies-per-thread (separate list items)
    rep = "30" if replies else "0"
    per = "3" if replies else "0"
    opts = {
        "getcomments": True,
        "extractor_args": {
            "youtube": {
                "comment_sort": [sort],
                "max_comments": [str(max_comments), str(max_comments), rep, per],
            }
        },
    }
    with _ydl(**opts) as y:
        i = y.extract_info(url, download=False)
    comments = i.get("comments") or []
    lines = [f"{len(comments)} comments (video total: {i.get('comment_count')})"]
    for c in comments:
        indent = "    ↳ " if c.get("parent") not in (None, "root") else "- "
        text = " ".join((c.get("text") or "").split())
        if len(text) > max_chars_each:
            text = text[:max_chars_each] + "…"
        likes = c.get("like_count") or 0
        meta = f"{likes}♥" if likes else ""
        if c.get("is_pinned"):
            meta += " pinned"
        lines.append(f"{indent}{c.get('author')} {('(' + meta.strip() + ') ') if meta else ''}{text}")
    return "\n".join(lines)


@mcp.tool()
def video_transcript(
    url: str,
    lang: str = "en",
    with_timestamps: bool = True,
    max_chars: int = 12000,
    start: str | None = None,
    end: str | None = None,
    chunk_seconds: int = 20,
) -> str:
    """Get video transcript (manual subs first, then auto-captions). lang e.g. 'en', 'tr'.
    Lines are merged into ~chunk_seconds blocks (0 = raw captions) to save tokens. Use start/end
    ('1:30') to read only part of a long video; output is capped at max_chars."""
    url = _check_url(url)
    with _ydl() as y:
        i = y.extract_info(url, download=False)
    used, events = _transcript_events(i, lang)
    if not events:
        return f"No transcript for lang '{lang}'. Try another lang; see video_info for available ones."
    s, e = _parse_time(start), _parse_time(end)
    if s is not None or e is not None:
        events = [(t, x) for t, x in events if (s is None or t >= s) and (e is None or t <= e)]
    events = _chunk(events, chunk_seconds)
    if with_timestamps:
        text = "\n".join(f"[{_fmt_time(t)}] {x}" for t, x in events)
    else:
        text = " ".join(x for _, x in events)
    if len(text) > max_chars:
        last_t = text[:max_chars].rfind("\n[")
        cut = text[:last_t] if last_t > 0 else text[:max_chars]
        text = cut + f"\n...[truncated at {len(cut)} of {len(text)} chars; use start/end to continue]"
    return f"(lang: {used})\n{text}"


@mcp.tool()
def youtube_search(query: str, n: int = 8) -> str:
    """Search YouTube. Returns title, channel, duration, views, URL."""
    with _ydl(extract_flat=True) as y:
        r = y.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in r.get("entries", []):
        out.append(
            f"- {e.get('title')} | {e.get('channel')} | {_fmt_time(e.get('duration') or 0)} | "
            f"{e.get('view_count')} views | {e.get('url')}"
        )
    return "\n".join(out) or "no results"


@mcp.tool()
def channel_videos(url: str, n: int = 10) -> str:
    """List recent videos of a channel or playlist URL (e.g. https://www.youtube.com/@name/videos)."""
    url = _check_url(url)
    with _ydl(extract_flat=True, playlistend=n) as y:
        r = y.extract_info(url, download=False)
    out = [f"{r.get('title')}"]
    for e in (r.get("entries") or [])[:n]:
        out.append(f"- {e.get('title')} | {_fmt_time(e.get('duration') or 0)} | {e.get('view_count')} views | {e.get('url')}")
    return "\n".join(out)


@mcp.tool()
def watch_video(
    url: str,
    n_frames: int = 6,
    start: str | None = None,
    end: str | None = None,
    width: int = 512,
    include_transcript: bool = True,
    lang: str = "en",
) -> list:
    """'Watch' a video: returns evenly spaced frames (images, each labelled with timestamp) plus the
    transcript for the same range. Use start/end ('90', '1:30', '0:01:30') to zoom on a segment and call
    again with a narrower range for more detail. n_frames max 24 (default 6, width 512 to save tokens;
    raise width to ~768 only when you must read small text on screen).
    """
    url = _check_url(url)
    n_frames = max(1, min(int(n_frames), 24))
    width = max(256, min(int(width), 1024))
    # one yt-dlp call: gives metadata, captions and a low-res stream URL together
    with _ydl(format="bv*[height<=480]/b[height<=480]/worst") as y:
        info = y.extract_info(url, download=False)
    dur = float(info.get("duration") or 0)
    s = _parse_time(start) or 0.0
    e = _parse_time(end) or dur
    e = min(e, dur) if dur else e
    if e <= s:
        return [f"Invalid range: start={s} end={e} duration={dur}"]

    tmp = Path(tempfile.mkdtemp(prefix="ytmcp_"))
    try:
        # ffmpeg seeks into the low-res stream URL, no full download
        fmt = (info.get("requested_formats") or [info])[0]
        stream = fmt["url"]
        headers = "".join(f"{k}: {v}\r\n" for k, v in (fmt.get("http_headers") or {}).items())
        step = (e - s) / n_frames
        out: list = [
            f"{info.get('title')} — frames from {_fmt_time(s)} to {_fmt_time(e)} "
            f"(of {_fmt_time(dur)}), {n_frames} frames, one every {step:.1f}s"
        ]
        for k in range(n_frames):
            t = s + step * (k + 0.5)
            fpath = tmp / f"f{k}.jpg"
            subprocess.run(
                [FFMPEG, "-y", "-loglevel", "error", "-protocol_whitelist", "https,http,tls,tcp,crypto", "-headers", headers, "-ss", f"{t:.2f}", "-i", stream,
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "6", str(fpath)],
                check=False, timeout=90,
            )
            if fpath.exists() and fpath.stat().st_size > 0:
                out.append(f"Frame @ {_fmt_time(t)}")
                out.append(Image(data=fpath.read_bytes(), format="jpeg"))
        if include_transcript:
            used, events = _transcript_events(info, lang)
            seg = _chunk([(t, x) for t, x in events if s <= t <= e], 15)
            if seg:
                txt = "\n".join(f"[{_fmt_time(t)}] {x}" for t, x in seg)
                if len(txt) > 5000:
                    txt = txt[:5000] + "\n...[cut; narrow start/end or use video_transcript]"
                out.append(f"Transcript ({used}):\n{txt}")
            else:
                out.append("(no transcript in range / lang)")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
