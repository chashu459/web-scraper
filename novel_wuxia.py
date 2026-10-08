#!/usr/bin/env python3
"""Download a novel from novellunar.com, but title each chapter with the
matching chapter title from wuxiaworld.com, and save as a single EPUB.

Why: novellunar chapter titles are generic ("Chapter 1", "Chapter 2", ...),
while wuxiaworld has real titles ("Chapter 1: Is It The End? Am I Going To
Be Eaten?"). The chapter *text* comes from novellunar; the *titles* come
from wuxiaworld.

Usage:
    python novel_wuxia.py https://novellunar.com/novel/soul-land-5-the-rebirth-of-tang-san \\
        --wuxia-slug rebirth-of-tang-san [-o book.epub]
        [--start 1] [--end 50] [--min-delay 2] [--max-delay 4]
        [--title-offset -1] [--no-cover] [--no-meta]
        [--cache-dir DIR] [--title-cache-dir DIR] [--refresh] [--no-dedup]

Title mapping: wuxiaworld chapter number = novellunar chapter number +
--title-offset. The default -1 was verified for Soul Land 5 (novellunar
chapter 1 has the same text as wuxiaworld chapter 0, "Prologue").

Wuxiaworld login-gates recent chapter *bodies*, but chapter *titles* still
render for anonymous visitors, so titles can be fetched without logging in.
(Titles of gated chapters carry a " (Teaser)" suffix, which is stripped.)

Resuming: novellunar chapter text is cached in ./.cache/<slug>/ (the same
cache `novel.py` uses, so previously downloaded chapters are reused), and
wuxiaworld titles are cached in ./.cache/wuxia-titles-<wuxia-slug>/.
Interrupting and re-running only fetches what is missing.

Politeness: requests are sequential with a random delay between them
(default 2–4s), one browser user agent is used for the whole run, headers
mimic a real browser, and rate-limit (429) responses trigger exponential
backoff.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

from scrape import chapter_html, fetch_with_retry, polite_sleep
import novel as novel_mod

WUXIA_BASE = "https://www.wuxiaworld.com"
TEASER_SUFFIX = re.compile(r"\s*\(Teaser\)\s*$", re.I)


def get_wuxia_title(session, wuxia_slug, wuxia_number, novel_url, timeout=30):
    """Fetch a wuxiaworld chapter page and return its heading title.

    Returns None if the title can't be found. Gated chapters still render
    their heading (with a " (Teaser)" suffix, which is stripped); only the
    body is withheld.
    """
    url = f"{WUXIA_BASE}/novel/{wuxia_slug}/sl5-chapter-{wuxia_number}"
    # NOTE: the "sl5-chapter-" slug prefix is novel-specific. It is
    # discovered from the wuxiaworld novel page (see wuxia_novel.py); for a
    # different novel, fetch the novel page first and take the prefix from a
    # chapter link, e.g. /novel/<slug>/<prefix>0.
    try:
        r = fetch_with_retry(session, url, timeout=timeout, referer=novel_url)
    except Exception as e:
        print(f"    title fetch failed: {e}", file=sys.stderr)
        return None
    soup = BeautifulSoup(r.text, "lxml")
    heading = soup.select_one('h4[data-testid="heading"]')
    if not heading:
        return None
    t = heading.select_one('[data-testid="title"]')
    title = (t or heading).get_text(" ", strip=True)
    return TEASER_SUFFIX.sub("", title).strip() or None


def load_title_cached(cache_dir, wuxia_number):
    p = os.path.join(cache_dir, f"{wuxia_number}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("title", "").strip():
            return d["title"]
    except Exception:
        pass
    return None


def save_title_cached(cache_dir, wuxia_number, title):
    os.makedirs(cache_dir, exist_ok=True)
    tmp = os.path.join(cache_dir, f"{wuxia_number}.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"title": title}, f)
    os.replace(tmp, os.path.join(cache_dir, f"{wuxia_number}.json"))


def build_epub(slug, wuxia_slug, chapters, session, out_path, book_title,
               min_delay, max_delay, start, end, title_offset,
               meta=None, cover_bytes=None, cache_dir=None,
               title_cache_dir=None, refresh=False, no_dedup=False):
    book = epub.EpubBook()
    book.set_identifier(f"novel-wuxia-{slug}-{int(time.time())}")
    book.set_title(book_title or (meta or {}).get("title") or slug.replace("-", " ").title())
    book.set_language("en")
    author = (meta or {}).get("author")
    book.add_author(author or "novellunar.com")
    description = (meta or {}).get("description")
    if description:
        book.add_metadata("DC", "description", description)
    for genre in (meta or {}).get("genres", []):
        book.add_metadata("DC", "subject", genre)
    if cover_bytes:
        book.set_cover("images/cover.jpg", cover_bytes)

    selected = [c for c in chapters if (start is None or c[0] >= start)
                and (end is None or c[0] <= end)]
    wuxia_novel_url = f"{WUXIA_BASE}/novel/{wuxia_slug}"
    toc = []
    n_cached = n_fresh = n_dupes = n_tcached = n_tfresh = 0
    seen_hashes = {}
    for i, (number, _nl_title) in enumerate(selected, 1):
        # --- chapter text from novellunar (reuses novel.py's cache) ---
        key = novel_mod.chapter_key(slug, number)
        cached = None if refresh else novel_mod.load_cached(cache_dir, key)
        if cached:
            text = cached["text"]
            print(f"[{i}/{len(selected)}] chapter {number}: text from cache")
            n_cached += 1
        else:
            print(f"[{i}/{len(selected)}] chapter {number}: downloading text")
            try:
                text = novel_mod.get_chapter_text(session, slug, number)
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                continue
            if not text:
                print("    no text found, skipped", file=sys.stderr)
                continue
            if cache_dir:
                save = f"Chapter {number}"
                novel_mod.save_cached(cache_dir, key, number, save, text)
            n_fresh += 1
            polite_sleep(min_delay, max_delay)

        # --- chapter title from wuxiaworld ---
        wuxia_number = number + title_offset
        chapter_title = None
        if wuxia_number >= 0:
            chapter_title = (None if refresh
                             else load_title_cached(title_cache_dir, wuxia_number))
            if chapter_title:
                n_tcached += 1
            else:
                print(f"    fetching wuxiaworld title (chapter {wuxia_number})")
                chapter_title = get_wuxia_title(session, wuxia_slug,
                                               wuxia_number, wuxia_novel_url)
                if chapter_title and title_cache_dir:
                    save_title_cached(title_cache_dir, wuxia_number,
                                      chapter_title)
                n_tfresh += 1
                polite_sleep(min_delay, max_delay)
        chapter_title = chapter_title or f"Chapter {number}"
        print(f"    title: {chapter_title[:70]}")

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
    stats = (f"{n_fresh} texts downloaded, {n_cached} texts from cache, "
             f"{n_tfresh} titles fetched, {n_tcached} titles from cache")
    if n_dupes:
        stats += f", {n_dupes} duplicates skipped"
    print(f"Saved {len(toc)} chapter(s) to {out_path} ({stats})")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Download a novellunar.com novel with wuxiaworld.com "
                    "chapter titles, saved as one EPUB.")
    ap.add_argument("url", help="Novellunar novel page URL, e.g. https://novellunar.com/novel/<slug>")
    ap.add_argument("--wuxia-slug", required=True,
                    help="Wuxiaworld novel slug, e.g. rebirth-of-tang-san")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title (default: novel title)")
    ap.add_argument("--start", type=float, default=None, help="First novellunar chapter number")
    ap.add_argument("--end", type=float, default=None, help="Last novellunar chapter number")
    ap.add_argument("--title-offset", type=int, default=-1,
                    help="Wuxia chapter number = novellunar number + offset (default: -1)")
    ap.add_argument("--min-delay", type=float, default=2.0,
                    help="Minimum seconds between requests (default: 2.0)")
    ap.add_argument("--max-delay", type=float, default=4.0,
                    help="Maximum seconds between requests (default: 4.0)")
    ap.add_argument("--no-cover", action="store_true",
                    help="Skip downloading the cover image")
    ap.add_argument("--no-meta", action="store_true",
                    help="Skip fetching novel metadata (title/author/synopsis)")
    ap.add_argument("--cache-dir", default=None,
                    help="Directory for cached chapter text (default: ./.cache/<slug>/, shared with novel.py)")
    ap.add_argument("--title-cache-dir", default=None,
                    help="Directory for cached wuxiaworld titles (default: ./.cache/wuxia-titles-<wuxia-slug>/)")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore the caches and re-download everything")
    ap.add_argument("--no-dedup", action="store_true",
                    help="Keep chapters with identical text (default: skip duplicates)")
    args = ap.parse_args()

    slug = novel_mod.slug_from_url(args.url)
    session = requests.Session()
    cache_dir = args.cache_dir or os.path.join(".cache", slug)
    title_cache_dir = (args.title_cache_dir
                       or os.path.join(".cache", "wuxia-titles-" + args.wuxia_slug))

    meta = {}
    if not args.no_meta:
        print("Fetching novel metadata ...")
        try:
            meta = novel_mod.get_novel_meta(session, slug)
            print(f"  title: {meta.get('title')}")
            print(f"  author: {meta.get('author')}")
            if meta.get("genres"):
                print(f"  genres: {', '.join(meta['genres'][:6])}")
        except Exception as e:
            print(f"  metadata failed: {e}", file=sys.stderr)

    cover_bytes = None
    if not args.no_cover and meta.get("cover_url"):
        print("Fetching cover ...")
        try:
            cover_bytes = novel_mod.get_cover_bytes(session, meta["cover_url"])
            print(f"  cover: {len(cover_bytes)//1024} KB jpeg")
        except Exception as e:
            print(f"  cover failed: {e}", file=sys.stderr)

    print("Fetching chapter list ...")
    chapters = novel_mod.get_chapters(slug, session)
    print(f"Found {len(chapters)} chapters.")
    if not chapters:
        sys.exit(1)

    novel_mod.migrate_cache_keys(cache_dir, slug, chapters)

    title = args.title or meta.get("title") or slug.replace("-", " ").title()
    out = args.output or f"{slug}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_epub(slug, args.wuxia_slug, chapters, session, out, title,
                    args.min_delay, args.max_delay,
                    args.start, args.end, args.title_offset,
                    meta, cover_bytes, cache_dir, title_cache_dir,
                    args.refresh, args.no_dedup)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
    