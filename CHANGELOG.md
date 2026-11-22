# Changelog

## 0.3.0

Renamed from TubeLens to **VideoLens**: video research beyond YouTube.

### Added
- Verified support for **TED** and the **Internet Archive** (info, frames, OCR, Whisper; TED subtitles in many languages).
- WebVTT, SRT and HLS-playlist subtitle parsing, not only YouTube's format.
- `VIDEOLENS_EXTRA_HOSTS` to opt in other yt-dlp sites (the old `TUBELENS_EXTRA_HOSTS` still works).

### Fixed
- `lang="auto"` picked an arbitrary language on videos with dozens of manual subtitles; now prefers English, then the first available.
- Vimeo and other login-only sites now fail with a clear message instead of a raw error.

### Notes
- Vimeo does not work (it requires a login, which VideoLens never uses).

## 0.2.0

Renamed to **VideoLens**, packaged for `pip`, and hardened after a full audit.

### Added
- `youtube_research`: multi-search topic research with ranking and per-video briefs.
- `find_in_video`: find where a term is said, optionally with the frame.
- `ocr_frames`: read on-screen text locally.
- `transcribe_video`: local Whisper for videos without captions.
- `batch_transcripts`, `channel_stats`, `doctor`.
- `video_comments(query=...)` keyword filter; `youtube_search` filters (`sort=date`, views, length).
- `watch_video(mode="scenes")`: keeps the most distinct moments.
- `lang="auto"`: uses the video's spoken language. `@handle` and bare video IDs accepted.
- 10-minute cache; friendly error messages; offline test suite and CI.

### Fixed (found by auditing the 0.1 tools against real videos)
- `sort="date"` search crashed (yt-dlp has no date search key). Now uses YouTube's own sort filter.
- Scene detection read the whole stream and timed out after 180 s. Replaced by sampling plus image difference; frame grabs now run 4 in parallel.
- "Video total" in comments showed the number fetched, not the real total.
- Default language `en` returned nothing for non-English videos. Now auto-detected.
- Whisper crashed on current PyAV (`metadata_errors`). Audio is now decoded directly.
- Raw yt-dlp tracebacks replaced by short, actionable messages (private, age-restricted, rate-limited, unavailable).
- Channel cadence was always missing. Now estimated from the newest uploads.

### Security
- All tools accept only YouTube URLs by default; ffmpeg runs with a protocol whitelist.
- Removed the Reddit tool: Reddit blocks anonymous API access and the only workaround is spoofing a browser.

## 0.1.0

First release: video info, comments, transcript, search, channel listing and frame-based watching.
