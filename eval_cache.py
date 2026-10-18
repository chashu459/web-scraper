#!/usr/bin/env python3
"""Evaluate the health of a novel chapter cache directory.

Scans .cache/<slug>/*.json and checks the invariants the downloaders rely on:
contiguous chapter numbers, no duplicates, every chapter titled and non-empty,
and no suspiciously short chapters (truncated fetches or teaser-only pages).

Usage:
    python eval_cache.py .cache/soul-land-5-the-rebirth-of-tang-san
    python eval_cache.py .cache/<slug> --min-words 200 --show 5

Exit code is 0 when every check passes, 1 otherwise, so it works in scripts.
"""

import argparse
import json
import os
import re
import sys

FILENAME_NUMBER = re.compile(r"^(\d+)-")


def load_chapters(cache_dir):
    """Return (chapters, problems). chapters maps number -> list of (path, data)."""
    chapters = {}
    problems = []
    try:
        names = sorted(os.listdir(cache_dir))
    except OSError as e:
        return chapters, [f"cannot list directory: {e}"]
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(cache_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            problems.append(f"{name}: unreadable ({e})")
            continue
        number = data.get("number")
        if not isinstance(number, int):
            m = FILENAME_NUMBER.match(name)
            number = int(m.group(1)) if m else None
        if number is None:
            problems.append(f"{name}: no chapter number found")
            continue
        chapters.setdefault(number, []).append((path, data))
    return chapters, problems


def check_contiguous(chapters):
    if not chapters:
        return False, "no chapters found"
    nums = sorted(chapters)
    gaps = [n for n in range(nums[0], nums[-1] + 1) if n not in chapters]
    if gaps:
        return False, f"{len(gaps)} gap(s): {gaps[:10]}{'...' if len(gaps) > 10 else ''}"
    return True, f"{len(nums)} chapters, {nums[0]}-{nums[-1]} contiguous"


def check_no_duplicates(chapters):
    dupes = sorted(n for n, files in chapters.items() if len(files) > 1)
    if dupes:
        return False, f"{len(dupes)} duplicate number(s): {dupes[:10]}"
    return True, "no duplicate chapter numbers"


def check_titles(chapters, show):
    bad = sorted(
        n for n, files in chapters.items()
        if not str(files[0][1].get("title") or "").strip()
    )
    if bad:
        return False, f"{len(bad)} missing title(s): {bad[:show]}"
    return True, "every chapter has a title"


def check_non_empty(chapters, show):
    bad = sorted(
        n for n, files in chapters.items()
        if not str(files[0][1].get("text") or "").strip()
    )
    if bad:
        return False, f"{len(bad)} empty chapter(s): {bad[:show]}"
    return True, "every chapter has text"


def check_length(chapters, min_words, show):
    short = []
    for n in sorted(chapters):
        text = str(chapters[n][0][1].get("text") or "")
        words = len(text.split())
        if words < min_words:
            short.append((n, words))
    if short:
        examples = ", ".join(f"ch.{n} ({w}w)" for n, w in short[:show])
        return False, f"{len(short)} chapter(s) under {min_words} words: {examples}"
    return True, f"all chapters >= {min_words} words"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Evaluate a novel chapter cache.")
    ap.add_argument("cache_dir", help="e.g. .cache/soul-land-5-the-rebirth-of-tang-san")
    ap.add_argument("--min-words", type=int, default=200,
                    help="flag chapters shorter than this (default: 200)")
    ap.add_argument("--show", type=int, default=5,
                    help="max examples to list per failing check (default: 5)")
    args = ap.parse_args(argv)

    chapters, problems = load_chapters(args.cache_dir)
    checks = [
        ("contiguous", check_contiguous(chapters)),
        ("no_duplicates", check_no_duplicates(chapters)),
        ("titles_present", check_titles(chapters, args.show)),
        ("non_empty", check_non_empty(chapters, args.show)),
        ("length_sanity", check_length(chapters, args.min_words, args.show)),
    ]

    width = max(len(name) for name, _ in checks)
    failed = 0
    for name, (ok, detail) in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{status}] {name:<{width}}  {detail}")
    for p in problems:
        print(f"[WARN] {p}")

    total = sum(len(v) for v in chapters.values())
    print(f"\n{total} file(s), "
          f"{len(chapters)} distinct chapter(s); "
          f"{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if (failed or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
