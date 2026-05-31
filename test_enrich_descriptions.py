"""
test_enrich_descriptions.py
---------------------------
Offline tests for the Atlantia description enrichment. No network — they run the
HTML parser against a fixture mirroring atlantia.sca.org's event-page markup, and
exercise the placeholder/triviality guards.

Run:
    python -m unittest test_enrich_descriptions -v
"""
from __future__ import annotations

import unittest

import enrich_descriptions as ed

# Mirrors the real page: fields are <div class="labelDiv">Label:</div> <div>value</div>
FIXTURE = """
<html><body><div class="entry-content">
  <h2>Allthing <br>(2026-06-20)</h2>
  <h3>Event Info</h3>
  <div class="labelDiv">Name:</div> <div>Allthing</div><br/>
  <div class="labelDiv">Group:</div> <div>Windmasters' Hill</div><br/>
  <div class="labelDiv">Description:</div> <div>The historic Icelandic Al&#254;ingi
       brought a   nation together.</div><br/>
  <div class="labelDiv">Email:</div> <div>steward@example.org</div><br/>
</div></body></html>
"""

NO_DESC_FIXTURE = """
<html><body><div class="entry-content">
  <div class="labelDiv">Name:</div> <div>Mystery Event</div><br/>
</div></body></html>
"""


class TestExtract(unittest.TestCase):
    def test_extracts_description_value(self):
        got = ed.extract_atlantia_description(FIXTURE)
        # whitespace collapsed to single spaces, the þ entity decoded
        self.assertEqual(got, "The historic Icelandic Alþingi brought a nation together.")

    def test_does_not_grab_other_fields(self):
        got = ed.extract_atlantia_description(FIXTURE)
        self.assertNotIn("steward@example.org", got)
        self.assertNotIn("Windmasters", got)

    def test_missing_description_returns_none(self):
        self.assertIsNone(ed.extract_atlantia_description(NO_DESC_FIXTURE))


EAST_FIXTURE = """
<html><body>
<article>
  <h2>A Day in the Park</h2>
  <div class="eventDetailsContent">
    The Shire of Blak Rose cordially invites you to join us at the
    Bandshell Pavilion on Saturday, May 30, 2026.
  </div>
</article>
</body></html>
"""


class TestExtractEast(unittest.TestCase):
    def test_extracts_eventDetailsContent(self):
        got = ed.extract_east_description(EAST_FIXTURE)
        self.assertIn("Shire of Blak Rose cordially invites", got)
        self.assertNotIn("<", got)   # tags stripped

    def test_missing_section_returns_none(self):
        self.assertIsNone(ed.extract_east_description("<html><body>no section</body></html>"))


class TestPlaceholder(unittest.TestCase):
    def test_matches_the_placeholder(self):
        self.assertTrue(ed.PLACEHOLDER_RE.match("Upcoming event in Storvik Event Flyer:"))
        self.assertTrue(ed.PLACEHOLDER_RE.match("Upcoming event in Windmasters' Hill Event Flyer:"))

    def test_ignores_a_real_writeup(self):
        self.assertFalse(ed.PLACEHOLDER_RE.match("Come join us! See the event flyer for details."))


class TestTriviality(unittest.TestCase):
    def test_coming_soon_is_not_real(self):
        self.assertFalse(ed.is_real("Coming soon"))
        self.assertFalse(ed.is_real(""))
        self.assertFalse(ed.is_real(None))
        self.assertFalse(ed.is_real("TBD"))

    def test_substantial_text_is_real(self):
        self.assertTrue(ed.is_real("A full weekend of tournaments, feast, and merriment."))


if __name__ == "__main__":
    unittest.main(verbosity=2)
