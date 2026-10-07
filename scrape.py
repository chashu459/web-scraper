#!/usr/bin/env python3
"""Scrape primary article content from web pages and save as an EPUB.

Usage:
    python scrape.py URL [URL ...] [-o book.epub] [--title "My Book"] [--lang en]

Each URL becomes a chapter. Content is extracted with trafilatura,
which pulls the main article text and drops nav/ads/comments.
"""
import argparse
import datetime
import html
import random
import sys
import time
from urllib.parse import urlparse

import requests
import trafilatura
from ebooklib import epub

# A small pool of real browser user agents; one is picked at random per
# request so traffic doesn't look like a single script signature.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]


# One identity per run: a real browser keeps the same user agent for the whole
# session, so we pick one at startup. Rotating it per request is a classic
# bot-detection signal — the opposite of what we want.
_SESSION_UA = random.choice(USER_AGENTS)


def make_headers(referer=None, user_agent=None):
    """Browser-like request headers, consistent within a run."""
    headers = {
        "User-Agent": user_agent or _SESSION_UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


HEADERS = make_headers()  # default, kept for backwards compatibility


def polite_sleep(min_s, max_s):
    """Sleep a random amount between min_s and max_s (human-like pacing)."""
    time.sleep(random.uniform(min_s, max_s))


def fetch_with_retry(client, url, timeout=30, max_retries=4, referer=None,
                     **kwargs):
    """GET with exponential backoff on rate limits (429) and server errors.

    Honors Retry-After when the server sends one. Raises on persistent
    failure or on 403 (blocked), so a block stops the run instead of
    hammering the server. Extra kwargs (e.g. params) go to client.get.
    """
    backoff = 5
    short = url if len(url) <= 80 else url[:77] + "..."
    for attempt in range(max_retries + 1):
        try:
            resp = client.get(url, headers=make_headers(referer),
                              timeout=timeout, **kwargs)
        except requests.RequestException as e:
            if attempt == max_retries:
                raise
            print(f"    request failed ({type(e).__name__}), "
                  f"retry {attempt + 1}/{max_retries} in {backoff}s: {short}",
                  file=sys.stderr)
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 403:
            raise RuntimeError(f"blocked by server (403) on {url}")
        if resp.status_code == 429:
            try:
                wait = int(resp.headers.get("Retry-After", backoff))
            except ValueError:
                wait = backoff
            print(f"    rate-limited, waiting {min(wait, 120)}s ...",
                  file=sys.stderr)
            time.sleep(min(wait, 120))
            backoff *= 2
            continue
        if resp.status_code in (500, 502, 503, 504):
            if attempt == max_retries:
                resp.raise_for_status()
            print(f"    server error {resp.status_code}, "
                  f"retry {attempt + 1}/{max_retries} in {backoff}s: {short}",
                  file=sys.stderr)
            time.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")


def fetch_article(url, timeout=30):
    resp = fetch_with_retry(requests, url, timeout=timeout)
    art = trafilatura.bare_extraction(
        resp.text,
        output_format="xml",
        include_images=False,
        include_comments=False,
        with_metadata=True,
        url=url,
    )
    if art is None:
        # fallback: plain-text extraction
        text = trafilatura.extract(resp.text, include_comments=False)
        if not text:
            return None
        return {"title": None, "text": text, "author": None, "date": None}

    paragraphs = []
    body = art.body
    if isinstance(body, str):
        # older trafilatura returns the XML body as a string
        from lxml import etree
        try:
            body = etree.fromstring(body.encode("utf-8"))
        except Exception:
            body = None
    if body is not None:
        for p in body.iter("p", "hi", "lb"):
            t = "".join(p.itertext()).strip()
            if t:
                paragraphs.append(t)
    text = "\n\n".join(paragraphs) or (art.text or "")
    if not text.strip():
        return None
    return {
        "title": art.title,
        "text": text,
        "author": art.author,
        "date": art.date,
    }


def chapter_html(title, body_text, author=None, date=None):
    parts = [f"<h1>{html.escape(title)}</h1>"]
    if author or date:
        meta = " — ".join(x for x in (author, date) if x)
        parts.append(f"<p><em>{html.escape(meta)}</em></p>")
    for para in body_text.split("\n\n"):
        para = para.strip()
        if para:
            parts.append(f"<p>{html.escape(para)}</p>")
    return "\n".join(parts)


def build_epub(urls, out_path, book_title, lang="en"):
    book = epub.EpubBook()
    book.set_identifier(f"scrape-{int(time.time())}")
    book.set_title(book_title)
    book.set_language(lang)
    book.add_author("web-scraper")

    chapters = []
    for i, url in enumerate(urls, 1):
        label = urlparse(url).netloc
        print(f"[{i}/{len(urls)}] fetching {url} ...")
        try:
            art = fetch_article(url)
        except Exception as e:
            print(f"    failed: {e}", file=sys.stderr)
            continue
        if not art or not art.get("text"):
            print(f"    no main content found, skipped", file=sys.stderr)
            continue

        title = art.get("title") or f"Article from {label}"
        body = art["text"]
        date = art.get("date")

        ch = epub.EpubHtml(
            title=title,
            file_name=f"chap{i}.xhtml",
            content=chapter_html(title, body, art.get("author"), date),
        )
        book.add_item(ch)
        chapters.append(ch)
        polite_sleep(1.5, 3.0)  # human-like pacing between requests

    if not chapters:
        print("Nothing to save — no articles extracted.", file=sys.stderr)
        return False

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters

    epub.write_epub(out_path, book)
    print(f"Saved {len(chapters)} chapter(s) to {out_path}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Scrape articles to EPUB.")
    ap.add_argument("urls", nargs="+", help="Page URLs to scrape")
    ap.add_argument("-o", "--output", default=None, help="Output .epub path")
    ap.add_argument("--title", default=None, help="Book title")
    ap.add_argument("--lang", default="en", help="Language code (default: en)")
    args = ap.parse_args()

    out = args.output
    if not out:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = f"scraped-{stamp}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    title = args.title or "Scraped articles"
    ok = build_epub(args.urls, out, title, args.lang)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
