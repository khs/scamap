"""A crash in one calendar's scraper must not stop the whole refresh, and the
My Calendar adapter must work on a site whose page has no REST <link> tag."""
from __future__ import annotations

import unittest
from unittest import mock

import ImportMaps
import scrapers


class _Resp:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data if data is not None else []

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class TestMyCalendarWithoutApiLink(unittest.TestCase):
    def test_falls_back_to_wp_json_root(self):
        urls = []

        def fake_get(url, **kw):
            urls.append(url)
            return _Resp(text="<html>no api link here</html>")

        with mock.patch.object(scrapers, "_http_get", side_effect=fake_get):
            result = scrapers.scrape_my_calendar("https://example.org/calendar/", "Barony of X")
        self.assertIsNone(result)                       # no events, but no NameError
        self.assertTrue(any(u.startswith("https://example.org/wp-json/my-calendar/v1/events")
                            for u in urls), urls)


class TestFetchAllEventsSurvivesACrash(unittest.TestCase):
    def test_crash_is_treated_as_a_failed_fetch(self):
        cals = [{"type": "baronial", "source": "Barony of Broken", "id": "x"},
                {"type": "baronial", "source": "Barony of Fine", "id": "y"}]
        calls = []

        def fake_fetch(cal):
            calls.append(cal["source"])
            if cal["source"] == "Barony of Broken":
                raise NameError("name 'urljoin' is not defined")
            return None            # a clean "fetch failed" for the other one

        with mock.patch.object(ImportMaps, "fetch_ics", side_effect=fake_fetch):
            ImportMaps.fetch_all_events(cals)           # must not raise
        self.assertEqual(calls, ["Barony of Broken", "Barony of Fine"])


if __name__ == "__main__":
    unittest.main()
