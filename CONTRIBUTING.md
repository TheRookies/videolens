# Contributing

Thanks for helping. TubeLens is small on purpose.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[all,dev]"     # Windows: .venv\Scripts\pip
pytest
ruff check src tests
```

## Guidelines

- Keep outputs **token-lean**. Every new tool needs sensible default limits and a way to ask for more.
- Never loosen the URL allow-list or ffmpeg protocol whitelist without a clear, documented opt-in.
- No secrets, tokens, cookies or personal paths in code, tests, fixtures or issues.
- Add an offline test for pure logic (`tests/test_offline.py`). Network behaviour is checked by hand against real videos; say which in the PR.
- No tool that logs in, posts, likes or otherwise acts on an account.

## Bug reports

Run the `doctor` tool and paste its output, plus the exact call and the error message.
