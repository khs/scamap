"""
test_map_links.py
-----------------
Offline tests for pasted map links: clean_sca_events strips them from the
displayed/geocoded address, and geocode_sca_events reads the pin out of the link
(full Google/OSM URLs directly, short maps.app.goo.gl links via one cached
redirect — mocked here, no network). Also covers the file: feed's tolerance of
a browser "Save page as" HTML wrapper (antir.ics).

Run:
    python -m unittest test_map_links -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import clean_sca_events as clean
import geocode_sca_events as geo
import ImportMaps

SHORT = "https://maps.app.goo.gl/iuUputabKvJbPErj9"
FULL = ("https://www.google.com/maps/place/Waipara+Adventure+Centre/@-43.07,172.74,636m"
        "/data=!3m2!1e3!4b1!4m6!3m5!1s0x6d:0x87!8m2!3d-43.0709561!4d172.7444703")


class TestStripFromLocation(unittest.TestCase):
    def test_labelled_short_link_removed(self):
        got = clean.clean_location("Waipara Adventure Centre 137 Darnley Road, Amberley, "
                                   f"New Zealand Google Maps link: {SHORT}")
        self.assertEqual(got, ("137 Darnley Road, Amberley, New Zealand", "high"))

    def test_parenthesised_full_link_removed(self):
        got = clean.clean_location(f"Town Hall, 1 Main St, Foo, OH (map: {FULL})")
        self.assertEqual(got[0], "1 Main St, Foo, OH")

    def test_link_only_is_empty(self):
        self.assertEqual(clean.clean_location(SHORT), ("", "empty"))

    def test_other_urls_untouched(self):
        # Only MAP links are stripped; a group website in the text stays.
        got = clean.clean_location("Hall, 1 Main St, Foo, OH https://example.org/site")
        self.assertIn("example.org", got[0])


class TestCoordsFromUrl(unittest.TestCase):
    def test_place_pin_beats_viewport(self):
        self.assertEqual(geo.coords_from_map_url(FULL), (-43.0709561, 172.7444703))

    def test_query_and_osm_forms(self):
        self.assertEqual(geo.coords_from_map_url(
            "https://maps.google.com/?q=39.4170948,-94.5083404"), (39.4170948, -94.5083404))
        self.assertEqual(geo.coords_from_map_url(
            "https://www.openstreetmap.org/?mlat=51.5&mlon=-0.12#map=15/51.5/-0.12"),
            (51.5, -0.12))

    def test_viewport_fallback(self):
        self.assertEqual(geo.coords_from_map_url(
            "https://www.google.com/maps/@40.9749376,-80.138691,15z"), (40.9749376, -80.138691))

    def test_no_coords(self):
        self.assertEqual(geo.coords_from_map_url("https://www.google.com/maps/search/Foo"),
                         (None, None))


class TestShortLinkResolution(unittest.TestCase):
    def _resp(self, location):
        r = mock.Mock()
        r.headers = {"Location": location} if location else {}
        return r

    def test_redirect_followed_and_cached(self):
        s = mock.Mock()
        s.get.return_value = self._resp(FULL)
        cache = {}
        got = geo.extract_map_link_coords(f"see {SHORT}", cache, session=s)
        self.assertEqual(got, (-43.0709561, 172.7444703))
        self.assertEqual(cache[SHORT], FULL)
        # Second call is served from the cache: no new request.
        geo.extract_map_link_coords(f"see {SHORT}", cache, session=s)
        self.assertEqual(s.get.call_count, 1)

    def test_eu_consent_wrapper_unwrapped(self):
        from urllib.parse import quote
        s = mock.Mock()
        s.get.return_value = self._resp(
            "https://consent.google.com/ml?continue=" + quote(FULL, safe=""))
        self.assertEqual(geo.extract_map_link_coords(SHORT, {}, session=s),
                         (-43.0709561, 172.7444703))

    def test_network_error_not_cached(self):
        s = mock.Mock()
        s.get.side_effect = geo.requests.ConnectionError("down")
        cache = {}
        self.assertEqual(geo.extract_map_link_coords(SHORT, cache, session=s), (None, None))
        self.assertEqual(cache, {})


class TestFileFeedHtmlWrapper(unittest.TestCase):
    def test_save_page_as_wrapper_stripped(self):
        body = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\n"
                "DTSTART;VALUE=DATE:20270101\r\nSUMMARY:X\r\nEND:VEVENT\r\nEND:VCALENDAR")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wrapped.ics"
            p.write_text('<html><head>\r\n<meta charset="UTF-8"></head><body>'
                         + body + "</body></html>", encoding="utf-8")
            with mock.patch.object(ImportMaps, "SCRIPT_DIR", Path(d)):
                cal = ImportMaps.fetch_ics({"id": "file:wrapped.ics", "source": "T",
                                            "type": "kingdom"})
        self.assertIsNotNone(cal)
        self.assertEqual(len(cal.walk("VEVENT")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
