# Web Scraper → EPUB

Scrapes the primary article content from web pages and saves it as an EPUB
(one chapter per URL). Navigation, ads, and comments are stripped out
automatically.

## Run it

```bash
cd web-scraper
./.venv/bin/python scrape.py URL [URL ...] [-o book.epub] [--title "My Book"] [--lang en]
```

Examples:

```bash
# single article
./.venv/bin/python scrape.py https://example.com/article -o article.epub

# batch into one book
./.venv/bin/python scrape.py URL1 URL2 URL3 -o reading-list.epub --title "Weekend reading"

# without -o, the file is auto-named, e.g. scraped-20261007-013000.epub
```

## Novels (novellunar.com)

`novel.py` downloads a whole novel as one EPUB:

```bash
./.venv/bin/python novel.py https://novellunar.com/novel/the-unrivaled-tang-sect \
    -o tang-sect.epub

# or a chapter range (numbers like 1.1 are supported)
./.venv/bin/python novel.py https://novellunar.com/novel/the-unrivaled-tang-sect \
    -o vol1.epub --start 1 --end 100
```

Chapter metadata comes from the site's public chapters API; each chapter's
text is server-rendered in the page HTML, so no JS or login is needed.

Being polite: both scripts pace themselves like a human reader —
`novel.py` waits a random 2–4 seconds between chapters (tune with
`--min-delay` / `--max-delay`), `scrape.py` waits 1.5–3 seconds between
pages. The tool presents one consistent browser identity per run (like a real
browser session), with matching browser headers including Referer, and a 429
rate-limit response triggers exponential backoff instead of retrying blindly. A full
multi-thousand-chapter novel takes a few hours at the default pace —
that is the point.

## Requirements

The bundled `.venv` already has everything installed (`requests`,
`trafilatura`, `ebooklib`). If you move the script, reinstall with:

```bash
python3 -m venv .venv
./.venv/bin/pip install requests trafilatura ebooklib
```

## Notes

- Works best on article-style pages. Pages behind logins or heavy JS may
  not extract cleanly.
- Requests are spaced 1 second apart to be polite to servers.
