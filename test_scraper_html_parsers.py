"""
test_scraper_html_parsers.py
----------------------------
Offline fixture tests for the HTML/JSON page parsers extracted from the
SimCal / NuEvent / MEC scrapers. These were previously inlined with the network
call (so untested); they're now pure functions that take page HTML and return
event dicts, mirroring the already-tested EventPrime and Drachenwald parsers.

Fixtures mirror each plugin's documented DOM. A selector change upstream that
breaks a kingdom's feed now fails here instead of silently emptying the map.

Run:
    python -m unittest test_scraper_html_parsers -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import scrapers


# ── SimCal (Windmasters' Hill) ─────────────────────────────────────────────
SIMCAL_TOOLTIP = """
<div class="simcal-event-details simcal-tooltip-content">
  <span class="simcal-event-title">Fighter Practice</span>
  <span class="simcal-event-start-date" data-event-start="{start}"></span>
  <span class="simcal-event-end-date" data-event-start="{end}"></span>
  <div class="simcal-event-address">
    <meta itemprop="address" content="123 Park Rd, Durham, NC 27701">
  </div>
  <div class="simcal-event-description">Bring armour and water.</div>
</div>
"""


class TestParseSimcal(unittest.TestCase):
    START = 1_782_000_000
    END = 1_782_003_600

    def _page(self, *tooltips):
        return "<html><body>" + "".join(tooltips) + "</body></html>"

    def test_single_event_fields(self):
        html = self._page(SIMCAL_TOOLTIP.format(start=self.START, end=self.END))
        evs = scrapers._parse_simcal_events(html, "http://wmh.example/")
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["summary"], "Fighter Practice")
        self.assertEqual(ev["location"], "123 Park Rd, Durham, NC 27701")
        self.assertEqual(ev["description"], "Bring armour and water.")
        self.assertEqual(ev["start"], datetime.fromtimestamp(self.START, tz=timezone.utc))
        self.assertEqual(ev["end"], datetime.fromtimestamp(self.END, tz=timezone.utc))

    def test_repeated_tooltip_deduped(self):
        tt = SIMCAL_TOOLTIP.format(start=self.START, end=self.END)
        evs = scrapers._parse_simcal_events(self._page(tt, tt), "http://wmh.example/")
        self.assertEqual(len(evs), 1)           # same (title, start) -> one event

    def test_missing_end_defaults_to_start_plus_hour(self):
        html = """<div class="simcal-event-details simcal-tooltip-content">
          <span class="simcal-event-title">A&S Night</span>
          <span class="simcal-event-start-date" data-event-start="1782000000"></span>
          </div>"""
        evs = scrapers._parse_simcal_events(html, "http://x/")
        self.assertEqual((evs[0]["end"] - evs[0]["start"]).total_seconds(), 3600)

    def test_missing_title_or_start_skipped(self):
        html = '<div class="simcal-event-details simcal-tooltip-content"><span class="simcal-event-title">No date</span></div>'
        self.assertEqual(scrapers._parse_simcal_events(html, "http://x/"), [])

    def test_non_integer_timestamp_skipped(self):
        html = """<div class="simcal-event-details simcal-tooltip-content">
          <span class="simcal-event-title">Bad TS</span>
          <span class="simcal-event-start-date" data-event-start="not-a-number"></span>
          </div>"""
        self.assertEqual(scrapers._parse_simcal_events(html, "http://x/"), [])

    def test_empty_page(self):
        self.assertEqual(scrapers._parse_simcal_events("<html></html>", "http://x/"), [])


# ── NuEvent (Meridies) ─────────────────────────────────────────────────────
NUEVENT_ARTICLE = """
<article class="event-list-item" id="event-1234">
  <h3 class="event-title"><a href="https://meridies.org/events/spring/">Spring Tourney</a></h3>
  <div class="event-host">Barony of Foo</div>
  <time datetime="2026-05-16T09:00:00"></time>
  <div class="meta-item location"><span class="screen-reader-text">Location:</span> 1 Main St, Atlanta, GA</div>
</article>
"""


class TestParseNuevent(unittest.TestCase):
    BASE = "https://meridies.org"

    def test_single_article_fields(self):
        evs = scrapers._parse_nuevent_page(NUEVENT_ARTICLE, self.BASE)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["summary"], "Spring Tourney")
        self.assertEqual(ev["url"], "https://meridies.org/events/spring/")
        self.assertEqual(ev["uid"], "nuevent-1234@https://meridies.org")
        self.assertEqual(ev["start"], datetime.fromisoformat("2026-05-16T09:00:00"))
        self.assertEqual(ev["description"], "Barony of Foo")

    def test_location_screen_reader_prefix_stripped(self):
        ev = scrapers._parse_nuevent_page(NUEVENT_ARTICLE, self.BASE)[0]
        self.assertNotIn("Location:", ev["location"])
        self.assertIn("1 Main St, Atlanta, GA", ev["location"])

    def test_article_without_id_skipped(self):
        html = '<article class="event-list-item"><h3 class="event-title"><a href="/x">T</a></h3><time datetime="2026-01-01"></time></article>'
        self.assertEqual(scrapers._parse_nuevent_page(html, self.BASE), [])

    def test_article_without_title_or_time_skipped(self):
        html = '<article class="event-list-item" id="event-9"><div class="event-host">H</div></article>'
        self.assertEqual(scrapers._parse_nuevent_page(html, self.BASE), [])

    def test_bad_datetime_skipped(self):
        html = ('<article class="event-list-item" id="event-9">'
                '<h3 class="event-title"><a href="/x">T</a></h3>'
                '<time datetime="not-a-date"></time></article>')
        self.assertEqual(scrapers._parse_nuevent_page(html, self.BASE), [])


# ── MEC (the Middle) ───────────────────────────────────────────────────────
MEC_PAGE = """
<html><body>
<div class="mec-event-meta">
  <span class="mec-start-date-label">Dec 19 - 21 2026</span>
  <div class="mec-single-event-location">
    <dd class="author"><h6>Castle Hall</h6></dd>
    <span class="mec-address">100 Keep Rd, Columbus, OH 43004</span>
  </div>
  <div class="mec-event-more-info"><a href="https://host.example/ev">More</a></div>
</div>
<div class="mec-single-event-description">A grand tournament weekend.</div>
</body></html>
"""

MEC_ITEM = {"link": "https://midrealm.org/events/grand/",
            "title": {"rendered": "Grand Tourney"}, "id": 99}


class TestParseMecEventPage(unittest.TestCase):
    SITE = "https://midrealm.org"

    def test_full_event(self):
        ev = scrapers._parse_mec_event_page(MEC_PAGE, MEC_ITEM, self.SITE)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["summary"], "Grand Tourney")
        self.assertEqual(ev["location"], "Castle Hall, 100 Keep Rd, Columbus, OH 43004")
        self.assertEqual(ev["description"], "A grand tournament weekend.")
        self.assertEqual(ev["uid"], "mec-99@https://midrealm.org")
        # the canonical WordPress link is preferred over the More-Info link
        self.assertEqual(ev["url"], "https://midrealm.org/events/grand/")
        self.assertEqual(ev["start"].strftime("%Y-%m-%d"), "2026-12-19")
        self.assertEqual(ev["end"].strftime("%Y-%m-%d"), "2026-12-21")

    def test_no_meta_block_returns_none(self):
        self.assertIsNone(scrapers._parse_mec_event_page("<html></html>", MEC_ITEM, self.SITE))

    def test_no_date_label_returns_none(self):
        html = '<div class="mec-event-meta"><span class="mec-address">X</span></div>'
        self.assertIsNone(scrapers._parse_mec_event_page(html, MEC_ITEM, self.SITE))

    def test_unparseable_date_returns_none(self):
        html = '<div class="mec-event-meta"><span class="mec-start-date-label">soon</span></div>'
        self.assertIsNone(scrapers._parse_mec_event_page(html, MEC_ITEM, self.SITE))

    def test_title_entities_stripped(self):
        item = {"link": "https://x/e", "title": {"rendered": "Foo &#038; Bar"}, "id": 1}
        ev = scrapers._parse_mec_event_page(MEC_PAGE, item, self.SITE)
        self.assertEqual(ev["summary"], "Foo  Bar")    # entity removed

    def test_more_info_used_when_no_canonical_link(self):
        item = {"link": "", "title": {"rendered": "T"}, "id": 2}
        ev = scrapers._parse_mec_event_page(MEC_PAGE, item, self.SITE)
        self.assertEqual(ev["url"], "https://host.example/ev")


if __name__ == "__main__":
    unittest.main(verbosity=2)
