"""
test_wars.py
------------
Offline tests for clean_sca_events.apply_wars (wars.csv): each big war's kingdom
listings collapse into one event at its permanent site with its own website,
dates taken from the calendars; a war no calendar lists gets a flagged
placeholder over its usual date range. Also guards the committed wars.csv.

Run:
    python -m unittest test_wars -v
"""
from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

import clean_sca_events as clean
import geocode_sca_events as geo

COLS = ["title", "start", "end", "location", "clean_location",
        "address_confidence", "description", "event_url", "facebook_url",
        "source", "calendar_type", "is_virtual", "lat", "lng",
        "geocode_status", "location_specificity"]
WAR_FIELDS = ["name", "title_keywords", "host_kingdom", "location", "lat", "lng",
              "website", "fallback_start", "fallback_end"]
GULF = {"name": "Gulf Wars", "title_keywords": "gulf wars",
        "host_kingdom": "Kingdom of Gleann Abhann", "location": "King's Arrow Ranch",
        "lat": "30.91", "lng": "-89.45", "website": "https://gulfwars.org/",
        "fallback_start": "", "fallback_end": ""}
LILIES = {"name": "Lilies War", "title_keywords": "lilies war|war of the lilies",
          "host_kingdom": "Kingdom of Calontir", "location": "Kelsey Short Youth Camp",
          "lat": "39.41", "lng": "-94.50", "website": "https://www.lilieswar.org/",
          "fallback_start": "06-01", "fallback_end": "06-30"}


def _ev(**over):
    r = {c: "" for c in COLS}
    r.update(calendar_type="kingdom", is_virtual="False", geocode_status="ok",
             lat="1", lng="2")
    r.update(over)
    return r


class TestApplyWars(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patch = mock.patch.object(clean, "SCRIPT_DIR", self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wars(self, *rows):
        with open(self.tmp / "wars.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=WAR_FIELDS)
            w.writeheader()
            w.writerows(rows)

    def _run(self, rows, today=date(2026, 9, 23)):
        return clean.apply_wars(pd.DataFrame(rows, columns=COLS), today=today)

    def test_listings_collapse_to_one_event_at_the_site(self):
        self._wars(GULF)
        out = self._run([
            _ev(title="Gulf Wars XXXV", source="Kingdom of Trimaris",
                start="2027-03-13 00:00:00", end="2027-03-21 23:59:59",
                clean_location="Luberton MS", event_url="https://trimaris.org/x"),
            _ev(title="Gulf Wars (Out of Kingdom)", source="Kingdom of Caid",
                start="2027-03-13", end="2027-03-22", description="A longer write-up"),
            _ev(title="Spring Crown", source="Kingdom of Caid",
                start="2027-04-01", end="2027-04-03"),
        ])
        wars = out[out["title"].str.contains("Gulf")]
        self.assertEqual(len(wars), 1)
        w = wars.iloc[0]
        self.assertEqual((w["lat"], w["lng"], w["geocode_status"]),
                         ("30.91", "-89.45", "override"))
        self.assertEqual(w["event_url"], "https://gulfwars.org/")
        self.assertEqual(w["clean_location"], "King's Arrow Ranch")
        self.assertEqual(w["source"], "Kingdom of Gleann Abhann")
        self.assertEqual(w["description"], "A longer write-up")
        self.assertEqual(w["dates_approximate"], "")
        self.assertIn("Spring Crown", set(out["title"]))

    def test_host_listing_dates_win(self):
        self._wars(GULF)
        out = self._run([
            _ev(title="Gulf Wars", source="Kingdom of Caid",
                start="2027-03-12", end="2027-03-22"),
            _ev(title="Gulf Wars", source="Kingdom of Atenveldt",
                start="2027-03-12", end="2027-03-22"),
            _ev(title="Gulf Wars XXXV", source="Kingdom of Gleann Abhann",
                start="2027-03-13 08:00:00", end="2027-03-21 10:00:00"),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["start"], "2027-03-13 08:00:00")
        self.assertEqual(out.iloc[0]["title"], "Gulf Wars XXXV")

    def test_majority_dates_win_without_host(self):
        self._wars(GULF)
        out = self._run([
            _ev(title="Gulf Wars", source="A", start="2027-03-10", end="2027-03-20"),
            _ev(title="Gulf Wars", source="B", start="2027-03-13", end="2027-03-21"),
            _ev(title="Gulf Wars", source="C", start="2027-03-13", end="2027-03-21"),
        ])
        self.assertEqual(out.iloc[0]["start"], "2027-03-13")

    def test_court_and_baronial_listings_untouched(self):
        self._wars(GULF)
        out = self._run([
            _ev(title="Gulf Wars Court", source="Kingdom of Artemisia",
                start="2027-03-18", end="2027-03-19", lat="5", lng="6"),
            _ev(title="Gulf Wars prep practice", calendar_type="baronial",
                source="Barony X", start="2027-03-01", end="2027-03-10"),
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual(set(out["geocode_status"]), {"ok"})

    def test_each_year_is_its_own_event(self):
        self._wars(GULF)
        out = self._run([
            _ev(title="Gulf Wars XXXV", source="A", start="2027-03-13", end="2027-03-21"),
            _ev(title="Gulf Wars XXXVI", source="A", start="2028-03-11", end="2028-03-19"),
        ])
        self.assertEqual(sorted(out["start"]), ["2027-03-13", "2028-03-11"])

    def test_fallback_placeholder_when_unlisted(self):
        self._wars(LILIES)
        out = self._run([], today=date(2026, 9, 23))   # June 2026 already past
        self.assertEqual(len(out), 1)
        p = out.iloc[0]
        self.assertEqual((p["title"], p["start"], p["end"]),
                         ("Lilies War", "2027-06-01", "2027-06-30"))
        self.assertEqual(p["dates_approximate"], "True")
        self.assertEqual(p["event_url"], "https://www.lilieswar.org/")
        self.assertEqual(p["geocode_status"], "override")

    def test_real_listing_replaces_placeholder(self):
        self._wars(LILIES)
        out = self._run([_ev(title="Lilies War XXXIX", source="Kingdom of Calontir",
                             start="2027-06-04", end="2027-06-13")],
                        today=date(2027, 1, 5))
        # 2027 comes from the calendar; 2028 (in the window) is a placeholder.
        got = sorted(zip(out["start"], out["dates_approximate"]))
        self.assertEqual(got, [("2027-06-04", ""), ("2028-06-01", "True")])

    def test_old_placeholder_is_regenerated_not_kept(self):
        # A placeholder carried forward from the last run must not be treated as
        # a real listing (or duplicated).
        self._wars(LILIES)
        prior = _ev(title="Lilies War", source="Kingdom of Calontir",
                    start="2027-06-01", end="2027-06-30")
        df = pd.DataFrame([prior], columns=COLS)
        df["dates_approximate"] = "True"
        out = clean.apply_wars(df, today=date(2026, 9, 23))
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["dates_approximate"], "True")

    def test_no_wars_file_is_noop(self):
        out = self._run([_ev(title="Gulf Wars", start="2027-03-13", end="2027-03-21")])
        self.assertEqual(out.iloc[0]["geocode_status"], "ok")


class TestCommittedWarsFile(unittest.TestCase):
    def test_rows_load_with_valid_fields(self):
        wars = clean._load_wars()
        self.assertTrue(wars, "wars.csv missing or empty")
        for w in wars:
            self.assertTrue(w["website"].startswith("https://"), w["name"])
            for md in (w["fallback_start"], w["fallback_end"]):
                if md:
                    date.fromisoformat(f"2027-{md}")   # raises if not MM-DD


class TestOnlineOnlyLocation(unittest.TestCase):
    def test_online_only(self):
        for loc in ("Zoom", "Online via Zoom", "Online", "Virtual meeting on Discord",
                    "Google Meet"):
            self.assertTrue(geo.is_online_only_location(loc), loc)

    def test_real_places_kept(self):
        for loc in ("Manteca, CA & Zoom", "123 Main St, Online WA", "Zion Park",
                    "", "Community Center (also on Zoom), Tulsa OK"):
            self.assertFalse(geo.is_online_only_location(loc), loc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
