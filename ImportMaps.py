"""
fetch_sca_events.py
-------------------
Fetches SCA events from public Google Calendar ICS feeds and saves them to a CSV file.

Calendar types:
  - KINGDOM:  Unique events (feasts, tournaments, wars). Fetched 2 years forward.
              Virtual events are included but flagged with is_virtual=True.
  - BARONIAL: Recurring local events (fighter practice, A&S nights etc). Fetched 2 months
              forward. Virtual events are skipped entirely.

Calendars are defined in calendars.csv (same directory as this script), with columns:
  id, source, type
To add a new calendar, just add a row to calendars.csv — no code changes needed.

Output columns:
  title, start, end, location, description, source, calendar_type, is_virtual

Requirements:
    pip install requests icalendar recurring-ical-events

Usage:
    python fetch_sca_events.py
"""

import csv
import re
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

import requests
from icalendar import Calendar
import recurring_ical_events

# Local module: turns non-ICS sources (HTML pages, REST APIs) into ICS text
# so the rest of this script can ingest them uniformly. Sources opt in via
# URL prefixes in calendars.csv (e.g. "simcal:https://example.com/calendar/").
from scrapers import maybe_scrape


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TODAY      = date.today()
FAR_END    = TODAY + timedelta(days=730)   # ~2 years  — Kingdom calendars
NEAR_END   = TODAY + timedelta(days=60)    # ~2 months — Baronial calendars

# Paths relative to this script's location
SCRIPT_DIR      = Path(__file__).parent
CALENDARS_FILE  = SCRIPT_DIR / "calendars.csv"
OUTPUT_FILE     = SCRIPT_DIR / "sca_events.csv"

# Keywords that indicate a virtual/online event (checked against location + description,
# case-insensitive substring match). Add more here as needed.
VIRTUAL_KEYWORDS = [
    "virtual",
    "online",
    "zoom",
    "discord",
    "remote",
    "livestream",
    "live stream",
]


# ---------------------------------------------------------------------------
# Load calendars from CSV
# ---------------------------------------------------------------------------

def load_calendars(filepath: Path) -> list[dict]:
    """Load calendar definitions from a CSV file with columns: id, source, type."""
    calendars = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cal_type = row["type"].strip().lower()
            if cal_type not in ("kingdom", "baronial"):
                print(f"  WARNING: Unknown calendar type '{cal_type}' for {row['source']} — skipping")
                continue
            calendars.append({
                "id":     row["id"].strip(),
                "source": row["source"].strip(),
                "type":   cal_type,
            })
    print(f"Loaded {len(calendars)} calendars from {filepath.name}")
    print(f"  {sum(1 for c in calendars if c['type'] == 'kingdom')} kingdom, "
          f"{sum(1 for c in calendars if c['type'] == 'baronial')} baronial\n")
    return calendars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ics_url(calendar_id: str) -> str:
    """
    Resolve a calendar id from calendars.csv to a fetchable ICS URL.

    Accepts three forms:
      - A full URL (http:// or https://) — used as-is. Useful for Tribe Events
        Calendar / WordPress sites that expose their feed at a custom path
        (e.g. gleannabhann.net/events/?ical=1).
      - A Google Calendar id ending in @group.calendar.google.com or
        @import.calendar.google.com — wrapped into the standard Google ICS URL.
      - A bare email-style id (used by a few kingdoms like Ealdormere) —
        also wrapped into the Google ICS URL since that's how Google addresses
        the calendar.

    Sites that use a scraper prefix (`simcal:`, `tribe-rest:`) bypass this
    function — see fetch_ics.
    """
    cid = calendar_id.strip()
    if cid.startswith("http://") or cid.startswith("https://"):
        return cid
    return f"https://calendar.google.com/calendar/ical/{cid}/public/basic.ics"


def fetch_ics(calendar: dict) -> Calendar | None:
    """Download and parse an ICS feed. Returns None on failure.

    If the calendar id has a scraper prefix (e.g. "simcal:" or "tribe-rest:"),
    runs the corresponding scraper from scrapers.py to synthesise ICS text from
    a non-ICS source. Otherwise fetches the ICS URL over HTTP.
    """
    # Scraper-prefixed sources go through scrapers.py exclusively. If a
    # scraper finds no events, treat that as a legitimate empty result rather
    # than falling through to the URL fetcher (which would try to fetch
    # "calendar.google.com/.../simcal:..." and 404).
    if calendar["id"].startswith(("simcal:", "tribe-rest:", "calontir-json:", "nuevent:", "mec-rest:")):
        scraped = maybe_scrape(calendar["id"], calendar["source"])
        if scraped is None:
            return None
        try:
            return Calendar.from_ical(scraped)
        except Exception as e:
            print(f"  WARNING: Could not parse scraped ICS for {calendar['source']}: {e}")
            return None

    url = make_ics_url(calendar["id"])
    try:
        # Some sites (Tribe Events Calendar in particular) require a real-ish
        # User-Agent before they'll serve the ICS feed.
        response = requests.get(
            url, timeout=30,
            headers={"User-Agent": "SCA Maps Project (calendar fetcher)"},
        )
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"  WARNING: Could not fetch {calendar['source']}: {e}")
        return None

    # A feed sometimes returns an HTML page instead of ICS — e.g. a Cloudflare
    # or WAF challenge served to a datacenter IP (this is what GitHub Actions
    # often gets). Parsing that HTML with Calendar.from_ical raised a ValueError
    # that ISN'T a RequestException, so it escaped the handler above and crashed
    # the entire fetch step, taking every other kingdom down with it. Detect a
    # non-ICS body and skip just this one feed.
    if b"BEGIN:VCALENDAR" not in response.content:
        ctype = response.headers.get("content-type", "?")
        snippet = response.text[:80].replace("\n", " ").replace("\r", " ").strip()
        print(f"  WARNING: {calendar['source']} returned non-ICS content "
              f"(content-type {ctype}) — skipping. First bytes: {snippet!r}")
        return None
    try:
        return Calendar.from_ical(response.content)
    except Exception as e:                       # noqa: BLE001 — never crash the whole fetch
        print(f"  WARNING: Could not parse ICS for {calendar['source']}: {e}")
        return None


def get_text(component, field: str, default="") -> str:
    """Safely extract a string field from a calendar component."""
    value = component.get(field, default)
    if value == default:
        return default
    if hasattr(value, "to_ical"):
        return value.to_ical().decode("utf-8", errors="replace")
    return str(value)


def get_datetime(component, field: str):
    """Safely extract and normalise a datetime/date value."""
    value = component.get(field)
    if value is None:
        return None
    dt = value.dt if hasattr(value, "dt") else value
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def clean_text(text: str) -> str:
    """
    Clean a text field extracted from an ICS feed:
      - Unescape ICS-escaped commas and semicolons  (\\, -> ,  and \\; -> ;)
      - Decode common HTML entities
      - Replace newlines and carriage returns with a space
      - Collapse multiple spaces into one
    """
    if not text:
        return text

    # Unescape ICS backslash-escaped characters
    text = text.replace("\\,", ",")
    text = text.replace("\\;", ";")
    text = text.replace("\\n", " ")
    text = text.replace("\\N", " ")

    # Decode common HTML entities
    html_entities = {
        "&amp;":   "&",
        "&lt;":    "<",
        "&gt;":    ">",
        "&quot;":  '"',
        "&#39;":   "'",
        "&#8217;": "\u2019",  # right single quotation mark
        "&#8216;": "\u2018",  # left single quotation mark
        "&#8211;": "\u2013",  # en dash
        "&#8212;": "\u2014",  # em dash
        "&#8220;": "\u201c",  # left double quotation mark
        "&#8221;": "\u201d",  # right double quotation mark
        "&nbsp;":  " ",
    }
    for entity, char in html_entities.items():
        text = text.replace(entity, char)

    # Replace actual newlines and carriage returns with a space
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text)

    return text.strip()


def is_virtual_event(location: str, description: str) -> bool:
    """Return True if the event appears to be virtual/online."""
    text = (location + " " + description).lower()
    return any(kw in text for kw in VIRTUAL_KEYWORDS)


# ---------------------------------------------------------------------------
# Main fetch logic
# ---------------------------------------------------------------------------

def fetch_all_events(calendars: list[dict]) -> list[dict]:
    all_events = []

    for calendar in calendars:
        is_baronial = calendar["type"] == "baronial"
        end_date    = NEAR_END if is_baronial else FAR_END

        print(f"Fetching [{calendar['type']:8s}] {calendar['source']}")

        cal = fetch_ics(calendar)
        if cal is None:
            continue

        try:
            components = recurring_ical_events.of(cal).between(TODAY, end_date)
        except Exception as e:
            print(f"  WARNING: Could not expand events for {calendar['source']}: {e}")
            continue

        count = 0
        skipped_virtual = 0

        for component in components:
            if component.name != "VEVENT":
                continue

            title    = clean_text(get_text(component, "SUMMARY"))
            location = clean_text(get_text(component, "LOCATION"))
            desc     = clean_text(get_text(component, "DESCRIPTION"))
            start    = get_datetime(component, "DTSTART")
            end_dt   = get_datetime(component, "DTEND")

            virtual = is_virtual_event(location, desc)

            # Baronial: skip virtual events and events with no location
            if is_baronial:
                if virtual:
                    skipped_virtual += 1
                    continue
                if not location:
                    continue

            all_events.append({
                "title":         title,
                "start":         start,
                "end":           end_dt,
                "location":      location,
                "description":   desc,
                "source":        calendar["source"],
                "calendar_type": calendar["type"],
                "is_virtual":    virtual,
            })
            count += 1

        skip_note = f" ({skipped_virtual} virtual skipped)" if skipped_virtual else ""
        print(f"  → {count} events collected{skip_note}")

    return all_events


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def save_to_csv(events: list[dict], filename: Path):
    if not events:
        print("No events to save.")
        return

    fieldnames = [
        "title", "start", "end", "location",
        "description", "source", "calendar_type", "is_virtual",
    ]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(events)

    total    = len(events)
    virtual  = sum(1 for e in events if e["is_virtual"])
    mappable = total - virtual
    print(f"\nSaved {total} events to '{filename.name}'")
    print(f"  {mappable} mappable (non-virtual), {virtual} virtual (Kingdom only)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Fetching SCA events from {TODAY} ...\n")
    print(f"  Kingdom calendars:  up to {FAR_END}")
    print(f"  Baronial calendars: up to {NEAR_END}\n")

    calendars = load_calendars(CALENDARS_FILE)
    events = fetch_all_events(calendars)
    print(f"\nTotal events collected: {len(events)}")
    save_to_csv(events, OUTPUT_FILE)