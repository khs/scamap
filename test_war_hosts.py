"""
test_war_hosts.py
-----------------
Offline tests for the build-time war-host tagging (annotate_war_hosts.is_war +
kingdom_geo.kingdom_at_point) that feeds the map's oversized "hosting kingdom"
war pins. No network — rides on the committed topojson + territory_kingdoms.csv.

Run:
    python -m unittest test_war_hosts -v
"""
from __future__ import annotations

import unittest

import annotate_war_hosts as awh
from kingdom_geo import kingdom_at_point


class TestIsWar(unittest.TestCase):
    def test_multiday_kingdom_war(self):
        self.assertTrue(awh.is_war("Pennsic War 53",
                                    "2026-07-24 00:00:00", "2026-08-09 00:00:00", "kingdom"))

    def test_wars_plural_counts(self):
        self.assertTrue(awh.is_war("Gulf Wars XXXV", "2027-03-13", "2027-03-22", "kingdom"))

    def test_exactly_five_days_included(self):
        self.assertTrue(awh.is_war("Foo War", "2026-06-30 00:00:00", "2026-07-05 00:00:00", "kingdom"))

    def test_four_days_excluded(self):
        self.assertFalse(awh.is_war("Foo War", "2026-06-30 00:00:00", "2026-07-04 00:00:00", "kingdom"))

    def test_short_war_practice_excluded(self):
        self.assertFalse(awh.is_war("War Practice", "2026-07-01 18:00:00", "2026-07-01 21:00:00", "kingdom"))

    def test_baronial_excluded(self):
        # A barony's multi-day "war" is not a hosting-kingdom war pin.
        self.assertFalse(awh.is_war("Baronial War", "2026-07-01", "2026-07-10", "baronial"))

    def test_word_boundary_no_false_positive(self):
        # "Warlord"/"Steward"/"Warcamp" contain "war" but aren't the word.
        self.assertFalse(awh.is_war("Fabric Warlord", "2026-07-01", "2026-07-10", "kingdom"))
        self.assertFalse(awh.is_war("Northern Region Warcamp", "2026-07-01", "2026-07-10", "kingdom"))

    def test_missing_dates_excluded(self):
        self.assertFalse(awh.is_war("Some War", "", "", "kingdom"))


class TestKingdomAtPoint(unittest.TestCase):
    """The point-in-territory lookup must match what the overlay would paint —
    including the county-level splits (PA, CA) and the Canada / world layers."""

    def test_known_war_hosts(self):
        cases = [
            (40.9749376, -80.138691, "Kingdom of AEthelmearc"),   # Pennsic, western PA (county split)
            (30.99, -89.45, "Kingdom of Gleann Abhann"),           # Gulf Wars, MS
            (42.4074459, -124.4217418, "Kingdom of An Tir"),       # AnTir-West War, OR
            (32.79573, -117.07963, "Kingdom of Caid"),             # Potrero, southern CA (county split)
        ]
        for lat, lng, expect in cases:
            self.assertEqual(kingdom_at_point(lat, lng), expect, f"({lat},{lng})")

    def test_off_map_point_is_none(self):
        # Gulf of Guinea — in no mapped territory.
        self.assertIsNone(kingdom_at_point(0.0, 0.0))

    def test_war_host_for_row_roundtrip(self):
        row = {"title": "Pennsic War 53", "start": "2026-07-24 00:00:00",
               "end": "2026-08-09 00:00:00", "calendar_type": "kingdom",
               "lat": "40.9749376", "lng": "-80.138691"}
        self.assertEqual(awh.war_host_for(row), "Kingdom of AEthelmearc")

    def test_war_host_for_non_war_is_blank(self):
        row = {"title": "Coronation", "start": "2026-07-24", "end": "2026-07-25",
               "calendar_type": "kingdom", "lat": "40.97", "lng": "-80.14"}
        self.assertEqual(awh.war_host_for(row), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
