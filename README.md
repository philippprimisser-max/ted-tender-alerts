# ted-tender-alerts

Get **new EU public tenders** that match your CPV codes, countries and keywords, without an account, API key or paid tender service. One Python file, standard library only.

It queries the official [TED (Tenders Electronic Daily)](https://ted.europa.eu) Search API v3, remembers what it has already shown you, and on every run reports only the new notices: in the terminal, as CSV, Markdown, an **Atom feed** for your feed reader, or by **e-mail** through your own SMTP server. An example GitHub Actions workflow runs it every weekday for free.

```bash
python ted_alerts.py --cpv 72 48 --country AUT --days 7 --lang de
```
```
22 matching notices, 22 new.
- Commvault
  Auftraggeber sind die Republik Österreich (Bund), die Bundesbeschaffung GmbH sow | AUT | 21,000,000 EUR | deadline 2026-11-09 (32 d)
  https://ted.europa.eu/de/notice/-/detail/696492-2026
- MA 9 – Digitalisierung 2027-2031
  Magistrat der Stadt Wien - Magistratsabteilung 9 | AUT | deadline 2026-11-17 (40 d)
  ...
```
Run it again and you get `22 matching notices, 0 new.` (output from 8 October 2026)

## Why

Commercial tender alert services usually charge a subscription. If you know which CPV codes you bid on, the official TED API is enough for a personal alert, and this script does it in about 300 lines of code, with output you control (files, a feed, your own SMTP server).

## Install

Python 3.10 or newer, nothing else.

```bash
git clone https://github.com/philippprimisser-max/ted-tender-alerts
cd ted-tender-alerts
python ted_alerts.py --help
```

## Usage

```bash
# IT services (CPV 72) and software (48) in Austria and Germany, last 3 days
python ted_alerts.py --cpv 72 48 --country AUT DEU --days 3

# Keyword search in the full text, any country
python ted_alerts.py --keywords "translation services" "interpreting" --days 7

# Only in titles, contract award notices instead of open calls
python ted_alerts.py --keywords "data centre" --title-only --type awards

# Files for other tools
python ted_alerts.py --cpv 79340000 --csv tenders.csv --markdown latest.md --atom feed.xml

# See the TED expert query that is sent (paste it into ted.europa.eu's expert search to compare)
python ted_alerts.py --cpv 72 --country AUT --print-query
```

| Option | Default | |
|---|---|---|
| `--cpv` | | CPV codes or prefixes. `72` matches everything under 72000000 |
| `--country` | | buyer country, ISO alpha-3 (`AUT`, `DEU`, `FRA`, …) |
| `--keywords` | | any of these words/phrases in the notice text |
| `--title-only` | | match keywords in the title only (fewer false hits) |
| `--type` | tenders | `tenders` (open calls), `awards`, `prior` (prior information), `all` |
| `--days` | 7 | look-back window by publication date |
| `--lang` | en | preferred language for titles and buyer names, falls back to English |
| `--state` | `ted_state.json` | notices already reported (kept 120 days) |
| `--all` | | print all matches, not only new ones |
| `--csv` / `--markdown` / `--atom` | | write new notices (Atom: the latest 100) |
| `--email` | | send new notices by e-mail, see below |

Exit code is 0 on success and 2 on errors (bad CPV code, TED unreachable after retries).

### E-mail

Set these environment variables and add `--email`:

```bash
export SMTP_HOST=smtp.example.com SMTP_PORT=587 SMTP_USER=you@example.com SMTP_PASSWORD=... MAIL_TO=you@example.com
python ted_alerts.py --cpv 72 --country AUT --email
```

The password is only read from the environment and never printed or written anywhere.

### Run it daily on GitHub Actions (free)

1. Fork or copy this repository (a private repository works).
2. Copy [`examples/daily-ted-alerts.yml`](examples/daily-ted-alerts.yml) to `.github/workflows/daily-ted-alerts.yml` in your copy and edit the filters in it. (It is kept outside `.github/workflows/` here so that this repository itself doesn't run it.)
3. Optional e-mail: add `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_TO` as repository secrets and add `--email` to the command.

The workflow commits `data/ted_state.json`, `data/latest.md` and `data/feed.xml` back to the repository, so the next run knows what you've already seen. Point your feed reader at the raw URL of `data/feed.xml` (public repositories) or just read `data/latest.md`.

## Finding your CPV codes

CPV is the EU's procurement vocabulary. Look at five tenders you would have bid on in the past and note their CPV codes (they're on every TED notice). Shorter prefixes catch more: `72` is all IT services, `7222` only IT consultancy. Start broad, then narrow down once you see the noise.

## Good to know

- The search is done by the TED API itself, so results are the same as TED's expert search with that query.
- Some notices have no estimated value or no deadline in the structured data; the notice itself (link) has the details.
- The script is gentle with the API: it pauses 0.5 s between result pages (TED's fair-use limit is 700 requests per minute) and stops after 20 pages (100 notices each) by default (`--max-pages`). If TED reports more matches than were fetched, it prints a warning.
- HTTP 429, 5xx and network errors are retried up to 4 times with exponential backoff and jitter; a `Retry-After` header from TED is respected. Other 4xx errors fail at once.
- New notices are only marked as seen after all outputs (including `--email`) succeeded, so a failed run is retried on the next run instead of being lost. A corrupt state file stops the run instead of re-sending everything.
- Data: TED – Tenders Electronic Daily, © European Union. Notices may be reused free of charge with attribution ([TED legal notice](https://ted.europa.eu/en/legal-notice)). The script adds the attribution line to every output.

## Tests

```bash
pip install pytest
python -m pytest -q        # one test calls the live TED API; SKIP_LIVE=1 to skip it
```

## Related

- [EU Tender Monitor](https://apify.com/prime619/eu-tender-monitor) on Apify: a hosted, paid version ($0.003 per notice delivered) with an input form, Apify scheduling, a short summary per notice and an Excel file per run. Disclosure: I built it. This script is free and works on its own.

## License

Code written with AI assistance and tested with the commands above.

MIT, see [LICENSE](LICENSE). Not affiliated with the EU Publications Office.
