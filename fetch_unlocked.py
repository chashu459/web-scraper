#!/usr/bin/env python3
"""Fetch wuxiaworld chapters unlocked on the user's account, using a browser
session exported by the daily unlock job.

The daily cron's browser task unlocks chapters while logged in but cannot
return chapter prose, so it exports the session auth instead (cookies and/or
tokens, saved as JSON by the cron worker). This script replays that session
with requests and downloads the full text of the unlocked chapters.

Usage:
    python fetch_unlocked.py --auth hidden_files/wuxia-cookies.json \
        --chapters 1101-1110 [--min-delay 2] [--max-delay 4]

The auth JSON looks like:
    {"cookies": {"name": "value", ...},
     "headers": {"Authorization": "Bearer ..."}}   # optional

Downloaded chapters are saved to ./.cache/wuxia-unlocked-rebirth-of-tang-san/
as <n>.json files with {"number", "title", "text"} — the same cache that
build_sl5_epub.py assembles into the book.

Politeness: sequential requests, random 2–4s delays, one browser user agent,
exponential backoff via fetch_with_retry.
"""
import argparse
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

from scrape import fetch_with_retry, polite_sleep

BASE = "https://www.wuxiaworld.com"
SLUG = "rebirth-of-tang-san"
PREFIX = "sl5-chapter-"
CACHE_DIR = os.path.join(".cache", "wuxia-unlocked-" + SLUG)
GATED_MARKERS = (
    "log in to continue",
    "sign up to continue",
    "unlock free chapters",
)
TEASER_SUFFIX = re.compile(r"\s*\(Teaser\)\s*$", re.I)


def parse_range(spec):
    nums = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            nums.extend(range(int(a), int(b) + 1))
        elif part:
            nums.append(int(part))
    return nums


def parse_chapter_page(html_text):
    """Return (title, text) from a wuxiaworld chapter page's HTML.

    text is None when the chapter is locked for the session or empty.
    Shared by the requests fetcher and the real-browser fetcher.
    """
    soup = BeautifulSoup(html_text, "lxml")
    heading = soup.select_one('h4[data-testid="heading"]')
    title = None
    if heading:
        t = heading.select_one('[data-testid="title"]')
        title = TEASER_SUFFIX.sub(
            "", (t or heading).get_text(" ", strip=True)).strip() or None
    if any(m in html_text.lower() for m in GATED_MARKERS):
        return title, None  # still locked for this session
    body = soup.select_one("div.chapter-content div.chapter-body")
    if not body:
        return title, None
    paras = []
    for p in body.select(":scope > p"):
        for badge in p.select('span[aria-hidden]'):
            badge.decompose()
        t = p.get_text(" ", strip=True)
        if t:
            paras.append(t)
    text = "\n\n".join(paras)
    return title, (text or None)


def get_chapter(session, slug, prefix, number, novel_url):
    """Fetch one wuxiaworld chapter page; return (title, text).

    `slug`/`prefix` are novel-specific (e.g. "rebirth-of-tang-san" /
    "sl5-chapter-"). Returns (title, None) when the chapter is still locked
    for this session.
    """
    url = f"{BASE}/novel/{slug}/{prefix}{number}"
    r = fetch_with_retry(session, url, timeout=30, referer=novel_url)
    return parse_chapter_page(r.text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth", required=True,
                    help="JSON file with session auth: {\"cookies\": {...}, "
                         "\"headers\": {...}}")
    ap.add_argument("--chapters", required=True,
                    help="Chapter numbers, e.g. 1101-1110 or 1101,1102")
    ap.add_argument("--min-delay", type=float, default=2.0)
    ap.add_argument("--max-delay", type=float, default=4.0)
    ap.add_argument("--refresh", action="store_true",
                    help="Re-download chapters already in the cache")
    args = ap.parse_args()

    with open(args.auth, encoding="utf-8") as f:
        auth = json.load(f)

    session = requests.Session()
    for name, value in (auth.get("cookies") or {}).items():
        session.cookies.set(name, value, domain=".wuxiaworld.com")
    session.headers.update(auth.get("headers") or {})

    novel_url = f"{BASE}/novel/{SLUG}"
    os.makedirs(CACHE_DIR, exist_ok=True)
    numbers = parse_range(args.chapters)
    n_ok = n_locked = n_cached = 0
    for i, n in enumerate(numbers, 1):
        path = os.path.join(CACHE_DIR, f"{n}.json")
        if os.path.exists(path) and not args.refresh:
            print(f"[{i}/{len(numbers)}] chapter {n}: already cached")
            n_cached += 1
            continue
        print(f"[{i}/{len(numbers)}] chapter {n}: downloading")
        try:
            title, text = get_chapter(session, SLUG, PREFIX, n, novel_url)
        except Exception as e:
            print(f"    failed: {e}", file=sys.stderr)
            continue
        if not text:
            print("    still locked for this session, skipped", file=sys.stderr)
            n_locked += 1
            continue
        title = title or f"Chapter {n}"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"number": n, "title": title, "text": text}, f)
        os.replace(tmp, path)
        print(f"    saved: {title[:60]} ({len(text)} chars)")
        n_ok += 1
        polite_sleep(args.min_delay, args.max_delay)

    print(f"Done: {n_ok} downloaded, {n_cached} already cached, "
          f"{n_locked} still locked")
    sys.exit(0 if (n_ok or n_cached) else 1)


if __name__ == "__main__":
    main()
