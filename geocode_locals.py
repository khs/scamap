"""
geocode_locals.py
-----------------
Fill in blank lat/lng for groups in locals.csv that have a `location` but no
coordinates yet — these power the "?" placeholder pins and the baronial-fallback
coords. Uses the shared cached + throttled geocoder (same Nominatim client and
cache as geocode_sca_events.py), so re-runs are cheap.

IMPORTANT: only touches rows whose lat AND lng are both blank. Any group that
already has coordinates is left exactly as-is — hand-placed pins are never moved.

Run standalone or as the first step of refresh.py:
    python geocode_locals.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import requests

import geocoder

LOCALS = Path(__file__).parent / "locals.csv"


def main():
    if not LOCALS.exists():
        print("  (no locals.csv — nothing to geocode)")
        return
    with open(LOCALS, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)

    # Skip rows with placeholder location text (not real places to geocode).
    SKIP_PATTERNS = {"no location", "no location available", "no calendar available",
                     "tbd", "unknown", "unconfirmed"}
    todo = [r for r in rows
            if (r.get("location") or "").strip()
            and (r.get("location") or "").strip().lower() not in SKIP_PATTERNS
            and not (r.get("lat") or "").strip()
            and not (r.get("lng") or "").strip()]
    print(f"geocode_locals: {len(todo)} group(s) with a location but no coords")
    if not todo:
        return

    session = requests.Session()
    filled = 0
    for r in todo:
        loc = r["location"].strip()
        lat, lng = geocoder.nominatim(loc, session)   # cached, throttled, SCA countries
        if lat is None:
            print(f"  - {r.get('group','')[:34]:34} | no match for {loc!r}")
            continue
        r["lat"] = f"{round(lat, 6):g}"
        r["lng"] = f"{round(lng, 6):g}"
        filled += 1
        print(f"  + {r.get('group','')[:34]:34} | {loc[:32]:32} -> {r['lat']}, {r['lng']}")

    if filled:
        # Minimal quoting to match locals.csv's hand-edited style (only fields with
        # commas get quoted), so the diff is just the rows we filled.
        with open(LOCALS, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            w.writerows(rows)
        geocoder.save_cache()
    print(f"geocode_locals: filled {filled} blank coordinate(s) in locals.csv")


if __name__ == "__main__":
    main()
