#!/usr/bin/env python3
"""Download a book from webnovel.com and save all chapters as a single EPUB.

Webnovel sits behind a Cloudflare JavaScript challenge that plain HTTP
clients (requests, curl) cannot pass, so this script drives a real Chromium
browser via Playwright's async API instead. Run it on your own machine — a
residential connection plus a real browser passes the challenge; datacenter
IPs may not.

Setup (one time):
    pip install playwright
    playwright install chromium

The script keeps a persistent browser profile in ./.webnovel-profile/ so
Cloudflare clearance cookies survive between runs: if you get a "Verifying
you are human" challenge, run once with --headed, solve it in the visible
window, and later runs won't be challenged.

Usage:
    python webnovel.py https://www.webnovel.com/book/36245749908869305 \\
        [-o book.epub] [--start 0] [--end 50]
        [--min-delay 3] [--max-delay 6] [--no-cover] [--no-meta]
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
them (default 3–6s), like a human reader turning pages. If the site answers
with a rate-limit (429) page, the chapter is retried with exponential
backoff (1m, 2m, 4m) instead of being skipped.
"""
import argparse
import asyncio
import hashlib
import html as htmlmod
import io
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

from ebooklib import epub
from PIL import Image
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("playwright is not installed. Run:\n"
             "    pip install playwright\n"
             "    playwright install chromium")

from scrape import chapter_html, polite_sleep

BASE = "https://www.webnovel.com"


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


def save_cached(cache_dir, key, number, chapter_id, title, text):
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache_path(cache_dir, key) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"number": number, "chapter_id": chapter_id,
                   "title": title, "text": text}, f)
    os.replace(tmp, cache_path(cache_dir, key))


def cached_chapters(cache_dir):
    """Return [(number, chapter_id, title, text)] from cached JSON files.

    Used by --offline: rebuilds the book purely from the local cache without
    opening the browser. The text rides along so no key-based re-read is
    needed.
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
            chapter_id = d.get("chapter_id") or ""
            text = d.get("text", "")
            if number is None or not str(text).strip():
                continue
            number = float(number)
            if number.is_integer():
                number = int(number)
            chapters.append((number, chapter_id, d.get("title"), text))
        except Exception:
            continue
    chapters.sort(key=lambda c: c[0])
    return chapters


def _og(soup, prop):
    tag = soup.find("meta", property=f"og:{prop}")
    return htmlmod.unescape(tag["content"]).strip() if tag and tag.get("content") else None


async def soup_of(page):
    return BeautifulSoup(await page.content(), "lxml")


def jsonld_text(soup):
    """Fallback: chapter paragraphs from the page's NewsArticle JSON-LD.

    The page embeds paragraph markup (<p>…</p>) in its structured data; if
    the visible container comes back empty (e.g. a throttled content
    request), this can still recover the text.
    """
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            desc = item.get("description") or ""
            if "<p" not in desc:
                continue
            sub = BeautifulSoup(desc, "lxml")
            paras = [p.get_text(" ", strip=True) for p in sub.select("p")]
            text = "\n\n".join(t for t in paras if t)
            if len(text) > 200:  # avoid truncated snippets
                return text
    return None


async def launch_context(pw, profile_dir, headed):
    """Launch a persistent browser context, preferring real Google Chrome.

    NOTE: we deliberately do NOT override the User-Agent — Playwright's
    default UA matches the real browser version, while a hardcoded UA
    mismatches the Sec-CH-UA client hints and looks like a bot.
    """
    args = ["--disable-blink-features=AutomationControlled"]
    for channel in ("chrome", None):
        try:
            ctx = await pw.chromium.launch_persistent_context(
                profile_dir,
                headless=not headed,
                args=args,
                **({"channel": channel} if channel else {}),
            )
            # navigator.webdriver=true is the most blatant automation flag.
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});")
            return ctx
        except Exception as e:
            print(f"  browser channel {channel or 'chromium'} unavailable: "
                  f"{str(e)[:120]}", file=sys.stderr)
    raise RuntimeError("no usable browser found")


async def wait_past_challenge(page, timeout_ms=180000):
    """Wait out Cloudflare's 'Verifying you are human' challenge.

    The managed challenge usually clears on its own after a few seconds in
    a real browser. If it needs a manual checkbox click, run with --headed
    and click it in the visible window — this waits up to `timeout_ms`.
    Returns True if the page looks clear.
    """
    start = time.time()
    announced = False
    while (time.time() - start) * 1000 < timeout_ms:
        try:
            title = await page.title()
            html = await page.content()
        except Exception:
            await page.wait_for_timeout(3000)
            continue
        clear = ("Just a moment" not in title
                 and "Verifying you are human" not in html
                 and "challenges.cloudflare.com" not in html)
        if clear:
            return True
        if not announced:
            print("  Cloudflare challenge detected — waiting for it to clear."
                  " If it needs a checkbox, run with --headed and click it.",
                  file=sys.stderr)
            announced = True
        await page.wait_for_timeout(3000)
    return False


async def dismiss_cookie_banner(page):
    """Click 'Disagree' on the privacy banner if it shows up."""
    try:
        btn = page.get_by_text("Disagree", exact=True)
        if await btn.count():
            await btn.first.click(timeout=3000)
            await page.wait_for_timeout(1000)
    except Exception:
        pass


async def get_book_meta(page, book_id):
    """Fetch title, author, synopsis and cover URL from the book page."""
    await page.goto(f"{BASE}/book/{book_id}", timeout=60000)
    await wait_past_challenge(page)
    await page.wait_for_timeout(3000)
    await dismiss_cookie_banner(page)
    soup = await soup_of(page)
    return {
        "title": _og(soup, "title"),
        "author": _og(soup, "author"),
        "description": _og(soup, "description"),
        "cover_url": _og(soup, "image"),
    }


def extract_chapters_from_soup(soup, book_id):
    """Parse (number, chapter_id, title, volume) from catalog/book soup."""
    chapters = []
    volume = None
    for el in soup.select("h4.catalog-volume-header, a[href]"):
        if el.name == "h4":
            volume = el.get_text(" ", strip=True) or None
            continue
        href = el.get("href", "").split("?")[0]
        if book_id not in href:
            continue
        m = re.search(r"/book/\d+/(\d+)\s*$", href)
        if not m:
            m = re.search(r"_(\d+)\s*$", href)
        if not m:
            continue
        chapter_id = m.group(1)
        num_tag = el.select_one("i")
        try:
            number = int(num_tag.get_text(strip=True)) if num_tag else None
        except ValueError:
            number = None
        if number is None:
            # fall back to "Chapter N" in the title text
            t = el.get("title") or el.get_text(" ", strip=True)
            mt = re.search(r"[Cc]hapter\s+(\d+)", t)
            number = int(mt.group(1)) if mt else None
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


async def get_chapters(page, book_id):
    """Return [(number, chapter_id, title, volume)] oldest first.

    Tries the /catalog page first, then the book page's Table of Contents
    tab. Saves the page HTML to webnovel-debug.html if nothing is found.
    """
    async def settle():
        await page.wait_for_timeout(2500)
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)
        await page.evaluate("window.scrollTo(0, 0)")

    await page.goto(f"{BASE}/book/{book_id}/catalog", timeout=60000)
    await wait_past_challenge(page)
    await settle()
    await dismiss_cookie_banner(page)
    try:
        await page.wait_for_selector('a[href*="/book/"]', timeout=20000)
    except Exception:
        pass
    chapters = extract_chapters_from_soup(await soup_of(page), book_id)
    if chapters:
        return chapters

    # Fallback: the book page's Table of Contents tab.
    print("  catalog page gave no chapters, trying the book page ToC tab ...")
    await page.goto(f"{BASE}/book/{book_id}", timeout=60000)
    await wait_past_challenge(page)
    await settle()
    await dismiss_cookie_banner(page)
    for label in ("Table of Contents", "Contents", "Catalog"):
        try:
            tab = page.get_by_text(label, exact=False)
            if await tab.count():
                await tab.first.click(timeout=5000)
                await page.wait_for_timeout(2500)
                break
        except Exception:
            continue
    await settle()
    chapters = extract_chapters_from_soup(await soup_of(page), book_id)
    if chapters:
        return chapters

    with open("webnovel-debug.html", "w", encoding="utf-8") as f:
        f.write(await page.content())
    print("  saved page HTML to webnovel-debug.html for diagnosis",
          file=sys.stderr)
    return []


async def is_rate_limited(page):
    """True if the page looks like a 429 / rate-limit block.

    Only the <title> and <head> markup are inspected, so chapter prose can
    never false-positive (even a chapter literally titled "Chapter 429").
    """
    try:
        title = (await page.title()).lower()
        html = (await page.content()).lower()
    except Exception:
        return False
    head = html.split("</head>")[0] if "</head>" in html else html[:2000]
    if "too many requests" in title:
        return True
    return ("too many requests" in head or "rate limit" in head
            or "ratelimited" in head)


async def get_chapter(page, book_id, chapter_id, timeout=45000, max_retries=3):
    """Return (title, text); text is None when the chapter is locked/empty.

    Rate-limit (429) pages AND empty chapter containers are retried with
    exponential backoff instead of being recorded as empty chapters, so a
    rerun doesn't have to redo them. Failed sub-requests (e.g. a throttled
    content XHR) are logged to help diagnose empty pages.
    """
    url = f"{BASE}/book/{book_id}/{chapter_id}"
    failed_reqs = []

    def on_response(resp):
        try:
            if resp.status >= 400:
                failed_reqs.append(f"{resp.status} {resp.url[:150]}")
        except Exception:
            pass

    page.on("response", on_response)
    try:
        for attempt in range(max_retries + 1):
            del failed_reqs[:]
            await page.goto(url, timeout=60000)
            await wait_past_challenge(page, timeout_ms=120000)
            if await is_rate_limited(page):
                wait_s = 60 * (2 ** attempt)
                print(f"    rate limited (attempt {attempt + 1}/{max_retries + 1}), "
                      f"waiting {wait_s}s ...", file=sys.stderr)
                await page.wait_for_timeout(wait_s * 1000)
                continue
            # Wait for actual paragraphs, not just the container: the container
            # can server-render empty while the text is injected lazily.
            try:
                await page.wait_for_selector("div.cha-words p", timeout=timeout)
            except Exception:
                pass
            # Scroll through the article to trigger lazy-loaded paragraphs.
            try:
                await page.evaluate("""async () => {
                    const el = document.querySelector('div.cha-words');
                    if (el) el.scrollIntoView({block: 'start'});
                    for (let i = 0; i < 6; i++) {
                        window.scrollBy(0, 700);
                        await new Promise(r => setTimeout(r, 700));
                    }
                    window.scrollTo(0, 0);
                }""")
            except Exception:
                pass
            if await is_rate_limited(page):
                wait_s = 60 * (2 ** attempt)
                print(f"    rate limited after load (attempt {attempt + 1}/"
                      f"{max_retries + 1}), waiting {wait_s}s ...", file=sys.stderr)
                await page.wait_for_timeout(wait_s * 1000)
                continue
            soup = await soup_of(page)
            heading = soup.select_one(".cha-tit")
            title = heading.get_text(" ", strip=True) if heading else _og(soup, "title")
            # Locked chapters render a masked container instead of full text.
            if soup.select_one(".cha-content._lock, .styles_locked_content__jH2rZ"):
                print("    chapter is locked (paywalled)", file=sys.stderr)
                return title, None
            body = soup.select_one("div.cha-words")
            text = None
            if body:
                for badge in body.select(".tag-num"):
                    badge.decompose()
                paras = [p.get_text(" ", strip=True)
                         for p in body.select(":scope > p")]
                if not any(paras):
                    # Fallback: paragraphs nested deeper than direct children.
                    paras = [p.get_text(" ", strip=True) for p in body.select("p")]
                text = "\n\n".join(t for t in paras if t)
            if not text:
                # Fallback: paragraph markup embedded in JSON-LD structured data.
                text = jsonld_text(soup)
                if text:
                    print("    recovered text from page metadata", file=sys.stderr)
            if not text:
                if failed_reqs:
                    print(f"    {len(failed_reqs)} failed sub-request(s):",
                          file=sys.stderr)
                    for fr in failed_reqs[:5]:
                        print(f"      {fr}", file=sys.stderr)
                if attempt < max_retries:
                    wait_s = 60 * (2 ** attempt)
                    print(f"    empty chapter text (attempt {attempt + 1}/"
                          f"{max_retries + 1}), waiting {wait_s}s ...",
                          file=sys.stderr)
                    await page.wait_for_timeout(wait_s * 1000)
                    continue
                inner = len(body.decode_contents()) if body else 0
                await save_chapter_debug(
                    page, chapter_id,
                    f"chapter text element was empty (innerHTML {inner} chars)")
                return title, None
            return title, text
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
    print("    still rate limited after retries, skipping (rerun to retry)",
          file=sys.stderr)
    return None, None


async def save_chapter_debug(page, chapter_id, reason):
    """Save a failing chapter's HTML for diagnosis."""
    dbg = f"webnovel-debug-ch-{chapter_id}.html"
    try:
        title = await page.title()
    except Exception:
        title = "?"
    print(f"    {reason}; page title: {title!r}; saved HTML to {dbg}",
          file=sys.stderr)
    try:
        with open(dbg, "w", encoding="utf-8") as f:
            f.write(await page.content())
    except Exception:
        pass


async def get_cover_bytes(page, cover_url):
    """Download cover art and normalize to JPEG (best e-reader support)."""
    resp = await page.request.get(cover_url, timeout=30000)
    if resp.status != 200:
        raise RuntimeError(f"cover download failed: HTTP {resp.status}")
    data = await resp.body()
    if data[:2] == b"\xff\xd8":
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


async def build_epub(book_id, chapters, page, out_path, book_title,
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

    # Normalize to (number, chapter_id, title, volume, text_or_None).
    # Offline chapters come from cached_chapters() as
    # (number, chapter_id, title, text) and need no downloading.
    if offline:
        norm = [(c[0], c[1], c[2], None, c[3]) for c in chapters]
    else:
        norm = [(c[0], c[1], c[2], c[3], None) for c in chapters]
    selected = [c for c in norm if (start is None or c[0] >= start)
                and (end is None or c[0] <= end)]
    toc = []
    sections = {}  # volume -> list of EpubHtml, preserving order
    n_cached = n_fresh = n_dupes = 0
    seen_hashes = {}
    for i, (number, chapter_id, list_title, volume, pre_text) in enumerate(selected, 1):
        chapter_title = list_title or f"Chapter {number}"
        text = pre_text
        if text is None:
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
                    page_title, text = await get_chapter(page, book_id, chapter_id)
                except Exception as e:
                    print(f"    failed: {e}", file=sys.stderr)
                    continue
                if page_title:
                    chapter_title = page_title
                if not text:
                    if page_title is None:
                        continue  # get_chapter already printed the reason
                    print("    locked or no text found, skipped", file=sys.stderr)
                    continue
                if cache_dir:
                    save_cached(cache_dir, key, number, chapter_id, chapter_title, text)
                n_fresh += 1
                polite_sleep(min_delay, max_delay)
        else:
            print(f"[{i}/{len(selected)}] chapter {number}: from cache")
            n_cached += 1
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


def parse_args():
    ap = argparse.ArgumentParser(description="Download a webnovel.com book to EPUB.")
    ap.add_argument("url", help="Book page URL, e.g. https://www.webnovel.com/book/<id>")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--title", default=None, help="Book title (default: from the book page)")
    ap.add_argument("--start", type=int, default=None, help="First chapter number")
    ap.add_argument("--end", type=int, default=None, help="Last chapter number")
    ap.add_argument("--min-delay", type=float, default=3.0,
                    help="Minimum seconds between chapters (default: 3.0)")
    ap.add_argument("--max-delay", type=float, default=6.0,
                    help="Maximum seconds between chapters (default: 6.0)")
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
    ap.add_argument("--profile", default=".webnovel-profile",
                    help="Browser profile directory (default: ./.webnovel-profile/). "
                         "Cookies (including Cloudflare clearance) persist here, so "
                         "you only have to pass the challenge once.")
    ap.add_argument("--cdp", default=None, metavar="URL",
                    help="Attach to an already-running Chrome via CDP, e.g. "
                         "http://localhost:9222. Ultimate fallback: quit Chrome, "
                         "reopen it with --remote-debugging-port=9222, visit "
                         "webnovel.com once in it, then run with --cdp.")
    return ap.parse_args()


async def run(args):
    """Async entry point. Returns the process exit code."""
    book_id = book_id_from_url(args.url)
    cache_dir = args.cache_dir or os.path.join(".cache", f"webnovel-{book_id}")

    meta = {}
    cover_bytes = None
    chapters = []
    page = None
    context = None

    if args.offline:
        print("Offline mode: reading chapter list from cache ...")
        chapters = cached_chapters(cache_dir)
        print(f"Found {len(chapters)} cached chapter(s).")
        if not chapters:
            return 1
        # page=None: every chapter must come from the cache, so get_chapter()
        # is never called.
        ok = await build_epub(book_id, chapters, None, out_path(args, book_id),
                              default_title(args, book_id, meta), args.min_delay,
                              args.max_delay, args.start, args.end, meta,
                              cover_bytes, cache_dir, args.refresh,
                              args.no_dedup, offline=True)
        return 0 if ok else 1

    async with async_playwright() as pw:
        if args.cdp:
            # Attach to the user's own running Chrome: its real profile,
            # history and cookies make Cloudflare see an ordinary session.
            print(f"Attaching to Chrome at {args.cdp} ...")
            browser = await pw.chromium.connect_over_cdp(args.cdp)
            context = browser.contexts[0] if browser.contexts else None
            if context is None:
                raise RuntimeError("no open browser contexts found")
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});")
            page = await context.new_page()
        else:
            os.makedirs(args.profile, exist_ok=True)
            print(f"Using browser profile: {args.profile}")
            print("First run? If Cloudflare shows a challenge, solve it in the "
                  "browser window (--headed) — it will be remembered next time.")
            context = await launch_context(pw, args.profile, args.headed)
            page = await context.new_page()

        if not args.no_meta:
            print("Fetching book metadata ...")
            try:
                meta = await get_book_meta(page, book_id)
                print(f"  title: {meta.get('title')}")
                print(f"  author: {meta.get('author')}")
            except Exception as e:
                print(f"  metadata failed: {e}", file=sys.stderr)
        if not args.no_cover and meta.get("cover_url"):
            print("Fetching cover ...")
            try:
                cover_bytes = await get_cover_bytes(page, meta["cover_url"])
                print(f"  cover: {len(cover_bytes)//1024} KB jpeg")
            except Exception as e:
                print(f"  cover failed: {e}", file=sys.stderr)
        print("Fetching chapter list ...")
        try:
            chapters = await get_chapters(page, book_id)
            print(f"Found {len(chapters)} chapters.")
        except Exception as e:
            print(f"  chapter list failed: {e}", file=sys.stderr)

        if not chapters:
            # In CDP mode the browser belongs to the user — just close our tab.
            if args.cdp:
                try:
                    await page.close()
                except Exception:
                    pass
            else:
                await context.close()
            return 1

        title = args.title or meta.get("title") or f"Webnovel {book_id}"
        out = out_path(args, book_id)
        try:
            ok = await build_epub(book_id, chapters, page, out, title,
                                  args.min_delay, args.max_delay,
                                  args.start, args.end, meta, cover_bytes,
                                  cache_dir, args.refresh, args.no_dedup,
                                  offline=args.offline)
        finally:
            if args.cdp:
                try:
                    await page.close()
                except Exception:
                    pass
            elif context:
                await context.close()
        return 0 if ok else 1


def out_path(args, book_id):
    out = args.output or f"webnovel-{book_id}.epub"
    if not out.endswith(".epub"):
        out += ".epub"
    return out


def default_title(args, book_id, meta):
    return args.title or meta.get("title") or f"Webnovel {book_id}"


def main():
    args = parse_args()
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if in_loop:
        # Inside an existing event loop (e.g. Jupyter): run in a worker
        # thread with its own loop.
        import threading
        box = {}

        def runner():
            try:
                box["code"] = asyncio.run(run(args))
            except BaseException as e:  # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=runner)
        t.start()
        t.join()
        if "error" in box:
            raise box["error"]
        sys.exit(box.get("code", 0))
    else:
        sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
