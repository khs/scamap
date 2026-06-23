"""
build_world_kingdoms.py
-----------------------
One-off helper that builds `world-kingdoms.json`, a minimal GeoJSON
containing only the non-US polygons we need for the kingdom-color overlay:

  • Canadian provinces (Avacal, Ealdormere, Tir Righ, Tir Mara)
  • Australian states + New Zealand (Lochac)
  • Western/Northern/Central European countries (Drachenwald)

Source data is Natural Earth (50m admin-1 for Canada/AU provinces, 50m
admin-0 for countries that we color whole). We tag every feature with a
`kingdom` property so the front-end can color without needing a separate
lookup table.

Output: world-kingdoms.geojson (~150 KB) committed to the repo.

Re-run only if the kingdom-to-territory mapping changes (rarely).
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
OUTPUT_FILE = SCRIPT_DIR / "world-kingdoms.topojson"

ADMIN1_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces.geojson"
COUNTRIES_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"

# ---------------------------------------------------------------------------
# Canada provinces by ISO 3166-2 code → kingdom
# ---------------------------------------------------------------------------
# Natural Earth admin-1 is province-level, so a few real sub-province splits
# can't be drawn here and fall to the larger kingdom:
#   • Ontario is Ealdormere, EXCEPT Essex County / Windsor (Midrealm) and
#     north-western Ontario (Northshield) — not separable at province level.
#   • British Columbia is mostly An Tir, but eastern BC is Avacal — likewise
#     not separable, so the whole province shows as An Tir.
# Nunavut is intentionally left unassigned (no kingdom claims it on this map).
CANADA_KINGDOM = {
    "CA-AB": "Kingdom of Avacal",
    "CA-SK": "Kingdom of Avacal",
    "CA-NT": "Kingdom of Avacal",           # Northwest Territories
    "CA-MB": "Kingdom of Northshield",
    "CA-ON": "Kingdom of Ealdormere",
    "CA-QC": "Kingdom of the East",
    "CA-NB": "Kingdom of the East",
    "CA-NS": "Kingdom of the East",
    "CA-PE": "Kingdom of the East",
    "CA-NL": "Kingdom of the East",
    "CA-YT": "Kingdom of An Tir",
    "CA-BC": "Kingdom of An Tir",
}

AUSTRALIA_KINGDOM = {
    # Whole continent is Lochac (NZ added at country level below)
}

# Whole-country kingdom mapping (admin-0 features)
COUNTRY_KINGDOM = {
    "Australia":           "Kingdom of Lochac",
    "New Zealand":         "Kingdom of Lochac",
    # Drachenwald — Western/Central/Northern Europe (rough coverage matching
    # the kingdom's actual baronies)
    "United Kingdom":      "Kingdom of Drachenwald",
    "Ireland":             "Kingdom of Drachenwald",
    "France":              "Kingdom of Drachenwald",
    "Germany":             "Kingdom of Drachenwald",
    "Belgium":             "Kingdom of Drachenwald",
    "Netherlands":         "Kingdom of Drachenwald",
    "Luxembourg":          "Kingdom of Drachenwald",
    "Switzerland":         "Kingdom of Drachenwald",
    "Austria":             "Kingdom of Drachenwald",
    "Italy":               "Kingdom of Drachenwald",
    "Spain":               "Kingdom of Drachenwald",
    "Portugal":            "Kingdom of Drachenwald",
    "Denmark":             "Kingdom of Drachenwald",
    "Sweden":              "Kingdom of Drachenwald",
    "Norway":              "Kingdom of Drachenwald",
    "Finland":             "Kingdom of Drachenwald",
    "Iceland":             "Kingdom of Drachenwald",
    "Poland":              "Kingdom of Drachenwald",
    "Czechia":             "Kingdom of Drachenwald",
    "Slovakia":            "Kingdom of Drachenwald",
    "Hungary":             "Kingdom of Drachenwald",
    "Slovenia":            "Kingdom of Drachenwald",
    "Croatia":             "Kingdom of Drachenwald",
    "Estonia":             "Kingdom of Drachenwald",
    "Latvia":              "Kingdom of Drachenwald",
    "Lithuania":           "Kingdom of Drachenwald",
    "Bulgaria":            "Kingdom of Drachenwald",
    "Romania":             "Kingdom of Drachenwald",
    "Greece":              "Kingdom of Drachenwald",
    "Ukraine":             "Kingdom of Drachenwald",
    # Balkans
    "Serbia":              "Kingdom of Drachenwald",
    "Bosnia and Herzegovina": "Kingdom of Drachenwald",
    "Montenegro":          "Kingdom of Drachenwald",
    "Albania":             "Kingdom of Drachenwald",
    "Kosovo":              "Kingdom of Drachenwald",
    "North Macedonia":     "Kingdom of Drachenwald",
    "Macedonia":           "Kingdom of Drachenwald",   # older Natural Earth name
    # Trimaris — Central America
    "Panama":              "Kingdom of Trimaris",
    # West — Pacific Rim
    "Japan":               "Kingdom of the West",
    "South Korea":         "Kingdom of the West",
    "Thailand":            "Kingdom of the West",
}


def main():
    print(f"Fetching {ADMIN1_URL[-50:]}...")
    admin1 = requests.get(ADMIN1_URL, timeout=60).json()
    print(f"  {len(admin1['features'])} admin-1 features")

    print(f"Fetching {COUNTRIES_URL[-50:]}...")
    countries = requests.get(COUNTRIES_URL, timeout=60).json()
    print(f"  {len(countries['features'])} country features")

    out_features = []

    # ── Canada provinces ────────────────────────────────────────────────
    for f in admin1["features"]:
        props = f.get("properties", {})
        if props.get("admin") != "Canada":
            continue
        iso = props.get("iso_3166_2", "")
        kingdom = CANADA_KINGDOM.get(iso)
        if not kingdom:
            print(f"  (skipping Canadian province {iso} — no kingdom mapping)")
            continue
        f["properties"] = {
            "kingdom": kingdom,
            "name":   props.get("name", ""),
            "admin1": iso,
        }
        out_features.append(f)

    # ── Whole-country mappings (Lochac AU+NZ, Drachenwald Europe) ───────
    for f in countries["features"]:
        props = f.get("properties", {})
        # Natural Earth abbreviates some NAMEs ("Bosnia and Herz."), so also
        # check the longer/admin name fields. Match on the country's OWN name
        # only — NOT SOVEREIGNT/GEOUNIT, which would drag in overseas
        # dependencies (Greenland, Falklands, New Caledonia, …) of UK/FR/DK.
        candidates = [props.get(k) for k in
                      ("NAME", "name", "NAME_LONG", "ADMIN", "NAME_EN")]
        kingdom = next((COUNTRY_KINGDOM[c] for c in candidates
                        if c in COUNTRY_KINGDOM), None)
        name = props.get("NAME") or props.get("name", "")
        if not kingdom:
            continue
        # Strip props down to just what we need (saves bytes)
        f["properties"] = {
            "kingdom": kingdom,
            "name":   name,
            "iso_a2": props.get("ISO_A2", ""),
        }
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
