"""
test_published_coords.py
------------------------
Offline tests for extract_published_coords() — preferring coordinates a calendar
already published (in the location OR description text) over geocoding the venue
name. Covers the Google-mangled "lat #lng" form Teufelberg's West feed emits,
cardinal-direction pairs, plain high-precision decimal pairs, the "GPS:" suffix,
and — critically — NOT mistaking a street address, time, price, or phone number
for a coordinate pair.

Run:
    python -m unittest test_published_coords -v
"""
from __future__ import annotations

import unittest

import geocode_sca_events as g


class TestExtractPublishedCoords(unittest.TestCase):
    def test_google_mangled_teufelberg(self):
        loc = ("Emerald Glen Arts and Fighter Practice, "
               "37 71232046276675 #121.87544721303145, Dublin, CA 94568, USA")
        lat, lng = g.extract_published_coords(loc)
        self.assertAlmostEqual(lat, 37.71232046276675, places=6)
        self.assertAlmostEqual(lng, -121.87544721303145, places=6)

    def test_mangled_defaults_western_without_source(self):
        lat, lng = g.extract_published_coords("47 6062 #122.3321")
        self.assertAlmostEqual(lat, 47.6062, places=4)
        self.assertLess(lng, 0)   # '#' replaced a dropped minus → western by default

    def test_plain_high_precision_pair(self):
        lat, lng = g.extract_published_coords("Site field, 37.7123, -121.8754")
        self.assertAlmostEqual(lat, 37.7123, places=4)
        self.assertAlmostEqual(lng, -121.8754, places=4)

    def test_pair_space_separated_negative(self):
        lat, lng = g.extract_published_coords("44.9778 -93.2650")
        self.assertAlmostEqual(lat, 44.9778, places=4)
        self.assertAlmostEqual(lng, -93.2650, places=4)

    def test_cardinal_pair_nw(self):
        lat, lng = g.extract_published_coords("Field gate at 37.7123 N, 121.8754 W")
        self.assertAlmostEqual(lat, 37.7123, places=4)
        self.assertAlmostEqual(lng, -121.8754, places=4)

    def test_cardinal_pair_se(self):
        lat, lng = g.extract_published_coords("33.8688 S 151.2093 E")
        self.assertAlmostEqual(lat, -33.8688, places=4)
        self.assertAlmostEqual(lng, 151.2093, places=4)

    def test_gps_suffix_delegates(self):
        lat, lng = g.extract_published_coords("Burger's Lake GPS: 32.85 N, 97.45 W")
        self.assertAlmostEqual(lat, 32.85, places=2)
        self.assertAlmostEqual(lng, -97.45, places=2)

    def test_extracts_coords_from_description_text(self):
        desc = ("Join us for fighter practice! Park entrance on the north side. "
                "Site coordinates: 38.5816, -121.4944. Bring water and armour.")
        lat, lng = g.extract_published_coords(desc)
        self.assertAlmostEqual(lat, 38.5816, places=4)
        self.assertAlmostEqual(lng, -121.4944, places=4)

    # ── False positives we must NOT make ────────────────────────────────
    def test_plain_address_is_not_coordinates(self):
        for addr in ("4100 Gleason Dr, Dublin, CA 94568, USA",
                     "Emerald Glen Park, 4201 Central Pkwy, Dublin, CA 94568, USA",
                     "123 Main St, Apt 4, Anytown, NY 12345"):
            self.assertEqual(g.extract_published_coords(addr), (None, None), addr)

    def test_three_decimal_bare_pair_rejected(self):
        # 3 decimals is below the 4-decimal bar for a BARE pair — too easy for an
        # innocent number to clear; a label/cardinal or more precision is required.
        self.assertEqual(g.extract_published_coords("37.712, -121.875"), (None, None))

    def test_times_and_prices_are_not_coordinates(self):
        for s in ("Doors 7.30, show 9.45; tickets $25.00 and $40.00",
                  "Call 555.123.4567 between 9.00 and 17.00",
                  "Feast 6.30pm, court 8.00pm, $30.50 per head"):
            self.assertEqual(g.extract_published_coords(s), (None, None), s)

    def test_out_of_range_rejected(self):
        self.assertEqual(g.extract_published_coords("99.9999, 191.5000"), (None, None))

    def test_empty(self):
        self.assertEqual(g.extract_published_coords(""), (None, None))
        self.assertEqual(g.extract_published_coords("Dublin, CA"), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
