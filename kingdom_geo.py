"""
kingdom_geo.py
--------------
Build-time "(lat,lng) -> which kingdom's territory is this in?" lookup.

This is the Python twin of the front-end overlay's point-in-territory logic: it
reads the SAME three boundary files (us-counties.topojson, canada-cd.topojson,
world-kingdoms.topojson) and the SAME territory_kingdoms.csv assignment table,
and does ray-casting point-in-polygon to resolve a coordinate to a kingdom.

We do it here, once per refresh, instead of in every visitor's browser — the
war-pin "hosting kingdom" is a pure function of the event's coordinates and the
(rarely-changing) territory map, so there's no reason to ship ~480 KB of polygons
to the client and recompute it on each page load. annotate_war_hosts.py calls
kingdom_at_point() and writes the answer into sca_events_clean.csv's war_host
column; the front-end just reads it.

Pure standard library — no shapely/geopandas/topojson dependency — so the cron
needs nothing beyond what the rest of the pipeline already uses.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent
US_COUNTIES = HERE / "us-counties.topojson"
CANADA_CD = HERE / "canada-cd.topojson"
WORLD = HERE / "world-kingdoms.topojson"
TERRITORY = HERE / "territory_kingdoms.csv"

# PRUID (Statistics Canada province code) -> ISO province key used in
# territory_kingdoms.csv. Mirrors PRUID_TO_ISO in index.html.
PRUID_TO_ISO = {
    "10": "CA-NL", "11": "CA-PE", "12": "CA-NS", "13": "CA-NB", "24": "CA-QC",
    "35": "CA-ON", "46": "CA-MB", "47": "CA-SK", "48": "CA-AB", "59": "CA-BC",
    "60": "CA-YT", "61": "CA-NT", "62": "CA-NU",
}


# ── TopoJSON decoding ─────────────────────────────────────────────────────
def _decode_arcs(topo: dict) -> list[list[tuple[float, float]]]:
    """Dequantize + delta-decode every arc into absolute (lng, lat) point lists."""
    transform = topo.get("transform")
    out = []
    for arc in topo["arcs"]:
        pts = []
        if transform:
            sx, sy = transform["scale"]
            tx, ty = transform["translate"]
            x = y = 0
            for dx, dy in arc:
                x += dx
                y += dy
                pts.append((x * sx + tx, y * sy + ty))
        else:
            pts = [(p[0], p[1]) for p in arc]
        out.append(pts)
    return out


def _stitch(arc_indices, arcs):
    """Join a ring's arc indices (negative = that arc, reversed) into one ring."""
    ring = []
    for idx in arc_indices:
        seg = arcs[idx] if idx >= 0 else arcs[~idx][::-1]
        ring.extend(seg if not ring else seg[1:])   # drop the shared join point
    return ring


def _load_features(path: Path, object_name: str) -> list[dict]:
    """Decode one topojson object into [{id, properties, polys}], where polys is a
    list of polygons and each polygon is [outer_ring, hole_ring, ...]."""
    topo = json.loads(path.read_text(encoding="utf-8"))
    arcs = _decode_arcs(topo)
    feats = []
    for g in topo["objects"][object_name]["geometries"]:
        gtype = g.get("type")
        if gtype == "Polygon":
            polys = [[_stitch(r, arcs) for r in g["arcs"]]]
        elif gtype == "MultiPolygon":
            polys = [[_stitch(r, arcs) for r in poly] for poly in g["arcs"]]
        else:
            continue   # null / point geometries carry no area
        feats.append({"id": g.get("id"), "properties": g.get("properties", {}),
                      "polys": polys})
    return feats


# ── Point-in-polygon (ray casting) ────────────────────────────────────────
def _point_in_ring(x: float, y: float, ring) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_polys(x: float, y: float, polys) -> bool:
    for rings in polys:
        if rings and _point_in_ring(x, y, rings[0]) \
                and not any(_point_in_ring(x, y, h) for h in rings[1:]):
            return True
    return False


def _feature_at(features, x: float, y: float) -> Optional[dict]:
    for f in features:
        if _point_in_polys(x, y, f["polys"]):
            return f
    return None


# ── Territory assignment table (territory_kingdoms.csv) ────────────────────
class _Territory:
    def __init__(self):
        self.state = {}     # 2-digit FIPS -> {kingdom, parent}
        self.county = {}    # 5-digit FIPS -> {kingdom, parent}
        self.cd = {}        # 4-digit CDUID -> {kingdom, parent}
        self.province = {}  # "CA-ON" -> {kingdom, parent}
        if TERRITORY.exists():
            with open(TERRITORY, encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    info = {"kingdom": (r.get("kingdom") or "").strip(),
                            "parent": (r.get("parent kingdom") or "").strip()}
                    t, i = r.get("type"), (r.get("id") or "").strip()
                    if t == "state":
                        self.state[i.zfill(2)] = info
                    elif t == "county":
                        self.county[i.zfill(5)] = info
                    elif t == "cd":
                        self.cd[i.zfill(4)] = info
                    elif t == "province":
                        self.province[i] = info

    @staticmethod
    def _effective(info) -> Optional[str]:
        # A principality is painted in its PARENT kingdom's colour, so the
        # colour-bearing name is the parent when present, else the kingdom.
        if not info:
            return None
        return info["parent"] or info["kingdom"] or None

    def for_fips(self, fips5: str):
        return self._effective(self.county.get(fips5) or self.state.get(fips5[:2]))

    def for_cd(self, cduid, pruid):
        return self._effective(
            self.cd.get(str(cduid).zfill(4))
            or self.province.get(PRUID_TO_ISO.get(str(pruid).zfill(2), "")))


# ── Public API (lazily loaded, memoised) ──────────────────────────────────
_counties = _canada = _world = _terr = None
_point_cache: dict = {}


def _ensure_loaded():
    global _counties, _canada, _world, _terr
    if _terr is None:
        _terr = _Territory()
        _counties = _load_features(US_COUNTIES, "counties") if US_COUNTIES.exists() else []
        _canada = _load_features(CANADA_CD, "cd") if CANADA_CD.exists() else []
        _world = _load_features(WORLD, "data") if WORLD.exists() else []


def kingdom_at_point(lat: float, lng: float) -> Optional[str]:
    """The effective kingdom whose territory contains (lat, lng), or None.

    Tries US counties (most specific), then Canadian census divisions, then the
    world layer — the same precedence as the front-end's kingdomAtPoint()."""
    key = (round(lat, 4), round(lng, 4))
    if key in _point_cache:
        return _point_cache[key]
    _ensure_loaded()
    x, y = lng, lat   # GeoJSON/topojson coordinates are (lng, lat)
    kingdom = None

    cf = _feature_at(_counties, x, y)
    if cf is not None:
        kingdom = _terr.for_fips(str(cf["id"]).zfill(5))
    if kingdom is None:
        df = _feature_at(_canada, x, y)
        if df is not None:
            p = df["properties"]
            kingdom = _terr.for_cd(p.get("CDUID"), p.get("PRUID"))
    if kingdom is None:
        wf = _feature_at(_world, x, y)
        if wf is not None:
            p = wf["properties"]
            kingdom = (p.get("parent") or "").strip() or (p.get("kingdom") or "").strip() or None

    _point_cache[key] = kingdom
    return kingdom


if __name__ == "__main__":
    # Sanity check against the hosts the front-end already resolved live.
    CASES = [
        ("Pennsic (Coopers Lake, PA)", 40.9749376, -80.138691, "Kingdom of AEthelmearc"),
        ("Gulf Wars (Lumberton, MS)", 30.99, -89.45, "Kingdom of Gleann Abhann"),
        ("AnTir-West War (Gold Beach, OR)", 42.4074459, -124.4217418, "Kingdom of An Tir"),
        ("Potrero War (S. California)", 32.79573, -117.07963, "Kingdom of Caid"),
        ("War of the Trillium (Ontario)", 43.6, -79.7, "Kingdom of Ealdormere"),
    ]
    ok = True
    for name, lat, lng, expect in CASES:
        got = kingdom_at_point(lat, lng)
        flag = "OK " if got == expect else "XX "
        if got != expect:
            ok = False
        print(f"  {flag}{name}: {got}  (expected {expect})")
    print("ALL MATCH" if ok else "MISMATCH — investigate")
