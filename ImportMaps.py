"""
fetch_sca_events.py
-------------------
Fetches SCA events from public Google Calendar ICS feeds and saves them to a CSV file.

Calendar types:
  - KINGDOM:  Unique events (feasts, tournaments, wars). Fetched 2 years forward.
              Virtual events are included but flagged with is_virtual=True.
  - BARONIAL: Recurring local events (fighter practice, A&S nights etc). Fetched 2 months
              forward. Virtual events are skipped entirely.

Calendars are defined in two CSVs (same directory as this script):
  - calendars.csv — kingdom feeds; columns: id, source, type
  - locals.csv    — local groups (baronies, shires, cantons, colleges, …) plus
                    maintainer reference info; columns: kingdom, group, type,
                    calendar_id, website, social, date_last_checked
To add a kingdom, add a row to calendars.csv; to add a local group, add a row
to locals.csv — no code changes needed.

Output columns:
  title, start, end, location, description, source, calendar_type, is_virtual

Requirements:
    pip install requests icalendar recurring-ical-events

Usage:
    python fetch_sca_events.py
"""

import csv
import json
import re
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

import requests
from icalendar import Calendar
import recurring_ical_events

# Local module: turns non-ICS sources (HTML pages, REST APIs) into ICS text
# so the rest of this script can ingest them uniformly. Sources opt in via
# URL prefixes in calendars.csv (e.g. "simcal:https://example.com/calendar/").
from scrapers import maybe_scrape, is_scraper_source


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TODAY      = date.today()
# Kingdom calendars fetch ~13 months forward (1 year + 1 month) so the date filter
# has data when a user expands it out to the next year, without pulling the sparse
# far-future tail (the old 2-year window) that made the late months look dead.
# Baronial calendars stay at ~2 months: they are dominated by weekly recurring
# practices, which merge to their nearest occurrence and so show regardless of how
# far the window reaches — widening it only multiplies recurrence expansion.
FAR_END    = TODAY + timedelta(days=395)   # ~13 months — Kingdom calendars
NEAR_END   = TODAY + timedelta(days=60)    # ~2 months  — Baronial calendars

# Paths relative to this script's location
SCRIPT_DIR      = Path(__file__).parent
CALENDARS_FILE  = SCRIPT_DIR / "calendars.csv"
LOCALS_FILE     = SCRIPT_DIR / "locals.csv"
OUTPUT_FILE     = SCRIPT_DIR / "sca_events.csv"
# Run-local (gitignored) handoff: which sources failed to fetch THIS run, so
# clean_sca_events can carry forward their last-good events instead of letting
# a transient outage / WAF block wipe a kingdom off the map.
FETCH_FAILURES_FILE = SCRIPT_DIR / "fetch_failures.json"

# Keywords that indicate a virtual/online event (checked against location + description,
# case-insensitive substring match). Add more here as needed.
VIRTUAL_KEYWORDS = [
    "virtual",
    "online",
    "zoom",
]
# "discord", "remote", "livestream", "live stream" deliberately NOT here -- they
# are weak signals dominated by false positives. SCA groups routinely run their
# in-person event coordination over Discord ("posted to our Discord event page"),
# describe campsites as "remote", and offer optional livestreams of in-person
# tournaments. Highlands War (Atenveldt, in-person camping at Camp Raymond) was
# flagged virtual purely because its description mentioned "Discord event pages".


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


def load_local_calendars(filepath: Path) -> list[dict]:
    """Load local-group feeds from locals.csv — the single registry of local SCA
    groups (baronies, shires, cantons, colleges, …). Columns: kingdom, group,
    type, calendar_id, website, social, date_last_checked. Most rows are
    registry-only: a group we know exists but that has no feed yet carries
    calendar_id "No Calendar Listed" and is skipped for import. Rows with a real
    calendar_id become {id, source, type='baronial'} dicts — the same shape as
    load_calendars (all local groups fetch on the baronial cadence), so the two
    lists concatenate."""
    NO_CALENDAR = {"", "no calendar listed", "no calendar available",
                   "none", "n/a", "tbd", "-", "practices added manually"}
    if not filepath.exists():
        print(f"  (no {filepath.name} — skipping local-group feeds)\n")
        return []
    calendars = []
    with open(filepath, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cal_id = (row.get("calendar_id") or "").strip()
            source = (row.get("group") or "").strip()
            if not source or cal_id.lower() in NO_CALENDAR:
                continue
            calendars.append({"id": cal_id, "source": source, "type": "baronial"})
    print(f"Loaded {len(calendars)} local-group calendars from {filepath.name}\n")
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
    # Local committed ICS file, e.g. "file:antir.ics" — for a kingdom whose feed
    # can't be fetched from CI (An Tir, behind Cloudflare). A human downloads the
    # .ics from a browser and commits it; every run ingests it like any other
    # feed, no network needed. A missing or eventless file is a quiet skip, not a
    # failure. See MAINTAINING.md → "Updating An Tir".
    if calendar["id"].startswith("file:"):
        path = SCRIPT_DIR / calendar["id"][len("file:"):]
        if not path.exists():
            print(f"  NOTE: {calendar['source']}: {path.name} not present yet — skipping")
            return None
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  WARNING: could not read {path.name} for {calendar['source']}: {e}")
            return None
        if "BEGIN:VCALENDAR" not in data:
            print(f"  NOTE: {calendar['source']}: {path.name} has no events yet — skipping")
            return None
        try:
            return Calendar.from_ical(data)
        except Exception as e:                       # noqa: BLE001
            print(f"  WARNING: could not parse {path.name} for {calendar['source']}: {e}")
            return None

    # Scraper-prefixed sources go through scrapers.py exclusively. If a
    # scraper finds no events, treat that as a legitimate empty result rather
    # than falling through to the URL fetcher (which would try to fetch
    # "calendar.google.com/.../simcal:..." and 404).
    if is_scraper_source(calendar["id"]):
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


def is_virtual_event(title: str, location: str, description: str) -> bool:
    """Return True if the event appears to be virtual/online.

    An explicit signal in the TITLE ("… (Virtual)") or the LOCATION ("Zoom", a
    meeting URL) marks it virtual. A real physical location means in-person —
    even if the free-text description happens to mention an online option (e.g.
    a site event whose notes say "register online" or "no virtual attendance").
    That description false-positive is what put an in-person event like
    "Summer's End" (a real NY address) into the virtual list. Only when there's
    no location at all do we fall back to scanning the description.
    """
    if any(kw in (title or "").lower() for kw in VIRTUAL_KEYWORDS):
        return True
    if any(kw in (location or "").lower() for kw in VIRTUAL_KEYWORDS):
        return True
    if not (location or "").strip():
        return any(kw in (description or "").lower() for kw in VIRTUAL_KEYWORDS)
    return False


# ---------------------------------------------------------------------------
# Main fetch logic
# ---------------------------------------------------------------------------

def expand_events(cal, start, end, source: str):
    """Expand a feed's recurring events, isolating any that fail to parse.

    The fast path is a single recurring_ical_events.between() over the whole
    feed. But ONE malformed event — e.g. Northshield's "The Catfish Ball" with
    a 0026-for-2026 year typo — makes that whole call raise, which used to drop
    the ENTIRE kingdom's events silently. Most kingdoms (and nearly every
    barony) are plain ICS feeds that never pass through scrapers.py's date
    guards, so this is the only chokepoint that protects all of them.

    On a bulk failure we fall back to expanding one VEVENT at a time so a
    single bad event can take down only itself. We carry the feed's VTIMEZONE
    definitions into each throwaway calendar so TZID references still resolve.
    """
    try:
        return recurring_ical_events.of(cal).between(start, end)
    except Exception as e:
        print(f"  WARNING: bulk expand failed for {source}: {e}"
              f" — falling back to per-event expansion")

    timezones = [c for c in cal.subcomponents if c.name == "VTIMEZONE"]
    good, bad_summaries = [], []
    for vevent in cal.walk("VEVENT"):
        mini = Calendar()
        for tz in timezones:
            mini.add_component(tz)
        mini.add_component(vevent)
        try:
            good.extend(recurring_ical_events.of(mini).between(start, end))
        except Exception:
            try:
                bad_summaries.append(str(vevent.get("SUMMARY", "?")))
            except Exception:
                bad_summaries.append("?")
    if bad_summaries:
        sample = ", ".join(f"'{s}'" for s in bad_summaries[:3])
        print(f"  NOTE: {source}: skipped {len(bad_summaries)} unparseable "
              f"event(s) ({sample}); kept {len(good)}")
    return good


def fetch_all_events(calendars: list[dict]) -> list[dict]:
    all_events = []
    failed_sources = []

    for calendar in calendars:
        is_baronial = calendar["type"] == "baronial"
        end_date    = NEAR_END if is_baronial else FAR_END

        print(f"Fetching [{calendar['type']:8s}] {calendar['source']}")

        cal = fetch_ics(calendar)
        if cal is None:
            # A None here means the fetch itself failed (network refused,
            # timeout, 403, non-ICS body) — NOT that the source is legitimately
            # empty. Record it so clean_sca_events keeps the last-good events.
            failed_sources.append(calendar["source"])
            continue

        components = expand_events(cal, TODAY, end_date, calendar["source"])

        count = 0
        skipped_virtual = 0

        for component in components:
            if component.name != "VEVENT":
                continue

            title    = clean_text(get_text(component, "SUMMARY"))
            location = clean_text(get_text(component, "LOCATION"))
            desc     = clean_text(get_text(component, "DESCRIPTION"))
            url_raw  = clean_text(get_text(component, "URL"))
            start    = get_datetime(component, "DTSTART")
            end_dt   = get_datetime(component, "DTEND")

            virtual = is_virtual_event(title, location, desc)

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
                # ICS VEVENT URL — the per-event page (e.g. midrealm.org/events/
                # smurf-shoot-4/) that the scrapers in scrapers.py put in URL:.
                # clean_sca_events.py prefers this over a URL scraped from the
                # description, so events link to their own page rather than the
                # kingdom calendar.
                "event_url":     url_raw,
            })
            count += 1

        skip_note = f" ({skipped_virtual} virtual skipped)" if skipped_virtual else ""
        print(f"  → {count} events collected{skip_note}")

    # Hand the failed-source list to clean_sca_events (always rewrite it, even
    # when empty, so a stale file from a previous run can't carry events forward
    # for a source that's actually fine now).
    try:
        FETCH_FAILURES_FILE.write_text(
            json.dumps(sorted(set(failed_sources))), encoding="utf-8")
    except OSError as e:
        print(f"  WARNING: could not write {FETCH_FAILURES_FILE.name}: {e}")
    if failed_sources:
        print(f"\n{len(failed_sources)} source(s) failed to fetch this run "
              f"(last-good events will be carried forward): "
              f"{', '.join(sorted(set(failed_sources)))}")

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
        "description", "source", "calendar_type", "is_virtual", "event_url",
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

    calendars = load_calendars(CALENDARS_FILE) + load_local_calendars(LOCALS_FILE)
    events = fetch_all_events(calendars)
    print(f"\nTotal events collected: {len(events)}")
    save_to_csv(events, OUTPUT_FILE)