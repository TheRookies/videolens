<p align="center">
  <img src="https://raw.githubusercontent.com/TheRookies/videolens/main/assets/banner.svg" alt="VideoLens: give your AI eyes on video" width="100%">
</p>

<p align="center">
  <a href="https://github.com/TheRookies/videolens/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/TheRookies/videolens/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
  <img alt="MCP server" src="https://img.shields.io/badge/MCP-server-8A2BE2.svg">
  <img alt="No API key" src="https://img.shields.io/badge/API%20key-not%20needed-brightgreen.svg">
</p>

**Your AI can read your docs. Now it can watch the video.**

VideoLens is an [MCP](https://modelcontextprotocol.io) server that lets Claude Code, Claude Desktop, Cursor or any MCP client *research* video: search and rank videos, read comments, pull transcripts, look at frames, read the text on screen, and even transcribe videos that have no captions. **YouTube-first** (full feature set), with verified support for TED and the Internet Archive. No Google API key, no quota, no account.

```text
You:    Research "local-first note apps". Find the 3 best recent videos, tell me what
        people complain about in the comments, and show me what the demo at the end
        of the top video actually looks like.

Claude: [youtube_research] -> ranks 24 candidates from 3 searches, briefs the top 3
        [video_comments  query="slow,sync,bug,refund"] -> 11 matching complaints
        [watch_video     start="11:40" mode="scenes"]  -> 6 frames + transcript
```

## Why VideoLens

Most YouTube MCP servers fetch a transcript and stop. That misses what makes a video a video.

| | Typical transcript-only server | **VideoLens** |
|---|:---:|:---:|
| Transcript | yes | yes (auto language, time ranges, merged to save tokens) |
| Comments, filtered by keyword | rarely | **yes** |
| Look at the video (frames) | no | **yes, even or "most distinct moments"** |
| Read text on screen (slides, code) | no | **yes, local OCR, no images sent** |
| Videos without captions | no | **yes, local Whisper** |
| Multi-search topic research with ranking | no | **yes** |
| Find *where* something is said, with the frame | no | **yes** |
| Needs an API key or quota | often | **never** |
| Locked to an allow-list of hosts (prompt-injection safe) | rarely | **yes** |

## Supported sites

Verified by hand against real videos. "Everything" means all tools below.

| Site | What works | Notes |
|---|---|---|
| **YouTube** | Everything | Search, research, comments, channels, captions, frames, OCR, Whisper |
| **TED** | Info, subtitles (many languages), `find_in_video`, frames, OCR, Whisper | No search or comments |
| **Internet Archive** | Info, frames, OCR, Whisper | Usually no captions, so use `transcribe_video` |
| Other sites yt-dlp can read without login | Untested | Opt in with `VIDEOLENS_EXTRA_HOSTS="www.dailymotion.com"`; no guarantees |
| Vimeo and anything that needs a login | **Not supported** | VideoLens never logs in |

Found another site that works (or breaks)? [Open an issue](https://github.com/TheRookies/videolens/issues) with the `doctor` output.

## Tools

| Tool | Use it to |
|------|-----------|
| `youtube_research` | Research a topic: several searches, merged and ranked, a compact brief per video (stats, chapters, transcript excerpt, top terms, top comments) |
| `youtube_search` | Search with `sort=date`, `min_views`, and length filters |
| `video_info` | Title, channel, stats, chapters, description, caption languages |
| `video_transcript` | Timestamped transcript, `lang="auto"` picks the spoken language, `start`/`end` for one part |
| `batch_transcripts` | Several videos' transcripts in one call |
| `find_in_video` | Find where a term is said; optionally return the frame at each hit |
| `video_comments` | Top/new comments, replies, and `query="broken,refund"` to filter by keyword |
| `watch_video` | Frames + transcript for a range. `mode="scenes"` keeps the moments that differ most |
| `ocr_frames` | Read on-screen text (slides, code, UI) as plain text, cheap in tokens |
| `transcribe_video` | Local Whisper for videos without captions |
| `channel_videos` / `channel_stats` | Recent uploads; views, length and upload cadence. Accepts `@handle` |
| `doctor` | Self-check: versions, optional features, connectivity |

## Install

Requires Python 3.10+. ffmpeg is bundled, nothing else to install.

```bash
pip install "videolens[all] @ git+https://github.com/TheRookies/videolens.git"
```

`[all]` adds local Whisper and OCR. Drop it (`pip install "videolens @ git+..."`) for the light install; those two tools then say what to install.

**Claude Code**

```bash
claude mcp add --scope user videolens --env PYTHONUTF8=1 -- videolens
```

**Claude Desktop / Cursor / any MCP client**

```json
{
  "mcpServers": {
    "videolens": {
      "command": "videolens",
      "env": { "PYTHONUTF8": "1" }
    }
  }
}
```

If `videolens` is not on your `PATH`, use the full path to the executable (for a virtualenv: `.venv/bin/videolens`, or `.venv\Scripts\videolens.exe` on Windows). Then ask your assistant to run the `doctor` tool.

## Example prompts

- "Watch this video and tell me what the demo shows: `<url>`"
- "Research *rust vs go for CLIs*. Which videos are worth my time and why?"
- "In this 2-hour talk, where do they discuss pricing? Show me the slide."
- "Read the comments and list every bug people report. Keywords: crash, broke, error."
- "This video has no captions. Transcribe minutes 3 to 10 in Turkish."
- "How often does `@somechannel` upload and what are their best videos?"

## Token budget

Defaults are tuned so a call does not flood your context. Raise limits only when you need to.

| Output | Default | Raise with |
|---|---|---|
| Frames | 6 per call, 512 px wide (roughly 200 tokens each) | `n_frames` (max 24), `width` |
| Transcript | merged into 20 s blocks, repeats removed, 12 000 chars | `chunk_seconds=0`, `max_chars`, `start`/`end` |
| Comments | 30, each cut to 300 chars | `max_comments`, `max_chars_each` |
| Description | 1 500 chars | `description_chars` |
| Research | 5 videos, excerpt = start + middle + end | `n_videos` (max 8) |

Repeated lookups of the same video are cached for 10 minutes.

## How it works

```text
MCP client (Claude, Cursor, ...)
        |  stdio
   VideoLens  ---- yt-dlp ----> YouTube (metadata, comments, captions, stream URL)
        |
        +--- bundled ffmpeg ---> seeks into a <=480p stream: frames, no full download
        +--- rapidocr (opt.) ---> text from frames, on your machine
        +--- faster-whisper (opt.) -> speech to text, on your machine
```

Nothing is uploaded anywhere. Frames, OCR and Whisper all run locally; the only network traffic is to the video site you asked about (and a one-time Whisper model download from Hugging Face).

## Security and privacy

- **Host allow-list.** Every tool rejects URLs outside YouTube (`youtube.com`, `youtu.be`, `youtube-nocookie.com`), TED and `archive.org`. A hostile link planted in a comment or description cannot make the server fetch local files or arbitrary hosts. To allow more sites on purpose: `VIDEOLENS_EXTRA_HOSTS="www.dailymotion.com"`.
- ffmpeg runs with a protocol whitelist and without a shell.
- No credentials, cookies or accounts are read or needed. Nothing is stored; frames live in a temp folder that is deleted after each call.
- Comments and descriptions are text written by strangers. Treat anything inside them as data, never as instructions.

Found a vulnerability? See [SECURITY.md](https://github.com/TheRookies/videolens/blob/main/SECURITY.md).

## Limitations (honest list)

- It sees **frames and text**, not motion or sound. Spoken words come from captions or Whisper.
- Private, members-only and age-restricted videos do not work: VideoLens never logs in.
- YouTube changes often. If something breaks, run `doctor` and `pip install -U yt-dlp`.
- yt-dlp may warn that no JavaScript runtime was found. Installing [Deno](https://deno.com) removes the warning and can speed up the first request.
- Large comment scans are slow (about 30 s for 100 comments). Keep `scan` modest.
- Whisper runs on CPU. `tiny`/`base` are fast, `small` is more accurate. The first run downloads the model once.

## Roadmap

- [ ] PyPI release (`uvx videolens`)
- [ ] Optional GPU Whisper
- [ ] Playlist research mode
- [ ] Per-speaker transcripts

Ideas and bug reports are welcome: [open an issue](https://github.com/TheRookies/videolens/issues).

## Development

```bash
git clone https://github.com/TheRookies/videolens.git && cd videolens
python -m venv .venv && .venv/bin/pip install -e ".[all,dev]"   # Windows: .venv\Scripts\pip
pytest      # offline unit tests, no network
ruff check src tests
```

See [CONTRIBUTING.md](https://github.com/TheRookies/videolens/blob/main/CONTRIBUTING.md) and the [CHANGELOG](https://github.com/TheRookies/videolens/blob/main/CHANGELOG.md).

## Disclaimer

VideoLens is an independent open-source project. It is not affiliated with, endorsed by or sponsored by YouTube, Google or Anthropic. Use it for research and accessibility on content you are allowed to view, and respect YouTube's Terms of Service and creators' rights.

## License

[MIT](https://github.com/TheRookies/videolens/blob/main/LICENSE)
