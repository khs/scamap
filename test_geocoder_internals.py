"""
test_geocoder_internals.py
--------------------------
Offline tests for the shared geocoder's cache layer (geocoder._cache_get /
_cache_put and the cached-failure TTL), plus the pure address helpers in
geocode_sca_events (state extraction, venue-prefix stripping, inline-GPS
parsing, and the state bounding-box checks).

STRICTLY OFFLINE. The cache tests manipulate geocoder._cache directly and
never call nominatim()/photon() in a way that issues HTTP: the two cache-key
tests pre-seed _cache with the exact key the query computes and pass
session=None, so a cache hit returns immediately and any accidental network
attempt would crash on None.get() instead of silently reaching the wire.

Run:
    python -m unittest test_geocoder_internals -v
"""
from __future__ import annotations

import time
import unittest

import geocoder as gc
import geocode_sca_events as g


class TestCacheLayer(unittest.TestCase):
    """Exercise _cache_get / _cache_put directly against the in-memory dict.

    _cache is a module-level singleton shared across the whole suite, so we
    snapshot and restore it (and the _dirty flag) around every test to avoid
    cross-test leakage.
    """

    def setUp(self):
        self._saved_cache = dict(gc._cache)
        self._saved_dirty = gc._dirty
        gc._cache.clear()

    def tearDown(self):
        gc._cache.clear()
        gc._cache.update(self._saved_cache)
        gc._dirty = self._saved_dirty

    # -- success round-trip -------------------------------------------------
    def test_put_then_get_returns_coords(self):
        gc._cache_put("k:success", (35.5, -78.6))
        self.assertEqual(gc._cache_get("k:success"), (35.5, -78.6))

    def test_put_stamps_entry_with_ts(self):
        before = int(time.time())
        gc._cache_put("k:ts", (1.0, 2.0))
        entry = gc._cache["k:ts"]
        self.assertEqual((entry["lat"], entry["lng"]), (1.0, 2.0))
        self.assertGreaterEqual(entry["ts"], before)
        self.assertIsInstance(entry["ts"], int)

    def test_put_sets_dirty_flag(self):
        gc._dirty = False
        gc._cache_put("k:dirty", (3.0, 4.0))
        self.assertTrue(gc._dirty)

    # -- misses -------------------------------------------------------------
    def test_missing_key_returns_miss(self):
        self.assertIs(gc._cache_get("never-stored"), gc._MISS)

    # -- cached failures + TTL ---------------------------------------------
    def test_fresh_failure_returns_none_tuple_and_stays_cached(self):
        # A failure (lat None) younger than _FAIL_TTL is honored: it returns
        # the (None, None) tuple rather than _MISS, so the caller does NOT
        # re-query. Use an explicit ts well inside the window.
        gc._cache["k:failfresh"] = {
            "lat": None, "lng": None, "ts": int(time.time()) - 60}
        got = gc._cache_get("k:failfresh")
        self.assertIsNot(got, gc._MISS)
        self.assertEqual(got, (None, None))

    def test_stale_failure_returns_miss_so_it_requeries(self):
        # A failure older than _FAIL_TTL is treated as a miss, so the caller
        # would re-query the service.
        gc._cache["k:failold"] = {
            "lat": None, "lng": None,
            "ts": int(time.time()) - gc._FAIL_TTL - 100}
        self.assertIs(gc._cache_get("k:failold"), gc._MISS)

    def test_failure_just_inside_ttl_is_still_cached(self):
        # One second short of the TTL: still a live failure (elapsed < TTL).
        gc._cache["k:failedge"] = {
            "lat": None, "lng": None,
            "ts": int(time.time()) - (gc._FAIL_TTL - 1)}
        self.assertEqual(gc._cache_get("k:failedge"), (None, None))

    def test_failure_missing_ts_is_treated_as_stale(self):
        # No 'ts' -> defaults to 0 -> elapsed is enormous -> _MISS (re-query).
        gc._cache["k:failnots"] = {"lat": None, "lng": None}
        self.assertIs(gc._cache_get("k:failnots"), gc._MISS)

    def test_success_missing_ts_returns_coords(self):
        # FLAGGED behavior: a success entry (lat not None) with no 'ts' still
        # returns its coords. The TTL branch is gated on lat is None, so the
        # missing-ts default of 0 never matters for a success. Pinning current
        # behavior; see flagged_bugs (it relies on lat None as the sole failure
        # marker rather than a dedicated flag).
        gc._cache["k:successnots"] = {"lat": 10.0, "lng": 20.0}
        self.assertEqual(gc._cache_get("k:successnots"), (10.0, 20.0))

    # -- cache-key path of the query primitives (no HTTP) ------------------
    def test_nominatim_returns_from_cache_without_http(self):
        # Pre-seed the EXACT key nominatim() computes for a default call:
        #   f"nom:{query}|{require_in_name}|{countrycodes}|{int(addressdetails)}"
        # then pass session=None: a cache hit returns before any .get(), and an
        # accidental network attempt would raise on None.get() (never silent).
        query = "Garner, NC"
        key = f"nom:{query}||{gc.SCA_COUNTRY_CODES}|0"
        gc._cache_put(key, (35.7, -78.6))
        self.assertEqual(gc.nominatim(query, session=None), (35.7, -78.6))

    def test_photon_returns_from_cache_without_http(self):
        gc._cache_put("pho:Garner, NC", (35.7, -78.6))
        self.assertEqual(gc.photon("Garner, NC", session=None), (35.7, -78.6))


class TestExtractStateFromAddress(unittest.TestCase):
    def test_us_address_with_zip_yields_state(self):
        self.assertEqual(
            g.extract_state_from_address("..., Garner, NC, 27529"), "NC")

    def test_zip_without_trailing_comma_still_works(self):
        self.assertEqual(
            g.extract_state_from_address("123 Main St, Garner, NC 27529"), "NC")

    def test_foreign_two_letter_without_us_zip_is_none(self):
        # 'Perth WA 6000' looks like Western Australia, not Washington state;
        # without a US 5-digit ZIP we must NOT claim a US state.
        self.assertIsNone(g.extract_state_from_address("Perth WA 6000"))

    def test_us_looking_state_without_zip_is_none(self):
        # State code present but no ZIP -> None (the ZIP gate is required).
        self.assertIsNone(
            g.extract_state_from_address("Somewhere, NC, no zip here"))


class TestStripVenuePrefix(unittest.TestCase):
    def test_strips_leading_venue_name(self):
        self.assertEqual(
            g.strip_venue_prefix(
                "Burger's Lake 1200 Meandering Rd, Fort Worth, TX"),
            "1200 Meandering Rd, Fort Worth, TX")

    def test_no_embedded_street_number_returned_unchanged(self):
        addr = "Some Community Center, Garner, NC"
        self.assertEqual(g.strip_venue_prefix(addr), addr)


class TestExtractInlineGps(unittest.TestCase):
    def test_north_west_parses_and_negates_west(self):
        self.assertEqual(
            g.extract_inline_gps("GPS: 33.5 N, 111.9 W"), (33.5, -111.9))

    def test_south_east_hemispheres(self):
        # 'S' negates latitude; 'E' stays positive.
        self.assertEqual(
            g.extract_inline_gps("blah GPS: 12.0 S, 45.0 E end"), (-12.0, 45.0))

    def test_no_gps_substring_returns_none_pair(self):
        self.assertEqual(
            g.extract_inline_gps("no gps here at all"), (None, None))


class TestCoordStateAndResultInState(unittest.TestCase):
    # Raleigh, NC is inside NC's box; (0,0) is in no US state box.
    def test_coord_state_in_box(self):
        self.assertEqual(g.coord_state(35.7, -78.6), "NC")

    def test_coord_state_outside_all_boxes(self):
        self.assertIsNone(g.coord_state(0.0, 0.0))

    def test_result_in_state_inside_box(self):
        self.assertTrue(g.result_in_state(35.7, -78.6, "NC"))

    def test_result_in_state_outside_box(self):
        self.assertFalse(g.result_in_state(40.0, -100.0, "NC"))

    def test_result_in_state_unknown_state_accepts(self):
        # Unknown state code: don't reject (returns True).
        self.assertTrue(g.result_in_state(0.0, 0.0, "ZZ"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
