"""
test_scrapers.py
----------------
Offline tests for the scraper adapters. No network — they exercise the pure
HTML-parsing helpers against fixtures.

Focus: the EventPrime (Avacal) adapter, whose event data is pulled from the
"Add to Google Calendar" link on each event page. That link is the part most
likely to drift if the source site changes, so lock its parsing in.

Run:
    python -m unittest test_scrapers -v
"""
from __future__ import annotations

import unittest
from urllib.parse import urlencode

import scrapers


def _event_page(text, dates, location, details_html):
    """Build a minimal event-page fixture carrying a Google-Calendar link."""
    href = "https://www.google.com/calendar/event?" + urlencode({
        "action": "TEMPLATE",
        "text": text,
        "dates": dates,
        "location": location,
        "details": details_html,
    })
    return f'<html><body><a href="{href}">Add to Google Calendar</a></body></html>'


class TestEventPrimeParse(unittest.TestCase):
    def test_parses_all_fields(self):
        html = _event_page(
            "Quad War XXIX",
            "20260701T000000Z/20260705T235900Z",
            "Some Park, Warburg, AB",
            "<p>Come <b>fight</b>   with us!</p>",
        )
        ev = scrapers._parse_eventprime_event(html)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["summary"], "Quad War XXIX")
        self.assertEqual(ev["start"], "20260701T000000Z")
        self.assertEqual(ev["end"], "20260705T235900Z")
        self.assertEqual(ev["location"], "Some Park, Warburg, AB")
        # HTML stripped and whitespace collapsed
        self.assertEqual(ev["description"], "Come fight with us!")

    def test_none_without_gcal_link(self):
        self.assertIsNone(scrapers._parse_eventprime_event("<html><body>no link here</body></html>"))

    def test_none_with_malformed_dates(self):
        self.assertIsNone(scrapers._parse_eventprime_event(_event_page("X", "not-a-date", "loc", "d")))

    def test_none_with_empty_title(self):
        self.assertIsNone(scrapers._parse_eventprime_event(
            _event_page("", "20260701T000000Z/20260705T235900Z", "loc", "d")))

    def test_dates_regex(self):
        m = scrapers._GCAL_DATES_RE.match("20260701T000000Z/20260705T235900Z")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "20260701T000000Z")
        self.assertEqual(m.group(2), "20260705T235900Z")


class TestDrachenwaldJson(unittest.TestCase):
    REAL = {
        "type": "event", "status": "official",
        "event-name": "PolderSlaughter",
        "start-date": "2026-06-12", "end-date": "2026-06-14", "start-time": "16:00",
        "site-address": "Buitencentrum de Voshaar, Oude Boekelerdijk 40", "town": "Enschede",
        "country": "Netherlands", "summary": "A grand war.", "host-branch": "Polderslot",
        "website": "polderslaughter.polderslot.info", "vc-url": "", "slug": "polderslot/polderslaughter",
    }
    BID = {"type": "other", "event-name": "Bids due for X", "start-date": "1899-12-30", "end-date": "1899-12-31"}
    CANCELLED = {"type": "event", "status": "cancelled", "event-name": "Off",
                 "start-date": "2026-07-01", "end-date": "2026-07-01"}
    ONLINE = {"type": "event", "status": "online", "event-name": "Virtual A&S",
              "start-date": "2026-06-20", "end-date": "2026-06-20",
              "site-address": "", "town": "", "country": "", "vc-url": "https://zoom.us/j/123"}

    def events(self, *recs):
        return scrapers._drachenwald_events_from_records(list(recs))

    def test_real_event_fields(self):
        ev = self.events(self.REAL)[0]
        self.assertEqual(ev["summary"], "PolderSlaughter")
        self.assertEqual(ev["start"].strftime("%Y-%m-%d"), "2026-06-12")
        self.assertEqual(ev["end"].strftime("%Y-%m-%d"), "2026-06-14")
        self.assertIn("Buitencentrum de Voshaar", ev["location"])
        self.assertIn("Netherlands", ev["location"])
        self.assertTrue(ev["url"].startswith("https://"))

    def test_drops_bid_and_cancelled(self):
        self.assertEqual(self.events(self.BID, self.CANCELLED), [])

    def test_online_event_uses_vc_url(self):
        ev = self.events(self.ONLINE)[0]
        self.assertEqual(ev["location"], "https://zoom.us/j/123")
        self.assertIn("Online: https://zoom.us/j/123", ev["description"])

    def test_mixed_batch_counts(self):
        evs = self.events(self.REAL, self.BID, self.CANCELLED, self.ONLINE)
        self.assertEqual(len(evs), 2)   # REAL + ONLINE only


if __name__ == "__main__":
    unittest.main(verbosity=2)
