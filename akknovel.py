#!/usr/bin/env python3
"""Download a novel from akknovel.com and save all chapters as one EPUB.

Usage:
    python akknovel.py https://www.akknovel.com/series/<slug> \\
        [-o <slug>.epub] [--start 1] [--end 50]
        [--min-delay 2] [--max-delay 4] [--no-cover] [--no-meta]
        [--cache-dir DIR] [--refresh] [--offline] [--no-dedup]

Resuming: downloaded chapters are cached in ./.cache/akknovel-<slug>/
(as JSON), so interrupting the run and starting it again only fetches
what is missing. Use --refresh to re-download everything.

How it works (Astro site, fully server-rendered — no JS or login needed):
- The series page embeds the whole chapter list in div#chapters: one
  link per chapter, "Ch.N Title", pointing at
  /series/<slug>/chapter-<n>-<title-slug>. One request gets all titles.
- Each chapter page carries its text in <article id="chapter-content">
  as plain <p> children (script/ad tags inside the article are ignored).

Politeness: requests are sequential with a random delay between them
(default 2–4s), one browser user agent is used for the whole run, headers
mimic a real browser, and rate-limit (429) responses trigger exponential
backoff. The site has no robots.txt, so nothing disallows crawling.

Sessions: a long-lived HTTP session can go stale after a few hundred
requests — every fetch starts timing out even though the site answers a
fresh connection fine. So the session is recreated every 100 fresh
downloads by default (see --session-refresh); cached chapters don't
count, since they make no requests.
"""
import argparse
import hashlib
import html as htmlmod
import io
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image

from scrape import chapter_html, fetch_with_retry, polite_sleep, make_session, load_cookies

BASE = "https://www.akknovel.com"


def series_slug(series_url):
    """'the-girl-who-...' from .../series/the-girl-who-... (trailing slash ok)."""
    m = re.search(r"/series/([^/?#]+)", series_url or "")
    if not m:
        raise ValueError(f"not an akknovel series URL: {series_url}")
    return m.group(1).rstrip("/")


def get_novel_meta(session, series_url):
    """Fetch title, author, synopsis and cover URL from the series page."""
    r = fetch_with_retry(session, series_url, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")

    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)

    page_text = soup.get_text(" ", strip=True)
    author = None
    m = re.search(r"Author:\s*(.+?)\s*(?:Last chapter|Status|Completed|Ongoing|$)",
                  page_text)
    if m:
        author = htmlmod.unescape(m.group(1)).strip() or None

    synopsis = None
    intro = soup.find("div", id="intro")
    if intro:
        synopsis = re.sub(r"[ \t]+", " ",
                          intro.get_text("\n", strip=True)).strip()
        synopsis = re.sub(r"\n{3,}", "\n\n", synopsis) or None

    cover_url = None
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if src.startswith("https://cdn.akknovel.com/"):
            cover_url = src
            break

    status = None
    m = re.search(r"\b(Completed|Ongoing)\b", page_text)
    if m:
        status = m.group(1)

    return {
        "title": title,
        "author": author,
        "description": synopsis,
        "genres": [],
        "cover_url": cover_url,
        "status": status,
    }


def clean_title(raw_title, number):
    """Turn 'Ch.1 Enter the Palace with a Decree' into
    'Chapter 1: Enter the Palace with a Decree'. Falls back to
    'Chapter <n>'."""
    name = (raw_title or "").strip()
    for pat in (rf"^Ch\.\s*{number}\s+",
                rf"^Chapter\s*{number}\s*[-: ]\s*",
                rf"^Chapter\s*{number}\s+",
                rf"^{number}:\s*"):
        name = re.sub(pat, "", name, count=1)
    name = name.strip()
    if not name:
        return f"Chapter {number}"
    return f"Chapter {number}: {name}"


def get_chapters(session, series_url):
    """Return [(number, title, url)] for every chapter, oldest first."""
    r = fetch_with_retry(session, series_url, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")
    container = soup.find("div", id="chapters")
    if not container:
        raise RuntimeError("could not find the chapter list (div#chapters)")
    chapters = []
    for a in container.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/chapter-(\d+)-", href)
        if not m:
            continue
        number = int(m.group(1))
        raw_title = a.get_text(" ", strip=True)
        url = href if href.startswith("http") else BASE + href
        chapters.append((number, raw_title, url))
    chapters.sort(key=lambda c: c[0])
    if not chapters:
        raise RuntimeError("chapter list parsed but no chapter links found")
    print(f"  chapter list: {len(chapters)} chapters (from div#chapters)")
    return chapters


def chapter_key(number, url):
    """Collision-proof cache key: chapter number + hash of its URL."""
    num = "%g" % number
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{num}-{digest}"


def old_chapter_key(number):
    """Pre-fix key format (bare number), for migrating existing caches."""
    return f"{number}"


def migrate_cache_keys(cache_dir, chapters):
    """Rename old-format cache files to the collision-proof format."""
    if not cache_dir or not os.path.isdir(cache_dir):
        return
    by_old = {}
    for number, _title, url in chapters:
        by_old.setdefault(old_chapter_key(number), []).append(
            (number, chapter_key(number, url)))
    n = 0
    for old_key, pairs in by_old.items():
        if len(pairs) != 1:
            continue
        _, new_key = pairs[0]
        old_p = cache_path(cache_dir, old_key)
        new_p = cache_path(cache_dir, new_key)
        if not os.path.exists(old_p) or old_p == new_p:
            continue
        if os.path.exists(new_p):
            os.remove(old_p)  # stale duplicate of an already-migrated file
        else:
            os.replace(old_p, new_p)
        n += 1
    if n:
        print(f"migrated {n} cached chapter(s) to the new key format")


def chapters_from_cache(cache_dir):
    """Return [(number, title)] from cached chapter JSON files, oldest first.

    Used by --offline: rebuilds the book purely from the local cache without
    touching the network.
    """
    chapters = []
    if not cache_dir or not os.path.isdir(cache_dir):
        return chapters
    for fname in os.listdir(cache_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(cache_dir, fname), encoding="utf-8") as f:
                d = json.load(f)
            number = d.get("number")
            if number is None or not str(d.get("text", "")).strip():
                continue
            number = float(number)
            if number.is_integer():
                number = int(number)
            chapters.append((number, d.get("title")))
        except Exception:
            continue
    chapters.sort(key=lambda c: c[0])
    return chapters


def cache_path(cache_dir, key):
    return os.path.join(cache_dir, f"{key}.json")


def load_cached(cache_dir, key):
    """Return cached chapter dict, or None if missing/corrupt/empty."""
    if not cache_dir:
        return None
    p = cache_path(cache_dir, key)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("text", "").strip():
            return d
    except Exception:
        pass
    return None


def save_cached(cache_dir, key, number, title, text):
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache_path(cache_dir, key) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"number": number, "title": title, "text": text}, f)
    os.replace(tmp, cache_path(cache_dir, key))


def get_cover_bytes(session, cover_url):
    """Download cover art and normalize to JPEG (best e-reader support)."""
    r = fetch_with_retry(session, cover_url, timeout=30)
    data = r.content
    ctype = r.headers.get("Content-Type", "")
    if "jpeg" in ctype or data[:2] == b"\xff\xd8":
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def page_title(soup, number):
    """Chapter title from the chapter page itself (<title> or h1)."""
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
    return clean_title(title, number) if title else f"Chapter {number}"


def get_chapter_text(session, url, number, timeout=30):
    r = fetch_with_retry(session, url, timeout=timeout, referer=BASE + "/")
    soup = BeautifulSoup(r.text, "lxml")
    article = soup.select_one("article#chapter-content")
    if not article:
        return None, None
    paras = [p.get_text(" ", strip=True)
             for p in article.find_all("p", recursive=False)]
    text = "\n\n".join(p for p in paras if p)
    return (text or None), page_title(soup, number)

def build_epub(chapters, session, out_path, book_title, slug,
               min_delay, max_delay, start, end, meta=None,
               cover_bytes=None, cache_dir=None, refresh=False,
               no_dedup=False, offline=False, session_factory=None,
               session_refresh=100):
    book = epub.EpubBook()
    book.set_identifier(f"akknovel-{slug}-{int(time.time())}")
    book.set_title(book_title or (meta or {}).get("title") or slug)
    book.set_language("en")
    author = (meta or {}).get("author")
    book.add_author(author or "Unknown")
    description = (meta or {}).get("description")
    if description:
        book.add_metadata("DC", "description", description)
    for genre in (meta or {}).get("genres", []):
        book.add_metadata("DC", "subject", genre)
    if cover_bytes:
        book.set_cover("images/cover.jpg", cover_bytes)

    selected = [c for c in chapters if (start is None or c[0] >= start)
                and (end is None or c[0] <= end)]
    toc = []
    n_cached = n_fresh = n_dupes = 0
    n_since_refresh = 0  # fresh downloads on the current session
    seen_hashes = {}  # normalized-text hash -> chapter number
    for i, entry in enumerate(selected, 1):
        number, title = entry[0], entry[1]
        url = entry[2] if len(entry) > 2 else None
        chapter_title = clean_title(title, number) if title else f"Chapter {number}"
        key = chapter_key(number, url) if url else old_chapter_key(number)
        cached = None if refresh else load_cached(cache_dir, key)
        if cached:
            text = cached["text"]
            chapter_title = cached.get("title") or chapter_title
            print(f"[{i}/{len(selected)}] chapter {number}: from cache")
            n_cached += 1
        elif offline:
            print(f"[{i}/{len(selected)}] chapter {number}: not in cache, "
                  f"skipped (offline)")
            continue
        else:
            print(f"[{i}/{len(selected)}] chapter {number}: {chapter_title[:60]}")
            try:
                text, page_chapter_title = get_chapter_text(session, url, number)
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                continue
            if not text:
                print("    no text found, skipped", file=sys.stderr)
                continue
            if not title and page_chapter_title:
                chapter_title = page_chapter_title
            if cache_dir:
                save_cached(cache_dir, key, number, chapter_title, text)
            n_fresh += 1
            n_since_refresh += 1
            polite_sleep(min_delay, max_delay)
            if (session_factory is not None and session_refresh
                    and n_since_refresh >= session_refresh):
                # Long-lived sessions go stale and start timing out;
                # a fresh session keeps the run going.
                try:
                    session.close()
                except Exception:
                    pass
                session = session_factory()
                n_since_refresh = 0
                print(f"  session refreshed after {session_refresh} downloads")
        # Some sites list stale duplicate chapters (same text, different
        # number). Skip exact duplicates so the book doesn't repeat itself.
        if not no_dedup:
            h = hashlib.md5("".join(text.split()).encode("utf-8")).hexdigest()
            if h in seen_hashes:
                print(f"    duplicate of chapter {seen_hashes[h]}, skipped")
                n_dupes += 1
                continue
            seen_hashes[h] = number
        ch = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chap{i:04d}.xhtml",
            content=chapter_html(chapter_title, text),
        )
        book.add_item(ch)
        toc.append(ch)

    if not toc:
        print("Nothing to save — no chapters extracted.", file=sys.stderr)
        return False

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + toc
    epub.write_epub(out_path, book)
    print(f"Saved {len(toc)} chapter(s) to {out_path} "
          f"({n_fresh} downloaded, {n_cached} from cache"
          f"{f', {n_dupes} duplicates skipped' if n_dupes else ''})")
    return True


def main():
    ap = argparse.ArgumentParser(description="Download an akknovel.com novel to EPUB.")
    ap.add_argument("url", nargs="?", default=None,
                    help="Series page URL, e.g. https://www.akknovel.com/series/<slug>")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title (default: from the series page)")
    ap.add_argument("--start", type=float, default=None, help="First chapter number")
    ap.add_argument("--end", type=float, default=None, help="Last chapter number")
    ap.add_argument("--min-delay", type=float, default=2.0,
                    help="Minimum seconds between requests (default: 2.0)")
    ap.add_argument("--max-delay", type=float, default=4.0,
                    help="Maximum seconds between requests (default: 4.0)")
    ap.add_argument("--no-cover", action="store_true",
                    help="Skip downloading the cover image")
    ap.add_argument("--no-meta", action="store_true",
                    help="Skip fetching novel metadata (title/author/synopsis)")
    ap.add_argument("--cache-dir", default=None,
                    help="Directory for cached chapters (default: ./.cache/akknovel-<slug>/)")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore the cache and re-download everything")
    ap.add_argument("--offline", action="store_true",
                    help="Don't fetch anything online; rebuild the EPUB from "
                         "cached chapters only (no metadata/cover download, "
                         "no chapter list fetch)")
    ap.add_argument("--no-dedup", action="store_true",
                    help="Keep chapters with identical text (default: skip duplicates)")
    ap.add_argument("--cookies", default=None, metavar="COOKIES_JSON",
                    help="Cookie-Editor JSON export to send with requests")
    ap.add_argument("--impersonate", default=None, metavar="TARGET",
                    help="Mimic a real browser's TLS fingerprint via curl_cffi "
                         "(e.g. --impersonate chrome); needs: pip install curl_cffi")
    ap.add_argument("--session-refresh", type=int, default=100, metavar="N",
                    help="Recreate the HTTP session after every N fresh "
                         "chapter downloads (default: 100; 0 disables). "
                         "Long-lived sessions go stale and start timing out.")
    args = ap.parse_args()

    if not args.url and not args.offline:
        ap.error("a series URL is required (e.g. https://www.akknovel.com/series/<slug>)")
    try:
        slug = series_slug(args.url) if args.url else None
    except ValueError as e:
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    if args.offline and not slug:
        # Offline rebuild needs the slug for the cache dir; derive it from
        # --cache-dir instead.
        if not args.cache_dir:
            ap.error("--offline needs a series URL or --cache-dir")
        slug = os.path.basename(os.path.normpath(args.cache_dir)).removeprefix("akknovel-")

    try:
        session = make_session(args.impersonate)
    except RuntimeError as e:
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    if args.impersonate:
        print(f"  TLS fingerprint impersonation: {args.impersonate}")
    if args.cookies:
        try:
            n = load_cookies(session, args.cookies)
            print(f"  loaded {n} cookies from {args.cookies}")
        except Exception as e:
            print(f"  --cookies failed: {e}", file=sys.stderr)
            sys.exit(1)
    cache_dir = args.cache_dir or os.path.join(".cache", f"akknovel-{slug}")

    meta = {}
    if not args.no_meta and not args.offline:
        print("Fetching novel metadata ...")
        try:
            meta = get_novel_meta(session, args.url)
            print(f"  title: {meta.get('title')}")
            print(f"  author: {meta.get('author')}")
            if meta.get("status"):
                print(f"  status: {meta['status']}")
        except Exception as e:
            print(f"  metadata failed: {e}", file=sys.stderr)

    cover_bytes = None
    if not args.no_cover and not args.offline and meta.get("cover_url"):
        print("Fetching cover ...")
        try:
            cover_bytes = get_cover_bytes(session, meta["cover_url"])
            print(f"  cover: {len(cover_bytes)//1024} KB jpeg")
        except Exception as e:
            print(f"  cover failed: {e}", file=sys.stderr)

    if args.offline:
        print("Offline mode: reading chapter list from cache ...")
        chapters = chapters_from_cache(cache_dir)
        print(f"Found {len(chapters)} cached chapter(s).")
    else:
        print("Fetching chapter list ...")
        chapters = get_chapters(session, args.url)
        print(f"Found {len(chapters)} chapters.")
        migrate_cache_keys(cache_dir, chapters)
    if not chapters:
        sys.exit(1)

    title = args.title or meta.get("title") or slug
    out = args.output or f"{slug}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_epub(chapters, session, out, title, slug,
                    args.min_delay, args.max_delay,
                    args.start, args.end, meta, cover_bytes,
                    cache_dir, args.refresh, args.no_dedup,
                    offline=args.offline,
                    session_factory=lambda: make_session(args.impersonate),
                    session_refresh=args.session_refresh)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
