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


class TestStripStructuredBits(unittest.TestCase):
    """Per-kingdom form/metadata-block stripping found by the description audit."""
    def s(self, desc, source):
        return clean.strip_structured_bits(desc, source)

    def test_west_keeps_slice_between_markers(self):
        d = ("Event Steward SCA Name: Krok Event Site Open: Aug 1 "
             "Further Event Information: Come join the Shire for rapier! "
             "Site Name: The Hall Site Address: 1 Main St Adult Registration: $20")
        out = self.s(d, "Kingdom of the West")
        self.assertEqual(out, "Come join the Shire for rapier!")

    def test_west_zoom_only_blanked(self):
        d = "West Kingdom Zoom is inviting you to a scheduled Zoom meeting. Meeting ID: 123"
        self.assertEqual(self.s(d, "Kingdom of the West"), "")

    def test_calontir_cuts_before_details(self):
        d = "Calontir Fall Crown Food trucks TBD Details Site Opens: Sat Nov 14 Gate Fees Member Registration: $20"
        self.assertEqual(self.s(d, "Kingdom of Calontir"), "Calontir Fall Crown Food trucks TBD")

    def test_calontir_widget_only_blanked(self):
        d = "Details Site Opens: Site Closes: Address X Gate Fees Make checks payable to SCA Inc."
        self.assertEqual(self.s(d, "Kingdom of Calontir"), "")

    def test_northshield_same_rule(self):
        d = "Bardic Blades is a relaxed camping event. Details Site Opens: Sat June 27 Gate Fees"
        self.assertEqual(self.s(d, "Kingdom of Northshield"), "Bardic Blades is a relaxed camping event.")

    def test_east_cuts_before_site_opens(self):
        d = "The Barony invites all to our annual event. Site Opens: 9AM Site Closes: 10PM"
        self.assertEqual(self.s(d, "Kingdom of the East"), "The Barony invites all to our annual event.")

    def test_meridies_hosted_by_stub_blanked(self):
        self.assertEqual(self.s("Hosted by Shire of Crimson River", "Kingdom of Meridies"), "")

    def test_drachenwald_strips_hosted_prefix(self):
        d = "Hosted by: Holmrike | A weekend event focused on Archery."
        self.assertEqual(self.s(d, "Kingdom of Drachenwald"), "A weekend event focused on Archery.")

    def test_antir_level_prefix_stripped(self):
        d = ("This is a Level 1: Other (Branch primary events of regional or Kingdom interest) event. "
             "Come join the Shire for a day of fun.")
        self.assertEqual(self.s(d, "Kingdom of An Tir"), "Come join the Shire for a day of fun.")

    def test_artemisia_cuts_before_site_opens_spaced(self):
        d = "A gentle gathering of good folk. Site opens : 09:00 Site closes : 21:00 Adult Registration : $20"
        self.assertEqual(self.s(d, "Kingdom of Artemisia"), "A gentle gathering of good folk.")

    def test_ansteorra_cuts_before_event_website(self):
        d = "Day of Classes, A&S, Bardic, and revelry. Event Website: PayPal PreRegistration:"
        self.assertEqual(self.s(d, "Kingdom of Ansteorra"), "Day of Classes, A&S, Bardic, and revelry.")

    def test_ealdormere_cuts_at_caps_header(self):
        d = "Baron's Brouhaha is a laid-back camping event. SCHEDULE: Friday gate opens 4pm"
        self.assertEqual(self.s(d, "Kingdom of Ealdormere"), "Baron's Brouhaha is a laid-back camping event.")

    def test_marker_is_source_scoped(self):
        # "Site Opens:" only strips for the East — a kingdom without that rule
        # whose real prose mentions it must NOT be truncated.
        d = "Our hall has odd hours. Site Opens: when the bell rings. Come anytime!"
        self.assertEqual(self.s(d, "Kingdom of Atlantia"),
                         "Our hall has odd hours. Site Opens: when the bell rings. Come anytime!")

    def test_clean_description_untouched_modulo_whitespace(self):
        d = "A grand tournament weekend with feast and dancing."
        self.assertEqual(self.s(d, "Kingdom of Lochac"), d)

    def test_empty_and_none(self):
        self.assertEqual(self.s("", "Kingdom of the West"), "")
        self.assertEqual(self.s(None, "Kingdom of Calontir"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
