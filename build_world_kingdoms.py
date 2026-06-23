"""
build_world_kingdoms.py
-----------------------
One-off helper that builds `world-kingdoms.topojson`, a minimal layer
containing only the non-US, non-Canada polygons we need for the overlay:

  • Australian states + New Zealand (Lochac)
  • Western/Northern/Central European countries (Drachenwald)
  • a few country-level principalities (The Marches in Asia, Insulae Draconis
    in the UK/Ireland) coloured by their parent kingdom

Canada is NOT here — it is drawn at census-division granularity from
canada-cd.topojson. Source data is Natural Earth (50m admin-0 for the whole
countries). We tag every feature with `kingdom` (and `parent` for a
principality) so the front-end can colour without a separate lookup.

Output: world-kingdoms.geojson (~150 KB) committed to the repo.

Re-run only if the kingdom-to-territory mapping changes (rarely).
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
OUTPUT_FILE = SCRIPT_DIR / "world-kingdoms.topojson"

ADMIN1_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces.geojson"
COUNTRIES_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"

# ---------------------------------------------------------------------------
# Province (Canada, by ISO 3166-2) + country (by name) -> kingdom assignments
# live in territory_kingdoms.csv, the single editable territory registry (which
# also holds the US state/county rows that index.html reads directly). Edit
# that CSV and re-run this script to change the world overlay.
# ---------------------------------------------------------------------------
TERRITORY_CSV = SCRIPT_DIR / "territory_kingdoms.csv"


def load_territory():
    """territory_kingdoms.csv -> (canada_by_iso, country_by_name) dicts, each
    mapping id -> (kingdom, parent_kingdom). `parent_kingdom` is set when the
    assignment is a principality, so the front-end can paint it in its parent
    kingdom's colour while the hover label names both."""
    canada, country = {}, {}
    with open(TERRITORY_CSV, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            t = (r.get("type") or "").strip().lower()
            kingdom = (r.get("kingdom") or "").strip()
            if not kingdom:
                continue
            parent = (r.get("parent kingdom") or "").strip()
            if t == "province":
                canada[(r.get("id") or "").strip()] = (kingdom, parent)
            elif t == "country":
                country[(r.get("id") or "").strip()] = (kingdom, parent)
    return canada, country


def main():
    # Canada is drawn at census-division granularity from canada-cd.topojson
    # (province default + per-CD overrides), so we no longer emit its provinces
    # here — only the country-level layers below would otherwise double-render.
    _canada, COUNTRY_KINGDOM = load_territory()
    print(f"Fetching {COUNTRIES_URL[-50:]}...")
    countries = requests.get(COUNTRIES_URL, timeout=60).json()
    print(f"  {len(countries['features'])} country features")

    out_features = []

    # ── Whole-country mappings (Lochac AU+NZ, Drachenwald Europe) ───────
    for f in countries["features"]:
        props = f.get("properties", {})
        # Natural Earth abbreviates some NAMEs ("Bosnia and Herz."), so also
        # check the longer/admin name fields. Match on the country's OWN name
        # only — NOT SOVEREIGNT/GEOUNIT, which would drag in overseas
        # dependencies (Greenland, Falklands, New Caledonia, …) of UK/FR/DK.
        candidates = [props.get(k) for k in
                      ("NAME", "name", "NAME_LONG", "ADMIN", "NAME_EN")]
        entry = next((COUNTRY_KINGDOM[c] for c in candidates
                      if c in COUNTRY_KINGDOM), None)
        name = props.get("NAME") or props.get("name", "")
        if not entry:
            continue
        kingdom, parent = entry
        # Strip props down to just what we need (saves bytes)
        f["properties"] = {
            "kingdom": kingdom,
            "name":   name,
            "iso_a2": props.get("ISO_A2", ""),
        }
        if parent:
            f["properties"]["parent"] = parent
        out_features.append(f)

    fc = {"type": "FeatureCollection", "features": out_features}

    # Emit TopoJSON (object name "data") — roughly a third the size of the
    # equivalent GeoJSON because shared province/country borders are stored
    # once as arcs. index.html decodes it with topojson-client.
    try:
        import topojson
    except ImportError:
        print("  topojson not installed (pip install topojson); writing GeoJSON instead")
        fallback = OUTPUT_FILE.with_suffix(".geojson")
        fallback.write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")
        print(f"  wrote {fallback.name}: {fallback.stat().st_size:,} bytes")
        return

    print(f"Writing {len(out_features)} features to {OUTPUT_FILE.name}")
    topo = topojson.Topology(fc, prequantize=1e5, topology=True)
    OUTPUT_FILE.write_text(topo.to_json(), encoding="utf-8")
    print(f"  size: {OUTPUT_FILE.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
