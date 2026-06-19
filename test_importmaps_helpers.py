"""
test_importmaps_helpers.py
--------------------------
Offline tests for the pure ICS-parsing helpers in ImportMaps.py. No network —
these exercise text/datetime/URL normalisation that the rest of the fetch
pipeline depends on.

Covered:
  - clean_text        ICS backslash-unescaping + HTML-entity decode + whitespace
  - is_virtual_event  location/title precedence over a description false-positive
                      (the Highlands War / Summer's End regression)
  - get_datetime      tz-aware -> naive UTC, date pass-through, missing -> None
  - get_text          present/absent field extraction
  - make_ics_url      full-URL pass-through vs. bare-id wrapping

Run:
    python -m unittest test_importmaps_helpers -v
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from icalendar import Event

import ImportMaps as m


class TestCleanText(unittest.TestCase):
    def test_ics_backslash_unescaping(self):
        # \,  ->  ,    \;  ->  ;    \n / \N  ->  space
        self.assertEqual(m.clean_text(r"a\, b\; c\n d\N e"), "a, b; c d e")

    def test_html_entity_decoding(self):
        self.assertEqual(m.clean_text("Tom &amp; Jerry"), "Tom & Jerry")
        self.assertEqual(m.clean_text("it&#39;s here"), "it's here")
        # &#8217; is the right single quotation mark (U+2019)
        self.assertEqual(m.clean_text("don&#8217;t"), "don’t")
        self.assertEqual(m.clean_text("&lt;tag&gt; &quot;x&quot;"), '<tag> "x"')
        self.assertEqual(m.clean_text("a&nbsp;b"), "a b")

    def test_newlines_and_cr_become_space(self):
        self.assertEqual(
            m.clean_text("line1\r\nline2\rline3\nline4"),
            "line1 line2 line3 line4",
        )

    def test_multi_space_collapse_and_strip(self):
        self.assertEqual(m.clean_text("a    b      c"), "a b c")
        self.assertEqual(m.clean_text("  trim me  "), "trim me")

    def test_empty_and_none_pass_through(self):
        # Falsy input is returned unchanged (no crash, no coercion).
        self.assertEqual(m.clean_text(""), "")
        self.assertIsNone(m.clean_text(None))


class TestIsVirtualEvent(unittest.TestCase):
    def test_online_location_is_virtual(self):
        self.assertTrue(m.is_virtual_event("Fighter Practice", "Online", ""))

    def test_zoom_url_location_is_virtual(self):
        self.assertTrue(m.is_virtual_event("A&S Night", "https://zoom.us/j/123", ""))

    def test_virtual_in_title_is_virtual(self):
        # An explicit "(Virtual)" tag in the title wins even with a real location.
        self.assertTrue(m.is_virtual_event("Curia (Virtual)", "Some Hall, NY", ""))

    def test_physical_location_beats_discord_in_description(self):
        # Highlands War regression: real camping site, description mentions
        # Discord/online — a physical location must win, so NOT virtual.
        self.assertFalse(m.is_virtual_event(
            "Highlands War",
            "Camp Raymond, Flagstaff, AZ",
            "Coordination posted to our Discord event page; register online.",
        ))

    def test_physical_location_beats_online_in_description(self):
        # Summer's End regression: real NY address, description happens to say
        # "virtual"/"online" — still in-person.
        self.assertFalse(m.is_virtual_event(
            "Summer's End",
            "123 Main St, Rochester, NY",
            "No virtual attendance available; please register online.",
        ))

    def test_empty_location_falls_back_to_virtual_description(self):
        # With no location at all, a virtual keyword in the description wins.
        self.assertTrue(m.is_virtual_event("Meeting", "", "Join us on Zoom"))

    def test_blank_whitespace_location_falls_back_to_description(self):
        # A whitespace-only location is treated as no location.
        self.assertTrue(m.is_virtual_event("Meeting", "   ", "Happening online"))

    def test_empty_location_plain_description_is_not_virtual(self):
        self.assertFalse(m.is_virtual_event("Meeting", "", "Come join us in person"))

    def test_discord_alone_is_not_a_keyword(self):
        # "discord" is deliberately excluded from VIRTUAL_KEYWORDS.
        self.assertFalse(m.is_virtual_event("Meeting", "discord", ""))


class TestGetDatetime(unittest.TestCase):
    def test_tzaware_normalised_to_naive_utc(self):
        ev = Event()
        # 12:00 at UTC-5  ==  17:00 UTC
        ev.add("dtstart", datetime(2026, 7, 1, 12, 0, 0,
                                   tzinfo=timezone(timedelta(hours=-5))))
        got = m.get_datetime(ev, "DTSTART")
        self.assertEqual(got, datetime(2026, 7, 1, 17, 0, 0))
        self.assertIsNone(got.tzinfo)           # made naive

    def test_date_passes_through_unchanged(self):
        ev = Event()
        ev.add("dtstart", date(2026, 7, 1))
        got = m.get_datetime(ev, "DTSTART")
        self.assertEqual(got, date(2026, 7, 1))
        self.assertNotIsInstance(got, datetime)  # still a plain date

    def test_missing_field_returns_none(self):
        self.assertIsNone(m.get_datetime(Event(), "DTEND"))


class TestGetText(unittest.TestCase):
    def test_present_field_returned(self):
        ev = Event()
        ev.add("summary", "My Event Title")
        self.assertEqual(m.get_text(ev, "SUMMARY"), "My Event Title")

    def test_absent_field_returns_default(self):
        self.assertEqual(m.get_text(Event(), "DESCRIPTION"), "")
        self.assertEqual(m.get_text(Event(), "DESCRIPTION", "N/A"), "N/A")


class TestMakeIcsUrl(unittest.TestCase):
    GCAL = "https://calendar.google.com/calendar/ical/{cid}/public/basic.ics"

    def test_full_url_passed_through(self):
        self.assertEqual(
            m.make_ics_url("https://gleannabhann.net/events/?ical=1"),
            "https://gleannabhann.net/events/?ical=1",
        )
        self.assertEqual(
            m.make_ics_url("http://example.com/feed.ics"),
            "http://example.com/feed.ics",
        )

    def test_bare_google_calendar_id_wrapped(self):
        cid = "abc123@group.calendar.google.com"
        self.assertEqual(m.make_ics_url(cid), self.GCAL.format(cid=cid))

    def test_bare_email_id_wrapped(self):
        cid = "ealdormere@gmail.com"
        self.assertEqual(m.make_ics_url(cid), self.GCAL.format(cid=cid))

    def test_surrounding_whitespace_stripped(self):
        self.assertEqual(
            m.make_ics_url("  https://x.org/f.ics  "),
            "https://x.org/f.ics",
        )
        cid = "abc@group.calendar.google.com"
        self.assertEqual(
            m.make_ics_url(f"  {cid}  "),
            self.GCAL.format(cid=cid),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
