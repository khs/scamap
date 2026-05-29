"""
test_geocoding.py
-----------------
Offline regression tests for the geocoding logic. NO network — these exercise
the pure functions and lookup tables only, so they run in milliseconds and are
safe in CI.

They lock in the fixes that were hard-won (and easy to silently regress, since
the cron runs the pipeline unattended every 2 days):

  - the vague-text simplifier (SCA idioms, "Greater X area", county lists)
  - _validate's accept/reject rules (the "Madera, Texas"->Arizona and
    Stierbach "auxillary gym mary washington"->Washington-state miscodes)
  - apostrophe/case/diacritic-insensitive seat-map lookups
  - every kingdom's hand-curated seat map covers every group we ship
  - kingdoms.py bbox/membership table integrity

Run:
    python -m unittest test_geocoding -v      # no pytest needed
    pytest test_geocoding.py                  # also works
"""
from __future__ import annotations

import csv
import unittest
from pathlib import Path

import kingdoms
import geocode_sca_events as gse
import build_group_locations as bgl
import ImportMaps as im

SCRIPT_DIR = Path(__file__).parent
_SENTINEL = "\x00__no_seat__"

# Kingdom -> its hand-curated seat-map dict. scrape_caid/middle/etc. prefer
# these over scraped text; every group we ship for the kingdom must be covered.
SEAT_MAPS = {
    "Kingdom of the Middle":   bgl.MIDDLE_CITY,
    "Kingdom of the West":     bgl.WEST_CITY,
    "Kingdom of Northshield":  bgl.NORTHSHIELD_CITY,
    "Kingdom of Ansteorra":    bgl.ANSTEORRA_CITY,
    "Kingdom of Atenveldt":    bgl.ATENVELDT_CITY,
    "Kingdom of Calontir":     bgl.CALONTIR_CITY,
    "Kingdom of Caid":         bgl.CAID_CITY,
}


class TestVagueSimplify(unittest.TestCase):
    """The simplifier reduces vague SCA location text to core placenames."""

    def cands(self, text):
        return gse._vague_simplify_candidates(text)

    def test_mundanely_known_as_keeps_state(self):
        # "the city mundanely known as Lubbock, Texas" -> "Lubbock, Texas"
        self.assertIn("Lubbock, Texas", self.cands("the city mundanely known as Lubbock, Texas"))

    def test_greater_area_reduces_to_city(self):
        self.assertIn("Birmingham", self.cands("Greater Birmingham area and Shelby County"))

    def test_metropolitan_area_reduces_to_city(self):
        self.assertIn("Atlanta", self.cands("Atlanta Metropolitan area"))

    def test_greater_savannah(self):
        self.assertIn("Savannah", self.cands("Greater Savannah area"))

    def test_centered_around_keeps_state(self):
        self.assertIn("Ridgecrest, CA",
                      self.cands("Centered around Ridgecrest, CA; includes the Owens Valley"))

    def test_county_list_takes_first(self):
        self.assertIn("Madera", self.cands("Madera, Fresno, Kings, and Tulare Counties"))

    def test_slash_list_takes_first(self):
        self.assertIn("Barstow", self.cands("Barstow/Victorville, Wrightwood, Big Bear"))

    def test_no_street_number_candidate(self):
        # Street fragments are handled by other ladder steps, not the simplifier.
        for c in self.cands("400 Mandt Parkway, Stoughton, WI, USA 53589"):
            self.assertFalse(c[0].isdigit(), f"street fragment leaked: {c!r}")

    def test_lowercase_junk_dropped(self):
        # All-lowercase venue text yields no placename candidate.
        self.assertEqual(self.cands("auxillary gym mary washington"), [])

    def test_the_stopword_dropped(self):
        # "the area of Northeastern Oklahoma" must not yield the bare word "the".
        self.assertNotIn("the", [c.lower() for c in self.cands("the area of Northeastern Oklahoma")])


class TestExtractState(unittest.TestCase):
    def test_state_with_zip(self):
        self.assertEqual(
            gse.extract_state_from_address("8401 Turkey Thicket Dr, Montgomery Village, MD 20879, USA"),
            "MD")

    def test_no_state_without_zip(self):
        # ZIP-gated on purpose: bare "PA" without a ZIP is not trusted here
        # (the street-number relaxation lives in try_geocode_with_fallbacks).
        self.assertIsNone(gse.extract_state_from_address("205 Currie Rd., Slippery Rock, PA"))

    def test_australian_state_not_us(self):
        # "...NSW 2590" must never be read as a US state.
        self.assertIsNone(gse.extract_state_from_address("81 Wallendoon St, Cootamundra NSW 2590"))


class TestCoordState(unittest.TestCase):
    def test_known_coords(self):
        self.assertEqual(gse.coord_state(33.749, -84.388), "GA")   # Atlanta
        self.assertEqual(gse.coord_state(34.186, -118.448), "CA")  # Van Nuys
        self.assertEqual(gse.coord_state(21.305, -157.856), "HI")  # Honolulu

    def test_in_sca_region(self):
        self.assertTrue(gse.in_sca_region(33.749, -84.388))    # Atlanta
        self.assertTrue(gse.in_sca_region(21.305, -157.856))   # Honolulu
        self.assertFalse(gse.in_sca_region(0.0, 20.0))         # equatorial Africa

    def test_lochac_region(self):
        # A Wisconsin coord is NOT in Lochac's regions; Melbourne is.
        self.assertFalse(gse.in_source_regions(42.91, -89.22, "Kingdom of Lochac"))
        self.assertTrue(gse.in_source_regions(-37.81, 144.96, "Kingdom of Lochac"))


class TestValidate(unittest.TestCase):
    CAID = {"CA", "NV", "HI", "AZ", "OR"}      # home + adjacent (as the code builds it)
    MERIDIES = {"AL", "GA", "TN", "KY", "FL"}

    def test_accepts_in_expected_state(self):
        self.assertTrue(gse._validate(34.186, -118.448, "CA", self.CAID, "Kingdom of Caid"))

    def test_rejects_when_address_state_mismatch(self):
        # Madera regression: address names CA but the result is in Arizona.
        self.assertFalse(gse._validate(33.259, -111.594, "CA", self.CAID, "Kingdom of Caid"))

    def test_rejects_none(self):
        self.assertFalse(gse._validate(None, None, "CA", self.CAID, "Kingdom of Caid"))

    def test_rejects_outside_sca_region(self):
        self.assertFalse(gse._validate(0.0, 20.0, "", self.CAID, "Kingdom of Caid"))

    def test_barony_event_stays_local(self):
        # Stierbach regression: a Virginia barony's event must not land in
        # Washington STATE off vague venue text.
        va_barony = {"VA", "MD", "NC", "DC", "WV", "KY", "TN"}
        self.assertFalse(gse._validate(47.61, -122.33, "", va_barony, "Barony of Stierbach"))
        # ...but an event genuinely in-region is fine.
        self.assertTrue(gse._validate(38.30, -77.46, "", va_barony, "Barony of Stierbach"))


class TestSeatLookup(unittest.TestCase):
    def test_case_insensitive(self):
        # group_locations.csv has "Shire Of Carreg Wen" (capital Of).
        self.assertEqual(
            bgl._seat_lookup(bgl.CAID_CITY, "Shire Of Carreg Wen", _SENTINEL),
            "Lompoc, California")

    def test_diacritic_match(self):
        # The scraped name carries an eth: "Canton of Skorragarðr".
        self.assertEqual(
            bgl._seat_lookup(bgl.ANSTEORRA_CITY, "Canton of Skorragarðr", _SENTINEL),
            "Shawnee, Oklahoma")

    def test_curly_apostrophe_match(self):
        # Northshield's scraped name uses a curly apostrophe.
        self.assertEqual(
            bgl._seat_lookup(bgl.NORTHSHIELD_CITY, "Shire of Falcon’s Keep", _SENTINEL),
            "Stevens Point, Wisconsin")

    def test_unknown_returns_default(self):
        self.assertEqual(bgl._seat_lookup(bgl.CAID_CITY, "Barony of Nowhere", _SENTINEL), _SENTINEL)


class TestSeatMapCoverage(unittest.TestCase):
    """Every group we ship for a seat-mapped kingdom must resolve to a seat —
    so adding a group without giving it a city is caught here, not on the map."""

    def test_every_group_has_a_seat(self):
        path = SCRIPT_DIR / "group_locations.csv"
        if not path.exists():
            self.skipTest("group_locations.csv not present")
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        missing = []
        for r in rows:
            cmap = SEAT_MAPS.get(r["kingdom"])
            if cmap is None:
                continue
            if bgl._norm_key(r["group"]) in bgl.CALONTIR_DROP:
                continue  # intentionally not a branch (event mis-scraped as one)
            if bgl._seat_lookup(cmap, r["group"], _SENTINEL) == _SENTINEL:
                missing.append(f'{r["kingdom"]} / {r["group"]}')
        self.assertEqual(missing, [], f"groups with no seat: {missing}")


class TestKingdomTables(unittest.TestCase):
    def test_bbox_well_formed(self):
        for code, (la_min, la_max, lng_min, lng_max) in kingdoms.STATE_BBOX.items():
            self.assertLess(la_min, la_max, f"{code}: lat min>=max")
            self.assertLess(lng_min, lng_max, f"{code}: lng min>=max")

    def test_kingdom_states_known(self):
        for kingdom, states in kingdoms.KINGDOM_STATES.items():
            for st in states:
                self.assertIn(st, kingdoms.STATE_BBOX, f"{kingdom} references unknown state {st}")

    def test_caid_home_has_no_arizona(self):
        # The Madera fix depends on AZ not being a Caid HOME state (the
        # simplifier only hints with home states), so guard it explicitly.
        self.assertEqual(set(kingdoms.KINGDOM_HOME_STATES["Kingdom of Caid"]), {"CA", "NV", "HI"})

    def test_northshield_includes_canada(self):
        # Winnipeg (Castel Rouge) / Thunder Bay (Mare Amethystinum) fix.
        self.assertIn("MB", kingdoms.KINGDOM_STATES["Kingdom of Northshield"])
        self.assertIn("ON", kingdoms.KINGDOM_STATES["Kingdom of Northshield"])


class TestIsVirtual(unittest.TestCase):
    """is_virtual_event must trust the title/location, not a stray keyword in
    the free-text description. Locks in the Summer's End / Pennsic regression:
    an in-person event with a real address whose notes say 'register online'
    was landing in the virtual list (and off the map)."""

    def v(self, title, location, desc):
        return im.is_virtual_event(title, location, desc)

    def test_real_address_with_online_in_desc_is_in_person(self):
        # Summer's End 2026 — real NY address, "online" buried in the notes.
        self.assertFalse(self.v("Summer's End 2026",
                                "10299 Savage Road, Holland, NY", "register online soon"))

    def test_pennsic_online_preregistration_is_in_person(self):
        self.assertFalse(self.v("Pennsic War LIV",
                                "Coopers Lake Campground, 205 Currie Rd", "online pre-registration is open"))

    def test_virtual_in_title_is_virtual(self):
        self.assertTrue(self.v("Barony of Tarnmists Business Meeting (Virtual)", "Zoom", ""))

    def test_zoom_location_is_virtual(self):
        self.assertTrue(self.v("Populace Meeting", "Zoom", ""))

    def test_no_location_with_zoom_desc_is_virtual(self):
        # No physical location at all -> fall back to the description.
        self.assertTrue(self.v("Populace Meeting", "", "Join us via the Zoom link"))

    def test_plain_in_person_event(self):
        self.assertFalse(self.v("Arts & Sciences Night", "Town Hall, Springfield", "bring your projects"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
