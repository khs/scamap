"""
probe_baronial_calendars.py
---------------------------
Walks every barony website in group_locations.csv (for a configurable set of
kingdoms) and attempts to discover an ICS or scrape-able calendar feed.

Strategy per site:
  1. Try common WordPress ICS endpoints (Tribe Events `?ical=1`, R34 plugin,
     MEC, Simple Calendar) and check for `BEGIN:VCALENDAR`.
  2. Fetch the homepage and look for embedded `google.com/calendar/ical/...`
     URLs (the universal pattern for Google-Calendar-backed sites).
  3. Fetch a /calendar/ or /events/ page, same checks.

Output: prints findings; appends new entries to locals.csv. Run with
   python probe_baronial_calendars.py [kingdom_substring …]

Examples:
   python probe_baronial_calendars.py East          # only East baronies
   python probe_baronial_calendars.py East Middle   # both
   python probe_baronial_calendars.py               # everything in TARGETS
"""
from __future__ import annotations

import base64
import csv
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests

# Data CSVs live in the repo root, one level up from this reference/ script.
ROOT = Path(__file__).parent.parent
LOC_FILE = ROOT / "group_locations.csv"
LOCALS_FILE = ROOT / "locals.csv"

TARGETS = (
    "Kingdom of the East",
    "Kingdom of the Middle",
    "Kingdom of Northshield",
    "Kingdom of the Outlands",
)

HDRS = {
    "User-Agent": "Mozilla/5.0 (compatible; SCA Maps research bot)",
    "Accept": "text/calendar, application/json, text/html, */*",
}
TIMEOUT = 10

# Common WordPress/plugin ICS suffixes — tried against the homepage URL
ICS_PROBE_PATHS = (
    "/?ical=1",
    "/events/?ical=1",
    "/event/?ical=1",
    "/calendar/?ical=1",
    "/?post_type=tribe_events&eventDisplay=upcoming&ical=1",
    "/feed/eo-events",
    "/events/feed/",
)


def is_ics(body: str) -> bool:
    return "BEGIN:VCALENDAR" in body[:200] or body.startswith("BEGIN:VCALENDAR")


def n_events(body: str) -> int:
    return body.count("BEGIN:VEVENT")


def _try_ics(url: str) -> tuple[str, int] | None:
    """Fetch `url`. If it's valid ICS with >0 events, return (url, count)."""
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=HDRS, allow_redirects=True)
        if r.status_code == 200 and is_ics(r.text):
            events = n_events(r.text)
            if events > 0:
                return (url, events)
    except requests.RequestException:
        pass
    return None


def _ics_from_embed(html: str) -> str | None:
    """Many baronial sites embed Google Calendar via an iframe whose src looks
    like .../calendar/embed?...&src=<base64-id>... where <base64-id> decodes to
    the calendar's actual ID. WordPress encodes `&` as `&#038;` in HTML, so we
    can't just key off "?&" — we use a lookbehind for "src=" instead.
    """
    # Decode common HTML entity for '&' so URL boundaries match cleanly
    src = html.replace("&#038;", "&").replace("&amp;", "&")
    for m in re.finditer(
        r"google\.com/calendar/embed[^\"\'<>]*?\bsrc=([A-Za-z0-9_\-+/=%]+)",
        src,
    ):
        encoded = m.group(1).rstrip("&")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            decoded = base64.b64decode(padded, validate=False).decode("utf-8", "replace")
        except Exception:
            continue
        # Decoded value should be a calendar ID like "abc@group.calendar.google.com"
        if "@" not in decoded:
            continue
        cid_url = quote(decoded, safe="")
        return f"https://www.google.com/calendar/ical/{cid_url}/public/basic.ics"
    return None


def probe(url: str) -> tuple[str, int] | None:
    """Returns (ics_url, event_count) on the first hit, or None."""
    base = url.rstrip("/")
    # Step 1: common ICS suffixes
    for suffix in ICS_PROBE_PATHS:
        result = _try_ics(base + suffix)
        if result:
            return result

    # Step 2: fetch homepage + /calendar/ + /events/ — scan for calendar URLs
    for sub in ("", "/calendar/", "/events/", "/subscribe-to-our-calendar/",
                "/event-calendar/", "/calendar"):
        try:
            r = requests.get(base + sub, timeout=TIMEOUT,
                             headers=HDRS, allow_redirects=True)
            if r.status_code != 200:
                continue
            html = r.text
            # 2a. Direct Google Calendar ICS URL in page
            m = re.search(
                r"https://(?:www\.)?google\.com/calendar/ical/"
                r"[^\s\"'<>]+/public/basic\.ics",
                html,
            )
            if m:
                result = _try_ics(m.group(0))
                if result:
                    return result
            # 2b. Embedded Google Calendar — decode src= base64 to get calendar ID
            embed_ics = _ics_from_embed(html)
            if embed_ics:
                result = _try_ics(embed_ics)
                if result:
                    return result
            # 2c. Any .ics URL in the page (Tribe Events / Drupal / etc.)
            m = re.search(r'https?://[^\s"\'<>]+\.ics', html)
            if m:
                result = _try_ics(m.group(0))
                if result:
                    return result
        except requests.RequestException:
            continue
    return None


def main():
    kingdom_filters = sys.argv[1:] or [k.split("of ")[-1] for k in TARGETS]
    print(f"Filtering kingdoms by: {kingdom_filters}")

    with open(LOC_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    rows = [r for r in rows
            if r.get("website")
            and any(f.lower() in r["kingdom"].lower() for f in kingdom_filters)]
    print(f"Probing {len(rows)} barony websites...")

    # Load existing locals.csv to avoid duplicates
    existing_sources = set()
    if LOCALS_FILE.exists():
        with open(LOCALS_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing_sources.add(row.get("group", "").strip())

    new_calendars: list[dict] = []
    for i, r in enumerate(rows, start=1):
        group = r["group"]
        site = r["website"]
        if group in existing_sources:
            continue
        print(f"  [{i}/{len(rows)}] {group} ({site[:60]})")
        result = probe(site)
        if result is None:
            print(f"    -> no calendar feed found")
            continue
        ics_url, events = result
        print(f"    -> {events} events at {ics_url[:80]}")
        gtype = (group.split(" of ", 1)[0] if " of " in group
                 else group.split(" ", 1)[0]).strip().lower()
        new_calendars.append({"kingdom": r.get("kingdom", ""), "group": group, "type": gtype,
                              "calendar_id": ics_url, "website": site, "social": "",
                              "date_last_checked": date.today().isoformat()})

    if not new_calendars:
        print("\nNo new calendars discovered.")
        return

    # Append to locals.csv (write a header if the file is new)
    fieldnames = ["kingdom", "group", "type", "calendar_id", "website", "social", "date_last_checked"]
    write_header = not LOCALS_FILE.exists()
    with open(LOCALS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for entry in new_calendars:
            writer.writerow(entry)
    print(f"\nAppended {len(new_calendars)} new calendar feeds to {LOCALS_FILE.name}")


if __name__ == "__main__":
    main()
