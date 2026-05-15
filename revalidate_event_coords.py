"""
revalidate_event_coords.py
--------------------------
One-shot helper that re-validates every already-geocoded event in
sca_events_clean.csv against the updated kingdom/state bounding boxes.
Rows that geocoded into the wrong state (e.g. "Greater Savannah area"
for a Meridies kingdom event landing in Belize) get their lat/lng/status
cleared so the next `python geocode_sca_events.py --retry-failed` run
re-geocodes them with the new state-hint retry logic.

Run once after extending KINGDOM_HOME_STATES or adjusting US_STATE_BBOX.
"""
from __future__ import annotations

import csv
from pathlib import Path

from geocode_sca_events import (
    acceptable_states_for_source,
    coord_state,
    extract_state_from_address,
    result_in_state,
)

CSV_FILE = Path(__file__).parent / "sca_events_clean.csv"


def main():
    with open(CSV_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys()) if rows else []

    cleared = 0
    for r in rows:
        lat = r.get("lat", "").strip()
        lng = r.get("lng", "").strip()
        if not lat or not lng:
            continue
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except ValueError:
            continue

        source = r.get("source", "").strip()
        address = r.get("clean_location", "").strip()

        expected_state = extract_state_from_address(address)
        source_states = acceptable_states_for_source(source) if source else None

        # Hard reject only when the address explicitly named a state and the
        # result lies outside it (Heroes and Heroines → Belize, Castle Wars →
        # India, etc., where Photon returned something on the wrong continent
        # for a vague address with no state hint). Out-of-kingdom events with
        # a real address (Pennsic in PA hosted by Ansteorra) stay put, and
        # legitimate non-US Tir Mara events in NB/NS/QC stay put too.
        bad = False
        if expected_state and not result_in_state(lat_f, lng_f, expected_state):
            bad = True
        # Note: no fallback "must be in source states" check — kingdoms can
        # legitimately list events anywhere (cross-kingdom invitations, KWACC,
        # Pennsic, Tir Mara events in Atlantic Canada, etc.). The Nominatim
        # countrycodes filter in geocode_sca_events.py keeps Photon from
        # returning Belize / India in the first place on the next run.

        if bad:
            cleared += 1
            print(f"  cleared: {r.get('title','')[:50]:50s} | "
                  f"{source:25s} | ({lat_f:.2f}, {lng_f:.2f}) | "
                  f"{address[:40]}")
            r["lat"] = ""
            r["lng"] = ""
            r["geocode_status"] = "failed"

    print(f"\nCleared {cleared} bad geocodes")
    if cleared:
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Re-run `python geocode_sca_events.py --retry-failed` to fix them.")


if __name__ == "__main__":
    main()
