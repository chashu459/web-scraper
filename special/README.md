# Special cases

Scripts that don't fit the normal one-site-one-scraper pattern live here.
They're kept separate on purpose: `grab.py` in the repo root handles the
standard cases, and these cover one-off combinations that would complicate
it.

## novel_wuxia.py — novellunar text with wuxiaworld titles

**When to use it:** you want a novel's chapter *text* from novellunar.com
but its chapter *titles* from wuxiaworld.com. This exists because
novellunar titles are generic ("Chapter 1", "Chapter 2", ...) while
wuxiaworld has real titles ("Chapter 1: Is It The End? Am I Going To Be
Eaten?"). It was built for one novel — Soul Land 5 — and the title-offset
it relies on was verified for that novel only.

**How it works:**
- Chapter text comes from novellunar (same cache `novel.py` uses, so
  previously downloaded chapters are reused).
- Chapter titles come from wuxiaworld. Wuxiaworld login-gates recent
  chapter *bodies*, but *titles* still render for anonymous visitors, so
  titles don't need a login. Gated titles carry a " (Teaser)" suffix,
  which the script strips.
- Title mapping: wuxiaworld chapter number = novellunar chapter number +
  `--title-offset`. The default `-1` was verified for Soul Land 5
  (novellunar chapter 1 has the same text as wuxiaworld chapter 0,
  "Prologue"). **For any other novel, verify the offset first** with a
  2-chapter test and diff the text.
- Tail chapters: if novellunar stops before wuxiaworld does (Soul Land 5:
  novellunar ends at 1101 = wuxiaworld 1100, wuxiaworld goes to 1183),
  pass `--auth` with exported wuxiaworld login cookies and the script
  downloads the tail chapters' full text from wuxiaworld too. Without
  `--auth` the tail is skipped with a note. A tail chapter still locked
  for your session is skipped — unlock it on wuxiaworld first, then
  re-run.

**Run it:**
```bash
# From the repo root:
./.venv/bin/python special/novel_wuxia.py \
    https://novellunar.com/novel/soul-land-5-the-rebirth-of-tang-san \
    --wuxia-slug rebirth-of-tang-san -o soul-land-5.epub

# Quick 2-chapter test before a full run:
./.venv/bin/python special/novel_wuxia.py <novel-url> \
    --wuxia-slug <slug> --start 1 --end 2 -o test.epub
```

**Exporting cookies for `--auth`:** in Chrome/Edge, log in to
wuxiaworld.com, install the "Cookie-Editor" extension, open Cookie-Editor
-> Export on a wuxiaworld.com tab, and save the JSON to e.g.
`wuxia-cookies.json`. Keep that file private — it's your login session.
Cookies expire; re-export when downloads start coming back locked. If
cookie export doesn't authenticate, use `--browser` instead: it opens a
real Chromium window with a persistent profile (`./.wuxia-profile/`); log
in once manually and press Enter. Later runs reuse the saved login (add
`--headless` to hide the window).

**Blocked by Cloudflare?** novellunar sometimes challenges script traffic
(HTTP 403). Either solve the challenge once in your real browser and pass
the cookies via `--cookies` (the `cf_clearance` cookie lets requests
through), or pass `--impersonate chrome` (needs `pip install curl_cffi`).
Keep the polite delays — hammering a challenged IP extends the block.
