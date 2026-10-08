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
