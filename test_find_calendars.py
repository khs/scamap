"""
test_find_calendars.py
----------------------
Offline tests for calendar discovery (find_calendars.py) and the My Calendar
adapter (scrapers.mycal:). No network.

Run:
    python -m unittest test_find_calendars -v
"""
from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import find_calendars as fc
import scrapers


def _eid(event_id: str, cal_id: str) -> str:
    return base64.urlsafe_b64encode(f"{event_id} {cal_id}".encode()).decode().rstrip("=")


class TestAddressFromText(unittest.TestCase):
    def test_multiline_block_with_venue(self):
        desc = "Every Tuesday, 7 p.m.\r\n\r\nOvation Playhouse\r\n75 Stark St\r\nHudson PA 18705, 2nd floor"
        self.assertEqual(scrapers.address_from_text(desc),
                         "Ovation Playhouse, 75 Stark St, Hudson PA 18705, 2nd floor")

    def test_one_line(self):
        self.assertEqual(scrapers.address_from_text("Meet at 12 Oak Road, Springfield, IL 62701 at 7."),
                         "12 Oak Road, Springfield, IL 62701")

    def test_html_breaks(self):
        self.assertEqual(scrapers.address_from_text("Town Hall<br>100 Main St<br/>Dayton, OH 45402"),
                         "Town Hall, 100 Main St, Dayton, OH 45402")

    def test_street_without_town_is_ignored(self):
        self.assertEqual(scrapers.address_from_text("Park at 12 Oak Road and walk in"), "")

    def test_times_and_online_are_not_addresses(self):
        self.assertEqual(scrapers.address_from_text("Via GoogleMeet, see FB page for link, 7 p.m."), "")


class TestMyCalendarRecords(unittest.TestCase):
    REC = {"occur_id": "145", "event_id": "10", "occur_begin": "2026-09-29 19:00:00",
           "occur_end": "2026-09-29 20:00:00", "ts_occur_begin": "1790722800",
           "event_title": "Steel Therapy Rapier Practice",
           "event_desc": "Ovation Playhouse\r\n75 Stark St\r\nHudson PA 18705",
           "event_label": "", "event_street": "", "event_city": "", "event_state": "",
           "event_postcode": "", "event_country": "", "event_link": "",
           "event_status": "1", "event_approved": "1"}
    PAGE = "https://example.org/calendar-events/"

    def test_local_time_address_and_link(self):
        ev = scrapers._parse_mycal_records({"2026-09-29": [self.REC]}, self.PAGE)[0]
        # Local wall-clock (naive), not UTC: a 7 PM practice stays on its day.
        self.assertEqual(ev["start"], datetime(2026, 9, 29, 19, 0))
        self.assertEqual(ev["location"], "Ovation Playhouse, 75 Stark St, Hudson PA 18705")
        self.assertEqual(ev["url"], self.PAGE + "?mc_id=145")

    def test_structured_venue_wins_and_external_link(self):
        rec = dict(self.REC, event_label="Scout Hut", event_street="1 Elm St", event_city="Ely",
                   event_state="MN", event_postcode="55731", event_link="https://x.org/e")
        ev = scrapers._parse_mycal_records([rec], self.PAGE)[0]
        self.assertEqual(ev["location"], "Scout Hut, 1 Elm St, Ely, MN 55731")
        self.assertEqual(ev["url"], "https://x.org/e")

    def test_unapproved_and_duplicate_occurrences_dropped(self):
        draft = dict(self.REC, occur_id="146", event_approved="0")
        evs = scrapers._parse_mycal_records({"a": [self.REC, self.REC], "b": [draft]}, self.PAGE)
        self.assertEqual(len(evs), 1)

    def test_prefix_is_registered(self):
        self.assertTrue(scrapers.is_scraper_source("mycal:https://example.org/cal/"))


class TestGoogleIds(unittest.TestCase):
    def test_eid_links_decode_to_calendar(self):
        html = (f'<a href="https://www.google.com/calendar/event?eid={_eid("abc_20260927T190000Z", "x.org_123@g")}&#038;ctz=America/Chicago">'
                f'<a href="https://www.google.com/calendar/event?eid={_eid("def", "k0ing@i")}">')
        self.assertEqual(fc.google_ids_in(html), ["x.org_123@group.calendar.google.com",
                                                  "k0ing@import.calendar.google.com"])

    def test_embed_src_base64_and_plain(self):
        b64 = base64.b64encode(b"barony@group.calendar.google.com").decode()
        html = (f'<iframe src="https://calendar.google.com/calendar/embed?src={b64}&amp;ctz=X">'
                '<iframe src="https://calendar.google.com/calendar/embed?height=600&src=shire%40gmail.com">')
        self.assertEqual(fc.google_ids_in(html), ["barony@group.calendar.google.com", "shire@gmail.com"])

    def test_id_key_matches_however_written(self):
        a = fc._id_key("https://calendar.google.com/calendar/ical/b%40group.calendar.google.com/public/basic.ics")
        self.assertEqual(a, fc._id_key("b@group.calendar.google.com"))
        self.assertEqual(fc._id_key("tribe-rest:http://www.x.org/"), fc._id_key("tribe-rest:https://x.org"))


class TestCandidatesFromPage(unittest.TestCase):
    def test_platforms_recognised(self):
        html = ('<link rel="https://api.w.org/" href="https://x.org/sub/wp-json/" />'
                '<script src="/wp-content/plugins/the-events-calendar/a.js"></script>'
                '<script src="/wp-content/plugins/event-organiser/a.js"></script>'
                '<a href="https://outlook.office365.com/owa/calendar/abc@x.org/def/calendar.html">cal</a>')
        ids = [c.calendar_id for c in fc.candidates_from_page("https://x.org/sub/events/", html)]
        self.assertIn("tribe-rest:https://x.org/sub", ids)
        self.assertIn("https://x.org/sub/feed/eo-events/", ids)
        self.assertIn("https://outlook.office365.com/owa/calendar/abc@x.org/def/calendar.ics", ids)

    def test_my_calendar_needs_rendered_calendar(self):
        plugin = '<link href="/wp-content/plugins/my-calendar/x.css">'
        self.assertEqual(fc.candidates_from_page("https://x.org/", plugin), [])
        page = plugin + '<div class="mc-main my-calendar-month">'
        self.assertEqual([c.calendar_id for c in fc.candidates_from_page("https://x.org/cal/", page)],
                         ["mycal:https://x.org/cal/"])


class TestVerifyClassification(unittest.TestCase):
    def test_holiday_and_duplicate_short_circuit(self):
        c = fc.verify(fc.Candidate(fc.google_ics_url("en.usa#holiday@group.v.calendar.google.com"), "t"),
                      "G", {}, set())
        self.assertEqual(c.verdict, "holiday")
        used = {fc._id_key("b@group.calendar.google.com"): "Kingdom of X"}
        c = fc.verify(fc.Candidate(fc.google_ics_url("b@group.calendar.google.com"), "t"), "G", used, set())
        self.assertEqual((c.verdict, c.note), ("duplicate", "already used by Kingdom of X"))


class TestOverlapClassification(unittest.TestCase):
    """A calendar that mostly repeats events already on the map is somebody
    else's calendar embedded on this site, not this group's own."""
    ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
           "BEGIN:VEVENT\r\nUID:1\r\nDTSTART:20991005T190000\r\nSUMMARY:Fighter Practice\r\n"
           "LOCATION:1 Elm St\\, Ely\\, MN 55731\r\nEND:VEVENT\r\n"
           "BEGIN:VEVENT\r\nUID:2\r\nDTSTART:20991012T190000\r\nSUMMARY:Populace Meeting\r\n"
           "LOCATION:1 Elm St\\, Ely\\, MN 55731\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

    def _verify(self, known):
        import icalendar
        cal = icalendar.Calendar.from_ical(self.ICS)
        with mock.patch.object(fc.ImportMaps, "fetch_ics", return_value=cal), \
             mock.patch.object(fc.ImportMaps, "FAR_END", datetime(2100, 1, 1)):
            return fc.verify(fc.Candidate("https://x.org/cal.ics", "t"), "Shire of Here", {}, known)

    SAME = "1 Elm St, Ely, MN 55731"

    def test_neighbours_calendar_is_duplicate(self):
        known = {("2099-10-05", "fighter practice"): [("Barony of There", self.SAME)],
                 ("2099-10-12", "populace meeting"): [("Barony of There", self.SAME)]}
        c = self._verify(known)
        self.assertEqual(c.verdict, "duplicate")
        self.assertIn("Barony of There", c.note)

    def test_kingdom_calendar_is_flagged_kingdom(self):
        known = {("2099-10-05", "fighter practice"): [("Kingdom of X", self.SAME)],
                 ("2099-10-12", "populace meeting"): [("Kingdom of X", self.SAME)],
                 ("2099-12-31", "horizon"): [("Kingdom of X", "")]}
        self.assertEqual(self._verify(known).verdict, "kingdom")

    def test_same_title_same_day_other_city_is_not_a_duplicate(self):
        # Two groups' "Fighter Practice" on the same evening in different towns.
        known = {("2099-10-05", "fighter practice"): [("Barony of There", "500 Oak Ave, Austin, TX 78701")],
                 ("2099-10-12", "populace meeting"): [("Barony of There", "500 Oak Ave, Austin, TX 78701")]}
        self.assertEqual(self._verify(known).verdict, "own")

    def test_new_events_are_own(self):
        c = self._verify({("2099-12-31", "something else"): [("Kingdom of X", "")]})
        self.assertEqual((c.verdict, c.upcoming, c.with_location), ("own", 2, 2))
        # Its events are kept, so a sweep can stop a second group claiming them.
        known = {}
        fc.remember(known, c, "Shire of Here")
        self.assertEqual(known[("2099-10-05", "fighter practice")], [("Shire of Here", self.SAME)])


class TestDirectoryPages(unittest.TestCase):
    def test_many_google_calendars_are_not_auto_picked(self):
        cands = [fc.Candidate(fc.google_ics_url(f"g{i}@group.calendar.google.com"), "t", verdict="own")
                 for i in range(3)] + [fc.Candidate("mycal:https://x.org/cal/", "t", verdict="own")]
        fc.mark_directory_pages(cands)
        self.assertEqual([c.verdict for c in cands], ["ambiguous"] * 3 + ["own"])
        self.assertEqual(fc.best(cands).calendar_id, "mycal:https://x.org/cal/")

    def test_two_google_calendars_are_fine(self):
        # Elfsea's page: its own calendar plus the kingdom's (caught as a duplicate).
        cands = [fc.Candidate(fc.google_ics_url(f"g{i}@group.calendar.google.com"), "t", verdict="own")
                 for i in range(2)]
        fc.mark_directory_pages(cands)
        self.assertEqual([c.verdict for c in cands], ["own", "own"])


class TestGeocodeTimeBudget(unittest.TestCase):
    """Adding many feeds at once must not push the refresh past its job limit:
    out of time, the geocoder stops, saves, and leaves the rest for next run."""

    def test_budget_exhausted_stops_without_losing_rows(self):
        import csv as _csv
        import geocode_sca_events as geo
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.csv"
            with open(p, "w", encoding="utf-8", newline="") as f:
                w = _csv.writer(f)
                w.writerow(["title", "start", "location", "clean_location", "address_confidence",
                            "description", "source", "lat", "lng", "geocode_status"])
                w.writerow(["Practice", "2026-10-01", "12 Oak Rd, Ely, MN 55731",
                            "12 Oak Rd, Ely, MN 55731", "high", "", "Barony of X", "", "", ""])
            lookup = mock.Mock(side_effect=AssertionError("geocoded despite a zero budget"))
            with mock.patch.multiple(geo, INPUT_FILE=p, OUTPUT_FILE=p,
                                     MAPLINK_CACHE_FILE=Path(d) / "maplink.json",
                                     GEOCODE_TIME_BUDGET_MIN=0,
                                     try_geocode_with_fallbacks=lookup,
                                     save_geo_cache=mock.Mock()):
                geo.main()
            row = next(_csv.DictReader(open(p, encoding="utf-8", newline="")))
        lookup.assert_not_called()
        self.assertEqual((row["lat"], row["geocode_status"]), ("", ""))   # left for next run


class TestApplyToLocals(unittest.TestCase):
    def test_only_no_calendar_rows_change_and_rest_is_untouched(self):
        text = ("kingdom,group,type,calendar_id,website,social,date_last_checked,location,lat,lng\r\n"
                'Kingdom of A,Barony of One,barony,No Calendar Available,https://one.org,,2026-06-01,"Town, ST",1,2\r\n'
                "Kingdom of A,Barony of Two,barony,https://existing.ics,https://two.org,,2026-06-01,Town,3,4\r\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "locals.csv"
            p.write_bytes(text.encode("utf-8"))
            with mock.patch.object(fc, "LOCALS_FILE", p):
                n = fc.apply_to_locals({"Barony of One": "mycal:https://one.org/cal/",
                                        "Barony of Two": "should-not-apply"})
            out = p.read_bytes().decode("utf-8")
        self.assertEqual(n, 1)
        lines = out.split("\r\n")
        self.assertIn("mycal:https://one.org/cal/", lines[1])
        self.assertIn('"Town, ST"', lines[1])              # quoting preserved
        self.assertEqual(lines[2], text.split("\r\n")[2])  # other rows byte-identical
        self.assertTrue(out.endswith("\r\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
