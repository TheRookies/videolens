# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub: **Security tab, "Report a vulnerability"** on this repository. Do not open a public issue for something exploitable.

You can expect an acknowledgement within a few days. Please include the tool name, the input that triggers the problem and what you expected to happen.

## Design notes

- Tools accept only `http(s)` URLs on an allow-list (YouTube, TED, archive.org) by default. Extra hosts are opt-in through `VIDEOLENS_EXTRA_HOSTS`.
- Caption downloads accept only `http(s)` URLs, so a crafted caption link cannot read local files.
- ffmpeg is started without a shell and with a protocol whitelist (`http`, `https`, `tls`, `tcp`, `crypto`).
- The server does not read credentials, cookies or files from the user's machine, and it stores nothing between calls except a 10-minute in-memory cache.
- Video titles, descriptions, captions and comments are untrusted text. MCP clients should treat them as data, not instructions.
