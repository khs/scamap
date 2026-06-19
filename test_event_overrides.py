"""
test_event_overrides.py
-----------------------
Offline tests for clean_sca_events' hand-maintained event override mechanism
(event_overrides.csv → apply_event_overrides). Covers location-independent
matching that survives upstream edits, and the two correction modes
(re-geocode a fixed address vs. pin exact coordinates).

Run:
    python -m unittest test_event_overrides -v
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import clean_sca_events as clean

COLS = ["title", "start", "end", "location", "clean_location",
        "address_confidence", "description", "event_url", "facebook_url",
        "source", "calendar_type", "is_virtual", "lat", "lng", "geocode_status"]


def _row(**over):
    r = {c: "" for c in COLS}
    r.update(calendar_type="kingdom", geocode_status="ok", lat="1.0", lng="2.0")
    r.update(over)
    return r


class TestOverrideMatching(unittest.TestCase):
    def _ov(self, **kw):
        base = {c: "" for c in clean.OVERRIDE_COLUMNS}
        base.update(kw)
        return base

    def test_url_match_wins(self):
        ov = self._ov(match_event_url="https://x.org/e/1")
        self.assertTrue(clean._override_matches(ov, _row(event_url="https://x.org/e/1")))
        self.assertFalse(clean._override_matches(ov, _row(event_url="https://x.org/e/2")))

    def test_url_match_ignores_title_and_location(self):
        # The whole point: a URL override survives title/location edits.
        ov = self._ov(match_event_url="https://x.org/e/1")
        ev = _row(event_url="https://x.org/e/1", title="Totally Renamed",
                  clean_location="somewhere else")
        self.assertTrue(clean._override_matches(ov, ev))

    def test_source_plus_title_match_is_normalized(self):
        ov = self._ov(match_source="Kingdom of Meridies", match_title="Smurf Shoot 4")
        # punctuation / case differences still match
        self.assertTrue(clean._override_matches(
            ov, _row(source="Kingdom of Meridies", title="SMURF  shoot #4")))
        # wrong kingdom does not
        self.assertFalse(clean._override_matches(
            ov, _row(source="Kingdom of Calontir", title="Smurf Shoot 4")))

    def test_date_pins_one_instance(self):
        ov = self._ov(match_source="Kingdom of Caid", match_title="Coronation",
                      match_date="2026-05-02")
        self.assertTrue(clean._override_matches(
            ov, _row(source="Kingdom of Caid", title="Coronation",
                     start="2026-05-02 09:00:00")))
        self.assertFalse(clean._override_matches(
            ov, _row(source="Kingdom of Caid", title="Coronation",
                     start="2027-05-01 09:00:00")))


class TestApplyOverrides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.f = Path(self.tmp) / "event_overrides.csv"
        self._orig = clean.OVERRIDES_FILE
        clean.OVERRIDES_FILE = self.f

    def tearDown(self):
        clean.OVERRIDES_FILE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, body: str):
        header = ",".join(clean.OVERRIDE_COLUMNS) + "\n"
        self.f.write_text(header + body, encoding="utf-8")

    def test_new_location_replaces_and_forces_regeocode(self):
        self._write(',Kingdom of Meridies,Vague Fest,,"Savannah, GA, USA",,,fix\n')
        df = pd.DataFrame([_row(source="Kingdom of Meridies", title="Vague Fest",
                                clean_location="Greater Savannah area",
                                location="Greater Savannah area",
                                lat="9.9", lng="9.9", geocode_status="ok")])
        out = clean.apply_event_overrides(df)
        r = out.iloc[0]
        self.assertEqual(r["clean_location"], "Savannah, GA, USA")
        self.assertEqual(r["location"], "Savannah, GA, USA")
        self.assertEqual(r["address_confidence"], "high")
        self.assertEqual((r["lat"], r["lng"], r["geocode_status"]), ("", "", ""))

    def test_latlng_pins_and_skips_geocoder(self):
        self._write("https://x.org/e/1,,,,,41.88,-87.63,pin\n")
        df = pd.DataFrame([_row(event_url="https://x.org/e/1",
                                clean_location="the cabbage patch")])
        out = clean.apply_event_overrides(df)
        r = out.iloc[0]
        self.assertEqual((r["lat"], r["lng"]), ("41.88", "-87.63"))
        self.assertEqual(r["geocode_status"], "override")
        # location text left alone when only coords are given
        self.assertEqual(r["clean_location"], "the cabbage patch")

    def test_location_and_pin_together(self):
        self._write('https://x.org/e/1,,,,"Real Site, PA",40.1,-75.2,both\n')
        df = pd.DataFrame([_row(event_url="https://x.org/e/1")])
        r = clean.apply_event_overrides(df).iloc[0]
        self.assertEqual(r["clean_location"], "Real Site, PA")
        self.assertEqual((r["lat"], r["lng"], r["geocode_status"]),
                         ("40.1", "-75.2", "override"))

    def test_non_matching_rows_untouched(self):
        self._write("https://x.org/e/1,,,,,41.88,-87.63,pin\n")
        df = pd.DataFrame([
            _row(event_url="https://x.org/e/1", title="Target"),
            _row(event_url="https://x.org/e/OTHER", title="Bystander",
                 lat="5.5", lng="6.6", geocode_status="ok"),
        ])
        out = clean.apply_event_overrides(df)
        bystander = out[out["title"] == "Bystander"].iloc[0]
        self.assertEqual((bystander["lat"], bystander["lng"]), ("5.5", "6.6"))

    def test_comment_and_keyless_rows_skipped(self):
        self._write("# this is a comment row,,,,,1,2,note\n"
                    ",,,,Nowhere,,,no match key at all\n")   # no url/source/title
        self.assertEqual(clean._load_overrides(), [])

    def test_missing_file_is_noop(self):
        clean.OVERRIDES_FILE = Path(self.tmp) / "does_not_exist.csv"
        df = pd.DataFrame([_row(title="X")])
        out = clean.apply_event_overrides(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["title"], "X")

    # -- sanity / safety of bad input ------------------------------------
    def _target(self):
        return pd.DataFrame([_row(event_url="https://x.org/e/1",
                                  clean_location="vague", lat="5.5", lng="6.6",
                                  geocode_status="ok")])

    def test_non_numeric_coords_ignored(self):
        self._write("https://x.org/e/1,,,,,not-a-number,also-bad,typo\n")
        r = clean.apply_event_overrides(self._target()).iloc[0]
        self.assertEqual((r["lat"], r["lng"], r["geocode_status"]),
                         ("5.5", "6.6", "ok"))            # garbage pin not placed

    def test_out_of_range_coords_ignored(self):
        self._write("https://x.org/e/1,,,,,418.8,-87.63,lat out of range\n")
        r = clean.apply_event_overrides(self._target()).iloc[0]
        self.assertEqual((r["lat"], r["lng"]), ("5.5", "6.6"))

    def test_half_filled_coords_ignored(self):
        self._write("https://x.org/e/1,,,,,41.88,,lat only no lng\n")
        r = clean.apply_event_overrides(self._target()).iloc[0]
        self.assertEqual((r["lat"], r["lng"], r["geocode_status"]),
                         ("5.5", "6.6", "ok"))

    def test_override_with_no_change_is_skipped(self):
        self._write("https://x.org/e/1,,,,,,,just a note, no change\n")
        r = clean.apply_event_overrides(self._target()).iloc[0]
        self.assertEqual((r["lat"], r["lng"], r["geocode_status"]),
                         ("5.5", "6.6", "ok"))

    def test_bad_pin_but_location_still_applies(self):
        # A bad pin must not block a valid location correction; the location is
        # taken and the event is re-geocoded (coords cleared).
        self._write('https://x.org/e/1,,,,"Real City, PA",badlat,badlng,loc good pin bad\n')
        r = clean.apply_event_overrides(self._target()).iloc[0]
        self.assertEqual(r["clean_location"], "Real City, PA")
        self.assertEqual((r["lat"], r["lng"], r["geocode_status"]), ("", "", ""))

    def test_valid_coord_bounds_helper(self):
        self.assertIsNotNone(clean._valid_override_coords("89.9", "-179.9"))
        self.assertIsNone(clean._valid_override_coords("90.1", "0"))     # lat too big
        self.assertIsNone(clean._valid_override_coords("0", "181"))      # lng too big
        self.assertIsNone(clean._valid_override_coords("x", "y"))        # non-numeric
        self.assertIsNone(clean._valid_override_coords("12.3", ""))      # incomplete


class TestCommittedOverridesFile(unittest.TestCase):
    """Guards on the real, committed event_overrides.csv so it can't drift from
    the code or ship an accidentally-live override."""

    def test_header_matches_code_columns(self):
        import csv as _csv
        with open(clean.OVERRIDES_FILE, encoding="utf-8", newline="") as f:
            header = next(_csv.reader(f))
        self.assertEqual(header, clean.OVERRIDE_COLUMNS)

    def test_no_active_overrides_committed(self):
        # Every example in the shipped file must stay commented out, so a fresh
        # checkout applies zero corrections until a human adds a real one.
        self.assertEqual(clean._load_overrides(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
