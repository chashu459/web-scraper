#!/usr/bin/env python3
"""Download a novel from novellunar.com (or a similar Next.js novel site)
and save all chapters as a single EPUB.

Usage:
    python novel.py https://novellunar.com/novel/the-unrivaled-tang-sect \\
        [-o book.epub] [--start 1] [--end 50]
        [--min-delay 2] [--max-delay 4] [--no-cover] [--no-meta]
        [--cache-dir DIR] [--refresh] [--offline] [--no-dedup]

Resuming: downloaded chapters are cached in ./.cache/<slug>/ (as JSON), so
interrupting the run and starting it again only fetches what is missing.
Use --refresh to re-download everything.

How it works:
- Novel metadata (title, author, synopsis, genres, cover) is read from the
  novel page's OpenGraph tags. The cover is downloaded and converted to
  JPEG for maximum e-reader compatibility.
- Chapter metadata (title + number) comes from the site's public chapters API.
- Each chapter page is server-rendered, so the text is parsed straight from
  the <article> element — no JS or login needed.

Politeness: requests are sequential with a random delay between them
(default 2–4s), one browser user agent is used for the whole run, headers
mimic a real browser, and
rate-limit (429) responses trigger exponential backoff. robots.txt allows
crawling (Allow: /).

If Cloudflare blocks plain HTTP entirely (403 / "Just a moment..." loop),
pass --browser: the script opens a real Chromium window with a persistent
profile, pauses so you can clear the "Verify you are human" check by hand,
then copies the browser's cookies and user agent into the requests session
and downloads the chapters the fast way. The profile remembers the
clearance, so later runs can add --headless.
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

import scrape as scrape_mod
from scrape import chapter_html, fetch_with_retry, polite_sleep, make_session, load_cookies

API = "https://novellunar.com/api/novels/chapters"
NOVEL_URL = "https://novellunar.com/novel/{slug}"


def _og(soup, prop):
    tag = soup.find("meta", property=f"og:{prop}")
    return htmlmod.unescape(tag["content"]).strip() if tag and tag.get("content") else None


def get_novel_meta(session, slug):
    """Fetch title, author, synopsis, genres and cover URL from the novel page."""
    url = NOVEL_URL.format(slug=slug)
    r = fetch_with_retry(session, url, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")

    title = _og(soup, "title")
    if title:
        title = re.sub(r"\s+Novel$", "", title).strip()

    description = _og(soup, "description") or ""
    author = None
    m = re.search(r"written by the author ([^,，]+)", description, re.I)
    if m:
        author = m.group(1).strip()
    synopsis = None
    m = re.search(r"the synopsis is:\s*(.*)$", description, re.I | re.S)
    if m:
        synopsis = m.group(1).strip()

    genres = []
    m = re.search(r'genres\\+":\s*\[(.*?)\]', r.text)
    if m:
        genres = [htmlmod.unescape(g).strip()
                  for g in re.findall(r'\\*"([^"\\]+)\\*"', m.group(1))]

    return {
        "title": title,
        "author": author,
        "description": synopsis or description or None,
        "genres": genres,
        "cover_url": _og(soup, "image"),
    }


def chapter_key(slug, number):
    """Collision-proof cache key: chapter number + hash of its URL.

    Also normalizes int/float numbering (1 vs 1.0) via %g formatting, in
    both the key and the hashed URL.
    """
    num = "%g" % number
    url = f"https://novellunar.com/novel/{slug}/chapter/{num}"
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{num}-{digest}"


def old_chapter_key(number):
    """Pre-fix key format (bare number), for migrating existing caches."""
    return f"{number}"


def migrate_cache_keys(cache_dir, slug, chapters):
    """Rename old-format cache files to the collision-proof format.

    Only migrates when an old key maps to exactly one chapter; ambiguous
    files are left alone (those chapters will simply re-download).
    """
    if not cache_dir or not os.path.isdir(cache_dir):
        return
    by_old = {}
    for number, _ in chapters:
        by_old.setdefault(old_chapter_key(number), []).append(
            (number, chapter_key(slug, number)))
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
    touching the network. Chapter numbers come from each file's stored
    "number" field (not the filename, which may include a URL hash).
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


def slug_from_url(url):
    m = re.search(r"/novel/([^/?#]+)", urlparse(url).path)
    if not m:
        raise ValueError("Could not find a /novel/<slug> in the URL")
    return m.group(1)


async def _launch_novellunar_context(pw, profile_dir, headed):
    """Launch a persistent Chromium, preferring real Google Chrome.

    Mirrors webnovel.py: no User-Agent override (Playwright's default
    matches the real browser), AutomationControlled disabled, and the
    navigator.webdriver flag hidden. Honors the standard *proxy env vars,
    which plain requests picks up automatically but Playwright needs
    passed explicitly.
    """
    args = ["--disable-blink-features=AutomationControlled"]
    launch_kw = {"headless": not headed, "args": args}
    proxy_url = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                 or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    if proxy_url:
        proxy = {"server": proxy_url}
        bypass = os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
        if bypass:
            proxy["bypass"] = bypass
        launch_kw["proxy"] = proxy
    last_err = None
    for channel in ("chrome", None):
        try:
            ctx = await pw.chromium.launch_persistent_context(
                profile_dir, **launch_kw,
                **({"channel": channel} if channel else {}),
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});")
            return ctx
        except Exception as e:
            last_err = e
            print(f"  browser channel {channel or 'chromium'} unavailable: "
                  f"{str(e)[:120]}", file=sys.stderr)
    raise RuntimeError(f"no usable browser found: {last_err}")


def _challenge_present(title, html):
    return ("Just a moment" in (title or "")
            or "Verifying you are human" in (html or "")
            or "challenges.cloudflare.com" in (html or ""))


def solve_challenge_via_browser(session, novel_url, profile_dir, headed=True):
    """Clear Cloudflare's check in a real browser, then reuse its session.

    Opens the novel page in a persistent Chromium (the profile remembers
    the clearance, so this is a first-run step). Waits briefly for the
    managed challenge to clear on its own, then — when headed — always
    pauses so the user can click the checkbox in the visible window before
    the download starts. Copies the browser's cookies and user agent into
    the requests session, so the bulk download still runs over fast plain
    HTTP instead of one browser navigation per chapter.

    Returns True if the session can now fetch the novel page, False
    otherwise. Raises RuntimeError if no usable browser is found.
    """
    import asyncio
    import threading
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as pw:
            ctx = await _launch_novellunar_context(pw, profile_dir, headed)
            try:
                page = await ctx.new_page()
                print(f"  opening {novel_url} in the browser ...")
                try:
                    await page.goto(novel_url, timeout=60000,
                                    wait_until="domcontentloaded")
                except Exception as e:
                    print(f"  browser navigation failed: {e}", file=sys.stderr)
                # Give the managed challenge a moment to clear on its own.
                cleared = False
                for _ in range(20):
                    try:
                        title = await page.title()
                        html = await page.content()
                    except Exception:
                        break
                    if not _challenge_present(title, html):
                        cleared = True
                        break
                    await page.wait_for_timeout(3000)
                if headed:
                    # Always pause: auto-detecting the challenge state is
                    # unreliable, and this is the user's chance to click
                    # the checkbox in the visible window.
                    if cleared:
                        print("  the challenge looks cleared already.")
                    else:
                        print("  Cloudflare is still showing its check.")
                    print("  Use the browser window to clear the "
                          "'Verify you are human' check if needed "
                          "(first run only — the profile remembers it).")
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None, input,
                        "Press Enter here when ready to continue... ")
                elif not cleared:
                    print("  Warning: the challenge is still up and the "
                          "browser is headless — re-run without --headless "
                          "to clear it by hand.", file=sys.stderr)
                try:
                    ua = await page.evaluate("navigator.userAgent")
                except Exception:
                    ua = None
                try:
                    cookies = await ctx.cookies()
                except Exception:
                    cookies = []
                n = 0
                for c in cookies:
                    try:
                        kw = {"path": c.get("path") or "/"}
                        if c.get("domain"):
                            kw["domain"] = c["domain"]
                        session.cookies.set(c["name"], c.get("value", ""), **kw)
                        n += 1
                    except Exception:
                        continue
                print(f"  copied {n} cookie(s) from the browser")
                if ua:
                    # fetch_with_retry builds its headers from scrape's
                    # per-run user agent; point that at the browser's UA so
                    # the clearance cookie and the UA agree.
                    scrape_mod._SESSION_UA = ua
                    print("  using the browser's user agent for requests")
                return True
            finally:
                await ctx.close()

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if in_loop:
        # e.g. Jupyter: run in a worker thread with its own loop
        box = {}

        def target():
            box["result"] = asyncio.run(_run())

        th = threading.Thread(target=target)
        th.start()
        th.join()
        ran = box["result"]
    else:
        ran = asyncio.run(_run())
    if not ran:
        return False
    # Verify the session can now fetch the novel page before downloading.
    try:
        r = fetch_with_retry(session, novel_url, timeout=30)
    except Exception as e:
        print(f"  session check failed: {e}", file=sys.stderr)
        return False
    if _challenge_present(None, r.text):
        print("  still challenged after the browser step — clear the check "
              "in the window and re-run.", file=sys.stderr)
        return False
    print(f"  session check passed: {r.status_code}, {len(r.text)} bytes")
    return True


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
                     min_delay, max_delay, start, end, meta=None,
                     cover_bytes=None, cache_dir=None, refresh=False,
                     no_dedup=False, offline=False):
    book = epub.EpubBook()
    book.set_identifier(f"novel-{slug}-{int(time.time())}")
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
    toc = []
    n_cached = n_fresh = n_dupes = 0
    seen_hashes = {}  # normalized-text hash -> chapter number
    for i, (number, title) in enumerate(selected, 1):
        chapter_title = title or f"Chapter {number}"
        key = chapter_key(slug, number)
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
                text = get_chapter_text(session, slug, number)
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                continue
            if not text:
                print("    no text found, skipped", file=sys.stderr)
                continue
            if cache_dir:
                save_cached(cache_dir, key, number, chapter_title, text)
            n_fresh += 1
            polite_sleep(min_delay, max_delay)
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
    ap.add_argument("--no-cover", action="store_true",
                    help="Skip downloading the cover image")
    ap.add_argument("--no-meta", action="store_true",
                    help="Skip fetching novel metadata (title/author/synopsis)")
    ap.add_argument("--cache-dir", default=None,
                    help="Directory for cached chapters (default: ./.cache/<slug>/)")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore the cache and re-download everything")
    ap.add_argument("--offline", action="store_true",
                    help="Don't fetch anything online; rebuild the EPUB from "
                         "cached chapters only (no metadata/cover download, "
                         "no chapter list fetch)")
    ap.add_argument("--no-dedup", action="store_true",
                    help="Keep chapters with identical text (default: skip duplicates)")
    ap.add_argument("--cookies", default=None, metavar="COOKIES_JSON",
                    help="Cookie-Editor JSON export to send with requests — "
                         "reuse a cf_clearance cookie after solving a "
                         "Cloudflare challenge once in a real browser")
    ap.add_argument("--impersonate", default=None, metavar="TARGET",
                    help="Mimic a real browser's TLS fingerprint via curl_cffi "
                         "(e.g. --impersonate chrome); needs: pip install curl_cffi")
    ap.add_argument("--chapter-count", type=int, default=None, metavar="N",
                    help="Skip the chapter-list API and assume chapters 1..N "
                         "(the API endpoint can be bot-blocked while chapter "
                         "pages still work; empty chapters are skipped)")
    ap.add_argument("--browser", action="store_true",
                    help="Clear Cloudflare's check in a real Chromium window "
                         "first (persistent profile), then download over "
                         "plain HTTP using the browser's cookies — use when "
                         "plain requests get 403 / 'Just a moment...'")
    ap.add_argument("--browser-profile", default="./.novellunar-profile/",
                    help="Browser profile directory for --browser (default: "
                         "./.novellunar-profile/)")
    ap.add_argument("--headless", action="store_true",
                    help="With --browser, hide the window (only after the "
                         "challenge is cleared and saved in the profile)")
    args = ap.parse_args()

    slug = slug_from_url(args.url)
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
    if args.browser:
        print("Solving the Cloudflare challenge in a real browser ...")
        novel_url = NOVEL_URL.format(slug=slug)
        try:
            ok = solve_challenge_via_browser(
                session, novel_url, args.browser_profile,
                headed=not args.headless)
        except RuntimeError as e:
            print(f"  browser step failed: {e}", file=sys.stderr)
            sys.exit(1)
        if not ok:
            print("  the browser step did not clear the block. Re-run "
                  "without --headless and clear the check by hand, or "
                  "pass --cookies with an exported cf_clearance cookie.",
                  file=sys.stderr)
            sys.exit(1)
    cache_dir = args.cache_dir or os.path.join(".cache", slug)

    meta = {}
    if not args.no_meta and not args.offline:
        print("Fetching novel metadata ...")
        try:
            meta = get_novel_meta(session, slug)
            print(f"  title: {meta.get('title')}")
            print(f"  author: {meta.get('author')}")
            if meta.get("genres"):
                print(f"  genres: {', '.join(meta['genres'][:6])}")
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
            # Bypass the /api/novels/chapters endpoint (it can be behind
            # stricter bot protection than the chapter pages themselves).
            # Empty (soft-404) chapters are skipped during download.
            chapters = [(n, f"Chapter {n}")
                        for n in range(1, args.chapter_count + 1)]
            print(f"  using explicit chapter count: {args.chapter_count}")
        else:
            chapters = get_chapters(slug, session)
        print(f"Found {len(chapters)} chapters.")
        migrate_cache_keys(cache_dir, slug, chapters)
    if not chapters:
        sys.exit(1)

    title = args.title or meta.get("title") or slug.replace("-", " ").title()
    out = args.output or f"{slug}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_novel_epub(slug, chapters, session, out, title,
                          args.min_delay, args.max_delay,
                          args.start, args.end, meta, cover_bytes,
                          cache_dir, args.refresh, args.no_dedup,
                          offline=args.offline)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
