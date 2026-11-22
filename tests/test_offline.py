"""Offline unit tests: no network, no YouTube. Run with `pytest`."""
import pytest

from tubelens import server as s


def test_parse_time():
    assert s._parse_time("1:30") == 90
    assert s._parse_time("0:01:30") == 90
    assert s._parse_time(45) == 45.0
    assert s._parse_time(None) is None
    assert s._parse_time("") is None


def test_fmt_time():
    assert s._fmt_time(59) == "0:59"
    assert s._fmt_time(3661) == "1:01:01"


def test_num():
    assert s._num(None) == "?"
    assert s._num(950) == "950"
    assert s._num(1500) == "1.5K"
    assert s._num(2_000_000) == "2M"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    "https://youtu.be/jNQXAC9IVRw",
    "https://m.youtube.com/watch?v=jNQXAC9IVRw",
])
def test_allowed_urls(url):
    assert s._check_url(url) == url


def test_bare_video_id_is_expanded():
    assert s._check_url("jNQXAC9IVRw") == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "http://127.0.0.1:8080/x",
    "https://evil.example/watch?v=1",
    "https://www.youtube.com.evil.example/x",
    "ftp://youtube.com/x",
    "javascript:alert(1)",
])
def test_blocked_urls(url):
    with pytest.raises(ValueError):
        s._check_url(url)


def test_extra_hosts_are_opt_in(monkeypatch):
    monkeypatch.delenv("TUBELENS_EXTRA_HOSTS", raising=False)
    with pytest.raises(ValueError):
        s._check_url("https://vimeo.com/1")
    monkeypatch.setenv("TUBELENS_EXTRA_HOSTS", "vimeo.com")
    assert s._check_url("https://vimeo.com/1") == "https://vimeo.com/1"


def test_channel_url_normalisation():
    assert s._channel_url("@Fireship") == "https://www.youtube.com/@Fireship/videos"
    assert s._channel_url("https://www.youtube.com/@Fireship") == "https://www.youtube.com/@Fireship/videos"
    assert s._channel_url("https://www.youtube.com/@Fireship/videos") == "https://www.youtube.com/@Fireship/videos"


def test_chunk_merges_and_dedupes():
    ev = [(0, "a"), (2, "b"), (2.5, "b"), (21, "c"), (23, "d")]
    assert s._chunk(ev, 20) == [(0, "a b"), (21, "c d")]
    assert s._chunk(ev, 0) == ev


def test_cap_truncates_on_line_boundary():
    text = "\n".join(f"[0:{i:02d}] line {i}" for i in range(50))
    out = s._cap(text, 100)
    assert "truncated" in out
    assert len(out) < len(text)


def test_pick_lang():
    assert s._pick_lang({}, "tr") == "tr"
    assert s._pick_lang({"language": "tr"}, "auto") == "tr"
    assert s._pick_lang({"automatic_captions": {"de-orig": [], "en": []}}, "auto") == "de"
    assert s._pick_lang({}, "auto") == "en"


def test_top_terms_ignores_stopwords():
    terms = s._top_terms("python python python the with that notebook notebook", 2)
    assert terms[0] == "python"
    assert "that" not in terms


def test_age_days():
    assert s._age_days(None) is None
    assert s._age_days("garbage") is None
    assert s._age_days("20000101") > 9000
