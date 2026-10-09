#!/usr/bin/env python3
"""ted-tender-alerts: get new EU public tenders that match your CPV codes, countries and keywords.

Uses the official TED (Tenders Electronic Daily) Search API v3, which needs no account or API key.
Remembers which notices it has already shown you (state file), so every run only reports new ones.
Outputs: terminal table, CSV, Markdown, an Atom feed for your feed reader, and optionally an e-mail
through your own SMTP server.

Standard library only. MIT License. https://github.com/philippprimisser-max/ted-tender-alerts
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import random
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from xml.sax.saxutils import escape

__version__ = "0.1.0"
API_URL = "https://api.ted.europa.eu/v3/notices/search"
USER_AGENT = f"ted-tender-alerts/{__version__} (+https://github.com/philippprimisser-max/ted-tender-alerts)"
PAGE_SIZE = 100
FIELDS = ["publication-number", "publication-date", "notice-type", "notice-title", "title-proc", "buyer-name",
          "buyer-country", "buyer-city", "classification-cpv", "deadline-receipt-tender-date-lot",
          "estimated-value-proc", "estimated-value-cur-proc", "links"]
NOTICE_TYPES = {
    "tenders": ["cn-standard", "cn-social", "cn-desg", "subco", "qu-sy"],
    "awards": ["can-standard", "can-social", "can-desg", "can-tran", "can-modif", "veat"],
    "prior": ["pin-only", "pin-buyer", "pin-rtl", "pin-tran", "pin-cfc-standard", "pin-cfc-social"],
}
LANG3 = {"en": "eng", "de": "deu", "fr": "fra", "it": "ita", "es": "spa", "nl": "nld", "pl": "pol"}
ATTRIBUTION = "Source: TED – Tenders Electronic Daily, © European Union, https://ted.europa.eu"


class TedError(RuntimeError):
    pass


# ---------------------------------------------------------------- query

def build_query(cpv: list[str], countries: list[str], keywords: list[str], days: int, notice_type: str,
                title_only: bool = False, today: date | None = None) -> str:
    today = today or date.today()
    parts = [f"publication-date>={(today - timedelta(days=days)).strftime('%Y%m%d')}"]
    if countries:
        parts.append(f"buyer-country IN ({' '.join(c.upper() for c in countries)})")
    if notice_type != "all":
        parts.append(f"notice-type IN ({' '.join(NOTICE_TYPES[notice_type])})")
    if cpv:
        for c in cpv:
            if not c.isdigit() or not 2 <= len(c) <= 8:
                raise TedError(f"CPV code {c!r} must be 2 to 8 digits (a prefix like 72 or 7222 works).")
        parts.append("(" + " OR ".join(f"classification-cpv={c}*" if len(c) < 8 else f"classification-cpv={c}"
                                       for c in cpv) + ")")
    if keywords:
        field = "notice-title" if title_only else "FT"
        kws = [k.replace('"', "") for k in keywords]
        parts.append("(" + " OR ".join(f'{field}~"{k}"' for k in kws) + ")")
    return " AND ".join(parts) + " SORT BY publication-date DESC"


# ---------------------------------------------------------------- API

def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Seconds from a Retry-After header (numeric form only), if the server sent one."""
    value = exc.headers.get("Retry-After") if exc.headers is not None else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def backoff_delay(attempt: int, retry_after: float | None = None, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter (2, 4, 8, 16 s ... capped); a Retry-After header wins."""
    if retry_after is not None:
        return min(retry_after, cap)
    return random.uniform(0, min(2 ** attempt * 2, cap))


def post_json(url: str, body: dict, retries: int = 4, sleep=time.sleep, opener=urllib.request.urlopen) -> dict:
    """POST with retries on 429, 5xx and network errors. 400 and other 4xx fail at once."""
    data = json.dumps(body).encode()
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT})
        wait_hint = None
        try:
            with opener(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {exc.code}: {text}"
            if exc.code == 400:
                raise TedError(f"TED rejected the query ({last})") from exc
            if exc.code != 429 and exc.code < 500:
                raise TedError(f"TED search failed ({last})") from exc
            wait_hint = _retry_after(exc)
        except urllib.error.URLError as exc:
            last = f"network error: {exc.reason}"
        except (TimeoutError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            delay = backoff_delay(attempt, wait_hint)
            print(f"TED: {last.splitlines()[0][:120]}; retry {attempt + 1}/{retries} in {delay:.1f} s", file=sys.stderr)
            sleep(delay)
    raise TedError(f"TED search failed after {retries + 1} attempts ({last})")


def search(query: str, max_pages: int = 20, poster=post_json, sleep=time.sleep) -> list[dict]:
    notices, total = [], 0
    for page in range(1, max_pages + 1):
        res = poster(API_URL, {"query": query, "fields": FIELDS, "page": page, "limit": PAGE_SIZE,
                               "paginationMode": "PAGE_NUMBER", "scope": "ALL"})
        batch = res.get("notices") or []
        total = int(res.get("totalNoticeCount") or 0)
        notices.extend(batch)
        if len(batch) < PAGE_SIZE or len(notices) >= total:
            break
        if page < max_pages:
            sleep(0.5)  # ~120 requests/min at most; TED's fair-use limit is 700/min
    if total > len(notices):
        print(f"Warning: TED reports {total} matches, fetched {len(notices)} (newest first, --max-pages {max_pages}). "
              "Narrow the query or raise --max-pages if you need all of them.", file=sys.stderr)
    return notices


# ---------------------------------------------------------------- normalising

def _first(v):
    return (v[0] if v else None) if isinstance(v, list) else v


def pick_lang(value, lang3: str) -> str:
    if isinstance(value, dict):
        for k in (lang3, "eng", "mul", *value.keys()):
            v = _first(value.get(k))
            if v:
                return str(v).strip()
        return ""
    v = _first(value)
    return "" if v is None else str(v).strip()


def normalize(n: dict, lang: str = "en", today: date | None = None) -> dict:
    today = today or date.today()
    l3 = LANG3.get(lang, "eng")
    pub = str(n.get("publication-number") or "")
    deadlines = sorted({str(d)[:10] for d in (n.get("deadline-receipt-tender-date-lot") or []) if d})
    deadline = deadlines[-1] if deadlines else ""
    days_left = None
    if deadline:
        try:
            days_left = (date.fromisoformat(deadline) - today).days
        except ValueError:
            pass
    try:
        value = float(_first(n.get("estimated-value-proc")))
    except (TypeError, ValueError):
        value = None
    links = n.get("links") or {}
    html_links = links.get("html") or {}
    url = html_links.get(l3.upper()) or html_links.get("ENG") or f"https://ted.europa.eu/{lang}/notice/-/detail/{pub}"
    return {
        "publication_number": pub,
        "published": str(_first(n.get("publication-date")) or "")[:10],
        "notice_type": str(n.get("notice-type") or ""),
        "title": pick_lang(n.get("title-proc"), l3) or pick_lang(n.get("notice-title"), l3),
        "buyer": pick_lang(n.get("buyer-name"), l3),
        "city": pick_lang(n.get("buyer-city"), l3),
        "country": ", ".join(dict.fromkeys(n.get("buyer-country") or [])),
        "cpv": ", ".join(dict.fromkeys(n.get("classification-cpv") or [])),
        "deadline": deadline,
        "days_left": days_left,
        "estimated_value": value,
        "currency": str(_first(n.get("estimated-value-cur-proc")) or ("EUR" if value else "")),
        "url": url,
    }


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"seen": {}, "recent": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Never silently start from an empty state: that would re-send every alert.
        raise TedError(f"State file {path} is not valid JSON ({exc}). Fix or delete it.") from exc
    state.setdefault("seen", {})
    state.setdefault("recent", [])
    return state


def save_state(path: Path, state: dict, keep_days: int = 120) -> None:
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["recent"] = state["recent"][:200]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def split_new(items: list[dict], state: dict) -> list[dict]:
    new = [it for it in items if it["publication_number"] not in state["seen"]]
    today = date.today().isoformat()
    for it in new:
        state["seen"][it["publication_number"]] = today
    state["recent"] = new + [r for r in state["recent"] if r["publication_number"] not in {i["publication_number"] for i in new}]
    return new


# ---------------------------------------------------------------- outputs

def money(it: dict) -> str:
    return f"{it['estimated_value']:,.0f} {it['currency']}" if it["estimated_value"] else ""


def to_table(items: list[dict]) -> str:
    lines = []
    for it in items:
        dl = f"deadline {it['deadline']} ({it['days_left']} d)" if it["deadline"] else "no deadline listed"
        extra = " | ".join(x for x in (it["country"], money(it), dl) if x)
        lines.append(f"- {it['title'][:110]}\n  {it['buyer'][:80]} | {extra}\n  {it['url']}")
    return "\n".join(lines)


def to_markdown(items: list[dict], query: str) -> str:
    rows = ["| Published | Title | Buyer | Country | Value | Deadline |", "|---|---|---|---|---|---|"]
    for it in items:
        title = it["title"].replace("|", "/")[:120]
        rows.append(f"| {it['published']} | [{title}]({it['url']}) | {it['buyer'].replace('|', '/')[:60]} | "
                    f"{it['country']} | {money(it)} | {it['deadline']} |")
    return f"# New EU tenders ({len(items)})\n\nQuery: `{query}`\n\n" + "\n".join(rows) + f"\n\n{ATTRIBUTION}\n"


def write_csv(path: Path, items: list[dict]) -> None:
    exists = path.is_file()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(items[0].keys()) if items else ["publication_number"])
        if not exists:
            w.writeheader()
        for it in items:
            w.writerow(it)


def to_atom(items: list[dict], title: str, feed_id: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for it in items[:100]:
        summary = " | ".join(x for x in (it["buyer"], it["country"], money(it),
                                         f"Deadline {it['deadline']}" if it["deadline"] else "", f"CPV {it['cpv']}") if x)
        entries.append(
            f"<entry><id>urn:ted:{escape(it['publication_number'])}</id><title>{escape(it['title'])}</title>"
            f"<link href=\"{escape(it['url'])}\"/><updated>{escape(it['published'] or now[:10])}T00:00:00Z</updated>"
            f"<summary>{escape(summary)}</summary></entry>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
            f"<title>{escape(title)}</title><id>{escape(feed_id)}</id><updated>{now}</updated>"
            f"<rights>{escape(ATTRIBUTION)}</rights><author><name>ted-tender-alerts</name></author>"
            + "".join(entries) + "</feed>\n")


def send_email(items: list[dict], subject: str) -> None:
    """Uses SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO from the environment."""
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not host or not to:
        raise TedError("For --email set SMTP_HOST and MAIL_TO (and usually SMTP_USER / SMTP_PASSWORD).")
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or to
    msg.set_content(to_table(items) + f"\n\n{ATTRIBUTION}\n")
    rows = "".join(f"<li><a href=\"{html.escape(i['url'])}\">{html.escape(i['title'])}</a><br>"
                   f"{html.escape(i['buyer'])} · {html.escape(i['country'])} · {html.escape(money(i))} · "
                   f"deadline {html.escape(i['deadline'] or 'n/a')}</li>" for i in items)
    msg.add_alternative(f"<ul>{rows}</ul><p style='color:#666'>{html.escape(ATTRIBUTION)}</p>", subtype="html")
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=60) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # The state is saved only after a successful send, so the next run tries these notices again.
        raise TedError(f"E-mail failed ({type(exc).__name__}: {exc}); state not updated, next run retries.") from exc


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ted_alerts.py", description="New EU tenders from TED that match your filters.")
    p.add_argument("--cpv", nargs="*", default=[], help="CPV codes or prefixes, e.g. 72 48 79340000")
    p.add_argument("--country", nargs="*", default=[], help="buyer countries, ISO 3166 alpha-3: AUT DEU ...")
    p.add_argument("--keywords", nargs="*", default=[], help='full-text keywords, e.g. "cloud" "data centre"')
    p.add_argument("--title-only", action="store_true", help="match keywords in the title only")
    p.add_argument("--type", default="tenders", choices=["tenders", "awards", "prior", "all"],
                   help="tenders = open calls (default), awards = results, prior = prior information")
    p.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    p.add_argument("--lang", default="en", help="preferred language for titles: en, de, fr, ... (default en)")
    p.add_argument("--state", default="ted_state.json", help="remembers notices already reported")
    p.add_argument("--all", action="store_true", help="show all matches, not only new ones")
    p.add_argument("--csv", help="append new notices to this CSV file")
    p.add_argument("--markdown", help="write new notices to this Markdown file")
    p.add_argument("--atom", help="write an Atom feed (latest 100) to this file")
    p.add_argument("--email", action="store_true", help="e-mail new notices via SMTP (see README)")
    p.add_argument("--max-pages", type=int, default=20, help="safety cap, 100 notices per page (default 20)")
    p.add_argument("--print-query", action="store_true", help="print the TED expert query and exit")
    p.add_argument("--version", action="version", version=__version__)
    return p


def main(argv=None, poster=post_json) -> int:
    args = build_parser().parse_args(argv)
    try:
        query = build_query(args.cpv, args.country, args.keywords, args.days, args.type, args.title_only)
        if args.print_query:
            print(query)
            return 0
        items = [normalize(n, args.lang) for n in search(query, args.max_pages, poster)]
        state_path = Path(args.state)
        state = load_state(state_path)
        new = split_new(items, state)
        shown = items if args.all else new
        print(f"{len(items)} matching notices, {len(new)} new.", file=sys.stderr)
        if shown:
            print(to_table(shown))
        if args.csv and new:
            write_csv(Path(args.csv), new)
        if args.markdown:
            Path(args.markdown).write_text(to_markdown(shown, query), encoding="utf-8")
        if args.atom:
            Path(args.atom).write_text(to_atom(state["recent"], "New EU tenders (TED)", "urn:ted-tender-alerts:"
                                               + hashlib.sha1(query.encode()).hexdigest()[:12]), encoding="utf-8")
        if args.email and new:
            send_email(new, f"{len(new)} new EU tender(s)")
        save_state(state_path, state)
        print(ATTRIBUTION, file=sys.stderr)
        return 0
    except TedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
