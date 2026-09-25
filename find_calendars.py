"""
find_calendars.py
-----------------
Find an importable calendar feed behind a group's website, verify it through
the real import code, and (optionally) fill it into locals.csv.

Groups often publish a calendar we could import but don't advertise the feed:
a WordPress plugin page, an embedded Google Calendar, an Outlook calendar.
This recognises the common platforms from the page itself:

  * Google Calendar  - embed iframes (base64 src=), .../calendar/ical/<id>/...
                       links, "add to Google" cid= links, and the eid= event
                       links Simple Calendar prints (each encodes its calendar)
  * My Calendar      - WordPress plugin               -> mycal:<page>
  * The Events Calendar (Tribe)                       -> tribe-rest:<site>
  * Modern Events Calendar                            -> mec-rest:<site>
  * EventPrime                                        -> eventprime:<site>
  * Event Organiser / Events Manager                  -> their built-in .ics feeds
  * Outlook "published calendar" pages                -> the .ics twin
  * any other .ics / webcal:// link on the page

Each candidate is fetched with ImportMaps' own fetch + recurrence expansion,
then classified:

  own        public, has upcoming events, not already used by another feed,
             and not mostly the kingdom's own events            -> importable
  empty      public and parses, but nothing upcoming in the window (still
             importable: events appear once the group posts them)
  no-location  has events, but none carry a location, and the importer
             drops local-group events without one
  kingdom    most of its events are also on the kingdom calendar (a site
             embedding the kingdom calendar) -> would only duplicate
  duplicate  already configured for another group/kingdom, or mostly the same
             events (same day, title AND venue) as a feed already on the map
  ambiguous  the site lists several Google calendars (a directory page), so
             which is this group's is left to a human
  holiday    a public-holiday calendar
  failed     not public / not a calendar / fetch error

Usage (from the project folder):
    python find_calendars.py <website-or-calendar-page-url> [...]
    python find_calendars.py --sweep [kingdom-substring ...]   # locals.csv groups
                                                               # with no calendar
    python find_calendars.py --sweep --apply                   # also write "own"
                                                               # results to locals.csv

--sweep writes calendar_candidates.csv (gitignored) with every candidate and
its verdict, for review. --apply only touches rows whose calendar_id is still
"No Calendar Listed" / "No Calendar Available".
"""
from __future__ import annotations

import base64
import csv
import io
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

import ImportMaps

SCRIPT_DIR = Path(__file__).parent
LOCALS_FILE = SCRIPT_DIR / "locals.csv"
CALENDARS_FILE = SCRIPT_DIR / "calendars.csv"
EVENTS_FILE = SCRIPT_DIR / "sca_events_clean.csv"
REPORT_FILE = SCRIPT_DIR / "calendar_candidates.csv"
NO_CALENDAR = {"no calendar listed", "no calendar available"}

UA = {"User-Agent": "Mozilla/5.0 (SCAMap event aggregator; +https://github.com/khs/scamap)"}
TIMEOUT = 15
PAGE_DELAY_S = 0.5           # between page fetches on one site
MAX_SUBPAGES = 3             # calendar/events pages followed from the homepage
KINGDOM_OVERLAP = 0.5        # share of events also on the kingdom calendar

PLUGIN_RE = re.compile(r"wp-content/plugins/([a-z0-9_\-]+)", re.I)
API_ROOT_RE = re.compile(r'<link[^>]+rel="https://api\.w\.org/"[^>]+href="([^"]+)"')
LINK_RE = re.compile(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', re.I | re.S)
EMBED_SRC_RE = re.compile(r"calendar(?:\.google\.com)?/(?:calendar/)?embed[^\"'<>]*?[?&]src=([^&\"'<>]+)", re.I)
ICAL_ID_RE = re.compile(r"calendar/ical/([^/\"'\s<>]+)/public/basic\.ics", re.I)
EID_RE = re.compile(r"[?&]eid=([A-Za-z0-9_\-]+)")
CID_RE = re.compile(r"[?&]cid=([A-Za-z0-9_\-=%.@]+)")
ICS_LINK_RE = re.compile(r"""["']((?:https?|webcal)://[^"'<>\s]+?\.ics(?:\?[^"'<>\s]*)?)["']""", re.I)
OUTLOOK_RE = re.compile(r"https://outlook\.(?:office365|live)\.com/owa/calendar/[^\"'<>\s]+?/calendar\.(?:html|ics)", re.I)

# Google shortens the calendar's domain inside eid= event links.
_EID_DOMAINS = {"@g": "@group.calendar.google.com", "@m": "@gmail.com",
                "@i": "@import.calendar.google.com", "@v": "@group.v.calendar.google.com"}


@dataclass
class Candidate:
    calendar_id: str          # what would go in locals.csv
    how: str                  # how it was found
    verdict: str = ""
    upcoming: int = 0
    with_location: int = 0
    sample: list = field(default_factory=list)
    note: str = ""
    events: list = field(default_factory=list, repr=False)   # (day, title, location)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def google_ics_url(cal_id: str) -> str:
    return f"https://calendar.google.com/calendar/ical/{quote(cal_id, safe='')}/public/basic.ics"


def _b64_calendar_id(raw: str) -> str | None:
    s = unescape(unquote(raw)).strip()
    if "@" in s:
        return s
    try:
        dec = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return None
    return dec if "@" in dec and " " not in dec else None


def _eid_calendar_id(eid: str) -> str | None:
    """A Google event link's eid is base64("<event id> <calendar id>")."""
    try:
        dec = base64.urlsafe_b64decode(eid + "=" * (-len(eid) % 4)).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return None
    parts = dec.split(" ")
    if len(parts) < 2 or "@" not in parts[1]:
        return None
    cid = parts[1]
    for short, full in _EID_DOMAINS.items():
        if cid.endswith(short):
            return cid[: -len(short)] + full
    return cid


def google_ids_in(html: str) -> list[str]:
    h = html.replace("&#038;", "&").replace("&amp;", "&")
    found = []
    for raw in EMBED_SRC_RE.findall(h) + CID_RE.findall(h):
        cid = _b64_calendar_id(raw)
        if cid:
            found.append(cid)
    found += [unquote(m) for m in ICAL_ID_RE.findall(h)]
    found += [c for c in (_eid_calendar_id(e) for e in EID_RE.findall(h)) if c]
    return list(dict.fromkeys(found))          # de-dupe, keep order


def _get(url: str):
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    return r if r.status_code == 200 else None


def _site_root(page_url: str, html: str) -> str:
    m = API_ROOT_RE.search(html)
    if m:
        return m.group(1).split("/wp-json")[0].rstrip("/")
    p = urlparse(page_url)
    return f"{p.scheme}://{p.netloc}"


def candidates_from_page(url: str, html: str) -> list[Candidate]:
    out: list[Candidate] = []
    plugins = {p.lower() for p in PLUGIN_RE.findall(html)}
    root = _site_root(url, html)
    for cid in google_ids_in(html):
        out.append(Candidate(google_ics_url(cid), f"Google Calendar id on {url}"))
    if "my-calendar" in plugins and re.search(r'class="[^"]*my-calendar-(?:table|month|list)', html):
        out.append(Candidate(f"mycal:{url}", "My Calendar plugin"))
    if plugins & {"the-events-calendar", "events-calendar-pro"}:
        out.append(Candidate(f"tribe-rest:{root}", "The Events Calendar plugin"))
    if plugins & {"modern-events-calendar", "modern-events-calendar-lite", "mec"}:
        out.append(Candidate(f"mec-rest:{root}", "Modern Events Calendar plugin"))
    if any(p.startswith("eventprime") for p in plugins):
        out.append(Candidate(f"eventprime:{root}", "EventPrime plugin"))
    if "event-organiser" in plugins:
        out.append(Candidate(f"{root}/feed/eo-events/", "Event Organiser plugin feed"))
    if "events-manager" in plugins:
        out.append(Candidate(f"{root}/events.ics", "Events Manager plugin feed"))
    for m in OUTLOOK_RE.findall(html):
        out.append(Candidate(re.sub(r"calendar\.html$", "calendar.ics", m), "Outlook published calendar"))
    for m in ICS_LINK_RE.findall(html.replace("&#038;", "&").replace("&amp;", "&")):
        out.append(Candidate(re.sub(r"^webcal://", "https://", m), "iCal link on page"))
    if "google-calendar-events" in plugins and not google_ids_in(html) and "simcal-" in html:
        out.append(Candidate(f"simcal:{url}", "Simple Calendar page (calendar id not exposed)"))
    return out


def discover(site: str) -> tuple[list[Candidate], str]:
    """Candidates from the homepage plus up to MAX_SUBPAGES same-site pages
    whose link text/URL looks like a calendar. Returns (candidates, error)."""
    r = _get(site)
    if r is None:
        return [], "site unreachable"
    pages = [(r.url, r.text)]
    host = urlparse(r.url).netloc
    subs = []
    for href, text in LINK_RE.findall(r.text):
        full = urljoin(r.url, unescape(href))
        label = re.sub(r"<[^>]+>", " ", text).lower() + " " + full.lower()
        if (urlparse(full).netloc == host and full.rstrip("/") != r.url.rstrip("/")
                and re.search(r"calendar|events?\b|practice|schedule|activities", label)
                and full not in subs):
            subs.append(full)
    for url in subs[:MAX_SUBPAGES]:
        time.sleep(PAGE_DELAY_S)
        r2 = _get(url)
        if r2 is not None:
            pages.append((r2.url, r2.text))
    seen, out = set(), []
    for url, html in pages:
        for c in candidates_from_page(url, html):
            key = c.calendar_id.lower().rstrip("/")
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out, ""


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()


def _configured_ids() -> dict:
    """Every calendar id already in use -> the group/kingdom using it."""
    used = {}
    for path, id_col, name_col in ((CALENDARS_FILE, "id", "source"),
                                   (LOCALS_FILE, "calendar_id", "group")):
        if path.exists():
            for r in csv.DictReader(open(path, encoding="utf-8", newline="")):
                cid = (r.get(id_col) or "").strip()
                if cid and cid.lower() not in NO_CALENDAR:
                    used[_id_key(cid)] = (r.get(name_col) or "").strip()
    return used


def _id_key(cid: str) -> str:
    """Comparable form of a calendar id: a Google calendar id however written
    (bare, %40-quoted, in an ical URL), or the lower-cased URL."""
    s = unquote(cid).strip().lower()
    m = re.search(r"calendar/ical/([^/]+)/", s)
    if m:
        return m.group(1)
    # "tribe-rest:http://www.x.org/" == "tribe-rest:https://x.org"
    return re.sub(r"^([a-z-]+:)?https?://(?:www\.)?", r"\1", s).rstrip("/")


def _known_events() -> dict:
    """(date, normalised title) -> [(source, venue)] for every event already on
    the map (the current sca_events_clean.csv)."""
    keys: dict = {}
    if EVENTS_FILE.exists():
        for r in csv.DictReader(open(EVENTS_FILE, encoding="utf-8", newline="")):
            keys.setdefault((r["start"][:10], _norm(r["title"])), []).append(
                (r["source"], r.get("clean_location") or r.get("location") or ""))
    return keys


def _same_event_elsewhere(known: dict, day: str, title: str, loc: str) -> set:
    """Sources already listing this event: same day, same title AND a
    compatible venue. The venue matters: two groups' "Archery Practice" on the
    same Sunday in Fort Worth and in Austin are different events."""
    import clean_sca_events as clean
    venue = clean.clean_location(loc)[0] if loc else ""
    return {src for src, other in known.get((day, _norm(title)), ())
            if clean._same_venue(venue, other)}


def remember(known: dict, c: Candidate, source: str) -> None:
    """Treat a chosen calendar's events as on the map, so a later group in the
    same sweep can't claim the same events."""
    for day, title, loc in c.events:
        known.setdefault((day, _norm(title)), []).append((source, loc))


def verify(c: Candidate, name: str, used: dict, known: dict) -> Candidate:
    key = _id_key(c.calendar_id)
    if "#holiday@" in key or key.endswith("holiday@group.v.calendar.google.com"):
        c.verdict, c.note = "holiday", "public-holiday calendar"
        return c
    if key in used:
        c.verdict, c.note = "duplicate", f"already used by {used[key]}"
        return c
    cal = ImportMaps.fetch_ics({"id": c.calendar_id, "source": name, "type": "baronial"})
    if cal is None:
        c.verdict, c.note = "failed", "not public, not a calendar, or fetch failed"
        return c
    today = date.today()
    comps = [e for e in ImportMaps.expand_events(cal, today, ImportMaps.FAR_END, name)
             if e.name == "VEVENT"]
    overlap: dict = {}          # existing source -> how many of these events it already has
    compared = 0                # events inside the window the map currently holds
    horizon = max((k[0] for k in known), default="")
    for e in comps:
        title = ImportMaps.clean_text(ImportMaps.get_text(e, "SUMMARY"))
        loc = ImportMaps.clean_text(ImportMaps.get_text(e, "LOCATION"))
        start = ImportMaps.get_datetime(e, "DTSTART")
        c.upcoming += 1
        c.with_location += bool(loc)
        day = str(start)[:10]
        c.events.append((day, title, loc))
        if day <= horizon:
            compared += 1
            for src in _same_event_elsewhere(known, day, title, loc):
                overlap[src] = overlap.get(src, 0) + 1
        if len(c.sample) < 3:
            c.sample.append(f"{day} {title[:40]}")
    top_src, top_n = max(overlap.items(), key=lambda kv: kv[1], default=("", 0))
    if not c.upcoming:
        c.verdict, c.note = "empty", "public, but nothing upcoming yet"
    elif compared and top_n / compared >= KINGDOM_OVERLAP:
        # Mostly events another feed already brings in: a site embedding the
        # kingdom calendar, or a neighbouring group's.
        c.verdict = "kingdom" if top_src.startswith("Kingdom of") else "duplicate"
        c.note = f"{top_n}/{compared} of its events are already on the map from {top_src}"
    elif not c.with_location:
        # The importer drops local-group events with no location at all.
        c.verdict, c.note = "no-location", "events carry no location, so none would show"
    else:
        c.verdict = "own"
    return c


def check_site(site: str, name: str, used: dict | None = None,
               known: dict | None = None) -> tuple[list[Candidate], str]:
    used = _configured_ids() if used is None else used
    known = _known_events() if known is None else known
    cands, err = discover(site)
    cands = [verify(c, name, used, known) for c in cands]
    mark_directory_pages(cands)
    return cands, err


def mark_directory_pages(cands: list[Candidate]) -> None:
    """A page exposing several Google calendars is usually a directory of
    other groups' calendars (a kingdom "find a group" page), not this group's
    own: don't pick one of them automatically."""
    google = [c for c in cands if "calendar.google.com" in c.calendar_id]
    if len(google) > 2:
        for c in google:
            if c.verdict == "own":
                c.verdict = "ambiguous"
                c.note = (f"the site lists {len(google)} Google calendars (a directory "
                          f"page?), so pick this group's one by hand")


def best(cands: list[Candidate]) -> Candidate | None:
    """The one to import: an "own" calendar with the most upcoming events."""
    own = [c for c in cands if c.verdict == "own"]
    return max(own, key=lambda c: (c.upcoming, c.with_location), default=None)


# ---------------------------------------------------------------------------
# locals.csv update
# ---------------------------------------------------------------------------

def apply_to_locals(chosen: dict[str, str]) -> int:
    """Set calendar_id (+ date_last_checked) for the given groups, only where
    the row still says it has no calendar. Rewrites just those lines, so the
    rest of the file is byte-for-byte unchanged."""
    raw = LOCALS_FILE.read_bytes()
    text = raw.decode("utf-8-sig")
    bom = raw.startswith(b"\xef\xbb\xbf")
    lines = text.splitlines(keepends=True)
    header = next(csv.reader([lines[0]]))
    gi, ci, di = header.index("group"), header.index("calendar_id"), header.index("date_last_checked")
    changed = 0
    for n, line in enumerate(lines[1:], start=1):
        fields = next(csv.reader([line]), None)
        if not fields or len(fields) <= max(gi, ci, di):
            continue
        group = fields[gi].strip()
        if group in chosen and fields[ci].strip().lower() in NO_CALENDAR:
            fields[ci] = chosen[group]
            fields[di] = date.today().isoformat()
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow(fields)
            ending = line[len(line.rstrip("\r\n")):]
            lines[n] = buf.getvalue() + ending
            changed += 1
    out = "".join(lines)
    LOCALS_FILE.write_bytes((b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8"))
    return changed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(name: str, site: str, cands: list[Candidate], err: str) -> None:
    print(f"\n{name}  ({site})")
    if err:
        print(f"   {err}")
    if not cands and not err:
        print("   no calendar platform or feed found on the site")
    for c in cands:
        extra = f"{c.upcoming} upcoming, {c.with_location} with a location" if c.upcoming else ""
        print(f"   [{c.verdict:9s}] {c.calendar_id}")
        print(f"               via {c.how}; {c.note or extra}"
              + (f"; e.g. {c.sample}" if c.sample else ""))
    b = best(cands)
    if b:
        print(f"   => put in locals.csv calendar_id: {b.calendar_id}")


def sweep(filters: list[str], apply: bool) -> None:
    rows = [r for r in csv.DictReader(open(LOCALS_FILE, encoding="utf-8", newline=""))
            if (r.get("calendar_id") or "").strip().lower() in NO_CALENDAR
            and (r.get("website") or "").lower().startswith("http")
            and (not filters or any(f.lower() in r["kingdom"].lower() for f in filters))]
    used, known = _configured_ids(), _known_events()
    report, chosen = [], {}
    print(f"Checking {len(rows)} group website(s) with no calendar ...")
    for r in rows:
        cands, err = check_site(r["website"].strip(), r["group"], used, known)
        _print(r["group"], r["website"], cands, err)
        for c in cands:
            report.append({"kingdom": r["kingdom"], "group": r["group"], "website": r["website"],
                           "verdict": c.verdict, "calendar_id": c.calendar_id, "found_via": c.how,
                           "upcoming": c.upcoming, "with_location": c.with_location,
                           "note": c.note, "sample": " | ".join(c.sample)})
        b = best(cands)
        if b:
            chosen[r["group"]] = b.calendar_id
            used[_id_key(b.calendar_id)] = r["group"]   # two groups can't claim one calendar
            remember(known, b, r["group"])               # ...nor the same events
    with open(REPORT_FILE, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["kingdom", "group", "website", "verdict", "calendar_id",
                                          "found_via", "upcoming", "with_location", "note", "sample"])
        w.writeheader()
        w.writerows(report)
    print(f"\n{len(chosen)} group(s) have an importable calendar of their own; "
          f"full results in {REPORT_FILE.name}.")
    if apply and chosen:
        n = apply_to_locals(chosen)
        print(f"Wrote {n} calendar_id(s) into {LOCALS_FILE.name}.")


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    if argv[0] == "--sweep":
        rest = [a for a in argv[1:] if a != "--apply"]
        sweep(rest, apply="--apply" in argv)
        return
    for site in argv:
        cands, err = check_site(site, urlparse(site).netloc)
        _print(urlparse(site).netloc, site, cands, err)


if __name__ == "__main__":
    main(sys.argv[1:])
