import json
import os
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

import ted_alerts as t

FIX = json.loads((Path(__file__).parent / "fixtures" / "ted_response.json").read_text())


def fake_poster(url, body):
    assert url == t.API_URL and body["limit"] == t.PAGE_SIZE
    return FIX


def test_build_query():
    q = t.build_query(["72", "79340000"], ["aut", "DEU"], ["cloud"], 3, "tenders", today=date(2026, 10, 8))
    assert q.startswith("publication-date>=20261005 AND buyer-country IN (AUT DEU) AND notice-type IN (cn-standard")
    assert "(classification-cpv=72* OR classification-cpv=79340000)" in q
    assert 'FT~"cloud"' in q and q.endswith("SORT BY publication-date DESC")
    assert 'notice-title~"x"' in t.build_query([], [], ['x"'], 1, "all", title_only=True)
    with pytest.raises(t.TedError):
        t.build_query(["7a"], [], [], 1, "all")


def test_normalize():
    it = t.normalize(FIX["notices"][0], "de", today=date(2026, 10, 8))
    assert it["publication_number"] and it["url"].startswith("https://ted.europa.eu/")
    assert it["published"].startswith("2026-")
    assert set(it) >= {"title", "buyer", "country", "cpv", "deadline", "days_left", "estimated_value"}


def test_pick_lang():
    assert t.pick_lang({"deu": ["A"], "eng": ["B"]}, "eng") == "B"
    assert t.pick_lang({"fra": "C"}, "eng") == "C"
    assert t.pick_lang(None, "eng") == ""


def test_state_only_new(tmp_path, capsys):
    state = tmp_path / "s.json"
    atom = tmp_path / "feed.xml"
    md = tmp_path / "new.md"
    csvf = tmp_path / "new.csv"
    args = ["--cpv", "72", "--state", str(state), "--atom", str(atom), "--markdown", str(md), "--csv", str(csvf)]
    assert t.main(args, poster=fake_poster) == 0
    n = len(FIX["notices"])
    assert f"{n} matching notices, {n} new." in capsys.readouterr().err
    assert t.main(args, poster=fake_poster) == 0
    assert f"{n} matching notices, 0 new." in capsys.readouterr().err
    root = ET.parse(atom).getroot()
    assert len(root.findall("{http://www.w3.org/2005/Atom}entry")) == n
    assert "Tenders Electronic Daily" in md.read_text()
    assert len(csvf.read_text().strip().splitlines()) == n + 1


def test_email_requires_env(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    rc = t.main(["--email", "--state", str(tmp_path / "s.json")], poster=fake_poster)
    assert rc == 2 and "SMTP_HOST" in capsys.readouterr().err


@pytest.mark.skipif(os.environ.get("SKIP_LIVE") == "1", reason="live API")
def test_live_api(tmp_path):
    assert t.main(["--cpv", "72", "--country", "AUT", "--days", "14", "--max-pages", "1",
                   "--state", str(tmp_path / "s.json")]) == 0


# ---------------------------------------------------------------- retries, pagination, state

import io
import smtplib
import urllib.error


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    import email.message
    msg = email.message.Message()
    for k, v in headers.items():
        msg[k] = v
    return urllib.error.HTTPError(t.API_URL, code, "err", msg, io.BytesIO(b"boom"))


def _opener(script):
    """Returns an opener that raises/returns the items of `script` in order."""
    calls = []

    def opener(req, timeout):
        item = script[len(calls)]
        calls.append(item)
        if isinstance(item, Exception):
            raise item
        return _Resp(json.dumps(item).encode())
    opener.calls = calls
    return opener


def test_retry_on_429_and_500_with_backoff():
    slept = []
    op = _opener([_http_error(500), _http_error(429, retry_after=7), {"notices": [], "totalNoticeCount": 0}])
    assert t.post_json(t.API_URL, {}, sleep=slept.append, opener=op) == {"notices": [], "totalNoticeCount": 0}
    assert len(op.calls) == 3
    assert 0 <= slept[0] <= 2          # first backoff: jitter in [0, 2] s
    assert slept[1] == 7               # Retry-After wins


def test_retry_gives_up_and_400_fails_fast():
    slept = []
    op = _opener([_http_error(503)] * 5)
    with pytest.raises(t.TedError, match="after 5 attempts"):
        t.post_json(t.API_URL, {}, retries=4, sleep=slept.append, opener=op)
    assert len(slept) == 4 and all(d <= 60 for d in slept)
    op = _opener([_http_error(400)])
    with pytest.raises(t.TedError, match="rejected"):
        t.post_json(t.API_URL, {}, sleep=slept.append, opener=op)
    assert len(op.calls) == 1


def test_backoff_delay_caps():
    assert all(0 <= t.backoff_delay(10) <= 60 for _ in range(50))
    assert t.backoff_delay(0, retry_after=500) == 60


def test_pagination_stops_and_warns_when_truncated(capsys):
    pages = []

    def poster(url, body):
        pages.append(body["page"])
        return {"notices": [{"publication-number": f"{body['page']}-{i}"} for i in range(t.PAGE_SIZE)],
                "totalNoticeCount": 5000}
    out = t.search("q", max_pages=3, poster=poster, sleep=lambda s: None)
    assert pages == [1, 2, 3] and len(out) == 3 * t.PAGE_SIZE
    assert "TED reports 5000 matches, fetched 300" in capsys.readouterr().err


def test_seen_ids_survive_restart_and_failed_email_is_retried(tmp_path, monkeypatch, capsys):
    state = tmp_path / "s.json"
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("MAIL_TO", "me@example.com")

    def broken_smtp(*a, **k):
        raise smtplib.SMTPConnectError(421, "down")
    monkeypatch.setattr(t.smtplib, "SMTP", broken_smtp)
    assert t.main(["--cpv", "72", "--state", str(state), "--email"], poster=fake_poster) == 2
    assert not state.exists()           # nothing marked as seen, next run retries
    monkeypatch.setattr(t, "send_email", lambda items, subject: None)
    n = len(FIX["notices"])
    assert t.main(["--cpv", "72", "--state", str(state), "--email"], poster=fake_poster) == 0
    assert set(json.loads(state.read_text())["seen"]) == {x["publication-number"] for x in FIX["notices"]}
    capsys.readouterr()
    assert t.main(["--cpv", "72", "--state", str(state)], poster=fake_poster) == 0   # "restart"
    assert f"{n} matching notices, 0 new." in capsys.readouterr().err


def test_corrupt_state_stops_instead_of_resending(tmp_path, capsys):
    state = tmp_path / "s.json"
    state.write_text("{not json")
    assert t.main(["--cpv", "72", "--state", str(state)], poster=fake_poster) == 2
    assert "not valid JSON" in capsys.readouterr().err
