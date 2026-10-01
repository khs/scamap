"""
test_local_online.py
--------------------
Offline tests for local groups' online and location-less events:

  * ImportMaps keeps them (they used to be dropped at import);
  * video-call platforms written the way calendars write them are "virtual";
  * de-duplication never merges two groups' online meetings just because
    both say "Zoom";
  * free/busy "Busy" blocks are not events.

Run:
    python -m unittest test_local_online -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest import mock

import icalendar
import pandas as pd

import clean_sca_events as clean
import ImportMaps

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
DTSTART:20991005T190000
SUMMARY:Fighter Practice
LOCATION:12 Oak Rd\\, Ely\\, MN 55731
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART:20991006T190000
SUMMARY:Populace Meeting
DESCRIPTION:Via GoogleMeet\\, see FB page for link
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART:20991007T190000
SUMMARY:Bardic Circle
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")


class TestImportKeepsLocalEvents(unittest.TestCase):
    def test_online_and_location_less_events_are_imported(self):
        cal = icalendar.Calendar.from_ical(ICS)
        with mock.patch.object(ImportMaps, "fetch_ics", return_value=cal), \
             mock.patch.object(ImportMaps, "TODAY", date(2099, 1, 1)), \
             mock.patch.object(ImportMaps, "NEAR_END", date(2099, 12, 31)), \
             mock.patch.object(ImportMaps, "FETCH_FAILURES_FILE", mock.MagicMock()):
            evs = ImportMaps.fetch_all_events(
                [{"id": "x", "source": "Shire of Here", "type": "baronial"}])
        by_title = {e["title"]: e for e in evs}
        self.assertEqual(set(by_title), {"Fighter Practice", "Populace Meeting", "Bardic Circle"})
        self.assertTrue(by_title["Populace Meeting"]["is_virtual"])       # "Via GoogleMeet"
        self.assertFalse(by_title["Bardic Circle"]["is_virtual"])         # no location, in person
        self.assertEqual(by_title["Bardic Circle"]["location"], "")


class TestVirtualKeywords(unittest.TestCase):
    def test_video_call_platforms(self):
        for desc in ("Via GoogleMeet", "Join on Google Meet", "meet.google.com/abc-defg",
                     "Microsoft Teams link to follow"):
            self.assertTrue(ImportMaps.is_virtual_event("Meeting", "", desc), desc)
            self.assertTrue(clean._looks_virtual("Meeting", desc), desc)

    def test_platform_only_location_is_virtual(self):
        for loc in ("Discord", "Our Discord server", "Online via Zoom", "Teams"):
            self.assertTrue(ImportMaps.is_virtual_event("Populace Meeting", loc, ""), loc)
        # ...but a place that merely mentions Discord is in person.
        self.assertFalse(ImportMaps.is_virtual_event("Practice", "Town Hall (see Discord)", ""))

    def test_in_person_with_a_place_is_not_virtual(self):
        self.assertFalse(ImportMaps.is_virtual_event(
            "Meeting", "Town Hall, 1 Main St, Ely MN", "Minutes shared on Google Meet later"))


class TestOnlineMeetingsNotMerged(unittest.TestCase):
    def test_two_groups_zoom_meetings_same_evening_both_kept(self):
        df = pd.DataFrame([
            {"title": "Business Meeting", "start": "2099-10-05 19:00:00", "location": "Zoom",
             "clean_location": "Zoom", "source": "Barony of A", "calendar_type": "baronial",
             "is_virtual": "True"},
            {"title": "Populace Meeting", "start": "2099-10-05 19:30:00", "location": "Zoom",
             "clean_location": "Zoom", "source": "Shire of B", "calendar_type": "baronial",
             "is_virtual": "True"},
        ])
        self.assertEqual(len(clean.deduplicate(df)), 2)

    def test_in_person_duplicates_still_merge(self):
        row = {"title": "Crown", "start": "2099-10-05", "location": "1 Main St, Ely, MN",
               "clean_location": "1 Main St, Ely, MN", "calendar_type": "kingdom",
               "is_virtual": "False"}
        df = pd.DataFrame([dict(row, source="Kingdom of A"), dict(row, source="Kingdom of B")])
        self.assertEqual(len(clean.deduplicate(df)), 1)


class TestRecurringMergeStaysWithinAGroup(unittest.TestCase):
    def test_same_title_blank_location_different_groups_not_merged(self):
        rows = []
        for src in ("Barony of Bonwicke", "Shire of Elsewhere"):
            for d in ("2099-10-01", "2099-10-08", "2099-10-15"):     # weekly
                rows.append({"title": "Populace Meeting", "start": f"{d} 20:00:00",
                             "clean_location": "", "source": src, "description": ""})
        out = clean.merge_recurring(pd.DataFrame(rows))
        self.assertEqual(sorted(out["source"]), ["Barony of Bonwicke", "Shire of Elsewhere"])
        self.assertTrue(all(t.endswith("(RECURRING)") for t in out["title"]))


class TestLocalCopiesOfKingdomEvents(unittest.TestCase):
    def _df(self, rows):
        base = {"is_virtual": "False", "description": ""}
        return pd.DataFrame([dict(base, **r) for r in rows])

    def test_barony_relisting_the_kingdom_event_is_dropped(self):
        df = self._df([
            {"title": "Bacon Bash 2026", "start": "2099-10-02", "clean_location": "1 Elm St, Ely, MN 55731",
             "source": "Kingdom of Meridies", "calendar_type": "kingdom"},
            {"title": "Bacon Bash 2026", "start": "2099-10-02", "clean_location": "1 Elm St, Ely, MN 55731",
             "source": "Barony of Bryn Madoc", "calendar_type": "baronial"}])
        out = clean.drop_local_copies_of_kingdom_events(df)
        self.assertEqual(list(out["source"]), ["Kingdom of Meridies"])

    def test_practice_at_the_same_venue_is_kept(self):
        df = self._df([
            {"title": "Crown Tourney", "start": "2099-10-02", "clean_location": "1 Elm St, Ely, MN 55731",
             "source": "Kingdom of X", "calendar_type": "kingdom"},
            {"title": "Archery Practice", "start": "2099-10-02", "clean_location": "1 Elm St, Ely, MN 55731",
             "source": "Barony of Y", "calendar_type": "baronial"}])
        self.assertEqual(len(clean.drop_local_copies_of_kingdom_events(df)), 2)

    def test_same_title_elsewhere_is_kept(self):
        df = self._df([
            {"title": "Crown Tourney", "start": "2099-10-02", "clean_location": "1 Elm St, Ely, MN 55731",
             "source": "Kingdom of X", "calendar_type": "kingdom"},
            {"title": "Crown Tourney", "start": "2099-10-02", "clean_location": "500 Oak Ave, Austin, TX",
             "source": "Barony of Y", "calendar_type": "baronial"}])
        self.assertEqual(len(clean.drop_local_copies_of_kingdom_events(df)), 2)


class TestNonEvents(unittest.TestCase):
    def test_free_busy_blocks(self):
        self.assertTrue(clean.is_non_event("Busy"))
        self.assertTrue(clean.is_non_event("  busy "))
        self.assertFalse(clean.is_non_event("Busy Bees Craft Night"))

    def test_officer_deadlines(self):
        self.assertTrue(clean.is_non_event("Anst. Webmin Quarterly Report Due on the 15th"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
