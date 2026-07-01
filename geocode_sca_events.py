"""
geocode_sca_events.py
---------------------
Adds latitude and longitude columns to sca_events_clean.csv using the
Nominatim geocoding API (OpenStreetMap) with Photon as a fallback.
Both services are free and require no API key.

IMPORTANT — Nominatim usage policy:
  - Maximum 1 request per second (enforced by this script)
  - Must identify your app with a User-Agent string (set below)
  - Do not run this in parallel / do not hammer the API

Caching strategy:
  - lat/lng are stored directly in sca_events_clean.csv
  - Rows that already have both lat and lng are SKIPPED on re-run
  - The file is saved after every successful geocode, so it's safe to
    interrupt and resume at any time

Geocoding priority:
  - Uses clean_location if address_confidence is "high" or "low"
  - Skips rows where address_confidence is "empty"
  - Records geocode_status for every row:
      "ok"               — geocoded successfully on first attempt
      "ok_retry"         — geocoded successfully after a fallback retry
      "ok_photon"        — geocoded successfully via the Photon fallback service
      "failed"           — neither service returned a result
      "skipped"          — no address available to geocode

Retry ladder when the first Nominatim call fails:
  1. Strip everything before the first street-number pattern (drops venue prefix)
  2. Try just the last 3 comma parts (city, state, country)
  3. Try just the last 2 comma parts (city, state)
  4. Try Photon (a separate OSM-based service that handles venue names better)

Requirements:
    pip install requests pandas

Usage:
    python geocode_sca_events.py [--retry-failed]

  --retry-failed   re-geocode rows currently marked "failed" (skips "ok"/"ok_*")
"""

import argparse
import csv
import functools
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

# Load a local .env (if present) so NOMINATIM_CONTACT_EMAIL and similar can
# be set out-of-band, without ever being committed. This MUST run before
# importing geocoder, which reads the env at import time to build its
# User-Agent. Optional dep: falls through silently if python-dotenv isn't
# installed (CI sets the env var directly).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import kingdoms
import geocoder

# Force unbuffered stdout so background runs show progress as it happens
print = functools.partial(print, flush=True)

# Windows console defaults to cp1252; force UTF-8 so we can print the arrows
# and warning glyphs without crashing on group names like Skorragarðr too.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR  = Path(__file__).parent
INPUT_FILE  = SCRIPT_DIR / "sca_events_clean.csv"
OUTPUT_FILE = INPUT_FILE   # overwrite in place (lat/lng added to same file)

# User-Agent + rate limit + country filter now live in geocoder.py (shared
# with build_group_pins.py). USER_AGENT is re-exported for the session setup.
USER_AGENT = geocoder.USER_AGENT

# How many rows to save after before writing progress to disk
# (1 = save after every geocode, higher = faster but more data loss if interrupted)
SAVE_EVERY_N = 10


# Regex matching "<digit(s)> <Street Name> <street suffix>" — used by the
# retry fallback to strip a leading venue name like "Burger's Lake 1200 …"
STREET_NUM_RE = re.compile(
    r"\b\d+\s+[\w\s.]+?(?:road|rd|street|st|avenue|ave|drive|dr|lane|ln|"
    r"blvd|boulevard|way|court|ct|place|pl|highway|hwy|parkway|pkwy|"
    r"circle|cir|trail|trl|run|pike|path|row)\b",
    re.IGNORECASE,
)

# Extract a 2-letter US state code from an address string like
# "..., Garner, NC, 27529" or "..., Garner, NC 27529"
STATE_FROM_ADDR_RE = re.compile(r",\s*([A-Z]{2})(?:[\s,]|$)")

# Rough bounding boxes for US states + DC, used to verify that a geocoded
# result actually lies inside the state named in the address. Boxes are
# generous (~20–50 mile buffer) so legitimate addresses near borders don't
# get rejected. Coords are (min_lat, max_lat, min_lng, max_lng).
US_STATE_BBOX = kingdoms.STATE_BBOX


US_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def extract_state_from_address(address: str) -> str | None:
    """
    Return the first 2-letter US state code found in an address, or None.
    Only treats the code as a US state if the address also contains a US-style
    5-digit ZIP code — otherwise "WA" might be Western Australia (Lochac),
    "VIC" might be Victoria, etc.
    """
    if not US_ZIP_RE.search(address):
        return None
    m = STATE_FROM_ADDR_RE.search(address)
    if m:
        code = m.group(1).upper()
        if code in US_STATE_BBOX:
            return code
    return None


# Canadian postal code: letter-digit-letter [space] digit-letter-digit. Excludes
# D, F, I, O, Q, U (never used) and W, Z (never lead). Used to give a rural
# Canadian address a precise OSM hit when the civic street number doesn't match,
# and to pre-empt the "PROVINCE, <partial postal>, Canada" comma-tail query, which
# Nominatim resolves to a single generic province-centre point.
CA_POSTAL_RE = re.compile(
    r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z])\s*(\d[ABCEGHJ-NPRSTV-Z]\d)\b",
    re.IGNORECASE,
)


def extract_ca_postal(address: str) -> str | None:
    """Return a normalised Canadian postal code ('V9A 7N2') found in the address,
    or None."""
    m = CA_POSTAL_RE.search(address or "")
    return f"{m.group(1)} {m.group(2)}".upper() if m else None


# ---------------------------------------------------------------------------
# Per-barony state validation
# ---------------------------------------------------------------------------
# Map from source name → home state(s). Used to verify that a baronial event
# geocodes into the barony's geographic area or an adjacent state. Kingdoms
# are intentionally omitted: kingdoms can host events anywhere in their
# territory, which often spans many states. Add new baronies here when you
# add them to calendars.csv.
BARONY_HOME_STATES = kingdoms.BARONY_HOME_STATES

# US state adjacency (sharing a border). Used together with BARONY_HOME_STATES
# so that baronial events held just over the state line in a neighbouring
# state don't get rejected.
US_STATE_ADJACENT = kingdoms.US_STATE_ADJACENT


KINGDOM_HOME_STATES = kingdoms.KINGDOM_HOME_STATES


# Build source -> kingdom from locals.csv so a baronial source without an
# explicit BARONY_HOME_STATES entry still inherits its kingdom's member states
# (e.g. every Caid barony -> CA/NV/HI) instead of accepting a match anywhere in
# North America. Silently empty if locals.csv is absent.
def _load_source_kingdom() -> dict:
    out = {}
    path = Path(__file__).parent / "locals.csv"
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                group = (r.get("group") or "").strip()
                kingdom = (r.get("kingdom") or "").strip()
                if group and kingdom:
                    out.setdefault(group, kingdom)
    return out


SOURCE_KINGDOM = _load_source_kingdom()


# source -> (lat, lng) from locals.csv, so acceptable_states_for_source can tell
# when a branch sits OUTSIDE its kingdom's US footprint — an overseas exclave like
# the Kingdom of the West's Barony of the Far West (South Korea / Thailand), whose
# events must not be constrained to the West's US states.
def _load_source_home_coords() -> dict:
    out = {}
    path = Path(__file__).parent / "locals.csv"
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                group = (r.get("group") or "").strip()
                try:
                    coords = (float((r.get("lat") or "").strip()),
                              float((r.get("lng") or "").strip()))
                except ValueError:
                    continue
                if group:
                    out.setdefault(group, coords)
    return out


SOURCE_HOME_COORDS = _load_source_home_coords()


def _strip_source_tag(source: str) -> str:
    """Drop a trailing "(Workshops)"-style tag so "Barony of X (Practices)"
    matches the "Barony of X" row in locals.csv."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", source or "").strip()


def acceptable_states_for_source(source: str) -> set | None:
    """Return the set of states an event from this source can legitimately
    be in (own state + adjacent states for a barony, member states only for
    a kingdom), or None if we have no record of the source."""
    home = BARONY_HOME_STATES.get(source) or KINGDOM_HOME_STATES.get(source)
    if not home:
        # An unlisted barony inherits its kingdom's member states (via
        # locals.csv) so it stays in-kingdom rather than anywhere in NA.
        kingdom = (SOURCE_KINGDOM.get(source)
                   or SOURCE_KINGDOM.get(_strip_source_tag(source)))
        home = KINGDOM_HOME_STATES.get(kingdom) if kingdom else None
    if not home:
        return None
    acceptable = set(home)
    for state in home:
        acceptable |= US_STATE_ADJACENT.get(state, set())
    # A branch can sit OUTSIDE its kingdom's US states — a Canadian shire under a
    # US kingdom (An Tir in BC, the East in QC), or an overseas exclave (the West's
    # Barony of the Far West in Asia). Constraining those to the kingdom's US states
    # rejects their every correct coordinate. Use the branch's own locals.csv home:
    coords = (SOURCE_HOME_COORDS.get(source)
              or SOURCE_HOME_COORDS.get(_strip_source_tag(source)))
    if coords:
        home_region = coord_state(*coords)
        if home_region is None:
            return None   # overseas, no bounding box to guard with (e.g. Asia)
        # Widen the guard to the branch's own province/state (+ its neighbours) so
        # local coordinates pass, while still catching a far misgeocode.
        acceptable.add(home_region)
        acceptable |= US_STATE_ADJACENT.get(home_region, set())
    return acceptable


def coord_state(lat: float, lng: float) -> str | None:
    """Return the 2-letter state code whose bounding box contains (lat, lng),
    or None. If multiple boxes overlap (state borders), returns the first match."""
    for code, (lat_min, lat_max, lng_min, lng_max) in US_STATE_BBOX.items():
        if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
            return code
    return None


def result_in_state(lat: float, lng: float, state: str) -> bool:
    """True if (lat, lng) falls within the rough bounding box of the named state."""
    box = US_STATE_BBOX.get(state)
    if not box:
        return True   # Unknown state: don't reject
    return box[0] <= lat <= box[1] and box[2] <= lng <= box[3]


# ---------------------------------------------------------------------------
# Geocoding primitives
# ---------------------------------------------------------------------------

# ── Geocoding primitives (shared) ──────────────────────────────────────
# The actual Nominatim/Photon clients, the persistent cache, the rate-limit
# gate, and the SCA country filter all live in geocoder.py so this script and
# build_group_pins.py share one cache file and one throttle. These thin
# wrappers preserve the names the retry-ladder below already calls.
SCA_COUNTRY_CODES = geocoder.SCA_COUNTRY_CODES


def nominatim_geocode(address: str, session: requests.Session) -> tuple:
    """Query Nominatim (cached). Returns (lat, lng) or (None, None)."""
    return geocoder.nominatim(address, session)


def photon_geocode(address: str, session: requests.Session) -> tuple:
    """Query Photon (cached). Returns (lat, lng) or (None, None)."""
    return geocoder.photon(address, session)


def save_geo_cache() -> None:
    """Flush the shared geocode cache to disk (called from main)."""
    geocoder.save_cache()


# Backwards-compatible alias (older versions used `geocode`)
def geocode(address: str, session: requests.Session) -> tuple:
    return nominatim_geocode(address, session)


def strip_venue_prefix(address: str) -> str:
    """
    Strip a leading venue name from the first comma-part of an address.
    e.g. "Burger's Lake 1200 Meandering Rd, Fort Worth, TX" →
         "1200 Meandering Rd, Fort Worth, TX"
    Returns the original string if no embedded street pattern is found.
    """
    parts = [p.strip() for p in address.split(",")]
    if not parts:
        return address
    m = STREET_NUM_RE.search(parts[0])
    if m and m.start() > 0:
        parts[0] = parts[0][m.start():].strip()
        return ", ".join(parts)
    return address


# Rough bounding boxes for the SCA's known geographic regions, keyed by name.
# We use these as a coarse continent/sub-continent filter to keep Photon from
# placing a Meridies "Atlanta Metropolitan area" event in Romania.
SCA_REGION_BBOXES = kingdoms.SCA_REGION_BBOXES

# Which regions a given kingdom's events can legitimately land in. Out-of-
# kingdom events (Pennsic, Gulf Wars, KWACC, Tir Mara) all stay within
# North America for US-based kingdoms, so a Meridies "Atlanta Metropolitan
# area" landing in Romania is clearly a Photon mis-match.
KINGDOM_REGIONS = kingdoms.KINGDOM_REGIONS


def in_sca_region(lat: float, lng: float) -> bool:
    """True if (lat, lng) is in any region where SCA kingdoms actually exist."""
    return any(lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng
               for (lo_lat, hi_lat, lo_lng, hi_lng) in SCA_REGION_BBOXES.values())


def in_source_regions(lat: float, lng: float, source: str) -> bool:
    """True if (lat, lng) is in one of the SCA regions a `source` kingdom's
    events can legitimately reach. Returns True for unknown sources (we have
    no opinion on where their events should be)."""
    regions = KINGDOM_REGIONS.get(source)
    if not regions:
        return True
    return any(box[0] <= lat <= box[1] and box[2] <= lng <= box[3]
               for name, box in SCA_REGION_BBOXES.items() if name in regions)


def _validate(lat, lng, expected_state, source_acceptable_states, source=""):
    """
    Decide whether to accept a geocoded (lat, lng).

    Hard reject (returns False):
      - Result is in a country outside SCA territory entirely (Belize, India,
        sub-Saharan Africa). This catches Photon mis-matches for vague
        addresses; Nominatim already filters via countrycodes.
      - Result is on the wrong continent for the source kingdom. A Meridies
        "Atlanta Metropolitan area" landing in Romania is a Photon mis-match,
        not a real cross-kingdom event.
      - Address named a US state and the result isn't in it. The address
        is authoritative.
      - For BARONY sources, hard reject if the result lies outside the
        barony's home state + adjacent states. Baronial events are local
        practices, meetings, demos — they don't legitimately happen far
        from home. (Stierbach's fencing practice geocoding to Washington
        STATE off "auxillary gym mary washington" was this bug.)

    Soft check (prints a warning, still returns True):
      - For KINGDOM sources without a state in the address: warn but
        accept results outside the kingdom's state set. Kingdoms
        legitimately list cross-kingdom events (Pennsic, Gulf Wars,
        Tir Mara events in Atlantic Canada, Known World events).
    """
    if lat is None or lng is None:
        return False
    if not in_sca_region(lat, lng):
        print(f"           ✗ rejected: ({lat:.3f}, {lng:.3f}) outside SCA regions")
        return False
    if source and not in_source_regions(lat, lng, source):
        print(f"           ✗ rejected: ({lat:.3f}, {lng:.3f}) wrong continent for {source}")
        return False
    if expected_state:
        if not result_in_state(lat, lng, expected_state):
            print(f"           ✗ rejected: ({lat:.3f}, {lng:.3f}) not in {expected_state}")
            return False
        return True   # Trust the address
    if source_acceptable_states:
        actual = coord_state(lat, lng)
        if actual is None or actual not in source_acceptable_states:
            allowed = ",".join(sorted(source_acceptable_states))
            # Baronial events are local — hard reject distant matches.
            # Kingdom events can be cross-kingdom (wars, KW gatherings) — soft warn.
            if source.lower().startswith("kingdom of"):
                print(f"           ⚠ out-of-region: ({lat:.3f}, {lng:.3f}) in "
                      f"{actual or 'no US state'}, expected one of {allowed} "
                      f"— accepting (likely cross-kingdom event)")
            else:
                print(f"           ✗ rejected: ({lat:.3f}, {lng:.3f}) in "
                      f"{actual or 'no US state'}, not in {source}'s region "
                      f"({allowed}) — baronial events stay local")
                return False
    return True


# Match an explicit "GPS: <lat> N, <lng> W" coordinate pair embedded in a
# location string (Meridies sometimes adds this for their event venues).
GPS_INLINE_RE = re.compile(
    r"GPS\s*[:\-]?\s*(-?\d+(?:\.\d+)?)\s*([NSns])\s*,?\s*(-?\d+(?:\.\d+)?)\s*([EWew])",
    re.IGNORECASE,
)


def extract_inline_gps(address: str) -> tuple:
    """If the address contains a 'GPS: lat N, lng W' substring, parse it and
    return (lat, lng). Otherwise (None, None). Saves a Nominatim round-trip
    and avoids cases where the trailing GPS suffix breaks the geocoder."""
    m = GPS_INLINE_RE.search(address)
    if not m:
        return (None, None)
    lat = float(m.group(1))
    lng = float(m.group(3))
    if m.group(2).upper() == "S":
        lat = -abs(lat)
    if m.group(4).upper() == "W":
        lng = -abs(lng)
    return (lat, lng)


# Coordinate pairs a calendar dropped into the location OR description text. We
# extract them so the pin uses the published spot instead of geocoding a venue name
# Nominatim only approximates — but free text (descriptions especially) is full of
# innocent numbers (times, prices, ZIPs, phone numbers), so each form below is
# deliberately strict and every hit is STILL region-checked by the caller.

def _plausible(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180


# Google Calendar mangles some West-kingdom location coordinates: the latitude's
# decimal point becomes a space and the longitude's minus sign becomes '#' — e.g.
# Teufelberg's "37 71232046276675 #121.87544721303145" means 37.71232, -121.87545.
GOOGLE_MANGLED_RE = re.compile(r"\b(\d{1,2})[ ](\d{4,})\s*#\s*(\d{1,3}\.\d{3,})")

# A pair written with cardinal directions, e.g. "37.7123 N, 121.8754 W" or
# "37.71°N 121.88°W" (optionally prefixed "GPS"). The N/S + E/W letters make it
# unambiguous, so 2+ decimals suffice; a decimal IS required so a street or highway
# direction ("95 N, 12 W") can't match.
CARDINAL_RE = re.compile(
    r"(?:gps[\s:\-]*)?(\d{1,3}\.\d{2,})\s*°?\s*([NS])[\s,]+"
    r"(\d{1,3}\.\d{2,})\s*°?\s*([EW])", re.IGNORECASE)

# A bare high-precision decimal pair, e.g. "37.71232, -121.87545" or a Google Maps
# "@40.7211695,-73.9525670" URL tail. Require 4+ decimals on BOTH so a price, time,
# measurement, or street number can't masquerade as a coordinate (a real published
# coordinate carries far more precision than any of those), and forbid an adjacent
# digit/dot so a longer number isn't sliced in half.
HIGH_PRECISION_PAIR_RE = re.compile(
    r"(?<![\d.])(-?\d{1,2}\.\d{4,})\s*[,/ ]\s*(-?\d{1,3}\.\d{4,})(?!\d)(?!\.\d)")


def extract_published_coords(text: str, source: str = "") -> tuple:
    """Return (lat, lng) when the text already carries explicit coordinates the
    calendar published — a 'GPS: lat N, lng W' suffix, a cardinal-direction pair,
    the Google-mangled 'lat #lng' form, or a bare high-precision decimal pair — so
    we use them verbatim instead of geocoding a venue name (the reason Teufelberg's
    pin kept landing off its real site). (None, None) when no plausible pair is
    present. The result is only trusted after the caller's region check, since an
    innocent number pair could in principle still parse. `source`, when given,
    disambiguates the sign of the Google-mangled form (whose '#' replaced a dropped
    minus) by keeping whichever hemisphere fits that source's region."""
    if not text:
        return (None, None)
    lat, lng = extract_inline_gps(text)            # "GPS: 33.45 N, 84.10 W"
    if lat is not None:
        return (lat, lng)
    m = GOOGLE_MANGLED_RE.search(text)
    if m:
        lat, mag = float(f"{m.group(1)}.{m.group(2)}"), float(m.group(3))
        # '#' replaced a dropped minus, so the longitude is normally western — but
        # don't force the hemisphere: keep whichever sign fits the source's region
        # (defends an eastern feed that ever emits this mangled form). With no
        # source opinion, default to the documented western sign.
        for cand in (-mag, mag):
            if _plausible(lat, cand) and (not source or in_source_regions(lat, cand, source)):
                return (lat, cand)
        if _plausible(lat, -mag):
            return (lat, -mag)
    m = CARDINAL_RE.search(text)
    if m:
        lat = float(m.group(1)) * (-1 if m.group(2).upper() == "S" else 1)
        lng = float(m.group(3)) * (-1 if m.group(4).upper() == "W" else 1)
        if _plausible(lat, lng):
            return (lat, lng)
    m = HIGH_PRECISION_PAIR_RE.search(text)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if _plausible(lat, lng):
            return (lat, lng)
    return (None, None)


# SCA-speak "the city/area mundanely known as <place>" prefix.
_MUNDANE_RE = re.compile(
    r"\bthe\s+(?:city|cities|town|area|region|lands?|shire|barony|canton|province)\s+"
    r"(?:mundanely\s+|currently\s+)?known\s+as\s+(?:the\s+greater\s+)?",
    re.IGNORECASE,
)


def _vague_simplify_candidates(address: str) -> list:
    """Reduce a vague SCA location description to candidate core placenames,
    most-specific first. Handles "the city mundanely known as Lubbock, Texas",
    "Greater Birmingham area and Shelby County", "Atlanta Metropolitan area",
    "Centered around Ridgecrest, CA; ...", and city/county lists ("Madera,
    Fresno, Kings, and Tulare Counties" -> "Madera"). Returns [] if nothing
    simplifies."""
    cands = []
    s = address.strip()

    m = _MUNDANE_RE.search(s)
    if m:
        s = s[m.end():].strip()
        cands.append(s)

    m = re.match(r"(?:centered\s+(?:around|on|in|near)|based\s+(?:in|near|around)|"
                 r"located\s+(?:in|near))\s+(.+)", s, re.IGNORECASE)
    if m:
        rest = re.split(r";|,?\s+including\b|,?\s+and\s+surrounding",
                        m.group(1), maxsplit=1)[0].strip().rstrip(",.;")
        if rest:
            cands.append(rest)

    # "Greater X area [and ...]" / "X Metropolitan area" -> X
    g = re.split(r"\s+and\s+", s, maxsplit=1)[0]
    g = re.sub(r"\b(?:greater|metro(?:politan)?)\b\s*", "", g, flags=re.IGNORECASE)
    g = re.sub(r"\s*\barea\b.*$", "", g, flags=re.IGNORECASE).strip().rstrip(",.;")
    if g and g.lower() != s.lower():
        cands.append(g)

    # First item of a comma/slash list, minus leading "the area of" and a
    # trailing "County/Counties".
    first = s.split(",")[0].strip()
    first = re.sub(r"^(?:the\s+area\s+of\s+|the\s+greater\s+)", "", first,
                   flags=re.IGNORECASE).strip()
    first = first.split("/")[0].strip()
    first_noco = re.sub(r"\s+Count(?:y|ies)$", "", first, flags=re.IGNORECASE).strip()
    for c in (first, first_noco):
        if c:
            cands.append(c)

    # Dedupe (case-insensitive); keep only candidates that look like a
    # placename: start with a capital letter (drops "the", "two additional
    # groups") and not a digit (street fragments are handled by other ladder
    # steps, not here).
    seen, out = set(), []
    for c in cands:
        cl = c.lower()
        if (cl not in seen and cl != address.strip().lower()
                and len(c) >= 4 and c[:1].isalpha() and c[0].isupper()):
            seen.add(cl)
            out.append(c)
    return out


def try_geocode_with_fallbacks(address: str, session: requests.Session,
                                source: str = "") -> tuple:
    """
    Try a sequence of progressively-broader queries. Returns (lat, lng, status)
    where status is one of "ok", "ok_retry", "ok_photon", or "failed".

    Results are validated against two constraints:
      - If the address contains a US state code, the result must be in that state
      - If the source is a tracked barony, the result must be in the barony's
        home state or an adjacent state (catches "auxillary gym mary washington"
        → Seattle-type miscodes for a Virginia barony)
    """
    expected_state = extract_state_from_address(address)
    source_states = acceptable_states_for_source(source) if source else None

    # extract_state_from_address needs a ZIP to trust a US state code (so a
    # Lochac "…Perth WA 6000" isn't read as Washington). But a US-sourced event
    # with a clear street number + state code is authoritative even without a
    # ZIP — trust it so cross-kingdom war addresses ("205 Currie Rd., Slippery
    # Rock, PA" on a Maryland barony's calendar) resolve instead of being
    # locality-rejected. Gated on source_states so it never fires for Lochac/AU.
    if expected_state is None and source_states and STREET_NUM_RE.search(address):
        m = STATE_FROM_ADDR_RE.search(address)
        if m and m.group(1).upper() in US_STATE_BBOX:
            expected_state = m.group(1).upper()

    # 0. Coordinates explicitly embedded in the address — use them directly.
    lat, lng = extract_inline_gps(address)
    if _validate(lat, lng, expected_state, source_states, source):
        print(f"           → using inline GPS")
        return (lat, lng, "ok")

    # Strip the inline GPS suffix, a trailing URL, and a trailing parenthetical
    # note (venue directions, "private property", map links) before geocoding.
    cleaned = GPS_INLINE_RE.sub("", address).strip().rstrip(",.;")
    cleaned = re.sub(r"\s+https?://\S+.*$", "", cleaned)
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", cleaned).strip().rstrip(",.;")

    # 1. Original address via Nominatim
    address = cleaned  # use cleaned for all subsequent attempts
    lat, lng = nominatim_geocode(address, session)
    if _validate(lat, lng, expected_state, source_states, source):
        return (lat, lng, "ok")

    # 1b. Canadian postal code. A rural civic address ("17317 Larch Rd, Telkwa,
    #     BC, V0J 2X2, Canada") often misses in Nominatim, and the comma-tail
    #     retry below ("BC, V0J 2X2, Canada") resolves to one generic southern-BC
    #     point that the province-sized bbox wrongly accepts (the An Tir mis-land).
    #     The full postal code geocodes precisely; query it scoped to Canada first,
    #     falling back to the FSA (first 3 chars) for the right region.
    ca_postal = extract_ca_postal(address)
    if ca_postal:
        print(f"           → CA postal: {ca_postal}")
        lat, lng = geocoder.nominatim(f"{ca_postal}, Canada", session, countrycodes="ca")
        if not _validate(lat, lng, expected_state, source_states, source):
            lat, lng = geocoder.nominatim(f"{ca_postal[:3]}, Canada", session, countrycodes="ca")
        if _validate(lat, lng, expected_state, source_states, source):
            return (lat, lng, "ok_retry")

    # 2. Strip embedded venue prefix from the street part
    stripped = strip_venue_prefix(address)
    if stripped != address:
        print(f"           → retrying without venue: {stripped[:70]}")
        lat, lng = nominatim_geocode(stripped, session)
        if _validate(lat, lng, expected_state, source_states, source):
            return (lat, lng, "ok_retry")

    # 3. Last 3 comma parts (street, city, state OR city, state, country)
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 3:
        simplified = ", ".join(parts[-3:])
        if simplified not in (address, stripped):
            print(f"           → retrying tail: {simplified[:70]}")
            lat, lng = nominatim_geocode(simplified, session)
            if _validate(lat, lng, expected_state, source_states, source):
                return (lat, lng, "ok_retry")

    # 4. Last 2 comma parts (city, state)
    if len(parts) >= 2:
        city_state = ", ".join(parts[-2:])
        print(f"           → retrying city/state: {city_state[:70]}")
        lat, lng = nominatim_geocode(city_state, session)
        if _validate(lat, lng, expected_state, source_states, source):
            return (lat, lng, "ok_retry")

    # 5. Photon fallback — uses original address (handles venue names better)
    print(f"           → trying Photon: {address[:70]}")
    lat, lng = photon_geocode(address, session)
    if _validate(lat, lng, expected_state, source_states, source):
        return (lat, lng, "ok_photon")

    # 5b. If we have an expected kingdom/state list, try appending each one to
    #     the address. Catches cases like "Greater Savannah area" (Meridies)
    #     where Photon returns Belize because the address is too vague.
    #     BUT: if the address already contains an explicit 2-letter state abbreviation
    #     (capitalized), the state is already clear — skip the hints.
    has_explicit_state = bool(re.search(r'\b[A-Z]{2}\b', address))
    if source_states and not has_explicit_state:
        for state in sorted(source_states):
            query = f"{address}, {state}, USA"
            print(f"           → retrying with state hint: {query[:70]}")
            lat, lng = nominatim_geocode(query, session)
            if _validate(lat, lng, expected_state, source_states, source):
                return (lat, lng, "ok_retry")

    # 6. Last-ditch: try just the US ZIP code as "ZIP, USA" — places a marker
    #    at the city level when the venue prefix or street layout was too
    #    weird for Nominatim to parse. Common East/Atlantia issue: "Core Creek
    #    Park - Pavillions 5 & 6 901 Bridgetown Pike Langhorne, PA 19047 US".
    zip_match = US_ZIP_RE.search(address)
    if zip_match:
        zip_code = zip_match.group(0)
        print(f"           → retrying ZIP only: {zip_code}")
        lat, lng = nominatim_geocode(f"{zip_code}, USA", session)
        if _validate(lat, lng, expected_state, source_states, source):
            return (lat, lng, "ok_retry")

    # 7. Vague-text simplification: SCA idioms ("the city mundanely known as
    #    Lubbock, Texas"), "Greater X area"/"X Metropolitan area", and city/
    #    county lists reduced to a core placename. Try "<place>, USA" first so
    #    Nominatim's prominence picks the real city, then each of the kingdom's
    #    HOME states. Accept ONLY a result that lands in a home state — not an
    #    adjacent one, and with no cross-kingdom soft-accept — so "Madera"
    #    can't resolve to an adjacent Arizona and "NE Tennessee" can't resolve
    #    to Nebraska. Capped; breaks on first valid hit.
    home_states = (KINGDOM_HOME_STATES.get(source)
                   or BARONY_HOME_STATES.get(source) or set())
    tried = 0
    for cand in _vague_simplify_candidates(address):
        if "," in cand:                       # candidate already carries a state
            queries = [cand]
        else:
            queries = [f"{cand}, USA"] + [f"{cand}, {st}, USA"
                                          for st in sorted(home_states)]
        for q in queries:
            if tried >= 8:
                break
            tried += 1
            print(f"           → simplified: {q[:70]}")
            lat, lng = nominatim_geocode(q, session)
            if lat is None:
                continue
            if home_states:
                if (coord_state(lat, lng) in home_states
                        and in_source_regions(lat, lng, source)):
                    return (lat, lng, "ok_retry")
            elif _validate(lat, lng, expected_state, source_states, source):
                return (lat, lng, "ok_retry")

    return (None, None, "failed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# "override" = a human pinned exact coords in event_overrides.csv; treat it as
# done/successful so the geocoder never touches it (see clean_sca_events.py
# apply_event_overrides and EDITING_EVENTS.md). "ok_organizer" = pinned by
# enrich_gleann_locations.py to the event's hosting group. "ok_published" = the
# calendar published coordinates in the location text (see the pre-pass in main).
# "ok_fallback" = a baronial no-address event pinned at its barony's own coords by
# clean_sca_events.apply_baronial_coords.
SUCCESS_STATUSES = {"ok", "ok_retry", "ok_photon", "cached", "override",
                    "ok_organizer", "ok_published", "ok_fallback"}


def main(retry_failed: bool = False):
    print(f"Reading {INPUT_FILE.name} ...")
    df = pd.read_csv(INPUT_FILE, dtype=str).fillna("")
    print(f"  {len(df)} rows loaded")

    # Add columns if they don't exist yet
    for col in ("lat", "lng", "geocode_status"):
        if col not in df.columns:
            df[col] = ""

    # Published-coordinate pre-pass: when an event's location OR description text
    # already carries explicit coordinates (a "GPS:" suffix, a cardinal pair, the
    # Google-mangled "lat #lng" form, or a high-precision decimal pair), use them
    # verbatim. They're authoritative, so they OVERRIDE a prior/carried-forward
    # geocode (this is what finally pins Teufelberg to its real site) and the
    # address is never sent to Nominatim. The region check below is the backstop
    # against an innocent number pair (a price/time can't parse, but a stray
    # coordinate-shaped pair must still land where this source's events can be).
    # A human override or an organizer-placed (Gleann) pin is left alone.
    published = 0
    for idx in df.index:
        if str(df.at[idx, "geocode_status"]) in ("override", "ok_organizer"):
            continue
        source = str(df.at[idx, "source"] or "")
        lat, lng = extract_published_coords(str(df.at[idx, "clean_location"] or ""), source)
        if lat is None:   # fall back to the description, where some organisers paste it
            lat, lng = extract_published_coords(str(df.at[idx, "description"] or ""), source)
        if lat is None:
            continue
        if in_sca_region(lat, lng) and in_source_regions(lat, lng, source):
            df.at[idx, "lat"] = str(lat)
            df.at[idx, "lng"] = str(lng)
            df.at[idx, "geocode_status"] = "ok_published"
            published += 1
    if published:
        print(f"  Used published coordinates for {published} event(s) (authoritative).")

    # Rows considered "done" — successful or deliberately skipped, but NOT failed
    # unless --retry-failed is on
    done_statuses = SUCCESS_STATUSES | {"skipped"}
    if not retry_failed:
        done_statuses = done_statuses | {"failed"}

    already_done = df["geocode_status"].isin(done_statuses) & (df["geocode_status"] != "")
    to_geocode   = ~already_done
    skippable    = (df.loc[to_geocode, "address_confidence"] == "empty")

    print(f"  Already done:                {already_done.sum()}")
    print(f"  To geocode:                  {to_geocode.sum() - skippable.sum()}")
    print(f"  No address (will skip):      {skippable.sum()}")
    if retry_failed:
        n_failed = (df["geocode_status"] == "failed").sum()
        print(f"  (retrying {n_failed} previously-failed rows)")
    print()

    if to_geocode.sum() == 0:
        if published:   # the pre-pass changed rows even though there's nothing to geocode
            df.to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
            print(f"  Saved {published} published-coordinate update(s).")
        print("Nothing to do — all rows already have geocode_status set.")
        return

    # Mark skippable rows immediately
    df.loc[to_geocode & (df["address_confidence"] == "empty"), "geocode_status"] = "skipped"

    # Never geocode a "location" that is just a URL — Zoom/Meet links and event
    # websites with no street address. Nominatim can't resolve them, and leaving
    # them "failed" means every --retry-failed run re-sends them, wasting calls
    # against Nominatim's bulk-geocoding budget. Match only locations that START
    # with http(s):// so a real address with a trailing URL still geocodes
    # (and hybrid events like "… (Manteca, CA & Zoom)" keep their pin).
    url_loc = to_geocode & df["clean_location"].fillna("").str.match(r"^\s*https?://")
    df.loc[url_loc, "geocode_status"] = "skipped"

    # Set up session with required User-Agent
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Rows that need actual geocoding
    needs_geocoding = (to_geocode & (df["address_confidence"] != "empty")
                       & ~url_loc)
    indices = df[needs_geocoding].index.tolist()
    total = len(indices)

    print(f"Starting geocoding {total} addresses ...\n")

    status_counts = {"ok": 0, "ok_retry": 0, "ok_photon": 0, "failed": 0}

    for i, idx in enumerate(indices, start=1):
        row = df.loc[idx]
        address = str(row.get("clean_location", "")).strip()
        source  = str(row.get("source", "")).strip()

        if not address:
            df.at[idx, "geocode_status"] = "skipped"
            continue

        print(f"  [{i}/{total}] {address[:70]}")

        lat, lng, status = try_geocode_with_fallbacks(address, session, source)

        if lat is not None:
            df.at[idx, "lat"]            = str(lat)
            df.at[idx, "lng"]            = str(lng)
            df.at[idx, "geocode_status"] = status
            print(f"           → {lat:.5f}, {lng:.5f}  ({status})")
            status_counts[status] += 1
        else:
            df.at[idx, "geocode_status"] = "failed"
            # Wipe stale coords from a previous failed attempt, just in case
            df.at[idx, "lat"] = ""
            df.at[idx, "lng"] = ""
            print(f"           → FAILED (no result from any service)")
            status_counts["failed"] += 1

        # Save progress periodically (rows + the geocode cache together)
        if i % SAVE_EVERY_N == 0:
            df.to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
            save_geo_cache()
            print(f"  [Progress saved at {i}/{total}]")


    # Final save
    df.to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
    save_geo_cache()

    print(f"\nDone!")
    print(f"  First-try Nominatim:    {status_counts['ok']}")
    print(f"  Nominatim w/ retry:     {status_counts['ok_retry']}")
    print(f"  Photon fallback:        {status_counts['ok_photon']}")
    print(f"  Failed:                 {status_counts['failed']}")
    print(f"  Skipped (no address):   {df['geocode_status'].eq('skipped').sum()}")
    print(f"\nResults saved to '{OUTPUT_FILE.name}'")
    print(f"\nTip: rows with geocode_status='failed' can be reviewed manually.")
    print(f"     Edit their clean_location and re-run with --retry-failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--retry-failed", action="store_true",
                        help="Also re-attempt rows currently marked 'failed'")
    args = parser.parse_args()
    main(retry_failed=args.retry_failed)
