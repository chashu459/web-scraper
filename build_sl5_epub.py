#!/usr/bin/env python3
"""Assemble the complete Soul Land 5 EPUB from two sources:

- Chapters 1..1101 (novellunar numbering): text from novellunar.com, cached
  by novel.py / novel_wuxia.py in ./.cache/soul-land-5-the-rebirth-of-tang-san/
  Titles prefer the wuxiaworld title cache
  (./.cache/wuxia-titles-rebirth-of-tang-san/) when present.
- Chapters 1101..1183 (wuxiaworld numbering): full text unlocked via the
  daily wuxiaworld job, stored in ./.cache/wuxia-unlocked-rebirth-of-tang-san/
  as <n>.json files with {"number", "title", "text"}. Fill this cache with
  fetch_unlocked.py --auth wuxia-cookies.json, or directly with
  novel_wuxia.py --auth wuxia-cookies.json (which also builds the EPUB).

Usage:
    python build_sl5_epub.py [-o soul-land-5.epub]

Run it again after each daily unlock batch; it rebuilds from all cached
chapters. Missing chapters are simply skipped (the book grows over time).
"""
import argparse
import glob
import hashlib
import io
import json
import os
import re
import sys

import requests
from ebooklib import epub
from PIL import Image

from scrape import chapter_html, fetch_with_retry, polite_sleep
import novel as novel_mod

NL_SLUG = "soul-land-5-the-rebirth-of-tang-san"
NL_CACHE = os.path.join(".cache", NL_SLUG)
TITLE_CACHE = os.path.join(".cache", "wuxia-titles-rebirth-of-tang-san")
UNLOCKED_CACHE = os.path.join(".cache", "wuxia-unlocked-rebirth-of-tang-san")
# novellunar chapter n == wuxiaworld chapter n-1
NL_LAST = 1101
WUXIA_FIRST_MISSING = 1101
WUXIA_LAST = 1183


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="soul-land-5-rebirth-of-tang-san.epub")
    ap.add_argument("--offline", action="store_true",
                    help="Don't fetch anything online; rebuild the EPUB from "
                         "cached chapters only (no metadata/cover download)")
    args = ap.parse_args()

    session = requests.Session()
    meta = {}
    cover_bytes = None
    if not args.offline:
        print("Fetching novel metadata ...")
        meta = novel_mod.get_novel_meta(session, NL_SLUG)
        if meta.get("cover_url"):
            try:
                cover_bytes = novel_mod.get_cover_bytes(session, meta["cover_url"])
                print(f"  cover: {len(cover_bytes)//1024} KB jpeg")
            except Exception as e:
                print(f"  cover failed: {e}", file=sys.stderr)

    book = epub.EpubBook()
    book.set_identifier("soul-land-5-complete")
    book.set_title(meta.get("title") or "Soul Land 5: The Rebirth of Tang San")
    book.set_language("en")
    book.add_author(meta.get("author") or "Tang Jia San Shao")
    if meta.get("description"):
        book.add_metadata("DC", "description", meta["description"])
    for genre in meta.get("genres", []):
        book.add_metadata("DC", "subject", genre)
    if cover_bytes:
        book.set_cover("images/cover.jpg", cover_bytes)

    toc = []
    seen_hashes = {}
    n_nl = n_wuxia = n_dupes = 0

    def add_chapter(display_num, title, text):
        nonlocal n_dupes
        h = hashlib.md5("".join(text.split()).encode("utf-8")).hexdigest()
        if h in seen_hashes:
            print(f"  chapter {display_num}: duplicate of {seen_hashes[h]}, skipped")
            n_dupes += 1
            return
        seen_hashes[h] = display_num
        ch = epub.EpubHtml(
            title=title,
            file_name=f"chap{display_num:04d}.xhtml",
            content=chapter_html(title, text),
        )
        book.add_item(ch)
        toc.append(ch)

    # Part 1: novellunar chapters 1..1101 (= wuxia 0..1100)
    for n in range(1, NL_LAST + 1):
        wuxia_n = n - 1
        text = None
        for p in glob.glob(os.path.join(NL_CACHE, "*.json")):
            d = load_json(p)
            if d and d.get("number") == n and d.get("text", "").strip():
                text = d["text"]
                break
        if not text:
            continue
        t = load_json(os.path.join(TITLE_CACHE, f"{wuxia_n}.json"))
        title = (t or {}).get("title") or f"Chapter {n}"
        add_chapter(n, title, text)
        n_nl += 1

    # Part 2: wuxiaworld unlocked chapters 1101..1183
    for n in range(WUXIA_FIRST_MISSING, WUXIA_LAST + 1):
        d = load_json(os.path.join(UNLOCKED_CACHE, f"{n}.json"))
        if not d or not d.get("text", "").strip():
            continue
        title = d.get("title") or f"Chapter {n}"
        add_chapter(10000 + n, title, d["text"])
        n_wuxia += 1

    if not toc:
        print("Nothing to assemble — no cached chapters found.", file=sys.stderr)
        sys.exit(1)

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + toc
    out = args.output if args.output.endswith(".epub") else args.output + ".epub"
    epub.write_epub(out, book)
    print(f"Saved {len(toc)} chapter(s) to {out} "
          f"({n_nl} from novellunar, {n_wuxia} unlocked from wuxiaworld"
          f"{f', {n_dupes} duplicates skipped' if n_dupes else ''})")


if __name__ == "__main__":
    main()
