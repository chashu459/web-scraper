# Web Scraper → EPUB

Two small Python tools that save web content as EPUB ebooks:

- **`scrape.py`** — extracts the main article text from any web page(s) and
  saves them as an EPUB (one chapter per URL). Navigation, ads, and comments
  are stripped automatically.
- **`novel.py`** — downloads a whole novel from novellunar.com as a single
  EPUB, with cover, title, author, synopsis, and genres filled in
  automatically.
- **`wp_novel.py`** — downloads a novel from a WordPress translation blog
  (e.g. bluesilvertranslations.wordpress.com) as a single EPUB. Chapter
  lists grouped under volume headings are parsed, and chapter text is pulled
  from the embedded Google Docs each post links to. Volumes appear as
  sections in the book's table of contents.
- **`wuxia_novel.py`** — downloads a novel from wuxiaworld.com as a single
  EPUB, with cover, title, and author filled in automatically. The
  novel-specific chapter URL prefix and the total chapter count are
  discovered from the novel page.
- **`dedup_cache.py`** — removes duplicate chapters from a `novel.py`
  cache directory.

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

# WordPress blog novel: quick 2-chapter test
./.venv/bin/python wp_novel.py https://bluesilvertranslations.wordpress.com/chapter-list/ \
    -o test.epub --start 1 --end 2 --min-delay 1 --max-delay 2

# Wuxiaworld novel: quick 3-chapter test (~1 minute)
./.venv/bin/python wuxia_novel.py https://www.wuxiaworld.com/novel/rebirth-of-tang-san \
    -o test.epub --start 0 --end 2 --min-delay 1 --max-delay 2

# Wuxiaworld novel: the whole thing (~1 hour at the default polite pace)
./.venv/bin/python wuxia_novel.py https://www.wuxiaworld.com/novel/rebirth-of-tang-san \
    -o rebirth-of-tang-san.epub

# Novellunar text + wuxiaworld titles: quick 2-chapter test
./.venv/bin/python novel_wuxia.py https://novellunar.com/novel/soul-land-5-the-rebirth-of-tang-san \
    --wuxia-slug rebirth-of-tang-san -o test.epub --start 1 --end 2 --min-delay 1 --max-delay 2
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
| `--cache-dir` | Where to keep downloaded chapters (default: `./.cache/<slug>/`) |
| `--refresh` | Ignore the cache and re-download everything |
| `--offline` | Don't fetch anything online; rebuild the EPUB from cached chapters only |
| `--no-dedup` | Keep chapters with identical text (default: skip duplicates) |

The novel URL looks like `https://novellunar.com/novel/<slug>` — just paste
the novel's main page.

### wuxia_novel.py — Wuxiaworld novels to EPUB

```bash
./.venv/bin/python wuxia_novel.py NOVEL_URL [-o book.epub] [--start N] [--end N]
    [--min-delay 2] [--max-delay 4] [--title "Title"] [--no-cover] [--no-meta]
```

| Option | What it does |
|---|---|
| `-o, --output` | Output file (default: `<novel-slug>.epub`) |
| `--title` | Book title (default: taken from the novel page) |
| `--start` / `--end` | Chapter range, e.g. `--start 0 --end 100` |
| `--min-delay` / `--max-delay` | Random pause between chapters in seconds (default: 2–4) |
| `--no-cover` | Skip downloading the cover image |
| `--no-meta` | Skip fetching title/author/count metadata (not supported for wuxiaworld) |
| `--cache-dir` | Where to keep downloaded chapters (default: `./.cache/wuxia-<slug>/`) |
| `--refresh` | Ignore the cache and re-download everything |
| `--no-dedup` | Keep chapters with identical text (default: skip duplicates) |

The novel URL looks like `https://www.wuxiaworld.com/novel/<slug>`. Chapter
numbers on wuxiaworld start at **0** (chapter 0 is usually the prologue).
The novel-specific chapter URL prefix (e.g. `sl5-chapter-`) and the total
chapter count are discovered from the novel page automatically.

Known limitation: the newest/final chapter of a novel may be login-gated on
the site (only a teaser renders for anonymous visitors). Such chapters are
skipped and reported at the end of the run rather than failing it.

### novel_wuxia.py — novellunar text with wuxiaworld titles

```bash
./.venv/bin/python novel_wuxia.py NOVEL_URL --wuxia-slug WUXIA_SLUG
    [-o book.epub] [--start N] [--end N] [--title-offset -1]
    [--min-delay 2] [--max-delay 4] [--title "Title"]
    [--no-cover] [--no-meta]
```

| Option | What it does |
|---|---|
| `--wuxia-slug` | **(required)** the novel's slug on wuxiaworld.com, e.g. `rebirth-of-tang-san` |
| `--title-offset` | Wuxiaworld chapter number = novellunar number + offset (default: `-1`) |
| `--title-cache-dir` | Where to keep fetched titles (default: `./.cache/wuxia-titles-<slug>/`) |

Chapter text comes from novellunar (whose titles are generic "Chapter N"),
and each chapter is re-titled with the matching wuxiaworld chapter title.
The offset was verified for Soul Land 5 (novellunar ch. 1 = wuxiaworld ch. 0
"Prologue"); adjust it if another novel's numbering differs. Text caching is
shared with `novel.py`, so chapters you already downloaded are reused.
Wuxiaworld renders chapter titles even for login-gated chapters (only the
body is gated), so titles work without logging in; the " (Teaser)" suffix on
gated titles is stripped automatically.

### Finishing Soul Land 5 via wuxiaworld's daily unlocks

Wuxiaworld login-gates its newest chapters, but logged-in readers get free
daily check-in keys plus Wait-to-Unlock free chapters. Two helpers support
that flow:

- **`fetch_unlocked.py`** — downloads the full text of chapters you've
  unlocked on your wuxiaworld account, using a session exported from your
  logged-in browser (the browser never sees the prose; it only exports the
  session cookies/tokens as JSON):
  ```bash
  ./.venv/bin/python fetch_unlocked.py --auth wuxia-cookies.json --chapters 1101-1110
  ```
  Saves to `./.cache/wuxia-unlocked-rebirth-of-tang-san/`.
- **`build_sl5_epub.py`** — assembles the complete book: novellunar chapters
  1–1101 plus every unlocked wuxiaworld chapter 1101–1183:
  ```bash
  ./.venv/bin/python build_sl5_epub.py -o soul-land-5.epub
  ```
  Re-run it as more chapters unlock; the book grows each time. Add `--offline`
  to rebuild purely from the local caches, without fetching metadata or the
  cover from the network.

### Duplicate chapters

Some sites list stale duplicate chapters (same text under two different
numbers — e.g. chapter 2 duplicating chapter 1.1). The tool hashes each
chapter's text and skips exact duplicates so the book doesn't repeat
itself, logging which chapter each dupe matched. Deduplication is per run;
use `--no-dedup` to keep everything as listed.

### Resuming interrupted runs

Downloaded chapters are cached as JSON files in `./.cache/<slug>/`, so if
the run is interrupted (or crashes), just run the same command again — only
the missing chapters are fetched, and the EPUB is rebuilt from everything.
Corrupt or empty cache files are automatically re-downloaded. Use
`--refresh` to force a full re-download, or `--cache-dir` to put the cache
somewhere else.

### Cleaning duplicates from an existing cache

If you downloaded chapters before deduplication existed, clean the cache
with the standalone script (it uses the same text-hashing as the EPUB
build):

```bash
./.venv/bin/python dedup_cache.py .cache/the-unrivaled-tang-sect --dry-run  # preview
./.venv/bin/python dedup_cache.py .cache/the-unrivaled-tang-sect           # quarantine dupes
```

For each set of identical chapters it keeps the lowest-numbered one and
moves the rest to `<cache>/_duplicates/` (recoverable). `--delete` removes
them permanently instead. After cleaning, re-running `novel.py` rebuilds
the EPUB without the repeats.

## wp_novel.py — WordPress blog novels to EPUB

```bash
./.venv/bin/python wp_novel.py CHAPTER_LIST_URL [-o book.epub] [--start N] [--end N]
    [--min-delay 2] [--max-delay 4] [--title "Title"] [--author "Author"]
    [--cache-dir DIR] [--refresh] [--no-dedup] [--dry-run]
```

| Option | What it does |
|---|---|
| `-o, --output` | Output file (default: `wordpress-novel.epub`) |
| `--title` / `--author` | Book title/author (default: parsed from the blog, e.g. "Douluo Dalu" by "Tang Jia San Shao") |
| `--start` / `--end` | Chapter range, 1-based positions in the list |
| `--min-delay` / `--max-delay` | Random pause between chapters in seconds (default: 2–4) |
| `--gdoc-delay` | Extra fixed pause after each Google Docs fetch, in seconds (default: 5) |
| `--cache-dir` | Where to keep downloaded chapters (default: `./.cache/<blog-domain>/`) |
| `--refresh` | Ignore the cache and re-download everything |
| `--no-dedup` | Keep chapters with identical text (default: skip duplicates) |
| `--dry-run` | List chapters that would be downloaded, then exit |

How it works: the chapter-list page's volume sections are parsed in order
(if the page repeats the list, the last complete listing wins); each
chapter post's Google Docs link is followed and the published doc's
paragraphs extracted. Volumes become sections in the book's table of
contents. Same politeness rules as `novel.py` (random delays, backoff on
rate limits), plus the same chapter cache for resumable runs. Cache files
are keyed by chapter slug plus a hash of the full URL, so generic slugs
can't collide; caches from older versions are migrated automatically on
the next run.

## Being polite to servers

All scripts behave like a human reader, not a bot swarm:

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

**`wp_novel.py` seems stuck on a chapter** — it's probably retrying with
backoff (now logged to stderr). Some chapter posts have malformed Google
Docs links (literal quotes pasted into the href); those are detected and
skipped in favor of the working embed URL.

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
- `wuxia_novel.py` reads the novel page's `<h1>`, JSON-LD author, cover from
  `og:image`, chapter count from its embedded data attribute, and discovers
  the novel-specific chapter slug prefix (e.g. `sl5-chapter-`) from chapter
  links. Each chapter page is server-rendered, so text is parsed straight
  from `div.chapter-content div.chapter-body > p`, with the per-paragraph
  comment badges stripped. The WebP cover is converted to JPEG.
