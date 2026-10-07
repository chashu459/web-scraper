# Web Scraper → EPUB

Two small Python tools that save web content as EPUB ebooks:

- **`scrape.py`** — extracts the main article text from any web page(s) and
  saves them as an EPUB (one chapter per URL). Navigation, ads, and comments
  are stripped automatically.
- **`novel.py`** — downloads a whole novel from novellunar.com as a single
  EPUB, with cover, title, author, synopsis, and genres filled in
  automatically.

## Quick start

```bash
# Articles: one URL per chapter
./.venv/bin/python scrape.py https://example.com/article -o article.epub

# Novel: quick 2-chapter test (~1 minute)
./.venv/bin/python novel.py https://novellunar.com/novel/the-unrivaled-tang-sect \
    -o test.epub --start 1 --end 2 --min-delay 1 --max-delay 2

# Novel: the whole thing (takes a few hours at the default polite pace)
./.venv/bin/python novel.py https://novellunar.com/novel/the-unrivaled-tang-sect \
    -o tang-sect.epub
```

## Setup from scratch

You need **Python 3.9+**. Check with `python3 --version`
(macOS: `brew install python` if you don't have it).

```bash
# 1. Go to the folder containing scrape.py and novel.py
cd web-scraper

# 2. Create an isolated environment (avoids messing with system Python)
python3 -m venv .venv

# 3. Install the dependencies
./.venv/bin/pip install requests trafilatura ebooklib beautifulsoup4 lxml pillow lxml_html_clean
```

On **Windows**, step 3 uses backslashes instead:

```bat
.venv\Scripts\pip install requests trafilatura ebooklib beautifulsoup4 lxml pillow lxml_html_clean
```

and you run the scripts with `.venv\Scripts\python novel.py ...`.

## Usage

### scrape.py — articles to EPUB

```bash
./.venv/bin/python scrape.py URL [URL ...] [-o book.epub] [--title "My Book"] [--lang en]
```

| Option | What it does |
|---|---|
| `-o, --output` | Output file (default: auto-named like `scraped-20261007-013000.epub`) |
| `--title` | Book title (default: "Scraped articles") |
| `--lang` | Language code (default: `en`) |

Example — bundle several articles into one reading list:

```bash
./.venv/bin/python scrape.py URL1 URL2 URL3 -o reading-list.epub --title "Weekend reading"
```

### novel.py — novels to EPUB

```bash
./.venv/bin/python novel.py NOVEL_URL [-o book.epub] [--start N] [--end N]
    [--min-delay 2] [--max-delay 4] [--title "Title"] [--no-cover] [--no-meta]
```

| Option | What it does |
|---|---|
| `-o, --output` | Output file (default: `<novel-slug>.epub`) |
| `--title` | Book title (default: taken from the novel page) |
| `--start` / `--end` | Chapter range, e.g. `--start 1 --end 100` (supports numbers like `1.1`) |
| `--min-delay` / `--max-delay` | Random pause between chapters in seconds (default: 2–4) |
| `--no-cover` | Skip downloading the cover image |
| `--no-meta` | Skip fetching title/author/synopsis metadata |

The novel URL looks like `https://novellunar.com/novel/<slug>` — just paste
the novel's main page.

## Being polite to servers

Both scripts behave like a human reader, not a bot swarm:

- **Randomized delays** — `novel.py` waits a random 2–4 seconds between
  chapters, `scrape.py` 1.5–3 seconds between pages. No metronome timing.
- **One consistent browser identity per run** — a real browser user agent
  with matching browser headers (including `Referer`), picked fresh each run.
- **Backs off when asked** — a `429` rate-limit response honors
  `Retry-After` with exponential backoff; a `403` stops the run instead of
  hammering.
- **Sequential requests** — one at a time, never parallel.
- The site's `robots.txt` allows crawling (`Allow: /`).

A full multi-thousand-chapter novel takes a few hours at the default pace.
That is intentional — use `--start`/`--end` to grab it in volumes.

## Troubleshooting

**`externally-managed-environment` when installing** — that's why we use a
venv (step 2 above); never install these packages into system Python.

**`ImportError: lxml.html.clean module is now a separate project`** —
install the missing piece: `./.venv/bin/pip install lxml_html_clean`.

**`NotOpenSSLWarning` / LibreSSL on macOS** — harmless warning from
urllib3; requests work fine.

**"blocked by server (403)"** — the site is refusing automated access.
Wait a while and try again with gentler pacing, e.g.
`--min-delay 5 --max-delay 10`.

**A chapter/page comes back empty** — some pages are behind logins or heavy
JavaScript and can't be extracted; they're skipped with a warning rather
than failing the whole run.

## How it works

- `scrape.py` fetches each page and uses
  [trafilatura](https://trafilatura.readthedocs.io/) to extract the primary
  article content, then builds the EPUB with
  [ebooklib](https://github.com/aerkalov/ebooklib).
- `novel.py` reads the novel page's OpenGraph tags (title, author, synopsis,
  cover) and the site's public chapters API for the chapter list, then parses
  each chapter's server-rendered `<article>` element — no JavaScript or login
  needed. The cover is converted to JPEG since many e-readers can't display
  WebP.
