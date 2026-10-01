"""
test_health_reminders.py
------------------------
Offline tests for health_check's reminders about hand-maintained calendar
files (An Tir's antir.ics): re-download monthly, or sooner if its events are
about to run out.

Run:
    python -m unittest test_health_reminders -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import health_check as h

ICS = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTAMP:20260901T010000Z\r\n"
       "DTSTART;TZID=America/Los_Angeles:20261219T100000\r\nSUMMARY:Yule\r\n"
       "END:VEVENT\r\nEND:VCALENDAR\r\n")


class TestManualFeedReminders(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "calendars.csv").write_text(
            "id,source,type\nfile:antir.ics,Kingdom of An Tir,kingdom\nabc@x,Kingdom of B,kingdom\n",
            encoding="utf-8")
        self.p = mock.patch.multiple(h, SCRIPT_DIR=self.tmp,
                                     CALENDARS_FILE=self.tmp / "calendars.csv",
                                     REMINDER_FILE=self.tmp / "health_reminder.md")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_fresh_file_with_events_ahead_is_quiet(self):
        (self.tmp / "antir.ics").write_text(ICS, encoding="utf-8")
        self.assertEqual(h.manual_feed_reminders(date(2026, 9, 20)), [])

    def test_month_old_download(self):
        (self.tmp / "antir.ics").write_text(ICS, encoding="utf-8")
        r = h.manual_feed_reminders(date(2026, 10, 15))
        self.assertEqual(len(r), 1)
        self.assertIn("downloaded 44 days ago", r[0])

    def test_running_out_of_events(self):
        (self.tmp / "antir.ics").write_text(ICS.replace("20260901", "20261201"), encoding="utf-8")
        r = h.manual_feed_reminders(date(2026, 12, 5))
        self.assertEqual(len(r), 1)
        self.assertIn("runs out of events soon (last event: 2026-12-19)", r[0])

    def test_missing_file(self):
        self.assertIn("missing", h.manual_feed_reminders(date(2026, 9, 20))[0])

    def test_reminder_file_written_and_removed(self):
        h._write_reminder_file(["An Tir: refresh it"])
        self.assertIn("An Tir: refresh it", (self.tmp / "health_reminder.md").read_text(encoding="utf-8"))
        h._write_reminder_file([])
        self.assertFalse((self.tmp / "health_reminder.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
