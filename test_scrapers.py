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
from datetime import datetime, timezone
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


class TestDateRobustness(unittest.TestCase):
    """A single typo'd source year (e.g. Northshield's 'The Catfish Ball'
    shipping end_date '0026-11-21') used to crash the whole feed: glibc's
    strftime('%Y') doesn't zero-pad year 26, emitting a 6-digit ICS date that
    icalendar refuses to parse. Lock in that we never emit such a date."""

    def test_fmt_utc_zero_pads_short_years(self):
        got = scrapers._fmt_utc(datetime(26, 11, 21, tzinfo=timezone.utc))
        self.assertEqual(got, "00261121T000000Z")     # 8-digit year, well-formed
        self.assertFalse(got.startswith("26"))         # NOT the 6-digit form

    def test_fmt_utc_normal_year(self):
        self.assertEqual(
            scrapers._fmt_utc(datetime(2026, 7, 1, 9, 30, 0, tzinfo=timezone.utc)),
            "20260701T093000Z")

    def test_plausible_year_bounds(self):
        self.assertTrue(scrapers._plausible_year(datetime(2026, 1, 1)))
        self.assertFalse(scrapers._plausible_year(datetime(26, 1, 1)))
        self.assertFalse(scrapers._plausible_year(datetime(3000, 1, 1)))

    def _emit_one(self, start, end, summary="E"):
        return scrapers._emit_calendar("Test", [{
            "uid": "u", "start": start, "end": end, "summary": summary,
            "location": "", "description": "", "url": ""}])

    def test_emit_clamps_bad_end_year_but_keeps_event(self):
        # Northshield "Catfish Ball": good start, end year 0026. Keep the event,
        # clamp DTEND to DTSTART, never emit the malformed 6-digit date.
        ics = self._emit_one(datetime(2026, 11, 21), datetime(26, 11, 21), "Catfish")
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)       # kept, not dropped
        self.assertIn("Catfish", ics)
        self.assertNotIn("DTEND:261121T", ics)               # no malformed 6-digit date
        self.assertIn("DTSTART:20261121T000000Z", ics)
        self.assertIn("DTEND:20261121T000000Z", ics)         # clamped to start

    def test_emit_clamps_backwards_range_with_valid_years(self):
        # Drachenwald "Smouldering Arrow 5": end a month before start, both
        # valid years — the year-only guard misses it; the end<start clamp catches it.
        ics = self._emit_one(datetime(2026, 7, 4), datetime(2026, 6, 7), "Smoulder")
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)       # kept
        self.assertIn("DTSTART:20260704T000000Z", ics)
        self.assertIn("DTEND:20260704T000000Z", ics)         # clamped to start

    def test_emit_drops_only_on_bad_start(self):
        # A bogus START has no safe fallback — drop just that event, keep the rest.
        evs = [
            {"uid": "a", "start": datetime(2026, 7, 1), "end": datetime(2026, 7, 2),
             "summary": "Good", "location": "", "description": "", "url": ""},
            {"uid": "b", "start": datetime(26, 11, 21), "end": datetime(2026, 11, 21),
             "summary": "BadStart", "location": "", "description": "", "url": ""},
        ]
        ics = scrapers._emit_calendar("Test", evs)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)
        self.assertIn("Good", ics)
        self.assertNotIn("BadStart", ics)
        self.assertNotIn("261121T", ics)


class _FakeResp:
    def __init__(self, status_code, text="[]"):
        self.status_code = status_code
        self.text = text                 # body, for the expect_json soft-block check


class _FakeGetter:
    """Records each .get() call's headers and returns scripted responses.
    Each element of `responses` is an int status code or a _FakeResp."""
    def __init__(self, responses):
        self.responses = [r if isinstance(r, _FakeResp) else _FakeResp(r)
                          for r in responses]
        self.calls = []          # list of (url, headers)

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers))
        return self.responses[len(self.calls) - 1]


class TestWafFallback(unittest.TestCase):
    """_http_get keeps our honest UA by default and escalates on a WAF block:
    first a Chrome TLS fingerprint via curl_cffi, then plain browser headers if
    curl_cffi is unavailable. The Calontir/Middle case. We stub _curl_cffi_get
    so these stay offline."""

    def setUp(self):
        self._real_cffi = scrapers._curl_cffi_get
        scrapers._TLS_REQUIRED_HOSTS.clear()     # don't leak the host-memo across tests

    def tearDown(self):
        scrapers._curl_cffi_get = self._real_cffi
        scrapers._TLS_REQUIRED_HOSTS.clear()

    def _stub_cffi(self, return_value):
        scrapers._curl_cffi_get = lambda url, **kw: return_value

    def test_no_block_uses_honest_ua_once(self):
        self._stub_cffi(None)
        g = _FakeGetter([200])
        scrapers._http_get("https://x.test/a", session=g)
        self.assertEqual(len(g.calls), 1)
        self.assertEqual(g.calls[0][1], scrapers.HTTP_HEADERS)

    def test_403_uses_curl_cffi_when_it_succeeds(self):
        # curl_cffi defeats the WAF -> its response is returned, no 2nd getter call.
        self._stub_cffi(_FakeResp(200))
        g = _FakeGetter([403])
        resp = scrapers._http_get("https://x.test/a", session=g)
        self.assertEqual(len(g.calls), 1)                 # only the honest attempt
        self.assertEqual(g.calls[0][1], scrapers.HTTP_HEADERS)
        self.assertEqual(resp.status_code, 200)           # the curl_cffi response

    def test_403_falls_back_to_browser_headers_without_curl_cffi(self):
        self._stub_cffi(None)                             # curl_cffi unavailable
        g = _FakeGetter([403, 200])
        resp = scrapers._http_get("https://x.test/a", session=g)
        self.assertEqual(len(g.calls), 2)
        self.assertEqual(g.calls[0][1], scrapers.HTTP_HEADERS)     # honest first
        self.assertEqual(g.calls[1][1], scrapers.BROWSER_HEADERS)  # then browser
        self.assertEqual(resp.status_code, 200)

    def test_503_and_429_also_escalate(self):
        self._stub_cffi(None)
        for code in (503, 429):
            g = _FakeGetter([code, 200])
            scrapers._http_get("https://x.test/a", session=g)
            self.assertEqual(len(g.calls), 2, f"{code} should trigger one retry")

    def test_does_not_retry_on_404(self):
        self._stub_cffi(_FakeResp(200))
        g = _FakeGetter([404])
        scrapers._http_get("https://x.test/a", session=g)
        self.assertEqual(len(g.calls), 1)        # a real 404 isn't a WAF block

    def test_passes_params_through(self):
        self._stub_cffi(None)
        g = _FakeGetter([200])
        scrapers._http_get("https://x.test/a", session=g, params={"page": 2})
        self.assertEqual(len(g.calls), 1)

    def test_non_json_200_escalates_when_expect_json(self):
        # Northshield's soft block: 200 status but an HTML challenge body.
        self._stub_cffi(_FakeResp(200, text='[{"ok":1}]'))
        g = _FakeGetter([_FakeResp(200, text="<html>Just a moment…</html>")])
        resp = scrapers._http_get("https://x.test/a", session=g, expect_json=True)
        self.assertEqual(len(g.calls), 1)               # honest, then curl_cffi
        self.assertEqual(resp.text, '[{"ok":1}]')       # curl_cffi response used

    def test_non_json_200_is_fine_without_expect_json(self):
        # An HTML page (a normal event page) must NOT be treated as a block.
        self._stub_cffi(_FakeResp(200))
        g = _FakeGetter([_FakeResp(200, text="<html>event page</html>")])
        scrapers._http_get("https://x.test/page", session=g)   # expect_json=False
        self.assertEqual(len(g.calls), 1)               # no escalation

    def test_blocked_host_is_memoized_and_skips_honest_next_time(self):
        self._stub_cffi(_FakeResp(200))
        g = _FakeGetter([403])                          # first call: blocked
        scrapers._http_get("https://memo.test/a", session=g)
        self.assertIn("memo.test", scrapers._TLS_REQUIRED_HOSTS)
        # Second call to the same host goes straight to curl_cffi — honest
        # getter is not called again.
        scrapers._http_get("https://memo.test/b", session=g)
        self.assertEqual(len(g.calls), 1)               # still just the first call


if __name__ == "__main__":
    unittest.main(verbosity=2)
