#!/usr/bin/env python3
"""Download Shadow Slave from shadowslave.space and save all chapters
as a single EPUB.

Usage:
    python shadowslave.py https://shadowslave.space/ \\
        [-o shadow-slave.epub] [--start 1] [--end 50]
        [--min-delay 2] [--max-delay 4] [--no-cover] [--no-meta]
        [--cache-dir DIR] [--refresh] [--offline] [--no-dedup]

Resuming: downloaded chapters are cached in ./.cache/shadow-slave/ (as
JSON), so interrupting the run and starting it again only fetches what is
missing. Use --refresh to re-download everything.

How it works:
- Chapter metadata (number + title) comes from the chapter-list page,
  which embeds the full list (allChapters) in its Next.js page payload —
  one request gets all 3,250 titles. If that parse ever fails, it falls
  back to the site's total chapter count and reads each chapter's title
  from its own page.
- Each chapter page is server-rendered, so the text is parsed straight
  from the <article> element — no JS or login needed. The header and the
  "read this chapter online" footer inside the article are dropped.

Politeness: requests are sequential with a random delay between them
(default 2–4s), one browser user agent is used for the whole run, headers
mimic a real browser, and
rate-limit (429) responses trigger exponential backoff. robots.txt allows
crawling (Allow: /).

Sessions: a long-lived HTTP session goes stale after a few hundred requests
— every fetch starts timing out even though the site answers a fresh
connection fine. So the session is recreated every 100 fresh downloads
by default (see --session-refresh); cached chapters don't count, since
they make no requests.
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

BASE = "https://shadowslave.space"
SLUG = "shadow-slave"
CHAPTER_LIST_URL = f"{BASE}/chapters/"


def _og(soup, prop):
    tag = soup.find("meta", property=f"og:{prop}")
    return htmlmod.unescape(tag["content"]).strip() if tag and tag.get("content") else None


def get_novel_meta(session):
    """Fetch title, author, synopsis and cover URL from the home page."""
    r = fetch_with_retry(session, BASE + "/", timeout=30)
    soup = BeautifulSoup(r.text, "lxml")

    title = _og(soup, "title")
    if title:
        title = re.sub(r"\s+Novel Online$", "", title).strip()

    description = _og(soup, "description") or ""
    author = None
    m = re.search(r"by\s+(Guiltythree)\b", r.text, re.I)
    if m:
        author = m.group(1)
    synopsis = None
    m = re.search(r"Story Overview\s*(.*?)(?:Read Shadow Slave novel online|$)",
                  soup.get_text(" ", strip=True), re.I | re.S)
    if m:
        synopsis = re.sub(r"\s+", " ", m.group(1)).strip()

    return {
        "title": title or "Shadow Slave",
        "author": author or "Guiltythree",
        "description": synopsis or description or None,
        "genres": [],
        "cover_url": _og(soup, "image"),
    }


def _unescape_title(s):
    """Undo the JSON escaping used in the Next.js page payload."""
    return htmlmod.unescape(
        s.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
    )


def get_chapters(session):
    """Return [(number, title)] for every chapter, oldest first."""
    r = fetch_with_retry(session, CHAPTER_LIST_URL, timeout=30)
    text = r.text
    m = re.search(r'\\"allChapters\\":(\[.*?\}\]),\\"config\\"', text, re.S)
    chapters = []
    if m:
        entries = re.findall(
            r'\\"id\\":\\"(\d+)\\",\\"title\\":\\"((?:[^\\"]|\\.)*?)\\"',
            m.group(1),
        )
        for num, raw_title in entries:
            chapters.append((int(num), _unescape_title(raw_title)))
        chapters.sort(key=lambda c: c[0])
    if chapters:
        print(f"  chapter list: {len(chapters)} chapters (from allChapters)")
        return chapters
    # Fallback: total chapter count, titles filled in per chapter page.
    m = re.search(r'\\"totalChapters\\":(\d+)', text)
    if not m:
        m = re.search(r"of\s+(\d+)\s+chapters", text)
    if not m:
        raise RuntimeError("could not find the chapter list or total count")
    total = int(m.group(1))
    print(f"  chapter list parse failed; using total count: {total}")
    return [(n, None) for n in range(1, total + 1)]


def clean_title(raw_title, number):
    """Turn 'Shadow Slave Chapter 1 - Chapter 1 - 1: Nightmare Begins'
    (or 'Shadow Slave Chapter 3250 - Chapter 3250 Take Us Back') into
    'Chapter 1: Nightmare Begins'. Falls back to 'Chapter <n>'."""
    name = (raw_title or "").strip()
    for pat in (rf"^Shadow Slave Chapter {number} - ",
                rf"^Chapter {number} - ",
                rf"^Chapter {number} ",
                rf"^{number}: "):
        name = re.sub(pat, "", name, count=1)
    name = name.strip()
    if not name:
        return f"Chapter {number}"
    return f"Chapter {number}: {name}"


def chapter_key(number):
    """Collision-proof cache key: chapter number + hash of its URL."""
    num = "%g" % number
    url = f"{BASE}/chapters/{num}"
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
    for number, _ in chapters:
        by_old.setdefault(old_chapter_key(number), []).append(
            (number, chapter_key(number)))
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
    """Chapter title from the chapter page itself (<title> or article h2)."""
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h2 = soup.select_one("article h2")
        if h2:
            title = h2.get_text(" ", strip=True)
    return clean_title(title, number) if title else f"Chapter {number}"


def get_chapter_text(session, number, timeout=30):
    url = f"{BASE}/chapters/{number}"
    r = fetch_with_retry(session, url, timeout=timeout, referer=BASE + "/")
    soup = BeautifulSoup(r.text, "lxml")
    article = soup.select_one("article")
    if not article:
        return None, None
    # The body lives in div.prose; the header (chapter label + full title)
    # and the "read this chapter online" footer sit outside it.
    body = article.select_one("div.prose") or article
    for tag in body.select("nav, button, a, .chapter-source-note"):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    text = "\n\n".join(p for p in paras if p)
    return (text or None), page_title(soup, number)


def build_epub(chapters, session, out_path, book_title,
               min_delay, max_delay, start, end, meta=None,
               cover_bytes=None, cache_dir=None, refresh=False,
               no_dedup=False, offline=False, session_factory=None,
               session_refresh=100):
    book = epub.EpubBook()
    book.set_identifier(f"novel-{SLUG}-{int(time.time())}")
    book.set_title(book_title or (meta or {}).get("title") or "Shadow Slave")
    book.set_language("en")
    author = (meta or {}).get("author")
    book.add_author(author or "Guiltythree")
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
    for i, (number, title) in enumerate(selected, 1):
        chapter_title = clean_title(title, number) if title else f"Chapter {number}"
        key = chapter_key(number)
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
                text, page_chapter_title = get_chapter_text(session, number)
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
    ap = argparse.ArgumentParser(description="Download Shadow Slave to EPUB.")
    ap.add_argument("url", nargs="?", default=BASE + "/",
                    help="Novel page URL, e.g. https://shadowslave.space/ "
                         "(default: the home page)")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title (default: Shadow Slave)")
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
                    help="Directory for cached chapters (default: ./.cache/shadow-slave/)")
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
    ap.add_argument("--chapter-count", type=int, default=None, metavar="N",
                    help="Skip the chapter-list parse and assume chapters 1..N "
                         "(titles are read from each chapter page)")
    ap.add_argument("--session-refresh", type=int, default=100, metavar="N",
                    help="Recreate the HTTP session after every N fresh "
                         "chapter downloads (default: 100; 0 disables). "
                         "Long-lived sessions go stale and start timing out.")
    args = ap.parse_args()

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
    cache_dir = args.cache_dir or os.path.join(".cache", SLUG)

    meta = {}
    if not args.no_meta and not args.offline:
        print("Fetching novel metadata ...")
        try:
            meta = get_novel_meta(session)
            print(f"  title: {meta.get('title')}")
            print(f"  author: {meta.get('author')}")
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
        if args.chapter_count:
            chapters = [(n, None) for n in range(1, args.chapter_count + 1)]
            print(f"  using explicit chapter count: {args.chapter_count}")
        else:
            chapters = get_chapters(session)
        print(f"Found {len(chapters)} chapters.")
        migrate_cache_keys(cache_dir, chapters)
    if not chapters:
        sys.exit(1)

    title = args.title or meta.get("title") or "Shadow Slave"
    out = args.output or "shadow-slave.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_epub(chapters, session, out, title,
                    args.min_delay, args.max_delay,
                    args.start, args.end, meta, cover_bytes,
                    cache_dir, args.refresh, args.no_dedup,
                    offline=args.offline,
                    session_factory=lambda: make_session(args.impersonate),
                    session_refresh=args.session_refresh)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
