#!/usr/bin/env python3
"""Download a book from webnovel.com and save all chapters as a single EPUB.

Webnovel sits behind a Cloudflare JavaScript challenge that plain HTTP
clients (requests, curl) cannot pass, so this script drives a real Chromium
browser via Playwright instead. Run it on your own machine — a residential
connection plus a real browser passes the challenge; datacenter IPs may not.

Setup (one time):
    pip install playwright
    playwright install chromium

Usage:
    python webnovel.py https://www.webnovel.com/book/36245749908869305 \\
        [-o book.epub] [--start 0] [--end 50]
        [--min-delay 2] [--max-delay 4] [--no-cover] [--no-meta]
        [--cache-dir DIR] [--refresh] [--offline] [--no-dedup] [--headed]

Resuming: downloaded chapters are cached in ./.cache/webnovel-<bookId>/ (as
JSON), so interrupting the run and starting it again only fetches what is
missing. Use --refresh to re-download everything, --offline to rebuild the
EPUB from the cache without touching the network.

How it works:
- Book metadata (title, author, synopsis, cover) is read from the book
  page's OpenGraph tags. The cover is downloaded and converted to JPEG for
  maximum e-reader compatibility.
- The chapter list comes from the book's /catalog page (server-rendered):
  each entry gives the chapter number, title, and numeric chapter ID.
  Chapter URLs look like /book/<bookId>/<chapterId>.
- Each chapter page is server-rendered: text is parsed from
  div.cha-words > p, with the per-paragraph comment icons (.tag-num)
  stripped. Locked chapters (paywalled) are skipped with a warning.

Politeness: chapters are fetched one at a time with a random delay between
them (default 2–4s), like a human reader turning pages.
"""
import argparse
import hashlib
import html as htmlmod
import io
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

from ebooklib import epub
from PIL import Image

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright is not installed. Run:\n"
             "    pip install playwright\n"
             "    playwright install chromium")

from scrape import chapter_html, polite_sleep

BASE = "https://www.webnovel.com"
CHAPTER_ID_RE = re.compile(r"/book/\d+/(\d+)|_(\d+)\s*$")


def book_id_from_url(url):
    m = re.search(r"/book/(\d+)", urlparse(url).path)
    if not m:
        raise ValueError("Could not find a /book/<id> in the URL")
    return m.group(1)


def chapter_key(number, chapter_id):
    """Collision-proof cache key: chapter number + hash of chapter ID."""
    num = "%g" % number
    digest = hashlib.md5(chapter_id.encode("utf-8")).hexdigest()[:8]
    return f"{num}-{digest}"


def dismiss_cookie_banner(page):
    """Click 'Disagree' on the privacy banner if it shows up."""
    try:
        btn = page.get_by_text("Disagree", exact=True)
        if btn.count():
            btn.first.click(timeout=3000)
            page.wait_for_timeout(1000)
    except Exception:
        pass


def soup_of(page):
    from bs4 import BeautifulSoup
    return BeautifulSoup(page.content(), "lxml")


def _og(soup, prop):
    tag = soup.find("meta", property=f"og:{prop}")
    return htmlmod.unescape(tag["content"]).strip() if tag and tag.get("content") else None


def get_book_meta(page, book_id):
    """Fetch title, author, synopsis and cover URL from the book page."""
    page.goto(f"{BASE}/book/{book_id}", timeout=60000)
    page.wait_for_timeout(3000)
    dismiss_cookie_banner(page)
    soup = soup_of(page)
    return {
        "title": _og(soup, "title"),
        "author": _og(soup, "author"),
        "description": _og(soup, "description"),
        "cover_url": _og(soup, "image"),
    }


def get_chapters(page, book_id):
    """Return [(number, chapter_id, title, volume)] oldest first.

    The /catalog page renders the full chapter list server-side, grouped
    under volume headers.
    """
    page.goto(f"{BASE}/book/{book_id}/catalog", timeout=60000)
    page.wait_for_timeout(3000)
    dismiss_cookie_banner(page)
    soup = soup_of(page)
    chapters = []
    volume = None
    # Anchors carry the chapter ID as the trailing number in the href and
    # the chapter number in an <i> tag; volume headers are h4 elements.
    for el in soup.select("h4.catalog-volume-header, a[href]"):
        if el.name == "h4":
            volume = el.get_text(" ", strip=True) or None
            continue
        href = el.get("href", "")
        m = re.search(rf"/book/{book_id}/(\d+)\s*$", href.split("?")[0])
        if not m:
            m = re.search(r"_(\d+)\s*$", href.split("?")[0])
        if not m:
            continue
        chapter_id = m.group(1)
        num_tag = el.select_one("i")
        try:
            number = int(num_tag.get_text(strip=True)) if num_tag else None
        except ValueError:
            number = None
        if number is None:
            continue
        title = el.get("title") or ""
        if not title:
            strong = el.select_one("strong")
            title = strong.get_text(" ", strip=True) if strong else f"Chapter {number}"
        title = htmlmod.unescape(title).strip()
        chapters.append((number, chapter_id, title, volume))
    # De-duplicate by chapter ID, keep document order, then sort by number.
    seen = set()
    unique = []
    for c in chapters:
        if c[1] not in seen:
            seen.add(c[1])
            unique.append(c)
    unique.sort(key=lambda c: c[0])
    return unique


def get_chapter(page, book_id, chapter_id, timeout=45000):
    """Return (title, text); text is None when the chapter is locked/empty."""
    url = f"{BASE}/book/{book_id}/{chapter_id}"
    page.goto(url, timeout=60000)
    try:
        page.wait_for_selector("div.cha-words", timeout=timeout)
    except Exception:
        pass
    soup = soup_of(page)
    heading = soup.select_one(".cha-tit")
    title = heading.get_text(" ", strip=True) if heading else _og(soup, "title")
    # Locked chapters render a masked container instead of full text.
    if soup.select_one(".cha-content._lock, .styles_locked_content__jH2rZ"):
        return title, None
    body = soup.select_one("div.cha-words")
    if not body:
        return title, None
    for badge in body.select(".tag-num"):
        badge.decompose()
    paras = [p.get_text(" ", strip=True) for p in body.select(":scope > p")]
    text = "\n\n".join(t for t in paras if t)
    return title, (text or None)


def get_cover_bytes(page, cover_url):
    """Download cover art and normalize to JPEG (best e-reader support)."""
    resp = page.request.get(cover_url, timeout=30000)
    if resp.status != 200:
        raise RuntimeError(f"cover download failed: HTTP {resp.status}")
    data = resp.body()
    if data[:2] == b"\xff\xd8":
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def cache_path(cache_dir, key):
    return os.path.join(cache_dir, f"{key}.json")


def load_cached(cache_dir, key):
    if not cache_dir:
        return None
    p = cache_path(cache_dir, key)
    if not os.path.exists(p):
        return None
    try:
        import json
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("text", "").strip():
            return d
    except Exception:
        pass
    return None


def save_cached(cache_dir, key, number, chapter_id, title, text):
    import json
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache_path(cache_dir, key) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"number": number, "chapter_id": chapter_id,
                   "title": title, "text": text}, f)
    os.replace(tmp, cache_path(cache_dir, key))


def cached_chapters(cache_dir):
    """Return [(number, chapter_id, title, None)] from cached JSON files.

    Used by --offline: rebuilds the book purely from the local cache without
    opening the browser. Chapter IDs come from each file's stored
    "chapter_id" field so cache keys resolve correctly.
    """
    import json
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
            chapter_id = d.get("chapter_id") or ""
            if number is None or not str(d.get("text", "")).strip():
                continue
            number = float(number)
            if number.is_integer():
                number = int(number)
            chapters.append((number, chapter_id, d.get("title"), None))
        except Exception:
            continue
    chapters.sort(key=lambda c: c[0])
    return chapters


def build_epub(book_id, chapters, page, out_path, book_title,
               min_delay, max_delay, start, end, meta=None,
               cover_bytes=None, cache_dir=None, refresh=False,
               no_dedup=False, offline=False):
    book = epub.EpubBook()
    book.set_identifier(f"webnovel-{book_id}-{int(time.time())}")
    book.set_title(book_title or (meta or {}).get("title") or f"Webnovel {book_id}")
    book.set_language("en")
    author = (meta or {}).get("author")
    book.add_author(author or "webnovel.com")
    description = (meta or {}).get("description")
    if description:
        book.add_metadata("DC", "description", description)
    if cover_bytes:
        book.set_cover("images/cover.jpg", cover_bytes)

    selected = [c for c in chapters if (start is None or c[0] >= start)
                and (end is None or c[0] <= end)]
    toc = []
    sections = {}  # volume -> list of EpubHtml, preserving order
    n_cached = n_fresh = n_dupes = 0
    seen_hashes = {}
    for i, (number, chapter_id, list_title, volume) in enumerate(selected, 1):
        chapter_title = list_title or f"Chapter {number}"
        key = chapter_key(number, chapter_id)
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
                page_title, text = get_chapter(page, book_id, chapter_id)
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                continue
            if page_title:
                chapter_title = page_title
            if not text:
                print("    locked or no text found, skipped", file=sys.stderr)
                continue
            if cache_dir:
                save_cached(cache_dir, key, number, chapter_id, chapter_title, text)
            n_fresh += 1
            polite_sleep(min_delay, max_delay)
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
        sections.setdefault(volume or "Chapters", []).append(ch)
        toc.append(ch)

    if not toc:
        print("Nothing to save — no chapters extracted.", file=sys.stderr)
        return False

    if len(sections) > 1:
        book.toc = [(epub.Section(vol), items)
                    for vol, items in sections.items()]
    else:
        book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + toc
    epub.write_epub(out_path, book)
    stats = f"{n_fresh} downloaded, {n_cached} from cache"
    if n_dupes:
        stats += f", {n_dupes} duplicates skipped"
    print(f"Saved {len(toc)} chapter(s) to {out_path} ({stats})")
    return True


def main():
    ap = argparse.ArgumentParser(description="Download a webnovel.com book to EPUB.")
    ap.add_argument("url", help="Book page URL, e.g. https://www.webnovel.com/book/<id>")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title (default: from the book page)")
    ap.add_argument("--start", type=int, default=None, help="First chapter number")
    ap.add_argument("--end", type=int, default=None, help="Last chapter number")
    ap.add_argument("--min-delay", type=float, default=2.0,
                    help="Minimum seconds between chapters (default: 2.0)")
    ap.add_argument("--max-delay", type=float, default=4.0,
                    help="Maximum seconds between chapters (default: 4.0)")
    ap.add_argument("--no-cover", action="store_true",
                    help="Skip downloading the cover image")
    ap.add_argument("--no-meta", action="store_true",
                    help="Skip fetching book metadata (title/author/synopsis)")
    ap.add_argument("--cache-dir", default=None,
                    help="Directory for cached chapters (default: ./.cache/webnovel-<id>/)")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore the cache and re-download everything")
    ap.add_argument("--offline", action="store_true",
                    help="Don't open the browser at all; rebuild the EPUB from cached chapters only")
    ap.add_argument("--no-dedup", action="store_true",
                    help="Keep chapters with identical text (default: skip duplicates)")
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser window (useful if Cloudflare challenges the headless browser)")
    args = ap.parse_args()

    book_id = book_id_from_url(args.url)
    cache_dir = args.cache_dir or os.path.join(".cache", f"webnovel-{book_id}")

    meta = {}
    cover_bytes = None
    chapters = []
    page = None
    browser = None

    if args.offline:
        print("Offline mode: reading chapter list from cache ...")
        chapters = cached_chapters(cache_dir)
        print(f"Found {len(chapters)} cached chapter(s).")
    else:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"))
        if not args.no_meta:
            print("Fetching book metadata ...")
            try:
                meta = get_book_meta(page, book_id)
                print(f"  title: {meta.get('title')}")
                print(f"  author: {meta.get('author')}")
            except Exception as e:
                print(f"  metadata failed: {e}", file=sys.stderr)
        if not args.no_cover and meta.get("cover_url"):
            print("Fetching cover ...")
            try:
                cover_bytes = get_cover_bytes(page, meta["cover_url"])
                print(f"  cover: {len(cover_bytes)//1024} KB jpeg")
            except Exception as e:
                print(f"  cover failed: {e}", file=sys.stderr)
        print("Fetching chapter list ...")
        try:
            chapters = get_chapters(page, book_id)
            print(f"Found {len(chapters)} chapters.")
        except Exception as e:
            print(f"  chapter list failed: {e}", file=sys.stderr)

    if not chapters:
        if browser:
            browser.close()
        sys.exit(1)

    title = args.title or meta.get("title") or f"Webnovel {book_id}"
    out = args.output or f"webnovel-{book_id}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    try:
        ok = build_epub(book_id, chapters, page, out, title,
                        args.min_delay, args.max_delay,
                        args.start, args.end, meta, cover_bytes,
                        cache_dir, args.refresh, args.no_dedup,
                        offline=args.offline)
    finally:
        if browser:
            browser.close()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
