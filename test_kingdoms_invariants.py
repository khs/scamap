"""
test_kingdoms_invariants.py
---------------------------
Offline data-integrity invariants over the geography tables in kingdoms.py
(the single source of truth shared by build_group_pins / geocode_sca_events /
clean_sca_events), plus two pure helpers from build_group_pins.

Why these guard rails matter: a single swapped bbox edge (lat_min > lat_max)
silently rejects every geocode forever; an asymmetric adjacency entry quietly
fails to validate a cross-border baronial event; a state code referenced by a
kingdom but missing from STATE_BBOX makes coord_state unable to resolve it; and
a KINGDOM_REGIONS region name that isn't in SCA_REGION_BBOXES is a dead filter.

build_group_pins imports cleanly and offline (it pulls in geocoder, which only
reads an env var at import time — no network), so its pure helpers are exercised
directly here.

Run:
    python -m unittest test_kingdoms_invariants -v
"""
from __future__ import annotations

import unittest

import kingdoms as k
import build_group_pins as bgp


# A reference box used for _in_box geometry tests (Pennsylvania's bbox shape:
# lat_min < lat_max, lng_min < lng_max). Values are illustrative.
BOX = (39.6, 42.4, -80.6, -74.6)


class TestBboxWellFormed(unittest.TestCase):
    """Every bounding box (state/province and region) must have lat_min <
    lat_max and lng_min < lng_max. A swapped edge silently breaks validation."""

    def test_state_bboxes_well_formed(self):
        for code, box in k.STATE_BBOX.items():
            self.assertEqual(len(box), 4, f"{code} bbox is not a 4-tuple")
            lat_min, lat_max, lng_min, lng_max = box
            self.assertLess(lat_min, lat_max,
                            f"{code}: lat_min {lat_min} !< lat_max {lat_max}")
            self.assertLess(lng_min, lng_max,
                            f"{code}: lng_min {lng_min} !< lng_max {lng_max}")

    def test_region_bboxes_well_formed(self):
        for name, box in k.SCA_REGION_BBOXES.items():
            self.assertEqual(len(box), 4, f"{name} bbox is not a 4-tuple")
            lat_min, lat_max, lng_min, lng_max = box
            self.assertLess(lat_min, lat_max,
                            f"{name}: lat_min {lat_min} !< lat_max {lat_max}")
            self.assertLess(lng_min, lng_max,
                            f"{name}: lng_min {lng_min} !< lng_max {lng_max}")


class TestAdjacencySymmetry(unittest.TestCase):
    """US_STATE_ADJACENT must be a symmetric relation with no self-edges."""

    def test_symmetric(self):
        for a, neighbours in k.US_STATE_ADJACENT.items():
            for b in neighbours:
                self.assertIn(a, k.US_STATE_ADJACENT.get(b, set()),
                              f"{b} lists {a} only one-way: {a} not in ADJ[{b}]")

    def test_no_self_adjacency(self):
        for a, neighbours in k.US_STATE_ADJACENT.items():
            self.assertNotIn(a, neighbours, f"{a} lists itself as adjacent")

    def test_adjacency_codes_have_bbox(self):
        # Both sides of every edge must be resolvable to a bounding box.
        codes = set(k.US_STATE_ADJACENT)
        for neighbours in k.US_STATE_ADJACENT.values():
            codes |= set(neighbours)
        missing = sorted(c for c in codes if c not in k.STATE_BBOX)
        self.assertEqual(missing, [],
                         f"adjacency codes with no STATE_BBOX entry: {missing}")


class TestReferencedStatesHaveBbox(unittest.TestCase):
    """Every state/province code referenced by KINGDOM_HOME_STATES and
    BARONY_HOME_STATES must have a STATE_BBOX entry so coord_state can resolve
    it. (KINGDOM_STATES is checked too — it feeds the same bbox acceptance.)"""

    def _referenced(self, table):
        out = set()
        for codes in table.values():
            out |= set(codes)
        return out

    def test_kingdom_home_states_have_bbox(self):
        missing = sorted(c for c in self._referenced(k.KINGDOM_HOME_STATES)
                         if c not in k.STATE_BBOX)
        self.assertEqual(missing, [],
                         f"KINGDOM_HOME_STATES codes missing from STATE_BBOX: {missing}")

    def test_barony_home_states_have_bbox(self):
        missing = sorted(c for c in self._referenced(k.BARONY_HOME_STATES)
                         if c not in k.STATE_BBOX)
        self.assertEqual(missing, [],
                         f"BARONY_HOME_STATES codes missing from STATE_BBOX: {missing}")

    def test_kingdom_states_have_bbox(self):
        missing = sorted(c for c in self._referenced(k.KINGDOM_STATES)
                         if c not in k.STATE_BBOX)
        self.assertEqual(missing, [],
                         f"KINGDOM_STATES codes missing from STATE_BBOX: {missing}")

    def test_canadian_provinces_referenced_are_documented(self):
        # KINGDOM_HOME_STATES' docstring calls itself "US-only", but it actually
        # carries the three Avacal provinces. They ARE present in STATE_BBOX, so
        # the resolvability invariant above still holds. Pin the exact non-US set
        # here as documentation (see flagged docstring mismatch).
        non_us = {c for c in self._referenced(k.KINGDOM_HOME_STATES)
                  if c not in k.US_STATE_NAMES}
        self.assertEqual(non_us, {"AB", "SK", "BC"})
        for c in non_us:
            self.assertIn(c, k.STATE_BBOX)            # still resolvable

    def test_provinces_without_bbox_are_unreferenced(self):
        # NT and NU appear in CA_PROVINCE_NAMES but have no STATE_BBOX. That's
        # fine only because no kingdom/barony table references them.
        no_box = {c for c in k.CA_PROVINCE_NAMES if c not in k.STATE_BBOX}
        self.assertEqual(no_box, {"NT", "NU"})
        referenced = (self._referenced(k.KINGDOM_STATES)
                      | self._referenced(k.KINGDOM_HOME_STATES)
                      | self._referenced(k.BARONY_HOME_STATES))
        self.assertEqual(no_box & referenced, set(),
                         "a bbox-less province is referenced and cannot resolve")


class TestKingdomRegionsResolve(unittest.TestCase):
    """Every region named in KINGDOM_REGIONS must exist in SCA_REGION_BBOXES,
    or the continent filter is a no-op for that kingdom."""

    def test_all_regions_have_bbox(self):
        bad = []
        for kingdom, regions in k.KINGDOM_REGIONS.items():
            self.assertTrue(regions, f"{kingdom} maps to an empty region set")
            for r in regions:
                if r not in k.SCA_REGION_BBOXES:
                    bad.append((kingdom, r))
        self.assertEqual(bad, [],
                         f"KINGDOM_REGIONS entries with no SCA_REGION_BBOXES box: {bad}")


# ---------------------------------------------------------------------------
# build_group_pins._in_box — boundary-inclusive point-in-box test.
# ---------------------------------------------------------------------------
class TestInBox(unittest.TestCase):
    def test_point_inside(self):
        self.assertTrue(bgp._in_box(40.0, -75.0, BOX))

    def test_point_outside_lat_high(self):
        self.assertFalse(bgp._in_box(50.0, -75.0, BOX))

    def test_point_outside_lat_low(self):
        self.assertFalse(bgp._in_box(10.0, -75.0, BOX))

    def test_point_outside_lng_low(self):
        self.assertFalse(bgp._in_box(40.0, -90.0, BOX))

    def test_point_outside_lng_high(self):
        self.assertFalse(bgp._in_box(40.0, -70.0, BOX))

    def test_min_corner_is_inclusive(self):
        # lat_min / lng_min are inside (closed interval).
        self.assertTrue(bgp._in_box(39.6, -80.6, BOX))

    def test_max_corner_is_inclusive(self):
        # lat_max / lng_max are inside (closed interval).
        self.assertTrue(bgp._in_box(42.4, -74.6, BOX))

    def test_just_outside_max_excluded(self):
        self.assertFalse(bgp._in_box(42.41, -74.6, BOX))
        self.assertFalse(bgp._in_box(42.4, -74.59, BOX))


# ---------------------------------------------------------------------------
# build_group_pins._simplify_candidates — yields progressively-simpler geocode
# queries for vague region text. Expected outputs verified empirically.
# ---------------------------------------------------------------------------
class TestSimplifyCandidates(unittest.TestCase):
    def cands(self, text, hint="", kingdom=""):
        return list(bgp._simplify_candidates(text, hint, kingdom))

    def test_greater_area_with_state_hint_resolves_city_state(self):
        # "Greater Savannah area" + GA hint -> the city, qualified to the state.
        self.assertEqual(self.cands("Greater Savannah area", "GA",
                                    "Kingdom of Meridies"),
                         ["Savannah, Georgia, USA"])

    def test_greater_area_no_hint_yields_bare_token(self):
        # No state context at all: just the stripped placename.
        self.assertEqual(self.cands("Greater Savannah area", "",
                                    "Kingdom of Meridies"),
                         ["Savannah"])

    def test_direction_prefix_stripped(self):
        # "Northern Virginia" -> direction stripped, leaving the placename.
        self.assertEqual(self.cands("Northern Virginia", "", "Kingdom of Atlantia"),
                         ["Virginia"])

    def test_trailing_state_in_text_detected(self):
        # A ", GA" in the text supplies the state with no separate hint.
        self.assertEqual(self.cands("Atlanta, GA", "", ""),
                         ["Atlanta, Georgia, USA"])

    def test_plain_placename_yields_itself(self):
        self.assertEqual(self.cands("Atlanta", "", ""), ["Atlanta"])
        self.assertEqual(self.cands("Chicago", "", ""), ["Chicago"])

    def test_already_full_address_normalised(self):
        self.assertEqual(self.cands("Savannah, GA, USA", "", ""),
                         ["Savannah, Georgia, USA"])

    def test_first_chunk_taken_on_slash_and_comma(self):
        # "Columbus / Central Ohio, OH" -> first chunk + detected state.
        self.assertEqual(self.cands("Columbus / Central Ohio, OH", "", ""),
                         ["Columbus, Ohio, USA"])

    def test_ie_parenthetical_extracts_city(self):
        # "Region (i.e. Norfolk), VA" -> the i.e. city is pulled out. Note the
        # trailing state is dropped because extraction replaces the whole string.
        self.assertEqual(self.cands("Tidewater region (i.e. Norfolk), VA", "", ""),
                         ["Norfolk"])

    def test_trailing_noise_stripped(self):
        # Scraper junk after "Dates:" is removed before geocoding.
        self.assertEqual(self.cands("Greater Savannah area Dates: June 5", "GA", ""),
                         ["Savannah, Georgia, USA"])

    def test_surrounding_areas_suffix_stripped(self):
        self.assertEqual(self.cands("Atlanta and surrounding areas", "", ""),
                         ["Atlanta"])

    def test_canada_kingdom_adds_country_qualified_form(self):
        # An Ealdormere placename with no state context gets a Canada fallback.
        self.assertEqual(self.cands("Toronto", "", "Kingdom of Ealdormere"),
                         ["Toronto", "Toronto, Canada"])

    def test_australia_kingdom_adds_country_qualified_form(self):
        self.assertEqual(self.cands("Sydney", "", "Kingdom of Lochac"),
                         ["Sydney", "Sydney, Australia"])

    def test_province_in_text_resolves_to_province(self):
        # CURRENT behaviour: with ", BC" present the " area" suffix is NOT
        # stripped (the string ends in "BC", not "area"), so "Vancouver area"
        # survives into the candidate. Pinning observed behaviour.
        self.assertEqual(self.cands("Greater Vancouver area, BC", "",
                                    "Kingdom of Avacal"),
                         ["Vancouver area, British Columbia, Canada"])

    def test_empty_text_yields_nothing(self):
        self.assertEqual(self.cands("", "", ""), [])

    def test_two_letter_junk_yields_nothing(self):
        # A bare 2-letter token that isn't a known state/province, with no state
        # context, produces no candidate (len(tok) <= 2 branch, no prov/st).
        self.assertEqual(self.cands("XQ", "", ""), [])
        self.assertEqual(self.cands("GA", "", ""), [])

    def test_pure_punctuation_yields_nothing(self):
        self.assertEqual(self.cands("...", "", ""), [])
        self.assertEqual(self.cands("?", "", ""), [])

    def test_candidates_are_deduped_in_order(self):
        out = self.cands("Toronto", "", "Kingdom of Ealdormere")
        self.assertEqual(len(out), len(set(out)))   # no duplicates


if __name__ == "__main__":
    unittest.main(verbosity=2)
