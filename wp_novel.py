#!/usr/bin/env python3
"""Download a WordPress translation-blog novel as a single EPUB.

Built for blogs like bluesilvertranslations.wordpress.com whose chapter list
page groups chapter links under volume headings, and whose chapter posts
embed the text via Google Docs ("Direct link" iframes).

Usage:
    python wp_novel.py https://bluesilvertranslations.wordpress.com/chapter-list/ \\
        [-o book.epub] [--start 1] [--end 50]
        [--min-delay 2] [--max-delay 4] [--gdoc-delay 5] [--no-dedup]

Resuming: downloaded chapters are cached in ./.cache/<site>/, so rerunning
picks up where it left off. See README.md for details.
"""
import argparse
import hashlib
import html as htmlmod
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

from scrape import chapter_html, fetch_with_retry, polite_sleep


def parse_chapter_list(html, base_url):
    """Extract [(volume, title, post_url)] from a chapter-list page.

    Expects volume headings (<p>) each followed by a <ul> of chapter links.
    If the page repeats chapters (stale duplicate listings), later listings
    win and final order follows the last listing.
    """
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one(".entry-content") or soup
    ordered = {}  # post_url -> [volume, title, seq]
    seq = 0
    volume = None
    for el in content.children:
        name = getattr(el, "name", None)
        if name == "p":
            t = el.get_text(strip=True)
            if t:
                volume = htmlmod.unescape(t)
        elif name == "ul" and volume:
            for a in el.select("a[href]"):
                url = urljoin(base_url, a["href"])
                title = htmlmod.unescape(a.get_text(strip=True).replace("\xa0", " "))
                if not title or not url.startswith("http"):
                    continue
                ordered[url] = [volume, title, seq]
                seq += 1
    # keep last occurrence of each URL, in last-seen order
    items = sorted(ordered.items(), key=lambda kv: kv[1][2])
    return [(vol, title, url) for url, (vol, title, _) in items]


def parse_site_meta(html):
    """Try to find novel title/author, e.g. 'Translating X by Y' headings."""
    soup = BeautifulSoup(html, "lxml")
    title, author = None, None
    for h in soup.select("h1, h2, h3"):
        m = re.search(r"translating\s+(.+?)\s+by\s+(.+)", h.get_text(strip=True), re.I)
        if m:
            title, author = m.group(1).strip(), m.group(2).strip()
            break
    if not title:
        og = soup.find("meta", property="og:site_name")
        title = og["content"].strip() if og and og.get("content") else None
    return title, author


def clean_gdoc_url(link):
    """Extract a usable Google Docs URL from a possibly-mangled link.

    Some posts have malformed hrefs like http://"https://docs.google.com/...
    (literal quotes pasted into the attribute). Pull out the real URL.
    """
    if not link:
        return None
    m = re.search(
        r"https://docs\.google\.com/document/d/(?:e/)?[A-Za-z0-9_-]+/pub[^\s\"']*",
        link,
    )
    return m.group(0) if m else None


def get_gdoc_url(session, post_url, referer=None):
    """Fetch a chapter post and return its Google Docs URL.

    Prefers the embed iframe; falls back to direct links. Skips malformed
    URLs rather than returning them.
    """
    r = fetch_with_retry(session, post_url, timeout=30, referer=referer)
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup.select("iframe[src]"):
        url = clean_gdoc_url(tag.get("src"))
        if url:
            return url
    for tag in soup.select("a[href]"):
        url = clean_gdoc_url(tag.get("href"))
        if url:
            return url
    return None


def get_chapter_text(session, gdoc_url, timeout=30):
    """Fetch a published Google Doc and extract its paragraphs."""
    r = fetch_with_retry(session, gdoc_url, timeout=timeout)
    soup = BeautifulSoup(r.text, "lxml")
    doc = soup.select_one(".doc-content")
    if not doc:
        return None
    # Each non-empty <p> is a paragraph; empty ones are just spacing.
    paras = []
    for p in doc.find_all("p"):
        t = " ".join(p.get_text().split())
        if t:
            paras.append(t)
    return "\n\n".join(paras) or None


def cache_path(cache_dir, key):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)
    return os.path.join(cache_dir, f"{safe}.json")


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


def save_cached(cache_dir, key, title, text):
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache_path(cache_dir, key) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"title": title, "text": text}, f)
    os.replace(tmp, cache_path(cache_dir, key))


def chapter_key(post_url):
    """Collision-proof cache key: slug + hash of the full URL.

    The slug alone isn't unique (e.g. several posts can end in /douluo-dalu/),
    so the URL hash disambiguates while keeping filenames human-readable.
    """
    slug = urlparse(post_url).path.strip("/").split("/")[-1][:50] or "chapter"
    digest = hashlib.md5(post_url.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def old_chapter_key(post_url):
    """Pre-fix key format (bare slug), for migrating existing caches."""
    return urlparse(post_url).path.strip("/").split("/")[-1][:80] or "chapter"


def migrate_cache_keys(cache_dir, chapters):
    """Rename old-format cache files to the collision-proof format.

    Only migrates when an old key maps to exactly one chapter URL; ambiguous
    files are left alone (those chapters will simply re-download).
    """
    if not cache_dir or not os.path.isdir(cache_dir):
        return
    by_old = {}
    for _, _, url in chapters:
        by_old.setdefault(old_chapter_key(url), []).append((url, chapter_key(url)))
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


def build_epub(chapters, session, out_path, book_title, author, min_delay,
               max_delay, start, end, cache_dir, refresh, no_dedup,
               list_url, gdoc_delay=5.0):
    book = epub.EpubBook()
    book.set_identifier(f"wp-novel-{int(time.time())}")
    book.set_title(book_title)
    book.set_language("en")
    book.add_author(author or urlparse(list_url).netloc)

    selected = chapters[(start - 1) if start else 0:end]
    toc = []
    volume_groups = []  # (volume, [chapters])
    seen_hashes = {}
    n_cached = n_fresh = n_dupes = 0

    for i, (volume, title, post_url) in enumerate(selected, 1):
        key = chapter_key(post_url)
        cached = None if refresh else load_cached(cache_dir, key)
        if cached:
            text = cached["text"]
            title = cached.get("title") or title
            print(f"[{i}/{len(selected)}] {title[:60]}: from cache")
            n_cached += 1
        else:
            print(f"[{i}/{len(selected)}] {title[:60]}")
            try:
                gdoc_url = get_gdoc_url(session, post_url, referer=list_url)
                if not gdoc_url:
                    print("    no Google Docs link found, skipped", file=sys.stderr)
                    continue
                text = get_chapter_text(session, gdoc_url)
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                continue
            if not text:
                print("    no text found, skipped", file=sys.stderr)
                continue
            if cache_dir:
                save_cached(cache_dir, key, title, text)
            n_fresh += 1
            if gdoc_delay > 0:
                time.sleep(gdoc_delay)  # extra breather for Google Docs
            polite_sleep(min_delay, max_delay)

        if not no_dedup:
            h = hashlib.md5("".join(text.split()).encode("utf-8")).hexdigest()
            if h in seen_hashes:
                print(f"    duplicate of '{seen_hashes[h][:50]}', skipped")
                n_dupes += 1
                continue
            seen_hashes[h] = title

        ch = epub.EpubHtml(
            title=title,
            file_name=f"chap{i:04d}.xhtml",
            content=chapter_html(title, text),
        )
        book.add_item(ch)
        toc.append(ch)
        if volume_groups and volume_groups[-1][0] == volume:
            volume_groups[-1][1].append(ch)
        else:
            volume_groups.append((volume, [ch]))

    if not toc:
        print("Nothing to save — no chapters extracted.", file=sys.stderr)
        return False

    book.toc = [(epub.Section(vol), chs) for vol, chs in volume_groups]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + toc
    epub.write_epub(out_path, book)
    print(f"Saved {len(toc)} chapter(s) to {out_path} "
          f"({n_fresh} downloaded, {n_cached} from cache"
          f"{f', {n_dupes} duplicates skipped' if n_dupes else ''})")
    return True


def main():
    ap = argparse.ArgumentParser(description="Download a WordPress novel to EPUB.")
    ap.add_argument("url", help="Chapter-list page URL")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title")
    ap.add_argument("--author", default=None, help="Book author")
    ap.add_argument("--start", type=int, default=None, help="First chapter (1-based)")
    ap.add_argument("--end", type=int, default=None, help="Last chapter (1-based, inclusive)")
    ap.add_argument("--min-delay", type=float, default=2.0)
    ap.add_argument("--max-delay", type=float, default=4.0)
    ap.add_argument("--gdoc-delay", type=float, default=5.0,
                    help="Extra fixed pause (seconds) after each Google Docs "
                         "fetch (default: 5.0)")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-dedup", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="List chapters that would be downloaded, then exit")
    args = ap.parse_args()

    session = requests.Session()
    netloc = urlparse(args.url).netloc
    cache_dir = args.cache_dir or os.path.join(".cache", netloc)

    print("Fetching chapter list ...")
    r = fetch_with_retry(session, args.url, timeout=30)
    chapters = parse_chapter_list(r.text, args.url)
    print(f"Found {len(chapters)} chapters.")
    if not chapters:
        sys.exit(1)

    migrate_cache_keys(cache_dir, chapters)

    if args.dry_run:
        for i, (vol, title, url) in enumerate(chapters, 1):
            print(f"{i:4d}. [{vol[:40]}] {title[:60]}")
        return

    meta_title, meta_author = parse_site_meta(r.text)
    title = args.title or meta_title or "WordPress Novel"
    author = args.author or meta_author
    out = args.output or "wordpress-novel.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_epub(chapters, session, out, title, author,
                     args.min_delay, args.max_delay,
                     args.start, args.end, cache_dir, args.refresh,
                     args.no_dedup, args.url, args.gdoc_delay)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
