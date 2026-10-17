#!/usr/bin/env python3
"""Download a novel from wuxiaworld.com and save all chapters as a single EPUB.

Usage:
    python wuxia_novel.py https://www.wuxiaworld.com/novel/rebirth-of-tang-san \\
        [-o book.epub] [--start 0] [--end 50]
        [--min-delay 2] [--max-delay 4] [--no-cover] [--no-meta]
        [--cache-dir DIR] [--refresh] [--no-dedup]

Resuming: downloaded chapters are cached in ./.cache/<slug>/ (as JSON), so
interrupting the run and starting it again only fetches what is missing.
Use --refresh to re-download everything.

How it works:
- Novel metadata (title, author, chapter count, cover) is read from the
  novel page's <h1>, JSON-LD/og tags, and its embedded data-djac-params.
- The chapter slug prefix (e.g. "sl5-chapter-") is discovered from chapter
  links on the novel page — it is novel-specific, not hard-coded.
- Chapter pages are server-rendered: text comes from
  div.chapter-content div.chapter-body > p. Paragraph comment badges
  (stray numbers appended to each paragraph) are stripped.

Known limitation: the newest/final chapter of an ongoing novel may be
login-gated on the site. Gated chapters are skipped and reported at the
end of the run.

Politeness: requests are sequential with a random delay between them
(default 2–4s), one browser user agent is used for the whole run, headers
mimic a real browser, and rate-limit (429) responses trigger exponential
backoff. robots.txt permits crawling of novel pages.
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
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image

from scrape import chapter_html, fetch_with_retry, polite_sleep

BASE = "https://www.wuxiaworld.com"
GATED_MARKERS = (
    "log in to continue",
    "sign up to continue",
    "unlock free chapters",
)


def slug_from_url(url):
    m = re.search(r"/novel/([^/?#]+)", urlparse(url).path)
    if not m:
        raise ValueError("Could not find a /novel/<slug> in the URL")
    return m.group(1)


def get_novel_meta(session, slug):
    """Fetch title, author, total chapters, cover URL, chapter slug prefix."""
    url = f"{BASE}/novel/{slug}"
    r = fetch_with_retry(session, url, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else slug.replace("-", " ").title()

    author = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and "author" in item:
                a = item["author"]
                if isinstance(a, dict):
                    author = a.get("name")
                elif isinstance(a, list) and a:
                    first = a[0]
                    author = first.get("name") if isinstance(first, dict) else first
                else:
                    author = a
                if author:
                    break
        if author:
            break

    total = None
    djac = soup.find(attrs={"data-djac-params": True})
    if djac:
        try:
            data = json.loads(djac["data-djac-params"])
            snap = (data.get("series_snapshot") or {})
            total = int(snap.get("total_chapters_available") or 0) or None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    cover_url = None
    tag = soup.find("meta", property="og:image")
    if tag and tag.get("content"):
        cover_url = htmlmod.unescape(tag["content"]).strip()

    # Chapter slug prefix is novel-specific (e.g. "sl5-chapter-"); find it
    # from an actual chapter link on the novel page.
    prefix = None
    pat = re.compile(rf"/novel/{re.escape(slug)}/([a-zA-Z0-9_-]+-chapter-)\d+/?$")
    for a in soup.select("a[href]"):
        m = pat.search(a["href"])
        if m:
            prefix = m.group(1)
            break
    if not prefix:
        m = pat.search(r.text)
        prefix = m.group(1) if m else None
    if not prefix:
        raise RuntimeError("could not find a chapter URL prefix on the novel page")

    return {
        "title": title,
        "author": author,
        "total": total,
        "cover_url": cover_url,
        "chapter_prefix": prefix,
    }


def chapter_key(number, url):
    """Collision-proof cache key: chapter number + hash of its URL."""
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{number}-{digest}"


def cache_path(cache_dir, key):
    return os.path.join(cache_dir, f"{key}.json")


def load_cached(cache_dir, key):
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


def get_chapter(session, chapter_url, novel_url):
    """Return (title, text); text is None when the chapter is login-gated."""
    r = fetch_with_retry(session, chapter_url, timeout=30, referer=novel_url)
    soup = BeautifulSoup(r.text, "lxml")
    heading = soup.select_one('h4[data-testid="heading"]')
    title = None
    if heading:
        t = heading.select_one('[data-testid="title"]')
        title = (t or heading).get_text(" ", strip=True)

    low = r.text.lower()
    if any(m in low for m in GATED_MARKERS):
        return title, None

    body = soup.select_one("div.chapter-content div.chapter-body")
    if not body:
        return title, None
    paras = []
    for p in body.select(":scope > p"):
        # strip paragraph comment badges (stray numbers at end of each <p>)
        for badge in p.select('span[aria-hidden]'):
            badge.decompose()
        t = p.get_text(" ", strip=True)
        if t:
            paras.append(t)
    text = "\n\n".join(paras)
    return title, (text or None)


def build_epub(slug, total, prefix, session, out_path, book_title,
               min_delay, max_delay, start, end, meta=None,
               cover_bytes=None, cache_dir=None, refresh=False,
               no_dedup=False):
    book = epub.EpubBook()
    book.set_identifier(f"wuxia-{slug}-{int(time.time())}")
    book.set_title(book_title or (meta or {}).get("title") or slug.replace("-", " ").title())
    book.set_language("en")
    author = (meta or {}).get("author")
    book.add_author(author or "wuxiaworld.com")
    if cover_bytes:
        book.set_cover("images/cover.jpg", cover_bytes)

    first = 0 if start is None else start
    last = (total - 1) if end is None else end
    numbers = list(range(first, last + 1))
    novel_url = f"{BASE}/novel/{slug}"
    toc = []
    n_cached = n_fresh = n_dupes = n_gated = 0
    seen_hashes = {}
    for i, number in enumerate(numbers, 1):
        chapter_url = f"{novel_url}/{prefix}{number}"
        key = chapter_key(number, chapter_url)
        cached = None if refresh else load_cached(cache_dir, key)
        title = f"Chapter {number}"
        if cached:
            text = cached["text"]
            title = cached.get("title") or title
            print(f"[{i}/{len(numbers)}] chapter {number}: from cache")
            n_cached += 1
        else:
            print(f"[{i}/{len(numbers)}] chapter {number}: downloading")
            try:
                title, text = get_chapter(session, chapter_url, novel_url)
                title = title or f"Chapter {number}"
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                continue
            if text is None:
                print("    login-gated or no text, skipped", file=sys.stderr)
                n_gated += 1
                continue
            if cache_dir:
                save_cached(cache_dir, key, number, title, text)
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
            title=title,
            file_name=f"chap{i:04d}.xhtml",
            content=chapter_html(title, text),
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
    stats = f"{n_fresh} downloaded, {n_cached} from cache"
    if n_dupes:
        stats += f", {n_dupes} duplicates skipped"
    if n_gated:
        stats += f", {n_gated} login-gated (skipped)"
    print(f"Saved {len(toc)} chapter(s) to {out_path} ({stats})")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Download a wuxiaworld.com novel to EPUB.")
    ap.add_argument("url", help="Novel page URL, e.g. https://www.wuxiaworld.com/novel/<slug>")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title (default: novel title)")
    ap.add_argument("--start", type=int, default=None, help="First chapter number")
    ap.add_argument("--end", type=int, default=None, help="Last chapter number")
    ap.add_argument("--min-delay", type=float, default=2.0,
                    help="Minimum seconds between requests (default: 2.0)")
    ap.add_argument("--max-delay", type=float, default=4.0,
                    help="Maximum seconds between requests (default: 4.0)")
    ap.add_argument("--no-cover", action="store_true",
                    help="Skip downloading the cover image")
    ap.add_argument("--no-meta", action="store_true",
                    help="Skip fetching novel metadata (title/author/count)")
    ap.add_argument("--cache-dir", default=None,
                    help="Directory for cached chapters (default: ./.cache/<slug>/)")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore the cache and re-download everything")
    ap.add_argument("--no-dedup", action="store_true",
                    help="Keep chapters with identical text (default: skip duplicates)")
    args = ap.parse_args()

    slug = slug_from_url(args.url)
    session = requests.Session()
    cache_dir = args.cache_dir or os.path.join(".cache", "wuxia-" + slug)

    meta = {}
    if not args.no_meta:
        print("Fetching novel metadata ...")
        try:
            meta = get_novel_meta(session, slug)
            print(f"  title: {meta.get('title')}")
            print(f"  author: {meta.get('author')}")
            print(f"  chapters: {meta.get('total')}")
            print(f"  chapter prefix: {meta.get('chapter_prefix')}")
        except Exception as e:
            print(f"  metadata failed: {e}", file=sys.stderr)

    cover_bytes = None
    if not args.no_cover and meta.get("cover_url"):
        print("Fetching cover ...")
        try:
            cover_bytes = get_cover_bytes(session, meta["cover_url"])
            print(f"  cover: {len(cover_bytes)//1024} KB jpeg")
        except Exception as e:
            print(f"  cover failed: {e}", file=sys.stderr)

    total = meta.get("total")
    prefix = meta.get("chapter_prefix")
    if not total or not prefix:
        print("Could not determine chapter list; --no-meta is not supported "
              "for wuxiaworld novels.", file=sys.stderr)
        sys.exit(1)

    title = args.title or meta.get("title") or slug.replace("-", " ").title()
    out = args.output or f"{slug}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_epub(slug, total, prefix, session, out, title,
                    args.min_delay, args.max_delay,
                    args.start, args.end, meta, cover_bytes,
                    cache_dir, args.refresh, args.no_dedup)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
