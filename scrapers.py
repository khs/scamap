"""
scrapers.py
-----------
Adapter layer that turns various non-ICS calendar sources into ICS text so the
rest of the pipeline (which already speaks ICS) can ingest them unchanged.

Each scraper takes a source URL and returns a complete RFC 5545 ICS string
(starting with "BEGIN:VCALENDAR"), or None on failure.

Supported sources, recognised by URL prefix in calendars.csv:

    simcal:<page-url>      — scrape a WordPress page that embeds the
                             "Simple Calendar - Google Calendar Plugin"
                             (e.g. Barony of Windmasters' Hill).

    tribe-rest:<site-url>  — query the Tribe Events Calendar REST API at
                             <site-url>/wp-json/tribe/events/v1/events
                             and synthesise ICS from the JSON.

URLs without one of these prefixes are returned to the caller unchanged
(handled by ImportMaps.fetch_ics directly — Google Calendar IDs / direct ICS).

This module is intentionally dependency-light: requests + beautifulsoup4
(already used elsewhere in the pipeline) plus stdlib datetime/uuid. No
headless browser, no JS execution.

--------------------------------------------------------------------
Baronies investigated but NOT added — what we found and why we skipped:

  Dun Carraig (https://duncarraig.atlantia.sca.org/calendar/)
    Uses the R34 "ICS Calendar" WordPress plugin. The plugin fetches its
    source ICS server-side via admin-ajax with a nonce; the source URL is
    stored only in the WP database, never sent to the client. No static
    HTML element or JS variable exposes it. Would require a headless
    browser or asking the barony directly for the ICS URL.

  Highland Foorde / Black Diamond / Caer Mear / Tir-y-Don
    Plain WordPress sites with no calendar plugin in use. Event info
    (when present) lives in unstructured blog posts. Each site would
    need a bespoke regex scraper that breaks the moment they redesign.
    Not worth the maintenance cost for a handful of events.

  Nottinghill Coill (event-calendar page)
    Embeds the Atlantia kingdom calendar itself, not a barony-specific
    calendar. Adding it would just duplicate kingdom events.

  Hidden Mountain & Raven's Cove
    Both use Tribe Events Calendar with proper REST APIs, BUT both
    currently return zero upcoming events. They're listed via the
    tribe-rest: prefix so they pick up automatically once events
    get posted upstream — no follow-up work needed.

  Stormwall & Aukesgate (Hawkwood's shires)
    Google Calendar IDs known, but the calendars have no events in the
    next 60 days. Already configured; we'll see events when they're
    scheduled upstream.
--------------------------------------------------------------------
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).parent


HTTP_HEADERS = {
    # Some sites (Tribe Events in particular) gate ICS responses on User-Agent
    "User-Agent": "Mozilla/5.0 (compatible; SCA Maps calendar fetcher)",
}
HTTP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# ICS emission helpers
# ---------------------------------------------------------------------------

def _ics_escape(text: str) -> str:
    """RFC 5545 §3.3.11 — backslash-escape special chars in TEXT values."""
    if not text:
        return ""
    return (text.replace("\\", "\\\\")
                .replace(";",  "\\;")
                .replace(",",  "\\,")
                .replace("\n", "\\n")
                .replace("\r", ""))


def _fold(line: str) -> str:
    """RFC 5545 §3.1 — content lines longer than 75 octets are folded with
    CRLF + a single space. We use simple ASCII fold-at-73 to stay safe."""
    if len(line) <= 73:
        return line
    out = [line[:73]]
    rest = line[73:]
    while rest:
        out.append(" " + rest[:72])
        rest = rest[72:]
    return "\r\n".join(out)


def _fmt_utc(dt: datetime) -> str:
    """Format a datetime as a UTC ICS DATE-TIME value."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _emit_calendar(name: str, events: list[dict]) -> str:
    """Build a complete VCALENDAR document from a list of event dicts.
    Each dict has keys: uid, start, end, summary, location, description, url."""
    now = _fmt_utc(datetime.now(timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SCA Maps Project//scrapers.py//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(name)}",
    ]
    for ev in events:
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}",
            f"DTSTAMP:{now}",
            f"DTSTART:{_fmt_utc(ev['start'])}",
            f"DTEND:{_fmt_utc(ev['end'])}",
            _fold(f"SUMMARY:{_ics_escape(ev['summary'])}"),
        ])
        if ev.get("location"):
            lines.append(_fold(f"LOCATION:{_ics_escape(ev['location'])}"))
        if ev.get("description"):
            lines.append(_fold(f"DESCRIPTION:{_ics_escape(ev['description'])}"))
        if ev.get("url"):
            lines.append(_fold(f"URL:{ev['url']}"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Simple Calendar (Google Calendar Plugin) — HTML scraper
# ---------------------------------------------------------------------------
#
# The Simple Calendar plugin (https://wordpress.org/plugins/google-calendar-events/)
# renders each event as a hidden `<div class="simcal-event-details simcal-tooltip-content">`
# tooltip with microdata. We pull title, start/end (as unix timestamps), the
# venue address (in `<meta itemprop="address">`), and the description (HTML).

def scrape_simple_calendar(page_url: str, name: str) -> Optional[str]:
    """Scrape a Simple Calendar plugin page and return an ICS string."""
    try:
        r = requests.get(page_url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  WARNING: SimCal scrape failed for {name}: {exc}")
        return None

    soup = BeautifulSoup(r.text, "lxml")
    tooltips = soup.select(".simcal-event-details.simcal-tooltip-content")

    events = []
    seen_uids = set()

    for tt in tooltips:
        title_el = tt.select_one(".simcal-event-title")
        start_el = tt.select_one(".simcal-event-start-date")
        end_el   = tt.select_one(".simcal-event-end-date") or start_el
        loc_meta = tt.select_one(".simcal-event-address meta[itemprop='address']")
        desc_el  = tt.select_one(".simcal-event-description")

        if not title_el or not start_el:
            continue

        try:
            start_ts = int(start_el["data-event-start"])
            end_ts   = int(end_el["data-event-start"]) if end_el is not start_el \
                       else start_ts + 3600
        except (KeyError, ValueError):
            continue

        title = title_el.get_text(strip=True)
        location = loc_meta["content"] if loc_meta else ""
        # Get description text including embedded URLs (as text, not HTML)
        description = desc_el.get_text(" ", strip=True) if desc_el else ""

        # Stable UID: title + start. SimCal repeats the same event tooltip
        # multiple times when an event spans multiple displayed weeks.
        uid_seed = f"{title}|{start_ts}"
        uid = f"simcal-{uuid.uuid5(uuid.NAMESPACE_URL, uid_seed)}@{page_url}"
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        events.append({
            "uid":         uid,
            "start":       datetime.fromtimestamp(start_ts, tz=timezone.utc),
            "end":         datetime.fromtimestamp(end_ts,   tz=timezone.utc),
            "summary":     title,
            "location":    location,
            "description": description,
        })

    if not events:
        return None
    return _emit_calendar(name, events)


# ---------------------------------------------------------------------------
# The Events Calendar (Tribe) — REST API
# ---------------------------------------------------------------------------

def scrape_tribe_rest(site_url: str, name: str) -> Optional[str]:
    """Pull events from a Tribe Events Calendar's WP REST API."""
    api = site_url.rstrip("/") + "/wp-json/tribe/events/v1/events"
    events = []
    page = 1
    while True:
        try:
            r = requests.get(
                api,
                params={"per_page": 50, "page": page},
                timeout=HTTP_TIMEOUT,
                headers=HTTP_HEADERS,
            )
            if r.status_code == 404:
                # Tribe REST returns 404 when paging past the end
                break
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as exc:
            print(f"  WARNING: Tribe REST failed for {name}: {exc}")
            break

        batch = data.get("events", []) if isinstance(data, dict) else []
        if not batch:
            break

        for e in batch:
            try:
                start = datetime.fromisoformat(e["start_date"])
                end   = datetime.fromisoformat(e["end_date"])
            except (KeyError, ValueError):
                continue

            venue = e.get("venue", {}) or {}
            addr_parts = [
                venue.get("venue", ""),
                venue.get("address", ""),
                venue.get("city", ""),
                venue.get("province", venue.get("state", "")),
                venue.get("zip", ""),
                venue.get("country", ""),
            ]
            location = ", ".join(p for p in addr_parts if p)

            events.append({
                "uid":         f"tribe-{e.get('id', uuid.uuid4())}@{site_url}",
                "start":       start,
                "end":         end,
                "summary":     e.get("title", "(untitled)"),
                "location":    location,
                "description": e.get("description", "") or "",
                "url":         e.get("url", ""),
            })

        # Stop if we got fewer than per_page — last page
        if len(batch) < 50:
            break
        page += 1

    if not events:
        return None
    return _emit_calendar(name, events)


# ---------------------------------------------------------------------------
# Modern Events Calendar (MEC) — WordPress plugin used by Middle Kingdom
# ---------------------------------------------------------------------------
# MEC exposes events via the standard WP REST API at /wp-json/wp/v2/mec-events.
# The listing endpoint gives us titles + per-event URLs but not the location;
# each event page has a Schema.org Event JSON-LD block with the full venue.
# We hit the list endpoint, then fetch each event page and parse the JSON-LD.

import json   # noqa: E402

_MEC_MONTH = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], start=1)}

def _parse_mec_date_range(text: str) -> tuple:
    """
    Parse MEC's "Dec 19 - 21 2026" / "May 16 2026" / "Dec 19 2026 - Jan 2 2027"
    style date ranges. Returns (start_datetime, end_datetime) or (None, None).
    """
    if not text:
        return (None, None)
    s = text.strip()
    # Single-day: "May 16 2026"
    m = re.match(r"^(\w{3,9})\s+(\d{1,2})(?:,?\s+)(\d{4})\s*$", s)
    if m:
        mon = _MEC_MONTH.get(m.group(1)[:3].lower())
        if mon:
            d = datetime(int(m.group(3)), mon, int(m.group(2)))
            return (d, d)
    # Same-month range: "Dec 19 - 21 2026" or "May 1 - 3 2026"
    m = re.match(r"^(\w{3,9})\s+(\d{1,2})\s*[-–—]\s*(\d{1,2})(?:,?\s+)(\d{4})\s*$", s)
    if m:
        mon = _MEC_MONTH.get(m.group(1)[:3].lower())
        if mon:
            start = datetime(int(m.group(4)), mon, int(m.group(2)))
            end   = datetime(int(m.group(4)), mon, int(m.group(3)))
            return (start, end)
    # Cross-month: "Dec 19 2026 - Jan 2 2027"
    m = re.match(
        r"^(\w{3,9})\s+(\d{1,2})(?:,?\s+)(\d{4})\s*[-–—]\s*(\w{3,9})\s+(\d{1,2})(?:,?\s+)(\d{4})\s*$",
        s,
    )
    if m:
        s_mon = _MEC_MONTH.get(m.group(1)[:3].lower())
        e_mon = _MEC_MONTH.get(m.group(4)[:3].lower())
        if s_mon and e_mon:
            start = datetime(int(m.group(3)), s_mon, int(m.group(2)))
            end   = datetime(int(m.group(6)), e_mon, int(m.group(5)))
            return (start, end)
    return (None, None)


def scrape_mec_rest(site_url: str, name: str) -> Optional[str]:
    """
    Scrape a Modern Events Calendar (MEC) WordPress site.

    Strategy:
      1. List events via /wp-json/wp/v2/mec-events?per_page=100 to get every
         event's title + URL (the REST API doesn't expose MEC's event dates).
      2. Fetch each event page; parse its `.mec-event-meta` block, which holds
         the date range ("Dec 19 - 21 2026"), the venue name (in a <dd class=
         "author fn org"> <h6>), and the street address (in <span class=
         "mec-address">).
    """
    base = site_url.rstrip("/")
    list_url = f"{base}/wp-json/wp/v2/mec-events"

    # Step 1: enumerate events
    items: list[dict] = []
    page = 1
    while True:
        try:
            r = requests.get(
                list_url,
                params={"per_page": 100, "page": page, "orderby": "date", "order": "asc"},
                timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS,
            )
            if r.status_code == 400:
                break
            r.raise_for_status()
            batch = r.json()
        except requests.RequestException as exc:
            print(f"  WARNING: MEC list fetch failed for {name} page {page}: {exc}")
            break
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    print(f"  MEC list returned {len(items)} events; fetching individual pages...")

    # Step 2: fetch each event's page and parse the meta block
    events = []
    for item in items:
        event_url = item.get("link", "")
        title = (item.get("title") or {}).get("rendered", "")
        if not event_url:
            continue
        try:
            ep = requests.get(event_url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
            ep.raise_for_status()
        except requests.RequestException as exc:
            print(f"  WARNING: MEC event page failed {event_url}: {exc}")
            continue

        soup = BeautifulSoup(ep.text, "lxml")
        meta = soup.select_one(".mec-event-meta")
        if not meta:
            continue

        # Date
        date_el = meta.select_one(".mec-start-date-label")
        if not date_el:
            continue
        start, end = _parse_mec_date_range(date_el.get_text(" ", strip=True))
        if start is None:
            continue

        # Location: venue name + address
        venue_el = meta.select_one(".mec-single-event-location dd.author h6")
        addr_el  = meta.select_one(".mec-single-event-location span.mec-address")
        loc_parts = []
        if venue_el:
            loc_parts.append(venue_el.get_text(" ", strip=True))
        if addr_el:
            loc_parts.append(addr_el.get_text(" ", strip=True))
        location = ", ".join(p for p in loc_parts if p)

        # External "More Info" link (often a host-group page or a Facebook event
        # with tracking params) -- kept as a fallback only. Prefer the canonical
        # midrealm.org/events/<slug>/ WordPress page, which is what users expect
        # and stays clean of fbclid/utm noise.
        more_info_a = meta.select_one(".mec-event-more-info a")
        external_url = more_info_a["href"] if more_info_a else ""

        # Real event description sits in .mec-single-event-description on the
        # same page we just fetched. Pull it as plain text so the map popup has
        # actual content instead of an empty string.
        desc_el = soup.select_one(".mec-single-event-description")
        description = ""
        if desc_el:
            description = re.sub(r"\s+", " ", desc_el.get_text(" ", strip=True)).strip()

        events.append({
            "uid":         f"mec-{item.get('id', uuid.uuid4())}@{site_url}",
            "start":       start,
            "end":         end,
            "summary":     re.sub(r"&#\d+;|<[^>]+>", "", title) or "(untitled)",
            "location":    location,
            "description": description,
            "url":         event_url or external_url,
        })

    if not events:
        return None
    return _emit_calendar(name, events)


# ---------------------------------------------------------------------------
# Calontir custom JSON endpoint
# ---------------------------------------------------------------------------
# The Kingdom of Calontir's calendar plugin (`calon`) exposes its event
# list as JSON at /wp-content/plugins/calon/calon_vload.php?didi=50. Each
# entry has separate street/city/state/zip fields, which is unusually clean
# for an SCA calendar source. Their Google Calendar feed, by contrast, is
# polluted with recurring "Deadline for articles for The Mews" entries.

def scrape_calontir_json(url: str, name: str) -> Optional[str]:
    """Fetch and convert Calontir's custom JSON event list to ICS."""
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        r.raise_for_status()
        records = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  WARNING: Calontir JSON fetch failed for {name}: {exc}")
        return None

    events = []
    for rec in records:
        try:
            start = datetime.fromisoformat(rec["start_date"])
            end   = datetime.fromisoformat(rec.get("end_date") or rec["start_date"])
        except (KeyError, ValueError):
            continue

        addr_parts = [
            rec.get("event_site_name", ""),
            rec.get("event_site_address", ""),
            rec.get("event_site_city", ""),
            rec.get("event_site_state", ""),
            rec.get("event_site_zip", ""),
        ]
        location = ", ".join(p for p in addr_parts if p)

        website = rec.get("primary_website", "") or ""
        if website and not website.startswith("http"):
            website = "https://" + website

        # Build a description that includes the host group and website
        desc_parts = []
        if rec.get("group_name"):
            desc_parts.append(f"Hosted by: {rec['group_name']}")
        if website:
            desc_parts.append(f"Website: {website}")

        # Canonical event URL is the kingdom-calendar's per-event page,
        # https://www.calontir.org/calendar/<event_id>/. The "primary_website"
        # field on each record points to the HOST GROUP (e.g. shirecai.
        # calontir.org for Feast of Eagles) -- useful, but not the link the
        # user expects when clicking the event. Fall back to it when there's
        # no event_id, and keep it in the description either way.
        event_id = rec.get("event_id")
        event_url = (f"https://www.calontir.org/calendar/{event_id}/"
                     if event_id else website)

        events.append({
            "uid":         f"calontir-{event_id or uuid.uuid4()}@calontir.org",
            "start":       start,
            "end":         end,
            "summary":     rec.get("event_name", "(untitled)"),
            "location":    location,
            "description": " | ".join(desc_parts),
            "url":         event_url,
        })

    if not events:
        return None
    return _emit_calendar(name, events)


# ---------------------------------------------------------------------------
# NuEvent (Meridies) — paginated events list scraper
# ---------------------------------------------------------------------------
# Meridies uses the "NuEvent" WordPress plugin. Their /events/?view=list page
# renders each event as an `<article class="event-list-item">` with structured
# metadata (title, host, date, location). The Google Calendar feed they also
# publish is missing locations on most events — this scraper recovers them.

def scrape_meridies_nuevent(base_url: str, name: str) -> Optional[str]:
    """Scrape Meridies' paginated /events/ list view."""
    base = base_url.rstrip("/")
    if not base.endswith("/events"):
        base += "/events"

    events = []
    seen_ids = set()
    for page in range(1, 30):  # generous upper bound; loop exits when no new IDs
        url = f"{base}/?view=list&cal_page={page}"
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"  WARNING: NuEvent fetch failed for {name} page {page}: {exc}")
            break

        soup = BeautifulSoup(r.text, "lxml")
        articles = soup.select("article.event-list-item")
        if not articles:
            break

        new_this_page = 0
        for art in articles:
            # ID is in the `id="event-1234"` attribute
            art_id = (art.get("id") or "").replace("event-", "")
            if not art_id or art_id in seen_ids:
                continue
            seen_ids.add(art_id)
            new_this_page += 1

            title_el = art.select_one(".event-title a")
            host_el  = art.select_one(".event-host")
            time_el  = art.select_one("time[datetime]")
            loc_el   = art.select_one(".meta-item.location")

            if not title_el or not time_el:
                continue

            try:
                start_date = datetime.fromisoformat(time_el["datetime"])
            except (KeyError, ValueError):
                continue

            # Strip the inline SVG icon out of the location element's text
            location_text = ""
            if loc_el:
                # Drop the screen-reader "Location:" prefix
                for sr in loc_el.select(".screen-reader-text"):
                    sr.decompose()
                location_text = loc_el.get_text(" ", strip=True)

            event_url = title_el.get("href", "") or ""
            description_parts = []
            if host_el:
                description_parts.append(host_el.get_text(" ", strip=True))

            events.append({
                "uid":         f"nuevent-{art_id}@{base_url}",
                "start":       start_date,
                "end":         start_date,
                "summary":     title_el.get_text(strip=True),
                "location":    location_text,
                "description": " | ".join(p for p in description_parts if p),
                "url":         event_url,
            })

        if not new_this_page:
            break

    if not events:
        return None
    return _emit_calendar(name, events)


# ---------------------------------------------------------------------------
# EventPrime (WordPress) — sitemap + per-event Google-Calendar-link scraper
# ---------------------------------------------------------------------------
# Some kingdoms (Avacal) run the EventPrime plugin. Its WordPress REST API is
# disabled and the calendar is AJAX/nonce-driven, but every event page renders
# an "Add to Google Calendar" link whose query string carries the whole event:
#     text=<title>&dates=<startZ>/<endZ>&location=<venue>&details=<html desc>
# We enumerate event URLs from the wp-sitemap (which includes <lastmod>) and
# parse that link on each page. Results are cached in eventprime_cache.json
# keyed by URL+lastmod, so after the first run only new or edited events are
# re-fetched — polite to the (volunteer-run) source site.

EVENTPRIME_CACHE = SCRIPT_DIR / "eventprime_cache.json"
EVENTPRIME_FETCH_DELAY = 1.0       # seconds between *new* page fetches
_GCAL_DATES_RE = re.compile(r"(\d{8}T\d{6}Z)/(\d{8}T\d{6}Z)")


def _eventprime_sitemap(base: str) -> dict:
    """Map every em_event URL to its <lastmod> from the site's wp-sitemap."""
    urls: dict[str, str] = {}
    try:
        idx = requests.get(f"{base}/wp-sitemap.xml", timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        idx.raise_for_status()
    except requests.RequestException as exc:
        print(f"  WARNING: EventPrime sitemap index failed for {base}: {exc}")
        return urls
    for sub in re.findall(r"<loc>([^<]*em_event[^<]*\.xml)</loc>", idx.text):
        try:
            r = requests.get(sub, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"  WARNING: EventPrime sub-sitemap failed {sub}: {exc}")
            continue
        for m in re.finditer(r"<url>\s*<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]*)</lastmod>)?", r.text):
            urls[m.group(1)] = m.group(2) or ""
    return urls


def _parse_eventprime_event(html: str) -> Optional[dict]:
    """Extract one event's fields from its page's Google-Calendar link.
    Returns dict(summary, start, end, location, description) with start/end as
    'YYYYMMDDTHHMMSSZ' strings, or None if the link/date is missing."""
    soup = BeautifulSoup(html, "lxml")
    link = soup.find("a", href=re.compile(r"google\.com/calendar/event"))
    if not link:
        return None
    q = parse_qs(urlparse(link["href"]).query)
    title = (q.get("text") or [""])[0].strip()
    m = _GCAL_DATES_RE.match((q.get("dates") or [""])[0])
    if not title or not m:
        return None
    details = (q.get("details") or [""])[0]
    description = ""
    if details:
        description = re.sub(r"\s+", " ", BeautifulSoup(details, "lxml").get_text(" ", strip=True)).strip()
    return {
        "summary":     title,
        "start":       m.group(1),
        "end":         m.group(2),
        "location":    (q.get("location") or [""])[0].strip(),
        "description": description,
    }


def scrape_eventprime(base_url: str, name: str) -> Optional[str]:
    """Scrape an EventPrime kingdom site (e.g. Avacal) into ICS text."""
    base = base_url.rstrip("/")
    host = urlparse(base).netloc
    sitemap = _eventprime_sitemap(base)
    if not sitemap:
        print(f"  WARNING: EventPrime found no event URLs for {name}")
        return None

    try:
        cache = json.loads(EVENTPRIME_CACHE.read_text(encoding="utf-8")) if EVENTPRIME_CACHE.exists() else {}
    except Exception:
        cache = {}
    site_cache = cache.setdefault(host, {})

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)   # keep just-finished events
    events, fetched = [], 0

    for url, lastmod in sitemap.items():
        hit = site_cache.get(url)
        if hit and hit.get("lastmod") == lastmod:
            data = hit.get("event")
        else:
            try:
                r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
                r.raise_for_status()
                data = _parse_eventprime_event(r.text)
            except requests.RequestException as exc:
                print(f"  WARNING: EventPrime page fetch failed {url}: {exc}")
                continue
            fetched += 1
            site_cache[url] = {"lastmod": lastmod, "event": data}
            time.sleep(EVENTPRIME_FETCH_DELAY)

        if not data:
            continue
        try:
            start = datetime.strptime(data["start"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            end   = datetime.strptime(data["end"],   "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if end < cutoff:                       # past event — don't emit
            continue
        # EventPrime encodes times in UTC; for SCA listings the date is what
        # matters, and a UTC time would render shifted in the viewer's zone, so
        # snap to date-only (the precise time is on the linked event page).
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end.replace(hour=0, minute=0, second=0, microsecond=0)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        events.append({
            "uid":         f"eventprime-{slug}@{host}",
            "start":       start,
            "end":         end,
            "summary":     data["summary"],
            "location":    data["location"],
            "description": data["description"],
            "url":         url,
        })

    # Drop cache entries for events removed upstream, then persist.
    for stale in set(site_cache) - set(sitemap):
        del site_cache[stale]
    try:
        EVENTPRIME_CACHE.write_text(
            json.dumps(cache, indent=0, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print(f"  WARNING: could not write {EVENTPRIME_CACHE.name}: {exc}")

    print(f"  EventPrime {name}: {len(events)} upcoming "
          f"({fetched} fetched this run, {len(sitemap)} in sitemap)")
    return _emit_calendar(name, events) if events else None


# ---------------------------------------------------------------------------
# Drachenwald — kingdom calendar JSON
# ---------------------------------------------------------------------------
# Drachenwald (Europe) runs a Jekyll static site whose calendar widget loads
# events from a clean JSON feed at dis.drachenwald.sca.org/data/calendar.json.
# We fetch that and synthesise ICS. Administrative pseudo-events (bid deadlines
# carry type="other" and 1899 placeholder dates) and cancelled events are
# dropped. Dates are emitted date-only: the feed's start-time is local to each
# event's country and would render shifted in a viewer's zone — the date is what
# the map needs (the precise time is on the linked event site).

def _drachenwald_events_from_records(records: list) -> list[dict]:
    """Pure JSON-records -> event-dicts transform (no network; unit-tested)."""
    events = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("type") != "event":                       # skip bid/admin items
            continue
        if str(rec.get("status", "")).strip().lower() == "cancelled":
            continue
        sd = (rec.get("start-date") or "").strip()
        ed = (rec.get("end-date") or "").strip() or sd
        try:
            start = datetime.strptime(sd, "%Y-%m-%d")
            end   = datetime.strptime(ed, "%Y-%m-%d")
        except ValueError:
            continue                                          # unparseable / 1899 placeholder

        # Location: venue address, then town, then country, de-duplicated.
        bits, seen = [], set()
        for part in (rec.get("site-address"), rec.get("town"), rec.get("country")):
            part = (part or "").strip().strip(",").strip()
            if part and part.lower() not in seen:
                seen.add(part.lower())
                bits.append(part)
        location = ", ".join(bits)
        vc = (rec.get("vc-url") or "").strip()
        if not location and vc:                               # online-only event
            location = vc

        website = (rec.get("website") or "").strip() or (rec.get("facebook") or "").strip()
        if website and not website.startswith("http"):
            website = "https://" + website

        desc_parts = []
        if rec.get("host-branch"):
            desc_parts.append(f"Hosted by: {rec['host-branch'].strip()}")
        if rec.get("summary"):
            desc_parts.append(rec["summary"].strip())
        if vc:
            desc_parts.append(f"Online: {vc}")

        slug = (rec.get("slug") or "").strip()
        events.append({
            "uid":         f"drachenwald-{slug or uuid.uuid4()}@drachenwald.sca.org",
            "start":       start,
            "end":         end,
            "summary":     (rec.get("event-name") or "(untitled)").strip(),
            "location":    location,
            "description": " | ".join(desc_parts),
            "url":         website,
        })
    return events


def scrape_drachenwald_json(url: str, name: str) -> Optional[str]:
    """Fetch Drachenwald's calendar JSON feed and convert it to ICS."""
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        r.raise_for_status()
        records = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  WARNING: Drachenwald JSON fetch failed for {name}: {exc}")
        return None
    events = _drachenwald_events_from_records(records)
    print(f"  Drachenwald {name}: {len(events)} events from {len(records)} records")
    return _emit_calendar(name, events) if events else None


# ---------------------------------------------------------------------------
# Dispatch — public entry point used by ImportMaps.fetch_ics
# ---------------------------------------------------------------------------

# Every recognised scraper prefix, in one place. ImportMaps.fetch_ics uses
# is_scraper_source() to decide a source must go through maybe_scrape (and never
# fall through to the URL / Google-Calendar fetcher, which would 404 on
# "calendar.google.com/.../eventprime:..."). Adding a new adapter? Add its
# prefix here and a branch in maybe_scrape — nothing in ImportMaps to touch.
SCRAPER_PREFIXES = (
    "simcal:", "tribe-rest:", "calontir-json:", "nuevent:", "mec-rest:",
    "eventprime:", "drachenwald-json:",
)


def is_scraper_source(calendar_id: str) -> bool:
    """True if this calendars.csv id must be handled by a scraper adapter."""
    return calendar_id.startswith(SCRAPER_PREFIXES)


def maybe_scrape(calendar_id: str, source_name: str) -> Optional[str]:
    """
    If `calendar_id` (the value from calendars.csv) carries a recognised
    scraper prefix, run the scraper and return ICS text. Otherwise return
    None and let the caller fetch the URL normally.

    Recognised prefixes:
        simcal:<page-url>
        tribe-rest:<site-url>
        calontir-json:<json-endpoint-url>
        nuevent:<base-site-url>
        mec-rest:<base-site-url>
        eventprime:<base-site-url>
        drachenwald-json:<json-feed-url>
    """
    if calendar_id.startswith("simcal:"):
        return scrape_simple_calendar(calendar_id[len("simcal:"):], source_name)
    if calendar_id.startswith("tribe-rest:"):
        return scrape_tribe_rest(calendar_id[len("tribe-rest:"):], source_name)
    if calendar_id.startswith("calontir-json:"):
        return scrape_calontir_json(calendar_id[len("calontir-json:"):], source_name)
    if calendar_id.startswith("nuevent:"):
        return scrape_meridies_nuevent(calendar_id[len("nuevent:"):], source_name)
    if calendar_id.startswith("mec-rest:"):
        return scrape_mec_rest(calendar_id[len("mec-rest:"):], source_name)
    if calendar_id.startswith("eventprime:"):
        return scrape_eventprime(calendar_id[len("eventprime:"):], source_name)
    if calendar_id.startswith("drachenwald-json:"):
        return scrape_drachenwald_json(calendar_id[len("drachenwald-json:"):], source_name)
    return None
