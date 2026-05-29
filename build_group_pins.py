"""
build_group_pins.py
-------------------
Geocodes each group's region from group_locations.csv and writes a
matching group_pins.csv with lat/lng coordinates. The map uses this file
to render placeholder pins for groups that have a website but no events
currently on the map: "For information on events in this barony, please
visit their website."

The placeholder pin disappears automatically as soon as the group has any
event in sca_events_clean.csv (the map does that check at render time).

Caching: this script preserves prior coordinates by joining on
(kingdom, group, location). Re-running it after a refresh only geocodes
new or relocated groups. To force a full re-geocode, delete group_pins.csv.

Output columns:
    kingdom, group, location, website, lat, lng

Usage:
    python build_group_pins.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
import time
from pathlib import Path

import requests

# Load .env before importing geocoder (which reads NOMINATIM_CONTACT_EMAIL at
# import time to build its User-Agent). Optional dep — CI sets the env var.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import kingdoms
import geocoder

# Default Windows console encoding (cp1252) crashes on Old Norse and other
# non-Latin-1 characters in group names. Force UTF-8 stdout so we can print
# Skorragarðr, Aarnimetsä, etc. without a UnicodeEncodeError mid-run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


SCRIPT_DIR  = Path(__file__).parent
INPUT_FILE  = SCRIPT_DIR / "group_locations.csv"
OUTPUT_FILE = SCRIPT_DIR / "group_pins.csv"

USER_AGENT = geocoder.USER_AGENT


def load_prior_coords() -> dict[tuple[str, str, str], tuple[str, str]]:
    """Return {(kingdom, group, location): (lat, lng)} from a prior run."""
    cache: dict = {}
    if not OUTPUT_FILE.exists():
        return cache
    try:
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row.get("kingdom", ""), row.get("group", ""), row.get("location", ""))
                lat = row.get("lat", ""); lng = row.get("lng", "")
                if lat and lng:
                    cache[key] = (lat, lng)
    except Exception as exc:
        print(f"  WARNING: could not read prior {OUTPUT_FILE.name}: {exc}")
    return cache


def _nominatim_one(query: str, session: requests.Session, *,
                    require_in_name: str = "") -> tuple:
    """One Nominatim call via the shared geocoder (cached + throttled there).

    Uses the narrower country filter this script always used (us/ca/au/nz/gb/de)
    plus addressdetails, so the require_in_name display-name check works.
    """
    return geocoder.nominatim(
        query, session,
        countrycodes="us,ca,au,nz,gb,de",
        require_in_name=require_in_name,
        addressdetails=True,
    )


def geocode_region(text: str, session: requests.Session, kingdom: str = "") -> tuple:
    """
    Look up a region centroid via Nominatim, with progressive simplification.

    Atlantia entries are long county lists ("Clarendon, Dillon, Florence,
    Horry, Marion"); Caid entries are county descriptions ("San Bernardino
    and Riverside counties"); others are single cities ("Watertown and Fort
    Drum, NY"). We try:

      1. The full text
      2. EACH comma-separated item independently, scored: keep results in
         the expected state, average the coords if multiple match
      3. The first word + state hint

    Step 2 is the fix for cases like "Canton of Misty Marsh by the Sea:
    Clarendon, Dillon, Florence, Horry, Marion" — every one of those is a
    South Carolina county, but trying just "Clarendon" picks "Clarendon, TX"
    by default. By checking each chunk in turn and rejecting ones outside
    the expected state, we lock onto a correct match.
    """
    if not text:
        return (None, None)

    # Pick acceptable states first — we use them to bbox-check every step
    # below so that step 1's "look up the text as-is" can't silently return
    # Durham, England for an Atlantian Durham County, or Montgomery, NZ for
    # a Meridies "Greater Montgomery area".
    state_in_text_m = re.search(r"\b([A-Z]{2})\b(?:\s*,|\s*$)", text)
    if state_in_text_m and state_in_text_m.group(1) in _STATE_BBOX:
        acceptable_states = [state_in_text_m.group(1)]
    else:
        acceptable_states = list(KINGDOM_STATES.get(kingdom, ()))
    acceptable_boxes = [_STATE_BBOX[s] for s in acceptable_states if s in _STATE_BBOX]
    state_hint = acceptable_states[0] if acceptable_states else ""

    def in_kingdom(lat: float, lng: float) -> bool:
        """True if (lat, lng) is in any of the kingdom's acceptable states
        — or unconditionally true when we have no bbox for the kingdom
        (non-US kingdoms; we trust the geocoder there)."""
        if not acceptable_boxes:
            return True
        return any(_in_box(lat, lng, box) for box in acceptable_boxes)

    # 1. Original — only accept if it lands in the kingdom's region.
    lat, lng = _nominatim_one(text, session)
    if lat is not None and in_kingdom(lat, lng):
        return (lat, lng)

    # 2. Try each comma-separated chunk against each acceptable state. Group
    #    hits by which state they landed in; the state with the most hits is
    #    the most likely intended one. Centroid the in-winning-state hits.
    #
    #    Example: "Clarendon, Dillon, Florence, Horry, Marion" for an Atlantian
    #    group. We try (Clarendon, VA), (Clarendon, MD), (Clarendon, NC),
    #    (Clarendon, SC), (Clarendon, DC); then the same for each other county.
    #    SC gets 5 hits and wins; we average those into a single SC centroid.
    chunks = [c.strip() for c in text.split(",") if c.strip()]
    chunks = [re.sub(r"^Cities?\s+of\s+", "", c, flags=re.IGNORECASE) for c in chunks]
    chunks = [c for c in chunks if not re.fullmatch(r"[A-Z]{2}", c)]   # drop bare state codes

    hits_by_state: dict[str, list[tuple[float, float]]] = {}
    any_match: tuple[float, float] | None = None

    for chunk in chunks[:6]:
        if chunk == text:
            continue
        # Try the chunk as a COUNTY in each acceptable state. The county form
        # is unambiguous — "Clarendon County, SC" returns Clarendon County SC,
        # not Virginia's centroid. Then we vote: the state with the most
        # genuine county-level hits wins.
        for state in acceptable_states or []:
            query = f"{chunk} County, {state}"
            print(f"    retrying with: {query[:60]}")
            # Require that the result's display_name actually contains
            # "<Chunk> County" — without this we get false positives from
            # Nominatim degrading e.g. "Dillon County, NC" to some other
            # NC place that just contains the word "Dillon".
            lat, lng = _nominatim_one(
                query, session,
                require_in_name=f"{chunk} County",
            )
            if lat is None:
                continue
            if _in_box(lat, lng, _STATE_BBOX[state]):
                hits_by_state.setdefault(state, []).append((lat, lng))
                print(f"      -> hit in {state}: ({lat:.3f}, {lng:.3f})")
            elif any_match is None:
                any_match = (lat, lng)

    if hits_by_state:
        # Pick the state with the most hits — ties broken alphabetically
        winning_state = max(hits_by_state,
                            key=lambda s: (len(hits_by_state[s]), -ord(s[0])))
        pts = hits_by_state[winning_state]
        avg_lat = sum(p[0] for p in pts) / len(pts)
        avg_lng = sum(p[1] for p in pts) / len(pts)
        return (avg_lat, avg_lng)
    # Only fall back to an out-of-region match when we DON'T have a known
    # kingdom region. Returning Durham, England for an Atlantian group
    # because we couldn't find a US Durham is worse than failing — failed
    # geocodes leave the pin off the map, which is better than wrong pin.
    if any_match is not None and not acceptable_boxes:
        return any_match

    # 3. First word + state + USA (last resort)
    first_chunk = chunks[0] if chunks else ""
    short = re.split(r"[,\s]+", first_chunk, maxsplit=1)[0] if first_chunk else ""
    if short and state_hint:
        query = f"{short}, {state_hint}, USA"
        print(f"    retrying with: {query[:60]}")
        lat, lng = _nominatim_one(query, session)
        if lat is not None and in_kingdom(lat, lng):
            return (lat, lng)

    # 4. General simplification fallback — handles vague text Nominatim can't
    #    parse: "Gulfport/Biloxi, MS and surrounding area" (take first city),
    #    "Western MA" (drop the direction, use the state), "Cold Lake, AB and
    #    area" (Canadian province), "Helsinki area" / "Großraum Köln, Bonn"
    #    (European, with a country hint). Each candidate is tried in turn.
    for query in _simplify_candidates(text, state_hint, kingdom):
        print(f"    retrying simplified: {query[:60]}")
        lat, lng = _nominatim_one(query, session)
        if lat is not None and in_kingdom(lat, lng):
            return (lat, lng)

    return (None, None)


# Full state/province names for expanding "Western MA" → "Massachusetts" etc.
_US_STATE_NAMES = kingdoms.US_STATE_NAMES
_CA_PROVINCE_NAMES = kingdoms.CA_PROVINCE_NAMES
# Leading direction / region qualifiers we strip to find the actual placename.
_DIRECTION_RE = re.compile(
    r"^(?:großraum|greater|the whole|the|upper|lower|mid|"
    r"north|south|east|west|central|northern|southern|eastern|western|"
    r"northeast(?:ern)?|northwest(?:ern)?|southeast(?:ern)?|southwest(?:ern)?|"
    r"north[\s-]central|south[\s-]central|east[\s-]central|west[\s-]central)"
    r"[\s,-]+", re.IGNORECASE)


def _simplify_candidates(text: str, state_hint: str, kingdom: str):
    """Yield progressively-simpler geocode queries for vague region text."""
    t = text.strip()
    # Strip trailing noise the scrapers sometimes leave behind.
    t = re.sub(r"\s*\bDates:.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\bContinue reading.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r",?\s+and covers .*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r",?\s+(?:and )?surrounding areas?$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+and area$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+(?:metro(?:politan)?\s+)?area$", "", t, flags=re.IGNORECASE)
    t = t.strip().rstrip(",.;:")

    # "Region (i.e. City)" → City
    m = re.search(r"\(i\.e\.?\s*([^),]+)", t, re.IGNORECASE)
    if m:
        t = m.group(1).strip()

    # Detect a trailing 2-letter US state or Canadian province in the text.
    prov = st = ""
    m2 = re.search(r",\s*([A-Z]{2})\b", t)
    if m2:
        code = m2.group(1)
        if code in _CA_PROVINCE_NAMES:
            prov = code
        elif code in _US_STATE_NAMES:
            st = code
    if not st and state_hint in _US_STATE_NAMES:
        st = state_hint

    # First token before a slash / comma / semicolon / " and ", direction stripped.
    tok = re.split(r"\s*[/,;]\s*|\s+and\s+", t)[0].strip()
    tok = _DIRECTION_RE.sub("", tok).strip().rstrip(",.;:")

    candidates = []
    # If the residual collapsed to nothing or a bare state code, geocode the
    # state/province centroid.
    if (not tok) or tok.upper() in _US_STATE_NAMES or tok.upper() in _CA_PROVINCE_NAMES or len(tok) <= 2:
        if prov:
            candidates.append(f"{_CA_PROVINCE_NAMES[prov]}, Canada")
        elif st:
            candidates.append(f"{_US_STATE_NAMES[st]}, USA")
    else:
        if prov:
            candidates.append(f"{tok}, {_CA_PROVINCE_NAMES[prov]}, Canada")
        elif st:
            candidates.append(f"{tok}, {_US_STATE_NAMES[st]}, USA")
        else:
            # No state context: try the bare token (works for big cities) and,
            # for non-US kingdoms, a country-qualified form.
            candidates.append(tok)
            country = {"Kingdom of Avacal": "Canada",
                       "Kingdom of Ealdormere": "Canada",
                       "Kingdom of Lochac": "Australia"}.get(kingdom)
            if country:
                candidates.append(f"{tok}, {country}")
    # De-dup while preserving order
    seen = set()
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            yield c


def _in_box(lat: float, lng: float, box) -> bool:
    lat_min, lat_max, lng_min, lng_max = box
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


# Bounding boxes for the US states + DC and a handful of Canadian provinces
# we care about. Used by step 2 of geocode_region to verify that each
# candidate location lies in the expected state. (Same shape as the
# clean_sca_events bbox table, kept duplicated to avoid coupling.)
_STATE_BBOX = kingdoms.STATE_BBOX


# States a kingdom actually covers. geocode_region accepts any one when
# scoring multi-chunk locations, since Atlantia is MD/VA/NC/SC/DC etc. —
# treating any of those as "in the right place" prevents Misty Marsh's
# all-SC-counties list from being mis-placed in VA.
KINGDOM_STATES = kingdoms.KINGDOM_STATES


def main():
    if not INPUT_FILE.exists():
        print(f"No {INPUT_FILE.name} found — run build_group_locations.py first.")
        return

    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Loaded {len(rows)} group definitions from {INPUT_FILE.name}")

    prior = load_prior_coords()
    print(f"Prior coords cached for {len(prior)} groups")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    fieldnames = ["kingdom", "group", "location", "website", "lat", "lng"]

    def save(rows_so_far):
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows_so_far)

    out_rows = []
    new_geocodes = 0
    for i, row in enumerate(rows, start=1):
        key = (row.get("kingdom", ""), row.get("group", ""), row.get("location", ""))
        out = {
            "kingdom":  row.get("kingdom", ""),
            "group":    row.get("group", ""),
            "location": row.get("location", ""),
            "website":  row.get("website", ""),
            "lat":      "",
            "lng":      "",
        }
        if key in prior:
            out["lat"], out["lng"] = prior[key]
        elif out["location"]:
            print(f"  [{i}/{len(rows)}] Geocoding: {out['group']} ({out['location'][:50]})")
            lat, lng = geocode_region(out["location"], session, out["kingdom"])
            if lat is not None:
                out["lat"] = f"{lat:.6f}"
                out["lng"] = f"{lng:.6f}"
                new_geocodes += 1
                print(f"    -> ({lat:.4f}, {lng:.4f})")
            else:
                print(f"    -> FAILED")
        out_rows.append(out)

        # Save progress every 10 entries so an interrupted run still leaves
        # a usable group_pins.csv. The rest of the rows (not yet processed)
        # are appended as blank-coord rows so the file stays consistent
        # with the current group_locations.csv.
        if i % 10 == 0:
            tail = [
                {**{k: r.get(k, "") for k in fieldnames}, "lat": "", "lng": ""}
                for r in rows[i:]
            ]
            save(out_rows + tail)
            geocoder.save_cache()

    save(out_rows)
    geocoder.save_cache()

    geocoded = sum(1 for r in out_rows if r["lat"] and r["lng"])
    with_site = sum(1 for r in out_rows if r["website"])
    print(f"\nWrote {len(out_rows)} groups to {OUTPUT_FILE.name}")
    print(f"  {geocoded} geocoded, {with_site} with website")
    print(f"  {new_geocodes} newly geocoded this run")


if __name__ == "__main__":
    main()
