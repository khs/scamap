"""
test_aggregator_private.py
--------------------------
Offline tests for two clean-up mechanisms:

  * drop_aggregator_duplicates — events from a locals.csv type "aggregator"
    calendar are dropped when a group's own calendar already lists them (same
    day, similar title, compatible venue) and kept when they add something new.
  * private_addresses — blocklisted addresses are scrubbed from event text and
    from every *_cache.json, with the events pinned at the public spot.

Run:
    python -m unittest test_aggregator_private -v
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import clean_sca_events as clean
import private_addresses as pa

COLS = ["title", "start", "clean_location", "source", "is_aggregator"]


def _df(rows):
    return pd.DataFrame([dict(zip(COLS, r)) for r in rows], columns=COLS)


class TestAggregatorDuplicates(unittest.TestCase):
    def test_copy_of_group_event_dropped(self):
        out = clean.drop_aggregator_duplicates(_df([
            ("CAM Fighters Practice (Armored and Rapier) (RECURRING)", "2026-09-28 18:30:00",
             "The Nest", "Barony of Caer Anterth Mawr", "False"),
            ("CAM Fighters Practice (Armored and Rapier)", "2026-09-28 18:30:00",
             "The Nest", "SE Wisconsin Armored Combat", "True"),
        ]))
        self.assertEqual(list(out["source"]), ["Barony of Caer Anterth Mawr"])
        self.assertNotIn("is_aggregator", out.columns)

    def test_new_information_kept(self):
        # Jararvellir's own feed has Archery at this time/place; the aggregator's
        # armoured practice is extra information and must survive.
        out = clean.drop_aggregator_duplicates(_df([
            ("Archery Practice (RECURRING)", "2026-09-27 12:00:00",
             "1675 Linden Dr, Madison, WI 53706, USA", "Barony of Jararvellir", "False"),
            ("Jara Fighters Practice", "2026-09-27 12:00:00",
             "1675 Linden Dr, Madison, WI 53706, USA", "SE Wisconsin Armored Combat", "True"),
        ]))
        self.assertEqual(len(out), 2)

    def test_shared_group_prefix_alone_is_not_a_match(self):
        out = clean.drop_aggregator_duplicates(_df([
            ("CAM Rapier and Armored Practice (RECURRING)", "2026-10-15 18:30:00",
             "900 S 119th St, West Allis, WI", "Barony of Caer Anterth Mawr", "False"),
            ("CAM Youth Practice", "2026-10-15 18:30:00",
             "900 S 119th St, West Allis, WI", "SE Wisconsin Armored Combat", "True"),
        ]))
        self.assertEqual(len(out), 2)

    def test_different_day_kept(self):
        out = clean.drop_aggregator_duplicates(_df([
            ("CAM Fighters Practice", "2026-09-28", "The Nest", "Barony X", "False"),
            ("CAM Fighters Practice", "2026-09-29", "The Nest", "Agg", "True"),
        ]))
        self.assertEqual(len(out), 2)

    def test_different_venue_kept(self):
        out = clean.drop_aggregator_duplicates(_df([
            ("Fighter Practice", "2026-09-28", "12 Oak St, Town A", "Barony X", "False"),
            ("Fighter Practice", "2026-09-28", "900 Elm Rd, Town B", "Agg", "True"),
        ]))
        self.assertEqual(len(out), 2)

    def test_no_aggregators_just_drops_column(self):
        out = clean.drop_aggregator_duplicates(_df([("X", "2026-09-28", "", "B", "False")]))
        self.assertEqual(len(out), 1)
        self.assertNotIn("is_aggregator", out.columns)


class TestPrivateAddresses(unittest.TestCase):
    # A made-up address: tests must never contain a real blocklisted one.
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "private_addresses.csv"
        pa.add("The Nest", "Sampletown, WI", "42.5", "-88.5", "", path=self.csv)
        pa.add("N12W3456 Example Rd", "Sampletown, WI", "42.5", "-88.5", "", path=self.csv)
        self.rows = pa.load(self.csv)

    def test_file_holds_no_address(self):
        raw = self.csv.read_text(encoding="utf-8").lower()
        for word in ("nest", "3456", "example"):
            self.assertNotIn(word, raw)

    def test_text_scrubbed_and_pinned(self):
        df = pd.DataFrame([{
            "title": "Summer Camp", "location": "The Nest, N12 W3456 example rd.",
            "clean_location": "N12W3456 Example Rd, Sampletown, WI 00000",
            "description": "Address The Nest N12W3456 Example Rd Sampletown, WI",
            "lat": "42.9", "lng": "-88.2", "geocode_status": "ok"}])
        r = pa.apply_to_events(df, self.rows).iloc[0]
        for col in ("location", "clean_location", "description"):
            self.assertNotIn("3456", r[col])
            self.assertNotIn("nest", r[col].lower())
        self.assertEqual((r["lat"], r["lng"], r["geocode_status"]), ("42.5", "-88.5", "override"))
        self.assertEqual(r["description"].count("Sampletown, WI"), 1)   # collapsed

    def test_similar_words_untouched(self):
        for s in ("Bathe Nesters daily", "The Nesting Ground"):
            self.assertEqual(pa.scrub_text(s, self.rows), (s, None))
        # Only a whole-word run matches: "Example Rdx" is not the address.
        self.assertIsNone(pa.scrub_text("N12W3456 Example Rdx", self.rows)[1])

    def test_replacement_containing_text_is_refused(self):
        bad = self.tmp / "bad.csv"
        pa.add("The Nest", "The Nest (private)", "1", "2", path=bad)
        self.assertEqual(pa.load(bad)[0]["replace_with"], "[address withheld]")

    def test_caches_scrubbed_keys_dropped_format_kept(self):
        geo = {"nom:The Nest, N12W3456 Example Rd, Sampletown||us|0": {"lat": None},
               "nom:Madison, WI||us|0": {"lat": 43.0}}
        raw = json.dumps(geo, separators=(",", ":"), sort_keys=True)
        (self.tmp / "geocode_cache.json").write_text(raw, encoding="utf-8")
        desc = {"https://x/1": {"text": "Address The Nest N12W3456 Example Rd"}}
        (self.tmp / "desc_cache.json").write_text(
            json.dumps(desc, indent=0, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        pa.scrub_files(self.rows, self.tmp)
        g = (self.tmp / "geocode_cache.json").read_text(encoding="utf-8")
        self.assertEqual(g, json.dumps({"nom:Madison, WI||us|0": {"lat": 43.0}},
                                       separators=(",", ":"), sort_keys=True))
        d = json.loads((self.tmp / "desc_cache.json").read_text(encoding="utf-8"))
        self.assertNotIn("3456", d["https://x/1"]["text"])


class TestCommittedPrivateFile(unittest.TestCase):
    def test_rows_load_with_public_pins(self):
        rows = pa.load()
        self.assertTrue(rows)
        for r in rows:
            self.assertIsNotNone(r["coords"], f"{r['fingerprint'][:8]} needs a public lat/lng")


if __name__ == "__main__":
    unittest.main(verbosity=2)
