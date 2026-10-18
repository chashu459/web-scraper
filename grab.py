#!/usr/bin/env python3
"""One front door for the novel scrapers: pick the adapter from the URL.

Usage:
    python grab.py <url> [adapter flags...]
    python grab.py                 # walks you through it

Detects the site from the URL and runs the matching adapter, forwarding any
extra flags straight through untouched. When the URL isn't enough on its own
(e.g. a WordPress blog home page instead of its chapter-list page), it asks
for the missing piece before running anything.

Options:
    -y, --yes        never prompt; fail instead if input is missing
                     (for scripts and cron)
    --adapter NAME   force an adapter: novel, wuxia, webnovel, wp
    --dry-run        print what would run, without running it

Examples:
    python grab.py https://www.wuxiaworld.com/novel/rebirth-of-tang-san -o rts.epub
    python grab.py https://bluesilvertranslations.wordpress.com/   # asks for chapter-list URL
    python grab.py --dry-run <url>    # show what would run, without running it
"""

import argparse
import os
import re
import subprocess
import sys
from urllib.parse import urlparse

ADAPTERS = {
    "novel": {
        "script": "novel.py",
        "site": "novellunar.com",
        "patterns": [r"(^|\.)novellunar\.com$"],
        "blurb": "novellunar.com novel page",
    },
    "wuxia": {
        "script": "wuxia_novel.py",
        "site": "wuxiaworld.com",
        "patterns": [r"(^|\.)wuxiaworld\.com$"],
        "blurb": "wuxiaworld.com novel page",
    },
    "webnovel": {
        "script": "webnovel.py",
        "site": "webnovel.com",
        "patterns": [r"(^|\.)webnovel\.com$"],
        "blurb": "webnovel.com book page (drives a real browser)",
    },
    "wp": {
        "script": "wp_novel.py",
        "site": "*.wordpress.com",
        "patterns": [r"(^|\.)wordpress\.com$"],
        "blurb": "WordPress translation blog chapter-list page",
    },
}

CHAPTER_LIST_HINTS = ("chapter", "toc", "contents", "list", "index")


class GrabError(Exception):
    pass


def normalize_url(url):
    url = url.strip()
    if not url:
        return url
    if "://" not in url:
        url = "https://" + url
    return url


def detect(url):
    """Return the adapter name matching the URL, or None."""
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return None
    if not host:
        return None
    for name, info in ADAPTERS.items():
        if any(re.search(p, host) for p in info["patterns"]):
            return name
    return None


def looks_like_chapter_list(url):
    path = urlparse(url).path.lower()
    return any(h in path for h in CHAPTER_LIST_HINTS)


def ask(prompt_text, no_prompt, input_fn, is_tty_fn):
    if no_prompt or not is_tty_fn():
        raise GrabError("need input but prompting is disabled "
                        "(use --yes only when every input is already given)")
    try:
        answer = input_fn(prompt_text).strip()
    except (EOFError, KeyboardInterrupt):
        raise GrabError("aborted: no input")
    if not answer:
        raise GrabError("aborted: empty input")
    return answer


def resolve(url, adapter_override=None, no_prompt=False,
            input_fn=None, is_tty_fn=None):
    """Walk through URL -> (adapter_name, final_url). Raises GrabError."""
    # Note: input_fn defaults to None (not input) so tests can patch
    # builtins.input; a default arg would bind the real builtin at def time.
    is_tty = is_tty_fn or sys.stdin.isatty
    input_fn = input_fn or input

    if not url:
        url = ask("Novel URL: ", no_prompt, input_fn, is_tty)
    url = normalize_url(url)

    name = adapter_override or detect(url)
    while not name:
        print("I don't recognize that site. Supported:")
        for key, info in ADAPTERS.items():
            print(f"  {key:<9} {info['blurb']}")
        choice = ask("Which one is it (name), or paste a different URL "
                     "[blank to abort]: ", no_prompt, input_fn, is_tty)
        if choice in ADAPTERS:
            name = choice
        elif "://" in choice or "." in choice:
            url = normalize_url(choice)
            name = detect(url)
            if not name:
                print("Still unrecognized.")
        else:
            raise GrabError(f"unknown adapter '{choice}'")
    if name not in ADAPTERS:
        raise GrabError(f"unknown adapter '{name}' "
                        f"(choose from: {', '.join(ADAPTERS)})")

    if name == "wp" and not looks_like_chapter_list(url):
        print("WordPress blogs need their chapter-list page, and this URL "
              "doesn't look like one.")
        url = ask("Chapter-list page URL (usually <blog>/chapter-list/): ",
                  no_prompt, input_fn, is_tty)
        url = normalize_url(url)
        if not detect(url) == "wp":
            raise GrabError("that doesn't look like a WordPress URL; aborting")

    return name, url


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Pick the right novel scraper from the URL and run it.",
        epilog="Examples:\n"
               "  grab.py https://www.wuxiaworld.com/novel/rebirth-of-tang-san -o rts.epub\n"
               "  grab.py https://bluesilvertranslations.wordpress.com/  # asks for chapter-list URL\n"
               "  grab.py --dry-run <url>\n\n"
               "Special cases live in special/ (e.g. novellunar text with "
               "wuxiaworld titles); run those scripts directly.")
    ap.add_argument("url", nargs="?", help="novel/chapter-list page URL")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="never prompt; fail if input is missing")
    ap.add_argument("--adapter", choices=list(ADAPTERS),
                    help="force an adapter instead of detecting from the URL")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would run, without running it")
    args, extra = ap.parse_known_args(argv)

    try:
        name, url = resolve(args.url, args.adapter, args.yes)
    except GrabError as e:
        print(f"grab: {e}", file=sys.stderr)
        return 2

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ADAPTERS[name]["script"])
    cmd = [sys.executable, script, url] + extra
    print(f"grab: {name} -> {' '.join(cmd)}")
    if args.dry_run:
        return 0
    try:
        proc = subprocess.run(cmd)
    except KeyboardInterrupt:
        return 130
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
