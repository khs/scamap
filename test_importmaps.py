"""
test_importmaps.py
------------------
Offline tests for ImportMaps.expand_events — the per-event expansion fallback
that protects EVERY feed (direct-ICS kingdoms and baronies included, not just
the scraped ones) from a single malformed event zeroing the whole feed.

This is the failure that took Northshield to 0: one event with a 0026-for-2026
year typo makes recurring_ical_events' bulk .between() raise, which used to
discard the entire kingdom. The fallback isolates the bad event and keeps the
rest.

Run:
    python -m unittest test_importmaps -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

import recurring_ical_events
from icalendar import Calendar

import ImportMaps


def _ics(*vevents: str) -> str:
    body = "\n".join(vevents)
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        f"{body}\nEND:VCALENDAR\n"
    )


def _ve(uid, dtstart, dtend, summary):
    return (f"BEGIN:VEVENT\nUID:{uid}\nDTSTAMP:20260101T000000Z\n"
            f"DTSTART:{dtstart}\nDTEND:{dtend}\nSUMMARY:{summary}\nEND:VEVENT")


WINDOW = (datetime(2026, 1, 1), datetime(2027, 12, 31))


class TestExpandEvents(unittest.TestCase):
    def _summaries(self, components):
        return sorted(str(c.get("SUMMARY")) for c in components)

    def test_clean_feed_passes_through(self):
        cal = Calendar.from_ical(_ics(
            _ve("a", "20260701T000000Z", "20260702T000000Z", "Alpha"),
            _ve("b", "20260801T000000Z", "20260801T000000Z", "Beta"),
        ))
        got = ImportMaps.expand_events(cal, *WINDOW, "Clean")
        self.assertEqual(self._summaries(got), ["Alpha", "Beta"])

    def test_one_bad_date_does_not_zero_the_feed(self):
        # The Northshield repro: a 6-digit (year-0026) DTEND crashes the bulk
        # expansion; the good events must still survive.
        cal = Calendar.from_ical(_ics(
            _ve("g1", "20260701T000000Z", "20260702T000000Z", "Good One"),
            _ve("bad", "20261121T000000Z", "261121T000000Z", "Catfish Ball"),
            _ve("g2", "20260801T000000Z", "20260801T000000Z", "Good Two"),
        ))
        # Confirm the bulk path really does raise (i.e. the fallback is exercised).
        with self.assertRaises(Exception):
            recurring_ical_events.of(cal).between(*WINDOW)
        got = ImportMaps.expand_events(cal, *WINDOW, "Northshield")
        self.assertEqual(self._summaries(got), ["Good One", "Good Two"])

    def test_all_bad_returns_empty_not_crash(self):
        cal = Calendar.from_ical(_ics(
            _ve("bad", "00261121T000000Z", "261121T000000Z", "Only Bad"),
        ))
        got = ImportMaps.expand_events(cal, *WINDOW, "AllBad")
        self.assertEqual(got, [])          # no crash, just empty

    def test_empty_feed(self):
        cal = Calendar.from_ical(_ics())
        self.assertEqual(ImportMaps.expand_events(cal, *WINDOW, "Empty"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
