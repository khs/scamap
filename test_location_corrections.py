"""
test_location_corrections.py
----------------------------
Offline tests for the location-fixing logic added to clean_sca_events:

  * is_cancelled        -- "cancelled" anywhere in the title, or only in the first
                           two words of the description (not buried in a sentence).
  * _looks_virtual      -- the virtual/online guard for the baronial fallback.
  * apply_location_corrections -- location_corrections.csv: per-(source, title
                           keyword) exact-coordinate pins for events on calendars
                           we otherwise auto-import in full.
  * apply_baronial_coords -- baronial no-address events flagged "vague" get pinned
                           at their barony's own ("?"-pin) coordinates.

These guard against silently mis-pinning (or dropping) events. No network.

Run:
    python -m unittest test_location_corrections -v
"""
from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import clean_sca_events as clean

COLS = ["title", "start", "end", "location", "clean_location",
        "address_confidence", "description", "source", "calendar_type",
        "is_virtual", "lat", "lng", "geocode_status", "location_specificity"]


def _df(rows):
    out = []
    for r in rows:
        base = {c: "" for c in COLS}
        base.update(calendar_type="baronial")
        base.update(r)
        out.append(base)
    return pd.DataFrame(out, columns=COLS)


class TestIsCancelled(unittest.TestCase):
    def test_title_anywhere(self):
        self.assertTrue(clean.is_cancelled("CANCELLED: Fighter Practice"))
        self.assertTrue(clean.is_cancelled("Fighter Practice - CANCELED"))
        self.assertTrue(clean.is_cancelled("Practice (cancelled this week)"))

    def test_description_leading_words(self):
        self.assertTrue(clean.is_cancelled("Practice", "Cancelled - icy roads"))
        self.assertTrue(clean.is_cancelled("Practice", "CANCELED this week"))

    def test_description_sentence_does_not_count(self):
        # The whole point: a "cancelled" deep in a sentence must NOT drop the event.
        self.assertFalse(clean.is_cancelled(
            "Heavy Practice",
            "We will move to the park if the gym booking is cancelled this month"))

    def test_non_cancellation_words_ignored(self):
        self.assertFalse(clean.is_cancelled("Cancellation Policy Workshop"))
        self.assertFalse(clean.is_cancelled("Fighter Practice", "All are welcome!"))

    def test_one_arg_still_works(self):
        # Back-compat: callers passing only a title must keep working.
        self.assertFalse(clean.is_cancelled("Regular Practice"))


class TestLooksVirtual(unittest.TestCase):
    def test_keywords_and_links(self):
        self.assertTrue(clean._looks_virtual("A&S Online Class", ""))
        self.assertTrue(clean._looks_virtual("Populace (Virtual)", ""))
        self.assertTrue(clean._looks_virtual("Meeting", "Join: https://us02web.zoom.us/j/1"))
        self.assertTrue(clean._looks_virtual("Class", "meet.google.com/abc-defg-hij"))

    def test_in_person_not_flagged(self):
        self.assertFalse(clean._looks_virtual("Fighter Practice",
                                              "In person at the park, all welcome"))

    def test_handles_non_strings(self):
        self.assertFalse(clean._looks_virtual(float("nan"), None))


class _TmpDirMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patch = mock.patch.object(clean, "SCRIPT_DIR", self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_csv(self, name, fieldnames, rows):
        with open(self.tmp / name, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)


class TestLocationCorrections(_TmpDirMixin):
    def _corrections(self, rows):
        self._write_csv("location_corrections.csv",
                        ["source", "keywords", "lat", "lng"], rows)

    def test_keyword_match_pins_and_marks_override(self):
        self._corrections([
            {"source": "Province of the Mists", "keywords": "Rockridge Bart",
             "lat": "37.844514", "lng": "-122.252972"},
        ])
        df = _df([{"title": "Rockridge BART Fighter Practice (Oakland)",
                   "source": "Province of the Mists", "lat": "37.80", "lng": "-122.27",
                   "geocode_status": "ok_retry"}])
        out = clean.apply_location_corrections(df)
        self.assertAlmostEqual(float(out.iloc[0]["lat"]), 37.844514, places=5)
        self.assertAlmostEqual(float(out.iloc[0]["lng"]), -122.252972, places=5)
        self.assertEqual(out.iloc[0]["geocode_status"], "override")

    def test_keyword_is_substring_and_case_insensitive(self):
        self._corrections([{"source": "Barony of Bright Hills", "keywords": "practice",
                            "lat": "39.416485", "lng": "-76.505546"}])
        df = _df([{"title": "Archery PRACTICE", "source": "Barony of Bright Hills"}])
        out = clean.apply_location_corrections(df)
        self.assertAlmostEqual(float(out.iloc[0]["lat"]), 39.416485, places=5)

    def test_keyword_matches_location_field(self):
        # The venue is named in the LOCATION field, not the title — the correction
        # must still fire (the Caer Anterth Mawr "the roost" case).
        self._corrections([{"source": "Barony of Caer Anterth Mawr", "keywords": "roost",
                            "lat": "42.887382", "lng": "-88.286"}])
        df = _df([{"title": "Thursday Fighter Practice",
                   "source": "Barony of Caer Anterth Mawr",
                   "location": "The Roost, 123 Main St, Town WI",
                   "clean_location": "123 Main St, Town WI"}])
        out = clean.apply_location_corrections(df)
        self.assertAlmostEqual(float(out.iloc[0]["lat"]), 42.887382, places=5)
        self.assertEqual(out.iloc[0]["geocode_status"], "override")

    def test_clears_vague_flag_on_precise_fix(self):
        self._corrections([{"source": "Barony of Bright Hills", "keywords": "practice",
                            "lat": "39.416485", "lng": "-76.505546"}])
        df = _df([{"title": "Archery Practice", "source": "Barony of Bright Hills",
                   "location_specificity": "vague"}])
        out = clean.apply_location_corrections(df)
        self.assertEqual(out.iloc[0]["location_specificity"], "")

    def test_wrong_source_untouched(self):
        self._corrections([{"source": "Province of the Mists", "keywords": "Rockridge",
                            "lat": "37.8", "lng": "-122.2"}])
        df = _df([{"title": "Rockridge Picnic", "source": "Some Other Barony",
                   "geocode_status": "ok", "lat": "1", "lng": "2"}])
        out = clean.apply_location_corrections(df)
        self.assertEqual(out.iloc[0]["geocode_status"], "ok")
        self.assertEqual(out.iloc[0]["lat"], "1")

    def test_missing_file_is_noop(self):
        df = _df([{"title": "X", "source": "Y", "geocode_status": "ok"}])
        out = clean.apply_location_corrections(df)   # no CSV written
        self.assertEqual(out.iloc[0]["geocode_status"], "ok")

    def test_bad_coords_skipped(self):
        self._corrections([{"source": "B", "keywords": "k", "lat": "999", "lng": "0"}])
        df = _df([{"title": "k event", "source": "B", "geocode_status": "ok"}])
        out = clean.apply_location_corrections(df)
        self.assertEqual(out.iloc[0]["geocode_status"], "ok")   # invalid -> not applied


class TestBaronialCoords(_TmpDirMixin):
    def _locals(self, rows):
        self._write_csv("locals.csv",
                        ["kingdom", "group", "type", "calendar_id", "website",
                         "social", "date_last_checked", "location", "lat", "lng"], rows)

    def test_vague_event_pinned_at_barony_coords(self):
        self._locals([{"group": "Province of the Mists", "location": "Oakland, California",
                       "lat": "37.804456", "lng": "-122.271356"}])
        df = _df([{"title": "Newcomers Night", "source": "Province of the Mists",
                   "location_specificity": "vague"}])
        out = clean.apply_baronial_coords(df)
        self.assertEqual(out.iloc[0]["geocode_status"], "ok_fallback")
        self.assertAlmostEqual(float(out.iloc[0]["lat"]), 37.804456, places=5)
        self.assertEqual(out.iloc[0]["clean_location"], "Oakland, California")

    def test_override_not_second_guessed(self):
        self._locals([{"group": "Province of the Mists", "location": "Oakland",
                       "lat": "37.8", "lng": "-122.2"}])
        df = _df([{"title": "Fixed", "source": "Province of the Mists",
                   "location_specificity": "vague", "geocode_status": "override",
                   "lat": "1.0", "lng": "2.0"}])
        out = clean.apply_baronial_coords(df)
        self.assertEqual(out.iloc[0]["geocode_status"], "override")
        self.assertEqual(out.iloc[0]["lat"], "1.0")

    def test_unknown_barony_drops_flag(self):
        self._locals([{"group": "Province of the Mists", "location": "Oakland",
                       "lat": "37.8", "lng": "-122.2"}])
        df = _df([{"title": "Mystery", "source": "Unlisted Barony",
                   "location_specificity": "vague"}])
        out = clean.apply_baronial_coords(df)
        self.assertEqual(out.iloc[0]["location_specificity"], "")   # can't claim a spot
        self.assertEqual(out.iloc[0]["geocode_status"], "")

    def test_non_vague_untouched(self):
        self._locals([{"group": "Province of the Mists", "location": "Oakland",
                       "lat": "37.8", "lng": "-122.2"}])
        df = _df([{"title": "Real Event", "source": "Province of the Mists",
                   "geocode_status": "ok", "lat": "5", "lng": "6"}])
        out = clean.apply_baronial_coords(df)
        self.assertEqual(out.iloc[0]["geocode_status"], "ok")
        self.assertEqual(out.iloc[0]["lat"], "5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
