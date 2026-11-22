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


@mcp.tool()
def video_info(url: str, description_chars: int = 4000) -> str:
    """Get YouTube video metadata: title, channel, stats, upload date, tags, chapters, description."""
    url = _check_url(url)
    with _ydl() as y:
        i = y.extract_info(url, download=False)
    desc = (i.get("description") or "")[:description_chars]
    chapters = "\n".join(
        f"  {_fmt_time(c['start_time'])} {c['title']}" for c in (i.get("chapters") or [])
    )
    avail = sorted(set((i.get("subtitles") or {}).keys()))
    auto = sorted((i.get("automatic_captions") or {}).keys())
    return (
        f"Title: {i.get('title')}\n"
        f"URL: {i.get('webpage_url')}\n"
        f"Channel: {i.get('channel')} ({i.get('channel_follower_count')} subs)\n"
        f"Uploaded: {i.get('upload_date')}  Duration: {_fmt_time(i.get('duration') or 0)}\n"
        f"Views: {i.get('view_count')}  Likes: {i.get('like_count')}  Comments: {i.get('comment_count')}\n"
        f"Categories: {i.get('categories')}  Tags: {(i.get('tags') or [])[:20]}\n"
        f"Manual subs: {avail}\nAuto-caption langs: {len(auto)} (e.g. {auto[:8]})\n"
        f"Chapters:\n{chapters or '  none'}\n\n"
        f"Description:\n{desc}"
    )


@mcp.tool()
def video_comments(url: str, max_comments: int = 50, sort: str = "top", replies: bool = False) -> str:
    """Read YouTube comments. sort: 'top' or 'new'. replies=True also fetches replies (max 3 per thread)."""
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
        lines.append(
            f"{indent}[{c.get('author')}] ({c.get('like_count', 0)} likes"
            f"{', pinned' if c.get('is_pinned') else ''}) {c.get('text', '').strip()}"
        )
    return "\n".join(lines)


@mcp.tool()
def video_transcript(url: str, lang: str = "en", with_timestamps: bool = True, max_chars: int = 30000) -> str:
    """Get video transcript (manual subs first, then auto-captions). lang e.g. 'en', 'tr'."""
    url = _check_url(url)
    with _ydl() as y:
        i = y.extract_info(url, download=False)
    used, events = _transcript_events(i, lang)
    if not events:
        return f"No transcript for lang '{lang}'. Try another lang; see video_info for available ones."
    if with_timestamps:
        text = "\n".join(f"[{_fmt_time(t)}] {s}" for t, s in events)
    else:
        text = " ".join(s for _, s in events)
    note = f"(lang: {used}, {len(events)} segments)\n"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[truncated, {len(text)} chars total]"
    return note + text


@mcp.tool()
def youtube_search(query: str, n: int = 10) -> str:
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
def channel_videos(url: str, n: int = 15) -> str:
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
    n_frames: int = 8,
    start: str | None = None,
    end: str | None = None,
    width: int = 768,
    include_transcript: bool = True,
    lang: str = "en",
) -> list:
    """'Watch' a video: returns evenly spaced frames (images, each labelled with timestamp) plus the
    transcript for the same range. Use start/end ('90', '1:30', '0:01:30') to zoom on a segment and call
    again with a narrower range for more detail. n_frames max 24.
    """
    url = _check_url(url)
    n_frames = max(1, min(int(n_frames), 24))
    with _ydl() as y:
        info = y.extract_info(url, download=False)
    dur = float(info.get("duration") or 0)
    s = _parse_time(start) or 0.0
    e = _parse_time(end) or dur
    e = min(e, dur) if dur else e
    if e <= s:
        return [f"Invalid range: start={s} end={e} duration={dur}"]

    tmp = Path(tempfile.mkdtemp(prefix="ytmcp_"))
    try:
        # resolve a low-res direct stream URL; ffmpeg seeks into it, no full download
        with _ydl(format="bv*[height<=480]/b[height<=480]/worst") as y:
            sel = y.extract_info(url, download=False)
        fmt = (sel.get("requested_formats") or [sel])[0]
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
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(fpath)],
                check=False, timeout=90,
            )
            if fpath.exists() and fpath.stat().st_size > 0:
                out.append(f"Frame @ {_fmt_time(t)}")
                out.append(Image(data=fpath.read_bytes(), format="jpeg"))
        if include_transcript:
            used, events = _transcript_events(info, lang)
            seg = [(t, x) for t, x in events if s <= t <= e]
            if seg:
                txt = "\n".join(f"[{_fmt_time(t)}] {x}" for t, x in seg)
                out.append(f"Transcript ({used}) for range:\n{txt[:15000]}")
            else:
                out.append("(no transcript in range / lang)")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
