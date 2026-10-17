#!/usr/bin/env python3
"""Remove duplicate chapters from a novel.py cache directory.

Usage:
    python dedup_cache.py .cache/the-unrivaled-tang-sect [--dry-run] [--delete]

For every set of cached chapters with identical text, keeps the
lowest-numbered one and moves the rest into <cache>/_duplicates/ so the
cleanup is recoverable. Pass --delete to remove them permanently, or
--dry-run to only report what would change.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys


def load_cached(cache_dir):
    """Return [(number, label, path, text)] for every readable cached chapter."""
    chapters = []
    for fn in sorted(os.listdir(cache_dir)):
        if not fn.endswith(".json"):
            continue
        label = fn[:-5]
        try:
            number = float(label)
        except ValueError:
            continue
        path = os.path.join(cache_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            print(f"  unreadable {fn}: {e}", file=sys.stderr)
            continue
        text = d.get("text", "")
        if not text.strip():
            print(f"  empty {fn}, skipped", file=sys.stderr)
            continue
        chapters.append((number, label, path, text))
    return chapters


def main():
    ap = argparse.ArgumentParser(description="Remove duplicate cached chapters.")
    ap.add_argument("cache_dir", help="Cache dir, e.g. .cache/the-unrivaled-tang-sect")
    ap.add_argument("--dry-run", action="store_true",
                    help="Only report duplicates, change nothing")
    ap.add_argument("--delete", action="store_true",
                    help="Delete duplicates permanently instead of quarantining")
    args = ap.parse_args()

    if not os.path.isdir(args.cache_dir):
        print(f"not a directory: {args.cache_dir}", file=sys.stderr)
        sys.exit(1)

    chapters = load_cached(args.cache_dir)
    print(f"scanning {len(chapters)} cached chapter(s) ...")

    groups = {}
    for number, label, path, text in chapters:
        h = hashlib.md5("".join(text.split()).encode("utf-8")).hexdigest()
        groups.setdefault(h, []).append((number, label, path))

    dupes = {h: ns for h, ns in groups.items() if len(ns) > 1}
    if not dupes:
        print("no duplicates found.")
        return

    quarantine = os.path.join(args.cache_dir, "_duplicates")
    n_moved = 0
    for h in sorted(dupes, key=lambda h: min(n for n, _, _ in dupes[h])):
        members = sorted(dupes[h], key=lambda t: t[0])
        keep_num, keep_label, _ = members[0]
        for number, label, path in members[1:]:
            action = "would quarantine"
            if not args.dry_run:
                if args.delete:
                    os.remove(path)
                    action = "deleted"
                else:
                    os.makedirs(quarantine, exist_ok=True)
                    shutil.move(path, os.path.join(quarantine, os.path.basename(path)))
                    action = "quarantined"
                n_moved += 1
            print(f"  chapter {label} duplicates chapter {keep_label}: {action}")

    if args.dry_run:
        print(f"{sum(len(v) - 1 for v in dupes.values())} duplicate(s) would be moved "
              f"(rerun without --dry-run to apply).")
    elif args.delete:
        print(f"deleted {n_moved} duplicate(s).")
    else:
        print(f"quarantined {n_moved} duplicate(s) to {quarantine}/")


if __name__ == "__main__":
    main()
