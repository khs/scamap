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
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

# Load a local .env (if present) so NOMINATIM_CONTACT_EMAIL and similar
# can be set out-of-band, without ever being committed. Optional dep:
# falls through silently if python-dotenv isn't installed (CI sets the
# env var directly, so it doesn't need the file at all).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

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

# Nominatim's usage policy requires a descriptive User-Agent identifying the
# application AND a working contact address. We read the contact from the
# NOMINATIM_CONTACT_EMAIL env var (set by .env locally, by a GitHub Actions
# secret in CI) so the actual address never lands in committed source.
_CONTACT = os.getenv("NOMINATIM_CONTACT_EMAIL", "nobody@example.com")
USER_AGENT = f"SCA Maps Project ({_CONTACT})"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL    = "https://photon.komoot.io/api"

# Seconds between API calls. Nominatim's published policy is "max 1 req/sec",
# but we're a hobby project running unattended on cron, so we sit at the much
# more conservative 4 calls/minute cap (15 sec/call). Photon — the fallback
# geocoder — uses the same gap.
REQUEST_DELAY = 15.0

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
US_STATE_BBOX = {
    "AL": (30.1, 35.1, -88.6, -84.8), "AK": (51.0, 71.5, -180.0, -129.0),
    "AZ": (31.2, 37.1, -114.9, -109.0), "AR": (32.9, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.5, -114.0), "CO": (36.9, 41.1, -109.1, -101.9),
    "CT": (40.9, 42.1, -73.8, -71.7), "DE": (38.4, 39.9, -75.8, -75.0),
    "DC": (38.7, 39.0, -77.2, -76.9), "FL": (24.4, 31.1, -87.7, -79.9),
    "GA": (30.2, 35.1, -85.7, -80.7), "HI": (18.8, 22.3, -160.3, -154.7),
    "ID": (41.9, 49.1, -117.3, -111.0), "IL": (36.9, 42.6, -91.6, -87.4),
    "IN": (37.7, 41.9, -88.2, -84.7), "IA": (40.3, 43.6, -96.7, -90.1),
    "KS": (36.9, 40.1, -102.1, -94.5), "KY": (36.4, 39.2, -89.7, -81.9),
    "LA": (28.8, 33.1, -94.1, -88.7), "ME": (43.0, 47.6, -71.2, -66.8),
    "MD": (37.8, 39.8, -79.6, -75.0), "MA": (41.1, 42.9, -73.6, -69.8),
    "MI": (41.6, 48.4, -90.5, -82.3), "MN": (43.4, 49.5, -97.3, -89.4),
    "MS": (30.1, 35.1, -91.8, -87.9), "MO": (35.9, 40.7, -95.9, -89.0),
    "MT": (44.3, 49.1, -116.2, -103.9), "NE": (39.9, 43.1, -104.1, -95.2),
    "NV": (34.9, 42.1, -120.1, -113.9), "NH": (42.6, 45.4, -72.7, -70.5),
    "NJ": (38.8, 41.4, -75.7, -73.8), "NM": (31.2, 37.1, -109.2, -102.9),
    "NY": (40.4, 45.1, -79.9, -71.7), "NC": (33.7, 36.7, -84.4, -75.4),
    "ND": (45.8, 49.1, -104.1, -96.5), "OH": (38.3, 42.1, -84.9, -80.4),
    "OK": (33.5, 37.1, -103.1, -94.3), "OR": (41.9, 46.4, -124.7, -116.4),
    "PA": (39.6, 42.4, -80.6, -74.6), "RI": (41.0, 42.1, -71.9, -71.0),
    "SC": (32.0, 35.3, -83.4, -78.4), "SD": (42.4, 45.9, -104.1, -96.3),
    "TN": (34.9, 36.8, -90.4, -81.5), "TX": (25.7, 36.6, -106.7, -93.4),
    "UT": (36.9, 42.1, -114.1, -108.9), "VT": (42.6, 45.1, -73.5, -71.4),
    "VA": (36.4, 39.5, -83.7, -75.1), "WA": (45.4, 49.1, -124.9, -116.8),
    "WV": (37.1, 40.7, -82.7, -77.6), "WI": (42.4, 47.1, -92.9, -86.7),
    "WY": (40.9, 45.1, -111.1, -103.9),
}


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


# ---------------------------------------------------------------------------
# Per-barony state validation
# ---------------------------------------------------------------------------
# Map from source name → home state(s). Used to verify that a baronial event
# geocodes into the barony's geographic area or an adjacent state. Kingdoms
# are intentionally omitted: kingdoms can host events anywhere in their
# territory, which often spans many states. Add new baronies here when you
# add them to calendars.csv.
BARONY_HOME_STATES = {
    # Atlantia — Maryland baronies
    "Barony of Lochmere":         {"MD"},
    "Barony of Bright Hills":     {"MD"},
    "Barony of Storvik":          {"MD"},
    "Barony of Dun Carraig":      {"MD"},
    "Barony of Highland Foorde":  {"MD"},
    # Atlantia — Virginia baronies
    "Barony of Ponte Alto":       {"VA"},
    "Barony of Stierbach":        {"VA"},
    "Barony of Stierbach (Workshops)": {"VA"},
    "Barony of Caer Mear":        {"VA"},
    "Barony of Marinus":          {"VA"},
    "Barony of Tir-y-Don":        {"VA"},
    "Barony of Black Diamond":    {"VA"},
    # Atlantia — North Carolina baronies
    "Barony of Windmasters' Hill": {"NC"},
    "Barony of Raven's Cove":     {"NC"},
    "Barony of Hawkwood":         {"NC"},
    "Barony of Sacred Stone":     {"NC"},
    # Atlantia — South Carolina baronies
    "Barony of Nottinghill Coill": {"SC"},
    "Barony of Hidden Mountain":  {"SC"},
    # Atlantia — shires under Hawkwood
    "Shire of Aukesgate":         {"NC"},
    "Shire of Stormwall":         {"NC"},
}

# US state adjacency (sharing a border). Used together with BARONY_HOME_STATES
# so that baronial events held just over the state line in a neighbouring
# state don't get rejected.
US_STATE_ADJACENT = {
    "AL": {"FL", "GA", "MS", "TN"},
    "AR": {"LA", "MO", "MS", "OK", "TN", "TX"},
    "AZ": {"CA", "CO", "NM", "NV", "UT"},
    "CA": {"AZ", "NV", "OR"},
    "CO": {"AZ", "KS", "NE", "NM", "OK", "UT", "WY"},
    "CT": {"MA", "NY", "RI"},
    "DC": {"MD", "VA"},
    "DE": {"MD", "NJ", "PA"},
    "FL": {"AL", "GA"},
    "GA": {"AL", "FL", "NC", "SC", "TN"},
    "IA": {"IL", "MN", "MO", "NE", "SD", "WI"},
    "ID": {"MT", "NV", "OR", "UT", "WA", "WY"},
    "IL": {"IA", "IN", "KY", "MO", "WI"},
    "IN": {"IL", "KY", "MI", "OH"},
    "KS": {"CO", "MO", "NE", "OK"},
    "KY": {"IL", "IN", "MO", "OH", "TN", "VA", "WV"},
    "LA": {"AR", "MS", "TX"},
    "MA": {"CT", "NH", "NY", "RI", "VT"},
    "MD": {"DC", "DE", "PA", "VA", "WV"},
    "ME": {"NH"},
    "MI": {"IN", "OH", "WI"},
    "MN": {"IA", "ND", "SD", "WI"},
    "MO": {"AR", "IA", "IL", "KS", "KY", "NE", "OK", "TN"},
    "MS": {"AL", "AR", "LA", "TN"},
    "MT": {"ID", "ND", "SD", "WY"},
    "NC": {"GA", "SC", "TN", "VA"},
    "ND": {"MN", "MT", "SD"},
    "NE": {"CO", "IA", "KS", "MO", "SD", "WY"},
    "NH": {"MA", "ME", "VT"},
    "NJ": {"DE", "NY", "PA"},
    "NM": {"AZ", "CO", "OK", "TX", "UT"},
    "NV": {"AZ", "CA", "ID", "OR", "UT"},
    "NY": {"CT", "MA", "NJ", "PA", "VT"},
    "OH": {"IN", "KY", "MI", "PA", "WV"},
    "OK": {"AR", "CO", "KS", "MO", "NM", "TX"},
    "OR": {"CA", "ID", "NV", "WA"},
    "PA": {"DE", "MD", "NJ", "NY", "OH", "WV"},
    "RI": {"CT", "MA"},
    "SC": {"GA", "NC"},
    "SD": {"IA", "MN", "MT", "ND", "NE", "WY"},
    "TN": {"AL", "AR", "GA", "KY", "MO", "MS", "NC", "VA"},
    "TX": {"AR", "LA", "NM", "OK"},
    "UT": {"AZ", "CO", "ID", "NM", "NV", "WY"},
    "VA": {"DC", "KY", "MD", "NC", "TN", "WV"},
    "VT": {"MA", "NH", "NY"},
    "WA": {"ID", "OR"},
    "WI": {"IA", "IL", "MI", "MN"},
    "WV": {"KY", "MD", "OH", "PA", "VA"},
    "WY": {"CO", "ID", "MT", "NE", "SD", "UT"},
}


KINGDOM_HOME_STATES = {
    "Kingdom of AEthelmearc":   {"PA", "WV", "NY"},
    "Kingdom of An Tir":        {"WA", "OR", "ID", "MT"},
    "Kingdom of Ansteorra":     {"OK", "TX"},
    "Kingdom of Artemisia":     {"MT", "UT", "ID", "WY"},
    "Kingdom of Atenveldt":     {"AZ"},
    "Kingdom of Atlantia":      {"VA", "MD", "NC", "SC", "DC"},
    "Kingdom of Caid":          {"CA", "NV", "HI"},
    "Kingdom of Calontir":      {"KS", "MO", "IA", "NE"},
    "Kingdom of the East":      {"CT", "DE", "ME", "MA", "NH", "NJ", "NY",
                                  "PA", "RI", "VT"},
    "Kingdom of Gleann Abhann": {"LA", "AR", "MS", "TN"},
    "Kingdom of Meridies":      {"AL", "GA", "TN", "KY", "FL"},
    "Kingdom of the Middle":    {"IL", "IN", "OH", "MI"},
    "Kingdom of Northshield":   {"MN", "WI", "ND", "SD"},
    "Kingdom of the Outlands":  {"CO", "WY", "NM"},
    "Kingdom of Trimaris":      {"FL"},
    "Kingdom of the West":      {"CA", "NV", "AK"},
}


def acceptable_states_for_source(source: str) -> set | None:
    """Return the set of states an event from this source can legitimately
    be in (own state + adjacent states for a barony, member states only for
    a kingdom), or None if we have no record of the source."""
    home = BARONY_HOME_STATES.get(source) or KINGDOM_HOME_STATES.get(source)
    if not home:
        return None
    acceptable = set(home)
    for state in home:
        acceptable |= US_STATE_ADJACENT.get(state, set())
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

# Countries where the SCA operates — passed to Nominatim so it doesn't
# resolve "Greater Savannah area" to Savannah, Belize or "Castle Wars" to
# something in India. Photon doesn't take a country filter, so we have to
# rely on the kingdom-state hint retry instead.
SCA_COUNTRY_CODES = ("us,ca,au,nz,gb,ie,fr,de,be,nl,lu,ch,at,it,es,pt,"
                     "se,no,fi,dk,is,pl,cz,sk,hu,si,hr,ee,lv,lt,bg,ro,gr,mt,cy")


# In-run memoisation. Nominatim's bulk-geocoding policy bans repeating the
# same query — and our pipeline naturally re-asks for things like
# "Louisiana, USA" (25× across Gleann Abhann events) or "Northern California"
# (18× across West events). Cache keyed by the exact query string so the
# whole retry ladder (original / stripped / city-state / state-hint / ZIP)
# shares hits across rows.
_nominatim_cache: dict[str, tuple] = {}
_photon_cache: dict[str, tuple] = {}

# Rate-limit gate: track the last real HTTP call's timestamp so we sleep
# only when an actual call is about to fire. Cache hits skip the sleep —
# they don't contribute to Nominatim's rate either. Shared across Nominatim
# AND Photon so the per-process HTTP rate is bounded too.
_last_http_call_time = 0.0


def _throttle_for_http() -> None:
    """Sleep until at least REQUEST_DELAY seconds have passed since the last
    geocoder HTTP call. Call this immediately before a real HTTP request."""
    global _last_http_call_time
    elapsed = time.time() - _last_http_call_time
    if 0 < elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_http_call_time = time.time()


def nominatim_geocode(address: str, session: requests.Session) -> tuple:
    """Query Nominatim. Returns (lat, lng) as floats, or (None, None) on failure."""
    if address in _nominatim_cache:
        return _nominatim_cache[address]
    params = {
        "q": address, "format": "json", "limit": 1,
        "countrycodes": SCA_COUNTRY_CODES,
    }
    result = (None, None)
    _throttle_for_http()
    try:
        response = session.get(NOMINATIM_URL, params=params, timeout=10)
        response.raise_for_status()
        results = response.json()
        if results:
            result = (float(results[0]["lat"]), float(results[0]["lon"]))
    except Exception as e:
        print(f"    WARNING: Nominatim error for '{address[:60]}': {e}")
    _nominatim_cache[address] = result
    return result


def photon_geocode(address: str, session: requests.Session) -> tuple:
    """Query Photon. Returns (lat, lng) as floats, or (None, None) on failure."""
    if address in _photon_cache:
        return _photon_cache[address]
    params = {"q": address, "limit": 1}
    result = (None, None)
    _throttle_for_http()
    try:
        response = session.get(PHOTON_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        features = data.get("features", [])
        if features:
            coords = features[0]["geometry"]["coordinates"]  # [lng, lat]
            result = (float(coords[1]), float(coords[0]))
    except Exception as e:
        print(f"    WARNING: Photon error for '{address[:60]}': {e}")
    _photon_cache[address] = result
    return result


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
SCA_REGION_BBOXES = {
    # name → (lat_min, lat_max, lng_min, lng_max)
    "north_america": (24.0, 72.0, -170.0, -52.0),   # US + Canada (incl. Alaska, Atlantic Canada)
    "hawaii":        (18.5, 22.5, -160.5, -154.5),  # Hawaii (Caid)
    "europe":        (34.5, 71.5,  -10.5,  41.0),   # Western/Central/Northern Europe (Drachenwald)
    "australia":     (-45.0, -9.0, 110.0, 156.0),   # Australia (Lochac)
    "new_zealand":   (-47.5, -33.5, 165.0, 179.5),  # New Zealand (Lochac)
}

# Which regions a given kingdom's events can legitimately land in. Out-of-
# kingdom events (Pennsic, Gulf Wars, KWACC, Tir Mara) all stay within
# North America for US-based kingdoms, so a Meridies "Atlanta Metropolitan
# area" landing in Romania is clearly a Photon mis-match.
KINGDOM_REGIONS = {
    # All US-mainland kingdoms can legitimately reach into Canada (Tir Mara,
    # Avacal-adjacent baronies, etc.).
    "Kingdom of AEthelmearc":   {"north_america"},
    "Kingdom of An Tir":        {"north_america"},
    "Kingdom of Ansteorra":     {"north_america"},
    "Kingdom of Artemisia":     {"north_america"},
    "Kingdom of Atenveldt":     {"north_america"},
    "Kingdom of Atlantia":      {"north_america"},
    "Kingdom of Avacal":        {"north_america"},
    "Kingdom of Caid":          {"north_america", "hawaii"},
    "Kingdom of Calontir":      {"north_america"},
    "Kingdom of Drachenwald":   {"europe"},
    "Kingdom of Ealdormere":    {"north_america"},
    "Kingdom of Gleann Abhann": {"north_america"},
    "Kingdom of Lochac":        {"australia", "new_zealand"},
    "Kingdom of Meridies":      {"north_america"},
    "Kingdom of the Middle":    {"north_america"},
    "Kingdom of Northshield":   {"north_america"},
    "Kingdom of the Outlands":  {"north_america"},
    "Kingdom of Trimaris":      {"north_america"},
    "Kingdom of the East":      {"north_america"},
    "Kingdom of the West":      {"north_america"},
}


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

    # 0. Coordinates explicitly embedded in the address — use them directly.
    lat, lng = extract_inline_gps(address)
    if _validate(lat, lng, expected_state, source_states, source):
        print(f"           → using inline GPS")
        return (lat, lng, "ok")

    # Strip the inline GPS suffix before sending to Nominatim either way
    cleaned = GPS_INLINE_RE.sub("", address).strip().rstrip(",.;")

    # 1. Original address via Nominatim
    address = cleaned  # use cleaned for all subsequent attempts
    lat, lng = nominatim_geocode(address, session)
    if _validate(lat, lng, expected_state, source_states, source):
        return (lat, lng, "ok")

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
    if source_states:
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

    return (None, None, "failed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SUCCESS_STATUSES = {"ok", "ok_retry", "ok_photon", "cached"}


def main(retry_failed: bool = False):
    print(f"Reading {INPUT_FILE.name} ...")
    df = pd.read_csv(INPUT_FILE, dtype=str).fillna("")
    print(f"  {len(df)} rows loaded")

    # Add columns if they don't exist yet
    for col in ("lat", "lng", "geocode_status"):
        if col not in df.columns:
            df[col] = ""

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
        print("Nothing to do — all rows already have geocode_status set.")
        return

    # Mark skippable rows immediately
    df.loc[to_geocode & (df["address_confidence"] == "empty"), "geocode_status"] = "skipped"

    # Set up session with required User-Agent
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Rows that need actual geocoding
    needs_geocoding = to_geocode & (df["address_confidence"] != "empty")
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

        # Save progress periodically
        if i % SAVE_EVERY_N == 0:
            df.to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
            print(f"  [Progress saved at {i}/{total}]")


    # Final save
    df.to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)

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
