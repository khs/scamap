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

import csv
import io
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

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


class TestArtemisiaFBMarker(unittest.TestCase):
    """The Artemisia flyer parser anchors on the 'Facebook Event Page/Link'
    label. Real PDFs have shipped with both spellings ('Facebook' and the
    occasional 'Fecebook' typo) and a couple of label variants, so the regex
    is deliberately tolerant of those exact forms but strict otherwise."""

    def test_matches_standard_event_page_label(self):
        self.assertIsNotNone(ed._ARTEMISIA_FB_MARKER_RE.search("Facebook Event Page"))

    def test_matches_event_link_label(self):
        self.assertIsNotNone(ed._ARTEMISIA_FB_MARKER_RE.search("Facebook Event Link"))

    def test_matches_page_for_this_event_label(self):
        self.assertIsNotNone(
            ed._ARTEMISIA_FB_MARKER_RE.search("Facebook Page for this Event")
        )

    def test_tolerates_fecebook_typo(self):
        # Several flyers contain "Fecebook Event Page" — observed in real PDFs.
        self.assertIsNotNone(ed._ARTEMISIA_FB_MARKER_RE.search("Fecebook Event Page"))

    def test_does_not_match_a_bare_facebook_mention(self):
        # A description that mentions Facebook in passing ('like us on Facebook!')
        # must NOT trip the marker, or we'd lop off most of the prose.
        self.assertIsNone(
            ed._ARTEMISIA_FB_MARKER_RE.search("Follow us on Facebook for updates!")
        )


class TestExtractArtemisia(unittest.TestCase):
    """The PDF extractor itself. We build minimal real PDFs in-memory with
    pypdf so the tests run offline and don't depend on captured-binary
    fixtures."""

    @staticmethod
    def _make_pdf(text: str) -> bytes:
        # pypdf-only fixture builder: lay each line out at 720 - i*14 so the
        # text extractor sees them in order. Two-page docs to exercise the
        # multi-page join.
        from pypdf import PdfWriter
        from pypdf.generic import (
            ArrayObject, ContentStream, DecodedStreamObject,
            DictionaryObject, FloatObject, NameObject, NumberObject,
        )

        # Build one page's content stream from the supplied text lines.
        def _page_dict(writer, lines):
            ops = ["BT /F1 12 Tf "]
            y = 720
            for line in lines:
                escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append(f"1 0 0 1 72 {y} Tm ({escaped}) Tj ")
                y -= 14
            ops.append("ET")
            stream = DecodedStreamObject()
            stream.set_data("".join(ops).encode("latin-1"))
            stream_ref = writer._add_object(stream)
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            font_ref = writer._add_object(font)
            resources = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            })
            page = DictionaryObject({
                NameObject("/Type"): NameObject("/Page"),
                NameObject("/MediaBox"): ArrayObject(
                    [NumberObject(0), NumberObject(0),
                     NumberObject(612), NumberObject(792)]),
                NameObject("/Resources"): resources,
                NameObject("/Contents"): stream_ref,
            })
            return page

        writer = PdfWriter()
        lines = text.splitlines() or [""]
        # Split into two pages so we exercise multi-page joining.
        mid = max(1, len(lines) // 2)
        for chunk in (lines[:mid], lines[mid:] or [""]):
            page = _page_dict(writer, chunk)
            # Use writer's internal add_page logic via the page-tree append.
            page_ref = writer._add_object(page)
            writer._root_object[NameObject("/Pages")][NameObject("/Kids")].append(page_ref)
            writer._root_object[NameObject("/Pages")][NameObject("/Count")] = NumberObject(
                len(writer._root_object[NameObject("/Pages")][NameObject("/Kids")])
            )
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def test_returns_text_after_facebook_marker(self):
        body = "\n".join([
            "Estrella War XLII",
            "Hosted by the Kingdom of Atenveldt",
            "Facebook Event Page",
            "Join us for a week of fighting, feasting, and fellowship",
            "in the Arizona desert. Site fee includes camping.",
        ])
        pdf = self._make_pdf(body)
        got = ed.extract_artemisia_description(pdf)
        self.assertIsNotNone(got)
        self.assertIn("week of fighting", got)
        self.assertIn("Site fee includes camping", got)
        # Header text before the marker should be excluded.
        self.assertNotIn("Estrella War", got)
        self.assertNotIn("Atenveldt", got)

    def test_returns_none_when_marker_is_missing(self):
        # Fail-silent: rather than ship arbitrary flyer text as the
        # description, return None so the event keeps its placeholder.
        pdf = self._make_pdf("Event flyer\nDate: TBD\nLocation: Coming soon")
        self.assertIsNone(ed.extract_artemisia_description(pdf))

    def test_handles_malformed_pdf(self):
        self.assertIsNone(ed.extract_artemisia_description(b"not a pdf"))
        self.assertIsNone(ed.extract_artemisia_description(b""))

    def test_normalises_whitespace_in_description(self):
        # PDFs frequently embed runs of spaces and stray newlines around the
        # extracted text. We promise single-space-collapsed output.
        body = "\n".join([
            "Facebook Event Link",
            "    Lots    of     spaces here.",
            "And a second line.",
        ])
        got = ed.extract_artemisia_description(self._make_pdf(body))
        self.assertIsNotNone(got)
        self.assertNotIn("  ", got)               # no double-spaces
        self.assertIn("Lots of spaces here", got)


class TestArtemisiaTrigger(unittest.TestCase):
    """The main loop's is_artemisia gate — make sure we trigger when a Drive
    URL has no useful description, and skip when one is already populated."""

    def test_triggers_when_description_is_empty(self):
        url = "https://drive.google.com/file/d/abc123/view"
        desc = ""
        is_artemisia = (ed.ARTEMISIA_DRIVE_HOST in url
                        and (not desc.strip()
                             or desc.strip().lower().endswith(".pdf")
                             or len(desc.strip()) < 50))
        self.assertTrue(is_artemisia)

    def test_triggers_when_description_is_just_pdf_filename(self):
        url = "https://drive.google.com/file/d/abc123/view"
        desc = "estrella-war-flyer.pdf"
        is_artemisia = (ed.ARTEMISIA_DRIVE_HOST in url
                        and (not desc.strip()
                             or desc.strip().lower().endswith(".pdf")
                             or len(desc.strip()) < 50))
        self.assertTrue(is_artemisia)

    def test_skips_when_description_is_substantial(self):
        url = "https://drive.google.com/file/d/abc123/view"
        desc = ("Estrella War XLII brings together fighters and artisans for "
                "a week in the Arizona desert. Join us for archery, fencing, "
                "feast, and dancing each evening.")
        is_artemisia = (ed.ARTEMISIA_DRIVE_HOST in url
                        and (not desc.strip()
                             or desc.strip().lower().endswith(".pdf")
                             or len(desc.strip()) < 50))
        self.assertFalse(is_artemisia)


class TestDriveIdExtraction(unittest.TestCase):
    def test_extracts_id_from_view_url(self):
        m = ed._DRIVE_ID_RE.search("https://drive.google.com/file/d/1AbC-_xyz/view")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "1AbC-_xyz")

    def test_extracts_id_from_edit_url(self):
        m = ed._DRIVE_ID_RE.search("https://drive.google.com/file/d/xyz_777/edit?usp=sharing")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "xyz_777")


class TestDaysUntilEvent(unittest.TestCase):
    NOW = datetime(2026, 6, 1, 12, 0, 0)

    def test_date_only_format(self):
        self.assertEqual(ed.days_until_event("2026-06-15", self.NOW), 14)

    def test_datetime_format(self):
        self.assertEqual(ed.days_until_event("2026-06-08 09:00:00", self.NOW), 7)

    def test_same_day_is_zero(self):
        self.assertEqual(ed.days_until_event("2026-06-01 20:00:00", self.NOW), 0)

    def test_past_event_is_negative(self):
        self.assertEqual(ed.days_until_event("2026-05-01", self.NOW), -31)

    def test_empty_and_garbage_return_none(self):
        self.assertIsNone(ed.days_until_event("", self.NOW))
        self.assertIsNone(ed.days_until_event("   ", self.NOW))
        self.assertIsNone(ed.days_until_event("sometime next spring", self.NOW))


class TestRefreshInterval(unittest.TestCase):
    """The proximity cadence: monthly far out, weekly in the final month,
    daily in the final week, frozen once the event is in the past."""

    def test_far_future_is_monthly(self):
        self.assertEqual(ed.refresh_interval_days(200), 30)
        self.assertEqual(ed.refresh_interval_days(30), 30)   # boundary: 30 is "a month out"

    def test_inside_a_month_is_weekly(self):
        self.assertEqual(ed.refresh_interval_days(29), 7)
        self.assertEqual(ed.refresh_interval_days(7), 7)     # boundary: 7 is "a week out"

    def test_inside_a_week_is_daily(self):
        self.assertEqual(ed.refresh_interval_days(6), 1)
        self.assertEqual(ed.refresh_interval_days(0), 1)     # event day

    def test_past_event_is_frozen(self):
        self.assertEqual(ed.refresh_interval_days(-1), ed.PAST_REFRESH_DAYS)
        self.assertGreater(ed.PAST_REFRESH_DAYS, 365)

    def test_undated_defaults_to_monthly(self):
        self.assertEqual(ed.refresh_interval_days(None), 30)

    def test_cadence_is_under_2x_a_flat_monthly_poll(self):
        # Sanity-check the user's "11 + 3 + 6 < 2x monthly" reasoning: simulate
        # one event from a year out down to its date, polling at whatever
        # interval the tier dictates, and count the fetches.
        day, fetches = 365, 0
        while day >= 0:
            fetches += 1
            day -= ed.refresh_interval_days(day)
        flat_monthly = 365 // 30 + 1            # ~13
        self.assertLess(fetches, 2 * flat_monthly)
        self.assertGreater(fetches, flat_monthly)   # but more than a flat poll


class TestCacheSchema(unittest.TestCase):
    def test_get_missing_url(self):
        self.assertEqual(ed.cache_get({}, "http://x"), (None, 0.0))

    def test_get_legacy_string_entry(self):
        # Old format {url: "text"} reads as ts=0 so it re-validates next run.
        text, ts = ed.cache_get({"http://x": "old description text"}, "http://x")
        self.assertEqual(text, "old description text")
        self.assertEqual(ts, 0.0)

    def test_get_new_dict_entry(self):
        cache = {"http://x": {"text": "fresh", "ts": 1780000000}}
        text, ts = ed.cache_get(cache, "http://x")
        self.assertEqual(text, "fresh")
        self.assertEqual(ts, 1780000000.0)

    def test_put_writes_dict_with_int_ts(self):
        cache = {}
        ed.cache_put(cache, "http://x", "hello", 1780000000.7)
        self.assertEqual(cache["http://x"], {"text": "hello", "ts": 1780000000})

    def test_put_then_get_roundtrips(self):
        cache = {}
        ed.cache_put(cache, "http://x", "roundtrip", 1779999999)
        text, ts = ed.cache_get(cache, "http://x")
        self.assertEqual(text, "roundtrip")
        self.assertEqual(ts, 1779999999.0)


class TestMainCadenceIntegration(unittest.TestCase):
    """Drive main() over a tiny CSV with the network stubbed and temp cache/
    events files, asserting the proximity cadence actually governs re-fetches.
    This covers the loop wiring the pure-function tests above can't reach."""

    CSV_COLS = ["title", "start", "end", "location", "clean_location",
                "address_confidence", "description", "event_url", "facebook_url",
                "source", "calendar_type", "is_virtual", "lat", "lng",
                "geocode_status"]
    # Atlantia always ships this placeholder from its feed, so the trigger fires
    # every run -- which is what lets the cadence re-validate already-known URLs.
    PLACEHOLDER = "Upcoming event in Storvik Event Flyer:"
    URL = "https://atlantia.sca.org/event/?event_id=abc123"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.events = Path(self.tmp) / "events.csv"
        self.cache = Path(self.tmp) / "cache.json"
        self._orig = (ed.EVENTS_FILE, ed.CACHE_FILE, ed.REQUEST_DELAY,
                      ed.fetch_description)
        ed.EVENTS_FILE = self.events
        ed.CACHE_FILE = self.cache
        ed.REQUEST_DELAY = 0                       # don't sleep in tests
        self.fetch_calls = []

        def fake_fetch(url, session):
            self.fetch_calls.append(url)
            return "FRESH LIVE DESCRIPTION pulled just now from the event page."
        ed.fetch_description = fake_fetch

    def tearDown(self):
        (ed.EVENTS_FILE, ed.CACHE_FILE, ed.REQUEST_DELAY,
         ed.fetch_description) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def _write_event(self, *, days_out, description):
        start = (datetime.now() + timedelta(days=days_out)).strftime("%Y-%m-%d")
        row = {c: "" for c in self.CSV_COLS}
        row.update(title="Test Event", start=start, end=start,
                   description=description, event_url=self.URL,
                   source="Kingdom of Atlantia", calendar_type="kingdom")
        with open(self.events, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.CSV_COLS, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerow(row)

    def _seed_cache(self, *, text, age_days):
        ts = int(time.time() - age_days * 86400)
        self.cache.write_text(
            ed.json.dumps({self.URL: {"text": text, "ts": ts}}),
            encoding="utf-8")

    def _seed_legacy_cache(self, text):
        self.cache.write_text(ed.json.dumps({self.URL: text}), encoding="utf-8")

    def _desc(self):
        with open(self.events, encoding="utf-8") as f:
            return next(csv.DictReader(f))["description"]

    # -- tests ------------------------------------------------------------
    def test_no_cache_fetches_and_stamps(self):
        self._write_event(days_out=200, description=self.PLACEHOLDER)
        ed.main()
        self.assertEqual(self.fetch_calls, [self.URL])
        self.assertIn("FRESH LIVE", self._desc())
        text, ts = ed.cache_get(ed.load_cache(), self.URL)
        self.assertIn("FRESH LIVE", text)
        self.assertGreater(ts, 0)                  # stamped with a real time

    def test_fresh_cache_is_reused_without_fetching(self):
        # 10 days old, event 200 days out -> monthly tier (30d) -> still fresh.
        self._write_event(days_out=200, description=self.PLACEHOLDER)
        self._seed_cache(text="CACHED PROSE that is plenty long to be real.",
                         age_days=10)
        ed.main()
        self.assertEqual(self.fetch_calls, [])     # no network
        self.assertIn("CACHED PROSE", self._desc())

    def test_stale_cache_far_event_refetches(self):
        # 40 days old, event 200 days out -> monthly tier (30d) -> stale.
        self._write_event(days_out=200, description=self.PLACEHOLDER)
        self._seed_cache(text="STALE PROSE from over a month ago, still long.",
                         age_days=40)
        ed.main()
        self.assertEqual(self.fetch_calls, [self.URL])
        self.assertIn("FRESH LIVE", self._desc())

    def test_same_age_cache_near_event_refetches_but_far_does_not(self):
        # A 2-day-old cache: stale for an event 3 days out (daily tier = 1d),
        # but fresh for one 200 days out (monthly tier = 30d). Same cache age,
        # opposite decision -> proves proximity, not just age, drives it.
        for days_out, expect_fetch in [(3, True), (200, False)]:
            with self.subTest(days_out=days_out):
                self.fetch_calls.clear()
                self._write_event(days_out=days_out, description=self.PLACEHOLDER)
                self._seed_cache(text="TWO DAY OLD cached prose, long enough.",
                                 age_days=2)
                ed.main()
                self.assertEqual(bool(self.fetch_calls), expect_fetch)

    def test_legacy_string_entry_revalidates_and_upgrades(self):
        # Bare-string cache entry (ts=0) reads as stale -> refetch + upgrade.
        self._write_event(days_out=200, description=self.PLACEHOLDER)
        self._seed_legacy_cache("LEGACY bare-string description, long enough.")
        ed.main()
        self.assertEqual(self.fetch_calls, [self.URL])
        self.assertIsInstance(ed.load_cache()[self.URL], dict)  # migrated

    def test_fetch_error_keeps_last_good_description(self):
        # Network blows up on a stale entry: we must NOT lose the cached prose.
        def boom(url, session):
            self.fetch_calls.append(url)
            raise RuntimeError("network down")
        ed.fetch_description = boom
        self._write_event(days_out=200, description=self.PLACEHOLDER)
        self._seed_cache(text="GOOD PRIOR description we must not drop.",
                         age_days=40)
        ed.main()
        self.assertEqual(self.fetch_calls, [self.URL])   # tried
        self.assertIn("GOOD PRIOR", self._desc())        # kept anyway


if __name__ == "__main__":
    unittest.main(verbosity=2)
