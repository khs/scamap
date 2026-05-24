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

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "SCA Maps Project (group pin builder)"
# Sit well below Nominatim's published 1 req/sec ceiling and well below
# their rate-limit lockout threshold: 4 calls/minute = 15 sec/call.
REQUEST_DELAY = 15.0


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
    """
    One Nominatim call. Returns (lat, lng) or (None, None).

    If `require_in_name` is set, the result is rejected unless its display_name
    contains the given substring (case-insensitive). Useful for filtering out
    Nominatim's "near miss" matches that degrade to the wrong county or state.
    """
    try:
        r = session.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1,
                    "addressdetails": 1,
                    "countrycodes": "us,ca,au,nz,gb,de"},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json()
        if not results:
            return (None, None)
        top = results[0]
        if require_in_name:
            display = (top.get("display_name") or "").lower()
            if require_in_name.lower() not in display:
                return (None, None)
        return (float(top["lat"]), float(top["lon"]))
    except Exception as exc:
        print(f"    WARNING: geocode error for '{query[:60]}': {exc}")
    return (None, None)


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
    time.sleep(REQUEST_DELAY)

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
            time.sleep(REQUEST_DELAY)
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

    return (None, None)


def _in_box(lat: float, lng: float, box) -> bool:
    lat_min, lat_max, lng_min, lng_max = box
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


# Bounding boxes for the US states + DC and a handful of Canadian provinces
# we care about. Used by step 2 of geocode_region to verify that each
# candidate location lies in the expected state. (Same shape as the
# clean_sca_events bbox table, kept duplicated to avoid coupling.)
_STATE_BBOX = {
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
    # Canadian provinces — Avacal (AB+SK+BC) and Ealdormere (ON).
    "AB": (49.0, 60.0, -120.0, -110.0),
    "BC": (48.3, 60.0, -139.1, -114.0),
    "SK": (49.0, 60.0, -110.0, -101.4),
    "ON": (41.7, 56.9, -95.2, -74.3),
    "QC": (45.0, 62.6, -79.8, -57.1),
    "MB": (49.0, 60.0, -102.0, -88.9),
    "NS": (43.4, 47.0, -66.4, -59.7),
    "NB": (44.6, 48.1, -69.1, -63.8),
    "NL": (46.6, 60.5, -67.8, -52.6),
    "PE": (45.9, 47.1, -64.4, -61.9),
    "YT": (60.0, 69.7, -141.1, -123.8),
}


# States a kingdom actually covers. geocode_region accepts any one when
# scoring multi-chunk locations, since Atlantia is MD/VA/NC/SC/DC etc. —
# treating any of those as "in the right place" prevents Misty Marsh's
# all-SC-counties list from being mis-placed in VA.
KINGDOM_STATES = {
    "Kingdom of AEthelmearc":   ("PA", "WV", "NY"),
    "Kingdom of An Tir":        ("WA", "OR", "ID", "MT", "BC"),
    "Kingdom of Ansteorra":     ("OK", "TX"),
    "Kingdom of Artemisia":     ("MT", "UT", "ID", "WY"),
    "Kingdom of Atenveldt":     ("AZ",),
    "Kingdom of Atlantia":      ("VA", "MD", "NC", "SC", "DC"),
    "Kingdom of Avacal":        ("AB", "SK", "BC"),
    "Kingdom of Caid":          ("CA", "NV", "HI"),
    "Kingdom of Calontir":      ("KS", "MO", "IA", "NE"),
    "Kingdom of Ealdormere":    ("ON",),
    "Kingdom of the East":      ("CT", "DE", "ME", "MA", "NH", "NJ", "NY",
                                 "PA", "RI", "VT",
                                 # Crown Principality of Tir Mara — Atlantic Canada
                                 "NS", "NB", "PE", "NL", "QC"),
    "Kingdom of Gleann Abhann": ("LA", "AR", "MS", "TN"),
    "Kingdom of Meridies":      ("AL", "GA", "TN", "KY", "FL"),
    "Kingdom of the Middle":    ("IL", "IN", "OH", "MI"),
    "Kingdom of Northshield":   ("MN", "WI", "ND", "SD"),
    "Kingdom of the Outlands":  ("CO", "WY", "NM"),
    "Kingdom of Trimaris":      ("FL",),
    "Kingdom of the West":      ("CA", "NV", "AK"),
    # Non-US kingdoms — we don't have state bboxes for these so they fall
    # back to bare-name geocoding without state scoring.
}


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
            time.sleep(REQUEST_DELAY)
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

    save(out_rows)

    geocoded = sum(1 for r in out_rows if r["lat"] and r["lng"])
    with_site = sum(1 for r in out_rows if r["website"])
    print(f"\nWrote {len(out_rows)} groups to {OUTPUT_FILE.name}")
    print(f"  {geocoded} geocoded, {with_site} with website")
    print(f"  {new_geocodes} newly geocoded this run")


if __name__ == "__main__":
    main()
