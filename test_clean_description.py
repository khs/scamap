"""
test_clean_description.py
-------------------------
Offline tests for clean_sca_events' description cleaning and URL extraction —
the logic that produces the popup text users read and the per-event links the
map points at. Covers PII/boilerplate stripping, HTML handling, widget/
placeholder blanking, and the event-vs-Facebook URL extractors.

Run:
    python -m unittest test_clean_description -v
"""
from __future__ import annotations

import unittest

import clean_sca_events as clean


class TestCleanDescription(unittest.TestCase):
    def _text(self, desc):
        return clean.clean_description(desc)[0]

    def _urls(self, desc):
        return clean.clean_description(desc)[1]

    def test_empty_and_none(self):
        self.assertEqual(clean.clean_description(""),
                         ("", {"event_url": None, "facebook_url": None}))
        self.assertEqual(clean.clean_description(None),
                         ("", {"event_url": None, "facebook_url": None}))
        self.assertEqual(clean.clean_description("   "),
                         ("", {"event_url": None, "facebook_url": None}))

    def test_email_removed(self):
        out = self._text("Questions to steward@example.org for details.")
        self.assertNotIn("@", out)
        self.assertIn("Questions to", out)

    def test_phone_removed(self):
        self.assertNotIn("555", self._text("Call 555-123-4567 to reserve."))

    def test_autocrat_contact_line_removed(self):
        out = self._text("A fun day. Autocrat: Lady Jane of Anytown")
        self.assertIn("A fun day", out)
        self.assertNotIn("Autocrat", out)
        self.assertNotIn("Jane", out)

    def test_po_box_removed(self):
        self.assertNotIn("Box", self._text("Send checks to P.O. Box 1234, Anytown."))

    def test_event_url_extracted_and_stripped_from_body(self):
        desc = "See https://midrealm.org/events/smurf-shoot-4/ for info."
        text, urls = clean.clean_description(desc)
        self.assertEqual(urls["event_url"], "https://midrealm.org/events/smurf-shoot-4/")
        self.assertNotIn("http", text)            # URL removed from prose

    def test_facebook_url_classified_separately(self):
        urls = self._urls("Details on https://www.facebook.com/events/12345/ soon.")
        self.assertEqual(urls["facebook_url"], "https://www.facebook.com/events/12345/")
        self.assertIsNone(urls["event_url"])

    def test_html_tags_stripped(self):
        text = self._text("<p>Come <b>fight</b> with us!</p>")
        self.assertNotIn("<", text)
        self.assertIn("Come", text)
        self.assertIn("fight", text)

    def test_widget_boilerplate_blanked(self):
        marker = clean.WIDGET_BOILERPLATE_MARKERS[0]
        text, urls = clean.clean_description(f"junk {marker} more junk")
        self.assertEqual(text, "")
        self.assertEqual(urls, {"event_url": None, "facebook_url": None})

    def test_whitespace_collapsed(self):
        self.assertNotIn("  ", self._text("Lots    of\t\tspace   here"))


class TestExtractUrlsFromText(unittest.TestCase):
    def test_event_and_facebook_split(self):
        urls = clean.extract_urls_from_text(
            "Flyer at https://kingdom.org/e/5 and https://facebook.com/events/9")
        self.assertEqual(urls["event_url"], "https://kingdom.org/e/5")
        self.assertEqual(urls["facebook_url"], "https://facebook.com/events/9")

    def test_trailing_punctuation_stripped(self):
        urls = clean.extract_urls_from_text("Info at https://kingdom.org/page).")
        self.assertEqual(urls["event_url"], "https://kingdom.org/page")

    def test_no_urls(self):
        self.assertEqual(clean.extract_urls_from_text("no links here"),
                         {"event_url": None, "facebook_url": None})

    def test_first_event_url_wins(self):
        urls = clean.extract_urls_from_text("https://a.org/1 then https://b.org/2")
        self.assertEqual(urls["event_url"], "https://a.org/1")


class TestExtractUrlsFromHtml(unittest.TestCase):
    def test_event_website_link_text(self):
        html = '<a href="https://kingdom.org/event/42">Event Website</a>'
        self.assertEqual(clean.extract_urls_from_html(html)["event_url"],
                         "https://kingdom.org/event/42")

    def test_facebook_link_classified(self):
        html = '<a href="https://www.facebook.com/events/7">FB</a>'
        urls = clean.extract_urls_from_html(html)
        self.assertEqual(urls["facebook_url"], "https://www.facebook.com/events/7")

    def test_fallback_first_non_facebook_link(self):
        html = ('<a href="https://www.facebook.com/x">fb</a>'
                '<a href="https://kingdom.org/real">Details</a>')
        urls = clean.extract_urls_from_html(html)
        self.assertEqual(urls["event_url"], "https://kingdom.org/real")
        self.assertEqual(urls["facebook_url"], "https://www.facebook.com/x")

    def test_no_links(self):
        self.assertEqual(clean.extract_urls_from_html("<p>no links</p>"),
                         {"event_url": None, "facebook_url": None})


if __name__ == "__main__":
    unittest.main(verbosity=2)
