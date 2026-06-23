"""
test_clean_location.py
----------------------
Offline tests for the pure location-parsing helpers in clean_sca_events:

  - clean_location              venue-stripping + confidence classification
  - extract_location_from_description   address mining from prose/labels
  - preferred_source            de-dup tiebreak (OOK / geography / kingdom)
  - _coord_state                point-in-bbox US-state lookup

No network, no file IO — every function under test is pure. Expected outputs
were captured by actually running each function (see the inline notes where a
result is non-obvious, e.g. the overlapping-bbox lookup that returns the first
matching box in dict order rather than the geographically tightest one).

Run:
    python -m unittest test_clean_location -v
"""
from __future__ import annotations

import unittest

import pandas as pd

import clean_sca_events as clean

COLS = ["title", "start", "end", "location", "clean_location",
        "address_confidence", "description", "event_url", "facebook_url",
        "source", "calendar_type", "is_virtual", "lat", "lng", "geocode_status"]


def _row(**over):
    r = {c: "" for c in COLS}
    r.update(over)
    return r


class TestCleanLocation(unittest.TestCase):
    # -- placeholders / templates collapse to empty ----------------------
    def test_tbd_is_empty(self):
        self.assertEqual(clean.clean_location("TBD"), ("", "empty"))

    def test_placeholder_with_trailing_dot_is_empty(self):
        # PLACEHOLDER_LOCATIONS membership is checked after .rstrip(".")
        self.assertEqual(clean.clean_location("TBD."), ("", "empty"))

    def test_see_description_placeholder_is_empty(self):
        self.assertEqual(clean.clean_location("See Description"), ("", "empty"))

    def test_aethelmearc_default_template_is_empty(self):
        # "Default R3, 18515" template location -> not a real venue.
        self.assertEqual(clean.clean_location("Default R3, 18515"), ("", "empty"))

    # -- bare state code / kingdom name defer to later fallbacks ---------
    def test_bare_state_code_is_empty(self):
        # A 2-letter state code is a key of US_STATE_NAMES -> deferred to empty.
        self.assertEqual(clean.clean_location("PA"), ("", "empty"))

    def test_bare_kingdom_name_is_empty(self):
        self.assertEqual(clean.clean_location("Kingdom of Northshield"), ("", "empty"))

    def test_the_kingdom_of_prefix_is_empty(self):
        # KINGDOM_NAME_RE also matches the optional leading "the".
        self.assertEqual(clean.clean_location("the Kingdom of Atlantia"), ("", "empty"))

    # -- real addresses keep "high" --------------------------------------
    def test_street_first_address_is_high_and_unchanged(self):
        # Case 1: first chunk starts with a street number -> kept verbatim.
        self.assertEqual(
            clean.clean_location("123 Main St, Springfield, IL 62704"),
            ("123 Main St, Springfield, IL 62704", "high"))

    def test_venue_prefixed_address_strips_to_street(self):
        # Case 2: a later comma-chunk starts with a street number; everything
        # before it (the venue name) is dropped.
        self.assertEqual(
            clean.clean_location("Burger's Lake, 1200 Meandering Rd, Fort Worth, TX 76131"),
            ("1200 Meandering Rd, Fort Worth, TX 76131", "high"))

    def test_embedded_street_in_first_chunk_strips_venue(self):
        # Case 3: street number embedded mid-chunk (no comma before it).
        self.assertEqual(
            clean.clean_location("Burger's Lake 1200 Meandering Rd"),
            ("1200 Meandering Rd", "high"))

    def test_embedded_street_first_chunk_with_trailing_city(self):
        self.assertEqual(
            clean.clean_location("Some Venue 555 Oak Avenue, Dayton, OH"),
            ("555 Oak Avenue, Dayton, OH", "high"))

    # -- city/ZIP without a street is only "low" -------------------------
    def test_city_zip_without_street_is_low(self):
        self.assertEqual(
            clean.clean_location("Springfield, IL 62704"),
            ("Springfield, IL 62704", "low"))

    def test_full_state_name_is_low_not_empty(self):
        # Only the 2-letter CODE is a US_STATE_NAMES key; the spelled-out name
        # is not, so it is not deferred and falls through to the "low" tail.
        self.assertEqual(clean.clean_location("Texas"), ("Texas", "low"))

    # -- empties ----------------------------------------------------------
    def test_empty_string_is_empty(self):
        self.assertEqual(clean.clean_location(""), ("", "empty"))

    def test_non_string_is_empty(self):
        self.assertEqual(clean.clean_location(None), ("", "empty"))


class TestAddressWithheld(unittest.TestCase):
    """Deliberately address-free locations collapse to ("", "empty") so the
    geocoder marks them "skipped" (no bogus pin, no endless --retry-failed).
    The predicate was verified against the full event set: zero real addresses
    are caught."""

    # Real rows from the data + adversarial synthetic strings.
    MUST_CATCH = [
        "Ask the marshal for the address",
        "Ask the Marshal for the address",                       # casing variant
        "DM Finn on Discord or email at scarey161@gmail.com for directions",
        "Mistress Rownan's Personal Abode in Cook. Contact her for the address (rgspencer@gmail.com)",
        "Angelino and Ylaire's home",                            # possessive private home
        "Discord",                                               # bare-field placeholder
        "  discord  ",
        "To Be Determine",                                       # typo of "to be determined"
        "please contact the autocrat for directions",
        "Email the seneschal for the address",
        "Lady Ana's house",
    ]
    # Real addresses (or venue names) that must keep a non-empty confidence —
    # they merely contain a trigger word.
    MUST_NOT_CATCH = [
        "St. Mary's Church, 100 Oak Ave, Leeds ME",             # possessive, not a home-word
        "Farmer's Market, 10 Center St, Dayton OH",
        "People's Place Community Center, 22 Village Ln, Akron OH",
        "123 Discord Lane, Cherokee, IA",                       # 'discord' inside a real street
        "Join our Discord server after you arrive at 5 Main St",  # 'discord' mid-string, no 'dm'
        "Private Property 190 State Rt 219, Leeds, ME 04263",    # 'private', not residence/home
    ]

    def test_withheld_locations_collapse_to_empty(self):
        for loc in self.MUST_CATCH:
            self.assertEqual(clean.clean_location(loc), ("", "empty"), loc)

    def test_predicate_spares_real_addresses(self):
        for loc in self.MUST_NOT_CATCH:
            self.assertFalse(clean.is_address_withheld(loc), f"false positive: {loc!r}")
            self.assertNotIn(loc.strip().lower().rstrip("."),
                             clean.PLACEHOLDER_LOCATIONS, loc)

    def test_predicate_is_case_insensitive(self):
        # Residual-risk tripwire: a future refactor must not drop re.IGNORECASE.
        self.assertTrue(clean.is_address_withheld("ASK THE MARSHAL FOR THE ADDRESS"))
        self.assertTrue(clean.is_address_withheld("ask the marshal for the address"))


class TestExtractLocationFromDescription(unittest.TestCase):
    def test_free_floating_full_address_is_high(self):
        # Step 1: a bare "<num> <street>, <city>, <ST> <zip>" in prose.
        self.assertEqual(
            clean.extract_location_from_description(
                "Join us! The site is 400 Mandt Parkway, Stoughton, WI 53589. Bring water."),
            ("400 Mandt Parkway, Stoughton, WI 53589", "high"))

    def test_labeled_value_with_street_and_zip_is_high(self):
        # "Address:" label whose value contains a full street+ZIP. (Step 1's
        # free-floating scan actually fires first and returns just the street
        # portion, dropping the venue prefix.)
        self.assertEqual(
            clean.extract_location_from_description(
                "Address: Mandt Center, 400 Mandt Parkway, Stoughton, WI 53589"),
            ("400 Mandt Parkway, Stoughton, WI 53589", "high"))

    def test_labeled_value_without_zip_is_low(self):
        # Step 2: a "Venue:" label with city/state but no street/ZIP -> "low".
        self.assertEqual(
            clean.extract_location_from_description(
                "Venue: Mandt Center, Stoughton, WI"),
            ("Mandt Center, Stoughton, WI", "low"))

    def test_labeled_value_terminates_at_next_field(self):
        # The value ends at the next recognised field label ("Contact").
        self.assertEqual(
            clean.extract_location_from_description(
                "Venue: Town Hall, Springfield, IL Contact: bob"),
            ("Town Hall, Springfield, IL", "low"))

    def test_held_in_city_state_sentence_is_low(self):
        # Step 3: the "<verb> in <City>, <ST>" fallback via CITY_STATE_RE.
        self.assertEqual(
            clean.extract_location_from_description(
                "The event was hosted in Burkburnett, TX last spring."),
            ("Burkburnett, TX", "low"))

    def test_no_address_is_empty(self):
        self.assertEqual(
            clean.extract_location_from_description(
                "Just a friendly note with no address whatsoever."),
            ("", "empty"))

    def test_empty_string_is_empty(self):
        self.assertEqual(clean.extract_location_from_description(""), ("", "empty"))

    def test_non_string_is_empty(self):
        self.assertEqual(clean.extract_location_from_description(None), ("", "empty"))


class TestPreferredSource(unittest.TestCase):
    def test_non_ook_row_beats_ook_row(self):
        # Two otherwise-identical rows; the one NOT marked OUT OF KINGDOM wins.
        g = pd.DataFrame([
            _row(title="War (OUT OF KINGDOM)", source="Kingdom of Caid",
                 calendar_type="kingdom", clean_location="Nowhere"),
            _row(title="War", source="Kingdom of Meridies",
                 calendar_type="kingdom", clean_location="Nowhere"),
        ])
        self.assertEqual(clean.preferred_source(g)["title"], "War")

    def test_all_ook_falls_back_to_first_row(self):
        # When every candidate is OOK, the OOK filter yields nothing usable and
        # the group is kept whole -> first row returned.
        g = pd.DataFrame([
            _row(title="A (OUT OF KINGDOM)", source="K1",
                 calendar_type="kingdom", clean_location="Z"),
            _row(title="B (OUT OF KINGDOM)", source="K2",
                 calendar_type="kingdom", clean_location="Z"),
        ])
        self.assertEqual(clean.preferred_source(g)["title"], "A (OUT OF KINGDOM)")

    def test_geographically_appropriate_kingdom_wins(self):
        # A TX address maps to Ansteorra via STATE_TO_KINGDOM.
        self.assertEqual(clean.STATE_TO_KINGDOM.get("TX"), "Kingdom of Ansteorra")
        g = pd.DataFrame([
            _row(title="Event", source="Kingdom of Meridies",
                 calendar_type="kingdom", clean_location="123 Main St, Dallas, TX 75001"),
            _row(title="Event", source="Kingdom of Ansteorra",
                 calendar_type="kingdom", clean_location="123 Main St, Dallas, TX 75001"),
        ])
        self.assertEqual(clean.preferred_source(g)["source"], "Kingdom of Ansteorra")

    def test_kingdom_type_beats_baronial_when_no_geo_match(self):
        # No extractable state -> skip the geography step -> kingdom over barony.
        g = pd.DataFrame([
            _row(title="E", source="Barony of X",
                 calendar_type="barony", clean_location="Somewhere"),
            _row(title="E", source="Kingdom of Y",
                 calendar_type="kingdom", clean_location="Somewhere"),
        ])
        self.assertEqual(clean.preferred_source(g)["calendar_type"], "kingdom")


class TestCoordState(unittest.TestCase):
    def test_texas_interior_point_in_box(self):
        # A deep-interior TX coordinate lands uniquely in the TX bbox.
        self.assertEqual(clean._coord_state(31.0, -99.0), "TX")

    def test_pennsylvania_point_below_ny_box(self):
        # 40.0 N is below NY's lat_min (40.4), so this resolves uniquely to PA.
        self.assertEqual(clean._coord_state(40.0, -77.0), "PA")

    def test_point_outside_all_boxes_is_none(self):
        # Mid-Atlantic ocean: in no US/CA bounding box.
        self.assertIsNone(clean._coord_state(10.0, -40.0))

    def test_overlapping_boxes_return_first_in_dict_order(self):
        # (40.4, -77.0) falls inside BOTH the NY and PA bounding boxes. The
        # lookup returns the first match in dict-iteration order (NY precedes
        # PA), NOT the geographically tightest state. Pinned as current
        # behavior; see flagged_bugs.
        self.assertEqual(clean._coord_state(40.4, -77.0), "NY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
