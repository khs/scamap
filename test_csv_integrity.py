"""
test_csv_integrity.py
---------------------
Offline integrity tests for the two hand-edited CSVs the map rides on:

  * territory_kingdoms.csv -- drives the "Colour background by kingdom" overlay
    (state/county/province/country -> kingdom). A typo here silently mis-colours
    or greys a whole region, with no other signal.
  * locals.csv             -- the local-group registry + placeholder-pin coords.
    A duplicate group double-pins the map; a bad lat/lng flings a pin into the
    ocean.

No network, no pipeline. These lock in the currently-good data: every kingdom
referenced must exist in index.html's KINGDOM_COLORS, ids must be well-formed,
there are no duplicate keys, and coordinates are in range. A failure here is a
data typo, not a logic bug.

Run:
    python -m unittest test_csv_integrity -v
"""
from __future__ import annotations

import csv
import re
import unittest
from pathlib import Path

HERE = Path(__file__).parent
TERRITORY = HERE / "territory_kingdoms.csv"
LOCALS = HERE / "locals.csv"
INDEX = HERE / "index.html"


def _kingdom_colors() -> set:
    """Parse the KINGDOM_COLORS object literal out of index.html -> the set of
    kingdom/principality names the front-end knows how to colour."""
    text = INDEX.read_text(encoding="utf-8")
    m = re.search(r"const KINGDOM_COLORS\s*=\s*\{(.*?)\n\s*\};", text, re.S)
    assert m, "could not locate KINGDOM_COLORS object in index.html"
    names = set(re.findall(r'"([^"]+)"\s*:', m.group(1)))
    assert names, "parsed KINGDOM_COLORS but found no kingdom names"
    return names


VALID_KINGDOMS = _kingdom_colors()


def _rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class TestTerritoryKingdoms(unittest.TestCase):
    def setUp(self):
        self.rows = _rows(TERRITORY)

    def test_header_is_exact(self):
        with open(TERRITORY, encoding="utf-8", newline="") as f:
            self.assertEqual(f.readline().strip(), "type,id,name,kingdom")

    def test_type_is_valid(self):
        bad = sorted({r["type"] for r in self.rows
                      if r["type"] not in {"state", "county", "province", "country"}})
        self.assertEqual(bad, [], f"unknown row types: {bad}")

    def test_every_kingdom_is_known_to_the_map(self):
        unknown = sorted({r["kingdom"] for r in self.rows
                          if r["kingdom"] and r["kingdom"] not in VALID_KINGDOMS})
        self.assertEqual(unknown, [], f"kingdoms not in KINGDOM_COLORS: {unknown}")

    def test_ids_are_well_formed(self):
        bad = []
        for r in self.rows:
            t, i = r["type"], r["id"]
            ok = ((t == "state" and re.fullmatch(r"\d{2}", i)) or
                  (t == "county" and re.fullmatch(r"\d{5}", i)) or
                  (t == "province" and re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{2,3}", i)) or
                  (t == "country" and i.strip()))
            if not ok:
                bad.append((t, i))
        self.assertEqual(bad, [], f"malformed type/id rows: {bad}")

    def test_no_duplicate_keys(self):
        seen, dups = set(), []
        for r in self.rows:
            k = (r["type"], r["id"])
            dups.append(k) if k in seen else seen.add(k)
        self.assertEqual(dups, [], f"duplicate (type,id) rows: {dups}")

    def test_counties_sit_under_a_mapped_state(self):
        # A county FIPS is <2-digit state><3-digit county>; its state prefix
        # should be one we also list, so the override has a base to override.
        states = {r["id"] for r in self.rows if r["type"] == "state"}
        orphans = sorted({r["id"] for r in self.rows
                          if r["type"] == "county" and r["id"][:2] not in states})
        self.assertEqual(orphans, [], f"county FIPS with unmapped state prefix: {orphans}")


class TestLocals(unittest.TestCase):
    def setUp(self):
        self.rows = _rows(LOCALS)

    def test_required_columns_present(self):
        with open(LOCALS, encoding="utf-8", newline="") as f:
            header = set(next(csv.reader(f)))
        required = {"kingdom", "group", "type", "calendar_id", "website",
                    "social", "date_last_checked", "location", "lat", "lng"}
        self.assertTrue(required <= header, f"missing columns: {required - header}")

    def test_no_duplicate_groups(self):
        seen, dups = set(), []
        for r in self.rows:
            g = (r.get("group") or "").strip().lower()
            if not g:
                continue
            dups.append(g) if g in seen else seen.add(g)
        self.assertEqual(dups, [], f"duplicate group rows: {dups}")

    def test_kingdoms_are_known_when_present(self):
        unknown = sorted({(r.get("kingdom") or "").strip() for r in self.rows
                          if (r.get("kingdom") or "").strip()
                          and (r.get("kingdom") or "").strip() not in VALID_KINGDOMS})
        self.assertEqual(unknown, [], f"locals kingdoms not in KINGDOM_COLORS: {unknown}")

    def test_lat_lng_paired_and_in_range(self):
        bad = []
        for r in self.rows:
            lat, lng = (r.get("lat") or "").strip(), (r.get("lng") or "").strip()
            if not lat and not lng:
                continue
            try:
                la, lo = float(lat), float(lng)
            except ValueError:
                bad.append((r.get("group"), lat, lng))
                continue
            if not (-90 <= la <= 90 and -180 <= lo <= 180):
                bad.append((r.get("group"), lat, lng))
        self.assertEqual(bad, [], f"out-of-range or unpaired coordinates: {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
