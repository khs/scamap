"""
check_links.py
--------------
Probe every external URL the map points at — kingdom calendar pages
(KINGDOM_CALENDAR_URLS in index.html) and barony websites (group_pins.csv).
For each URL, report:

  - HTTP status
  - Whether the body contains a clear "page not found" signal
  - Whether the body is suspiciously short
  - The final URL after redirects (so we can spot kingdoms that moved hosts)

Run periodically as a hygiene check after any kingdom redesigns its site.
With `--fix`, the script also clears broken or unreachable website URLs
out of group_pins.csv so the map stops linking to them. The placeholder
pin disappears entirely for any group without a working website.

Usage:
    python check_links.py            # report only
    python check_links.py --fix      # also overwrite group_pins.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests


SCRIPT_DIR = Path(__file__).parent
HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
TIMEOUT = 15

NOT_FOUND_SIGNALS = [
    r"\b(404|page\s+not\s+found|not\s+found|doesn(?:'|&#39;|’)t\s+exist|"
    r"this\s+page\s+(?:has\s+been\s+)?(?:moved|deleted|removed)|"
    r"oops!\s+(?:that\s+page|this\s+page)|"
    r"sorry,\s+(?:the\s+)?(?:page\s+)?(?:you|doesn))\b",
]
NOT_FOUND_RE = re.compile("|".join(NOT_FOUND_SIGNALS), re.IGNORECASE)


def extract_kingdom_urls_from_html() -> dict[str, str]:
    """Pull the KINGDOM_CALENDAR_URLS dict out of index.html."""
    index = (SCRIPT_DIR / "index.html").read_text(encoding="utf-8")
    block_m = re.search(
        r"const\s+KINGDOM_CALENDAR_URLS\s*=\s*{(.+?)};",
        index, re.DOTALL,
    )
    if not block_m:
        return {}
    block = block_m.group(1)
    urls = {}
    for m in re.finditer(r'"([^"]+)"\s*:\s*"([^"]+)"', block):
        urls[m.group(1)] = m.group(2)
    return urls


def check(url: str) -> tuple:
    """Returns (status, problem_or_None, final_url)."""
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HDRS, allow_redirects=True)
    except requests.RequestException as exc:
        return (None, f"fetch failed: {exc}", url)

    # Strip HTML tags for content checks so meta tags etc. don't generate false positives
    text_only = re.sub(r"<[^>]+>", " ", r.text)
    text_only = re.sub(r"\s+", " ", text_only)
    short = len(text_only.strip()) < 300

    problem = None
    if r.status_code != 200:
        problem = f"HTTP {r.status_code}"
    elif short:
        problem = f"body suspiciously short ({len(text_only)} chars)"
    elif NOT_FOUND_RE.search(text_only[:5000]):
        # Match in the first 5KB so we don't trip on footer "404 image not found"
        m = NOT_FOUND_RE.search(text_only[:5000])
        problem = f"found 'not found' signal: '{m.group(0)}'"

    return (r.status_code, problem, r.url)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fix", action="store_true",
                        help="Clear broken website URLs in group_pins.csv")
    args = parser.parse_args()

    # 1. KINGDOM_CALENDAR_URLS from index.html
    print("=== Kingdom calendar URLs (from index.html) ===")
    kingdom_urls = extract_kingdom_urls_from_html()
    print(f"Found {len(kingdom_urls)} kingdom URLs.\n")
    for name, url in sorted(kingdom_urls.items()):
        status, problem, final = check(url)
        flag = "OK" if not problem else "BAD"
        print(f"  [{flag}] {name}")
        print(f"        url:   {url}")
        if final != url:
            print(f"        final: {final}")
        if problem:
            print(f"        !! {problem}")
    print()

    # 2. group_pins.csv websites
    pins_path = SCRIPT_DIR / "group_pins.csv"
    if not pins_path.exists():
        print("(no group_pins.csv — skipping baronial website check)")
        return
    print("=== Barony / group websites (from group_pins.csv) ===")
    with open(pins_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys()) if rows else []
    rows_with_url = [r for r in rows if r.get("website")]
    print(f"Checking {len(rows_with_url)} URLs (this takes a couple minutes)...\n")
    bad = []
    bad_urls: set[str] = set()
    for row in rows_with_url:
        url = row["website"]
        status, problem, final = check(url)
        if problem:
            bad.append((row["kingdom"], row["group"], url, problem, final))
            bad_urls.add(url)
    if bad:
        print(f"Found {len(bad)} problematic links:\n")
        for kingdom, group, url, problem, final in bad:
            print(f"  [{kingdom}] {group}")
            print(f"    url:   {url}")
            if final != url:
                print(f"    final: {final}")
            print(f"    !! {problem}")
    else:
        print("All barony URLs OK.")

    if args.fix and bad_urls:
        cleared = 0
        for row in rows:
            if row.get("website") in bad_urls:
                row["website"] = ""
                cleared += 1
        with open(pins_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n--fix: cleared website URL on {cleared} rows of {pins_path.name}")
        print("(re-run check_links.py without --fix later to re-validate; some")
        print(" failures are transient like rate-limits or DNS hiccups)")


if __name__ == "__main__":
    main()
