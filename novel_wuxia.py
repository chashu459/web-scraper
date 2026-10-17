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
        [--auth wuxia-cookies.json] [--wuxia-last 1183]

Title mapping: wuxiaworld chapter number = novellunar chapter number +
--title-offset. The default -1 was verified for Soul Land 5 (novellunar
chapter 1 has the same text as wuxiaworld chapter 0, "Prologue").

Wuxiaworld login-gates recent chapter *bodies*, but chapter *titles* still
render for anonymous visitors, so titles can be fetched without logging in.
(Titles of gated chapters carry a " (Teaser)" suffix, which is stripped.)

Tail chapters: novellunar sometimes stops before wuxiaworld does (Soul
Land 5: novellunar ends at chapter 1101 = wuxiaworld 1100, while wuxiaworld
goes to 1183). Pass --auth with your exported wuxiaworld login cookies and
the script downloads the tail chapters' full *text* from wuxiaworld too,
saving one complete EPUB. Without --auth the tail is skipped with a note.
Tail chapters are cached in ./.cache/wuxia-unlocked-<wuxia-slug>/ (shared
with fetch_unlocked.py), so re-running only fetches newly unlocked ones.
A tail chapter that is still locked for your session is skipped — unlock it
on wuxiaworld first (e.g. via the daily unlock job), then re-run.

Exporting cookies (--auth): in Chrome/Edge, log in to wuxiaworld.com, then
install the "Cookie-Editor" extension. With a wuxiaworld.com tab open, open
Cookie-Editor -> Export and save the JSON (a list of {"name","value",...}
entries) to a file, e.g. wuxia-cookies.json. The script also accepts
{"cookies": {name: value}, "headers": {...}}. Keep this file private — it
is your login session. Cookies expire; re-export when downloads start
coming back locked.

If cookie export doesn't authenticate (the site may keep session state
outside cookies), use --browser instead: the script opens a real Chromium
window with a persistent profile (./.wuxia-profile/); log in to wuxiaworld
manually once in that window and press Enter in the terminal. The login is
remembered, so later runs just work — add --headless once the login is
saved to hide the window.

Blocked by a captcha? novellunar.com sits behind Cloudflare, which sometimes
challenges script traffic (HTTP 403). Two workarounds, combinable:
  1. Solve the challenge once in your real browser (open the novel page and
     click through "Verify you are human"), then export cookies with
     Cookie-Editor and pass them via --cookies. The cf_clearance cookie lets
     the script's requests through.
  2. Pass --impersonate chrome (needs: pip install curl_cffi) so the
     script's TLS fingerprint looks like real Chrome instead of Python.
Keep the polite delays — hammering a challenged IP can extend the block.

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
import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

from scrape import chapter_html, fetch_with_retry, polite_sleep, make_session, load_cookies
import novel as novel_mod
import fetch_unlocked as wuxia_text_mod

WUXIA_BASE = "https://www.wuxiaworld.com"
TEASER_SUFFIX = re.compile(r"\s*\(Teaser\)\s*$", re.I)


def discover_wuxia_prefix(session, wuxia_slug, novel_url, timeout=30):
    """Return (chapter_url_prefix, total_chapters) from the wuxiaworld novel page.

    The chapter slug prefix is novel-specific (e.g. "sl5-chapter-"); it is
    read from an actual chapter link. total_chapters comes from the page's
    embedded series snapshot (may be None).
    """
    r = fetch_with_retry(session, novel_url, timeout=timeout)
    soup = BeautifulSoup(r.text, "lxml")
    pat = re.compile(rf"/novel/{re.escape(wuxia_slug)}/([a-zA-Z0-9_-]+-chapter-)\d+/?$")
    prefix = None
    for a in soup.select("a[href]"):
        m = pat.search(a.get("href") or "")
        if m:
            prefix = m.group(1)
            break
    if not prefix:
        m = pat.search(r.text)
        prefix = m.group(1) if m else None
    total = None
    djac = soup.find(attrs={"data-djac-params": True})
    if djac:
        try:
            data = json.loads(djac["data-djac-params"])
            total = int((data.get("series_snapshot") or {}).get("total_chapters_available") or 0) or None
        except Exception:
            pass
    return prefix, total


def load_auth_session(auth_path):
    """Build an authenticated requests session from exported browser cookies.

    Accepts {"cookies": {name: value}, "headers": {...}} (the fetch_unlocked
    format) or a Cookie-Editor style list [{"name":..., "value":...}, ...].
    """
    with open(auth_path, encoding="utf-8") as f:
        auth = json.load(f)
    headers = {} if isinstance(auth, list) else (auth.get("headers") or {})
    s = requests.Session()
    n = load_cookies(s, auth_path)
    if not n:
        raise ValueError(f"no cookies found in {auth_path}")
    s.headers.update(headers)
    return s


def unlocked_cache_dir(wuxia_slug):
    return os.path.join(".cache", "wuxia-unlocked-" + wuxia_slug)


def load_unlocked(cache_dir, wuxia_number):
    p = os.path.join(cache_dir, f"{wuxia_number}.json")
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


def save_unlocked(cache_dir, wuxia_number, title, text):
    os.makedirs(cache_dir, exist_ok=True)
    tmp = os.path.join(cache_dir, f"{wuxia_number}.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"number": wuxia_number, "title": title, "text": text}, f)
    os.replace(tmp, os.path.join(cache_dir, f"{wuxia_number}.json"))


async def _launch_wuxia_context(pw, profile_dir, headed):
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


def fetch_tail_via_browser(wuxia_slug, prefix, numbers, novel_url,
                           unlocked_dir, title_cache_dir,
                           min_delay, max_delay, profile_dir, headed=True):
    """Download tail chapters through a real Chromium (persistent profile).

    The login lives in the browser profile, so this works however the site
    stores its session (cookies, localStorage, ...). On the first run the
    browser opens visibly: log in to wuxiaworld manually in the window, then
    press Enter in the terminal. Later runs reuse the saved login.

    Saves into the unlocked cache (same format as fetch_unlocked.py).
    Returns (n_downloaded, n_locked).
    """
    import asyncio
    import threading
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as pw:
            ctx = await _launch_wuxia_context(pw, profile_dir, headed)
            try:
                page = await ctx.new_page()
                # Login check via the rewards page (redirects when logged out).
                try:
                    await page.goto(
                        f"{WUXIA_BASE}/manage/subscriptions/daily-rewards",
                        timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"  rewards page failed to load: {e}", file=sys.stderr)
                # Let any redirect (login, challenge) settle.
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                # page.url needs no execution context; the selector may race
                # a navigation, so it gets its own guard.
                logged_out = "login" in page.url.lower()
                if not logged_out:
                    try:
                        logged_out = (
                            await page.query_selector('input[type="password"]')
                            is not None
                        )
                    except Exception:
                        pass
                if headed:
                    # Always pause: auto-detecting login state is unreliable,
                    # and this gives the user a chance to log in (or clear a
                    # captcha) in the visible window before fetching starts.
                    if logged_out:
                        print("You don't look logged in to wuxiaworld yet.")
                    else:
                        print("You look logged in already.")
                    print("Use the browser window to log in to wuxiaworld if "
                          "needed (first run only — the profile remembers it).")
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None, input,
                        "Press Enter here when ready to continue... ")
                elif logged_out:
                    print("Warning: you don't look logged in, but the browser "
                          "is headless — re-run without --headless to log in "
                          "first.", file=sys.stderr)
                n_ok = n_locked = 0
                for i, wuxia_n in enumerate(numbers, 1):
                    print(f"[tail wuxia {wuxia_n}] [{i}/{len(numbers)}] "
                          f"loading in browser")
                    url = (f"{WUXIA_BASE}/novel/{wuxia_slug}/"
                           f"{prefix}{wuxia_n}")
                    try:
                        await page.goto(url, timeout=60000)
                    except Exception as e:
                        print(f"    failed: {e}", file=sys.stderr)
                        continue
                    try:
                        await page.wait_for_selector(
                            'h4[data-testid="heading"]', timeout=20000)
                    except Exception:
                        pass
                    # Scroll like a reader; also triggers lazy-loaded content.
                    try:
                        await page.evaluate("""async () => {
                            for (let k = 0; k < 10; k++) {
                                window.scrollBy(0, 800);
                                await new Promise(r => setTimeout(r, 500));
                            }
                        }""")
                    except Exception:
                        pass
                    title, text = wuxia_text_mod.parse_chapter_page(
                        await page.content())
                    if not text:
                        print("    still locked or empty — unlock it on "
                              "wuxiaworld first, then re-run", file=sys.stderr)
                        n_locked += 1
                        continue
                    title = title or f"Chapter {wuxia_n}"
                    save_unlocked(unlocked_dir, wuxia_n, title, text)
                    if title_cache_dir:
                        save_title_cached(title_cache_dir, wuxia_n, title)
                    print(f"    saved: {title[:60]} ({len(text)} chars)")
                    n_ok += 1
                    await asyncio.sleep(random.uniform(min_delay, max_delay))
                return n_ok, n_locked
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
        return box["result"]
    return asyncio.run(_run())


def get_wuxia_title(session, wuxia_slug, prefix, wuxia_number, novel_url, timeout=30):
    """Fetch a wuxiaworld chapter page and return its heading title.

    Returns None if the title can't be found. Gated chapters still render
    their heading (with a " (Teaser)" suffix, which is stripped); only the
    body is withheld.
    """
    url = f"{WUXIA_BASE}/novel/{wuxia_slug}/{prefix}{wuxia_number}"
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
               title_cache_dir=None, refresh=False, no_dedup=False,
               wuxia_prefix=None, wuxia_last=None, auth_session=None,
               unlocked_dir=None, browser_tail=False, wuxia_profile_dir=None,
               headless_browser=False):
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
                                               wuxia_prefix, wuxia_number,
                                               wuxia_novel_url)
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

    # --- tail chapters past novellunar's range: full text from wuxiaworld ---
    tail_stats = ""
    nl_last = max((c[0] for c in chapters), default=None)
    tail_start = int(nl_last + title_offset + 1) if nl_last is not None else None
    want_tail = end is None or (nl_last is not None and end >= nl_last)
    if wuxia_last and tail_start is not None and tail_start <= wuxia_last and want_tail:
        if auth_session is None and not browser_tail:
            print(f"novellunar ends at chapter {int(nl_last)} (= wuxiaworld "
                  f"{tail_start - 1}); wuxiaworld has "
                  f"{wuxia_last - tail_start + 1} more chapters "
                  f"({tail_start}..{wuxia_last}). Pass --auth <cookies.json> "
                  f"or --browser to include their full text in this EPUB.")
        else:
            udir = unlocked_dir or os.path.join(".cache",
                                                "wuxia-unlocked-" + wuxia_slug)
            n_tcch = n_tfr = n_tlock = 0
            numbers = list(range(tail_start, wuxia_last + 1))
            if browser_tail:
                # Real Chromium with a persistent profile: the user logs in
                # manually once, then the profile remembers it. Fills the
                # unlocked cache; the loop below just reads it.
                to_fetch = [n for n in numbers
                            if refresh or not load_unlocked(udir, n)]
                n_tcch = len(numbers) - len(to_fetch)
                if to_fetch:
                    print(f"Opening a browser window for wuxiaworld "
                          f"({len(to_fetch)} chapters to fetch) ...")
                    b_ok, b_locked = fetch_tail_via_browser(
                        wuxia_slug, wuxia_prefix, to_fetch, wuxia_novel_url,
                        udir, title_cache_dir, min_delay, max_delay,
                        wuxia_profile_dir or "./.wuxia-profile",
                        headed=not headless_browser)
                    n_tfr += b_ok
                    n_tlock += b_locked
            for wuxia_n in numbers:
                d = load_unlocked(udir, wuxia_n)
                if d:
                    title, text = d.get("title"), d["text"]
                    print(f"[tail wuxia {wuxia_n}] text from unlocked cache")
                    if not browser_tail:
                        n_tcch += 1
                elif browser_tail:
                    # Already reported as locked during the browser run.
                    continue
                else:
                    print(f"[tail wuxia {wuxia_n}] downloading text (authenticated)")
                    try:
                        title, text = wuxia_text_mod.get_chapter(
                            auth_session, wuxia_slug, wuxia_prefix, wuxia_n,
                            wuxia_novel_url)
                    except Exception as e:
                        print(f"    failed: {e}", file=sys.stderr)
                        continue
                    if not text:
                        print("    still locked for this session or empty — unlock "
                              "it on wuxiaworld first, then re-run", file=sys.stderr)
                        n_tlock += 1
                        continue
                    title = title or f"Chapter {wuxia_n}"
                    save_unlocked(udir, wuxia_n, title, text)
                    if title_cache_dir:
                        save_title_cached(title_cache_dir, wuxia_n, title)
                    n_tfr += 1
                    polite_sleep(min_delay, max_delay)
                if not no_dedup:
                    h = hashlib.md5("".join(text.split()).encode("utf-8")).hexdigest()
                    if h in seen_hashes:
                        print(f"    duplicate of chapter {seen_hashes[h]}, skipped")
                        n_dupes += 1
                        continue
                    seen_hashes[h] = f"wuxia {wuxia_n}"
                ch = epub.EpubHtml(
                    title=title,
                    file_name=f"chap{len(toc)+1:04d}.xhtml",
                    content=chapter_html(title, text),
                )
                book.add_item(ch)
                toc.append(ch)
            tail_stats = (f", tail: {n_tfr} downloaded, {n_tcch} from unlocked "
                          f"cache" + (f", {n_tlock} still locked" if n_tlock else ""))

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + toc
    epub.write_epub(out_path, book)
    stats = (f"{n_fresh} texts downloaded, {n_cached} texts from cache, "
             f"{n_tfresh} titles fetched, {n_tcached} titles from cache"
             f"{tail_stats}")
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
    ap.add_argument("--auth", default=None, metavar="COOKIES_JSON",
                    help="Exported wuxiaworld login cookies ({\"cookies\": {...}} "
                         "or Cookie-Editor JSON list). Enables downloading full "
                         "chapter TEXT for chapters past novellunar's range.")
    ap.add_argument("--wuxia-last", type=int, default=None,
                    help="Last wuxiaworld chapter number to include in the tail "
                         "(default: auto from the novel page)")
    ap.add_argument("--wuxia-prefix", default=None,
                    help="Chapter URL prefix, e.g. sl5-chapter- (default: "
                         "auto-discovered from the novel page)")
    ap.add_argument("--cookies", default=None, metavar="COOKIES_JSON",
                    help="Cookie-Editor JSON export to send with novellunar "
                         "requests — reuse a cf_clearance cookie after solving "
                         "a Cloudflare challenge once in a real browser")
    ap.add_argument("--impersonate", default=None, metavar="TARGET",
                    help="Mimic a real browser's TLS fingerprint via curl_cffi "
                         "(e.g. --impersonate chrome); needs: pip install curl_cffi")
    ap.add_argument("--chapter-count", type=int, default=None, metavar="N",
                    help="Skip the chapter-list API and assume chapters 1..N "
                         "(the API endpoint can be bot-blocked while chapter "
                         "pages still work; empty chapters are skipped)")
    ap.add_argument("--browser", action="store_true",
                    help="Fetch tail chapters through a real Chromium window "
                         "(persistent profile). Log in to wuxiaworld manually "
                         "once in the window; the login is remembered. Use "
                         "instead of --auth when cookie export doesn't work.")
    ap.add_argument("--wuxia-profile", default=None, metavar="DIR",
                    help="Browser profile directory (default: ./.wuxia-profile/)")
    ap.add_argument("--headless", action="store_true",
                    help="With --browser, run the browser headless (only "
                         "after the login is already saved in the profile)")
    args = ap.parse_args()

    slug = novel_mod.slug_from_url(args.url)
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
    if args.chapter_count:
        # Bypass the /api/novels/chapters endpoint (it can be behind
        # stricter bot protection than the chapter pages themselves).
        # Novellunar titles are generic anyway; real titles come from
        # wuxiaworld. Empty (soft-404) chapters are skipped during download.
        chapters = [(n, f"Chapter {n}")
                    for n in range(1, args.chapter_count + 1)]
        print(f"  using explicit chapter count: {args.chapter_count}")
    else:
        chapters = novel_mod.get_chapters(slug, session)
    print(f"Found {len(chapters)} chapters.")
    if not chapters:
        sys.exit(1)

    novel_mod.migrate_cache_keys(cache_dir, slug, chapters)

    wuxia_novel_url = f"{WUXIA_BASE}/novel/{args.wuxia_slug}"
    wuxia_prefix = args.wuxia_prefix
    wuxia_total = None
    if not wuxia_prefix:
        print("Detecting wuxiaworld chapter URL prefix ...")
        try:
            wuxia_prefix, wuxia_total = discover_wuxia_prefix(
                session, args.wuxia_slug, wuxia_novel_url)
        except Exception as e:
            print(f"  Could not detect chapter URL prefix: {e}", file=sys.stderr)
            print("  Pass --wuxia-prefix explicitly.", file=sys.stderr)
            sys.exit(1)
    print(f"  chapter prefix: {wuxia_prefix}"
          + (f" ({wuxia_total} chapters listed)" if wuxia_total else ""))

    wuxia_last = args.wuxia_last
    if wuxia_last is None and wuxia_total:
        # wuxiaworld numbers Soul Land 5 chapters from 0
        wuxia_last = wuxia_total - 1
        print(f"  tail will run to wuxiaworld chapter {wuxia_last} "
              f"(override with --wuxia-last)")

    auth_session = None
    if args.auth:
        try:
            auth_session = load_auth_session(args.auth)
            print("  authenticated session ready for tail chapters")
        except Exception as e:
            print(f"  --auth failed: {e}", file=sys.stderr)
            sys.exit(1)
    if args.browser and args.auth:
        print("  --browser takes precedence over --auth for tail chapters")

    title = args.title or meta.get("title") or slug.replace("-", " ").title()
    out = args.output or f"{slug}.epub"
    if not out.endswith(".epub"):
        out += ".epub"

    ok = build_epub(slug, args.wuxia_slug, chapters, session, out, title,
                    args.min_delay, args.max_delay,
                    args.start, args.end, args.title_offset,
                    meta, cover_bytes, cache_dir, title_cache_dir,
                    args.refresh, args.no_dedup,
                    wuxia_prefix=wuxia_prefix, wuxia_last=wuxia_last,
                    auth_session=auth_session,
                    browser_tail=args.browser,
                    wuxia_profile_dir=args.wuxia_profile,
                    headless_browser=args.headless)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
