#!/usr/bin/env python3
"""Diagnose why a chapter fetch fails and suggest the fix.

Makes one polite request for a chapter URL and classifies the failure from
what the server actually returns, instead of guessing:

  - Cloudflare bot block  -> use the browser path or --cookies
  - plain 403 anti-bot    -> try --cookies / --impersonate chrome
  - 429 rate limit        -> slow down (--min-delay/--max-delay)
  - soft-404 (200, empty) -> chapter does not exist upstream
  - teaser/gated content  -> needs a logged-in session (--auth)
  - JS-rendered page      -> needs a real browser (webnovel.py --browser)

Usage:
    python diagnose.py https://novellunar.com/novel/x/chapter-1102
    python diagnose.py <url> --cookies cookies.txt --impersonate chrome

Exit code is 0 when the fetch looks fine, 1 when a problem is classified.
"""

import argparse
import re
import sys

import requests
from bs4 import BeautifulSoup

from scrape import make_session, load_cookies

CF_BODY_MARKERS = (
    "attention required",
    "just a moment",
    "cf-challenge",
    "challenge-platform",
    "cf_clearance",
)
TEASER_MARKERS = (
    "continue reading",
    "this chapter is locked",
    "chapter is locked",
    "teaser",
    "sign in to continue",
    "log in to continue",
)
SOFT_404_MAX_HTML_BYTES = 2048


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return soup.get_text(separator="\n")


def looks_like_cloudflare(status, headers, html):
    if status != 403:
        return False
    lowered = {k.lower(): v.lower() for k, v in headers.items()}
    if "cf-ray" in lowered or "cloudflare" in lowered.get("server", ""):
        return True
    body = html.lower()
    return any(m in body for m in CF_BODY_MARKERS)


def looks_like_teaser(text):
    lowered = text.lower()
    return any(m in lowered for m in TEASER_MARKERS)


def classify(status, headers, html):
    """Pure classifier: (status, headers, html) -> (label, observation, suggestion)."""
    words = len(extract_text(html).split())
    observed = f"HTTP {status}, {len(html)} bytes HTML, ~{words} words extracted"

    if status == 429:
        return ("rate_limited", observed,
                "Slow down and retry later: raise --min-delay/--max-delay, "
                "or resume the run after a pause.")
    if looks_like_cloudflare(status, headers, html):
        return ("cloudflare_block", observed,
                "Cloudflare bot check. Plain HTTP won't pass: drive a real "
                "browser (see webnovel.py --browser) or reuse --cookies with "
                "a cf_clearance cookie solved once in your own browser.")
    if status == 403:
        return ("anti_bot_403", observed,
                "Blocked without a Cloudflare challenge. Try --cookies with "
                "an exported session, or --impersonate chrome.")
    if status == 404:
        return ("not_found_404", observed,
                "Server says the chapter does not exist. Check the chapter "
                "numbering / URL pattern (an off-by-one upstream looks like this).")
    if status != 200:
        return ("http_error", observed,
                f"Unexpected HTTP {status}. Retry once; if it persists the "
                "problem is upstream, not the extractor.")
    if words == 0:
        if len(html) <= SOFT_404_MAX_HTML_BYTES:
            return ("soft_404", observed,
                    "HTTP 200 but the page is nearly empty: this is a "
                    "soft-404, the chapter has no content upstream "
                    "(like novellunar chapters past the last real one).")
        return ("js_rendered", observed,
                "HTTP 200 with plenty of HTML but no text in the static "
                "markup: the chapter is likely JavaScript-rendered. Fetch "
                "with a real browser (see webnovel.py --browser).")
    text = extract_text(html)
    if looks_like_teaser(text):
        return ("teaser_gated", observed,
                "The page serves teaser/locked content to anonymous visitors. "
                "Fetch with a logged-in session (--auth / --cookies).")
    return ("fetch_ok", observed,
            "The fetch looks fine: 200 with real text. The problem is "
            "elsewhere (flags, cache state, or the EPUB build).")


def probe(url, cookies=None, impersonate=None, timeout=30):
    session = make_session(impersonate=impersonate)
    if cookies:
        load_cookies(session, cookies)
    session.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/126.0 Safari/537.36"})
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as e:
        return ("network_error",
                f"request failed: {e}",
                "Check the URL and your network first: DNS, connectivity, "
                "or the site being down. Not a scraper problem.")
    return classify(resp.status_code, dict(resp.headers), resp.text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Diagnose a failed chapter fetch.")
    ap.add_argument("url", help="chapter URL to probe")
    ap.add_argument("--cookies", default=None, help="cookies.txt to load first")
    ap.add_argument("--impersonate", default=None,
                    help="TLS impersonation profile, e.g. chrome")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args(argv)

    label, observed, suggestion = probe(args.url, args.cookies,
                                       args.impersonate, args.timeout)
    print(f"observed:   {observed}")
    print(f"diagnosis:  {label}")
    print(f"suggested:  {suggestion}")
    return 0 if label == "fetch_ok" else 1


if __name__ == "__main__":
    sys.exit(main())
