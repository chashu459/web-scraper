#!/usr/bin/env python3
"""Download a novel from novellunar.com (or a similar Next.js novel site)
and save all chapters as a single EPUB.

Usage:
    python novel.py https://novellunar.com/novel/the-unrivaled-tang-sect \\
        [-o book.epub] [--start 1] [--end 50]
        [--min-delay 2] [--max-delay 4]

How it works:
- Chapter metadata (title + number) comes from the site's public chapters API.
- Each chapter page is server-rendered, so the text is parsed straight from
  the <article> element — no JS or login needed.

Politeness: requests are sequential with a random delay between them
(default 2–4s), user agents rotate, headers mimic a real browser, and
rate-limit (429) responses trigger exponential backoff. robots.txt allows
crawling (Allow: /).
"""
import argparse
import re
import sys
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

from scrape import chapter_html, fetch_with_retry, polite_sleep

API = "https://novellunar.com/api/novels/chapters"


def slug_from_url(url):
    m = re.search(r"/novel/([^/?#]+)", urlparse(url).path)
    if not m:
        raise ValueError("Could not find a /novel/<slug> in the URL")
    return m.group(1)


def get_chapters(slug, session, limit=500):
    """Return [(number, title)] for every chapter, oldest first."""
    chapters = []
    page = 1
    referer = f"https://novellunar.com/novel/{slug}"
    while True:
        r = fetch_with_retry(
            session,
            API,
            timeout=30,
            referer=referer,
            params={"novelSlug": slug, "page": page, "limit": limit,
                    "sort": "asc"},
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            break
        chapters.extend((c["chapterNumber"], c.get("title")) for c in data)
        print(f"  chapter list: page {page} ({len(chapters)} so far)")
        if len(data) < limit:
            break
        page += 1
        polite_sleep(1.0, 2.0)
    return chapters


def get_chapter_text(session, slug, number, timeout=30):
    novel_url = f"https://novellunar.com/novel/{slug}"
    url = f"{novel_url}/chapter/{number}"
    r = fetch_with_retry(session, url, timeout=timeout, referer=novel_url)
    soup = BeautifulSoup(r.text, "lxml")
    article = soup.select_one("article")
    if not article:
        return None
    # drop any nav/link clutter inside the article
    for tag in article.select("nav, button, a"):
        tag.decompose()
    lines = [ln.strip() for ln in article.get_text(separator="\n").split("\n")]
    text = "\n\n".join(ln for ln in lines if ln)
    return text or None


def build_novel_epub(slug, chapters, session, out_path, book_title,
                     min_delay, max_delay, start, end):
    book = epub.EpubBook()
    book.set_identifier(f"novel-{slug}-{int(time.time())}")
    book.set_title(book_title)
    book.set_language("en")
    book.add_author("novellunar.com")

    selected = [c for c in chapters if (start is None or c[0] >= start)
                and (end is None or c[0] <= end)]
    toc = []
    for i, (number, title) in enumerate(selected, 1):
        chapter_title = title or f"Chapter {number}"
        print(f"[{i}/{len(selected)}] chapter {number}: {chapter_title[:60]}")
        try:
            text = get_chapter_text(session, slug, number)
        except Exception as e:
            print(f"    failed: {e}", file=sys.stderr)
            continue
        if not text:
            print("    no text found, skipped", file=sys.stderr)
            continue
        ch = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chap{i:04d}.xhtml",
            content=chapter_html(chapter_title, text),
        )
        book.add_item(ch)
        toc.append(ch)
        polite_sleep(min_delay, max_delay)

    if not toc:
        print("Nothing to save — no chapters extracted.", file=sys.stderr)
        return False

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + toc
    epub.write_epub(out_path, book)
    print(f"Saved {len(toc)} chapter(s) to {out_path}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Download a novel to EPUB.")
    ap.add_argument("url", help="Novel page URL, e.g. https://novellunar.com/novel/<slug>")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title (default: novel slug)")
    ap.add_argument("--start", type=float, default=None, help="First chapter number")
    ap.add_argument("--end", type=float, default=None, help="Last chapter number")
    ap.add_argument("--min-delay", type=float, default=2.0,
                    help="Minimum seconds between requests (default: 2.0)")
    ap.add_argument("--max-delay", type=float, default=4.0,
                    help="Maximum seconds between requests (default: 4.0)")
    args = ap.parse_args()

    slug = slug_from_url(args.url)
    session = requests.Session()

    print("Fetching chapter list ...")
    chapters = get_chapters(slug, session)
    print(f"Found {len(chapters)} chapters.")
    if not chapters:
        sys.exit(1)

    title = args.title or slug.replace("-", " ").title()
    out = args.output or f"{slug}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_novel_epub(slug, chapters, session, out, title,
                          args.min_delay, args.max_delay,
                          args.start, args.end)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
