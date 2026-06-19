"""
test_scrapers_parsers.py
------------------------
Offline tests for the pure parsing / ICS-emission helpers in scrapers.py that
aren't already covered by test_scrapers.py. No network — every helper here is a
pure string/datetime function.

Covered:
  * _parse_mec_date_range  — MEC's "Dec 19 - 21 2026" date-range grammar
  * _ics_escape            — RFC 5545 §3.3.11 TEXT escaping
  * _fold                  — RFC 5545 §3.1 content-line folding
  * _looks_like_json       — JSON-vs-WAF-challenge body sniff

Run:
    python -m unittest test_scrapers_parsers -v
"""
from __future__ import annotations

import unittest
from datetime import datetime

import scrapers


class TestParseMecDateRange(unittest.TestCase):
    """MEC ships dates as human strings on each event page; this grammar is the
    fragile bit most likely to drift if the source theme changes."""

    def test_single_day(self):
        start, end = scrapers._parse_mec_date_range("Dec 19 2026")
        self.assertEqual(start, datetime(2026, 12, 19))
        self.assertEqual(end, datetime(2026, 12, 19))

    def test_single_day_with_comma(self):
        # The grammar tolerates an optional comma after the day: "May 16, 2026".
        start, end = scrapers._parse_mec_date_range("May 16, 2026")
        self.assertEqual(start, datetime(2026, 5, 16))
        self.assertEqual(end, datetime(2026, 5, 16))

    def test_same_month_range(self):
        # "Dec 19 - 21 2026" -> Dec 19 .. Dec 21, same month + year.
        start, end = scrapers._parse_mec_date_range("Dec 19 - 21 2026")
        self.assertEqual(start, datetime(2026, 12, 19))
        self.assertEqual(end, datetime(2026, 12, 21))

    def test_cross_month_year_range(self):
        # "Dec 19 2026 - Jan 2 2027" -> spans the year boundary.
        start, end = scrapers._parse_mec_date_range("Dec 19 2026 - Jan 2 2027")
        self.assertEqual(start, datetime(2026, 12, 19))
        self.assertEqual(end, datetime(2027, 1, 2))

    def test_garbage_returns_none_pair(self):
        self.assertEqual(scrapers._parse_mec_date_range("not a date at all"),
                         (None, None))

    def test_empty_returns_none_pair(self):
        self.assertEqual(scrapers._parse_mec_date_range(""), (None, None))

    def test_unknown_month_returns_none_pair(self):
        # Matches the single-day shape but "Zzz" isn't a month -> no datetime built.
        self.assertEqual(scrapers._parse_mec_date_range("Zzz 19 2026"),
                         (None, None))

    def test_impossible_calendar_date_is_unparseable(self):
        # An impossible-but-well-formatted date matches a regex but datetime()
        # would raise; the parser must swallow that and return (None, None)
        # rather than crashing the whole MEC feed scrape on one typo.
        self.assertEqual(scrapers._parse_mec_date_range("Feb 30 2026"), (None, None))
        self.assertEqual(scrapers._parse_mec_date_range("Apr 31 2026"), (None, None))
        self.assertEqual(scrapers._parse_mec_date_range("Jun 1 - 32 2026"), (None, None))


class TestIcsEscape(unittest.TestCase):
    """RFC 5545 §3.3.11 TEXT escaping."""

    def test_backslash_doubled(self):
        self.assertEqual(scrapers._ics_escape("a" + chr(92) + "b"),
                         "a" + chr(92) * 2 + "b")

    def test_semicolon_escaped(self):
        self.assertEqual(scrapers._ics_escape("a;b"), "a\\;b")

    def test_comma_escaped(self):
        self.assertEqual(scrapers._ics_escape("a,b"), "a\\,b")

    def test_newline_escaped(self):
        self.assertEqual(scrapers._ics_escape("a\nb"), "a\\nb")

    def test_carriage_return_stripped(self):
        # A lone CR is removed entirely (replaced with ""), not escaped.
        self.assertEqual(scrapers._ics_escape("a\rb"), "ab")

    def test_crlf_collapses_to_escaped_newline(self):
        # In "\r\n" the \n is turned into the literal "\n" escape and the \r is
        # then stripped, leaving a single "\n" token.
        self.assertEqual(scrapers._ics_escape("a\r\nb"), "a\\nb")

    def test_combined_order(self):
        # Backslash is escaped first, so a literal backslash followed by special
        # chars stays well-formed: ";,\n\\" -> "\;\,\n\\".
        src = ";" + "," + "\n" + chr(92)
        self.assertEqual(scrapers._ics_escape(src), "\\;\\,\\n" + chr(92) * 2)

    def test_empty_string(self):
        self.assertEqual(scrapers._ics_escape(""), "")

    def test_none_safe(self):
        # falsy guard: None -> "" (no AttributeError).
        self.assertEqual(scrapers._ics_escape(None), "")


class TestFold(unittest.TestCase):
    """RFC 5545 §3.1 content-line folding (ASCII fold-at-73)."""

    def test_short_line_unchanged(self):
        line = "A" * 73
        self.assertEqual(scrapers._fold(line), line)
        self.assertNotIn("\r\n", scrapers._fold(line))

    def test_boundary_72_unchanged(self):
        line = "B" * 72
        self.assertEqual(scrapers._fold(line), line)

    def test_long_line_folds_with_crlf_space(self):
        line = "C" * 200
        folded = scrapers._fold(line)
        self.assertIn("\r\n ", folded)              # CRLF + leading space
        parts = folded.split("\r\n")
        self.assertEqual(len(parts), 3)             # 73 + 72 + 72 -> 3 segments
        # First segment is exactly 73 chars; continuations start with a space.
        self.assertEqual(len(parts[0]), 73)
        for cont in parts[1:]:
            self.assertTrue(cont.startswith(" "))
        # Continuation payload (excluding the leading space) is at most 72 chars.
        self.assertEqual([len(p) for p in parts], [73, 73, 56])

    def test_fold_is_lossless(self):
        # Unfolding (strip CRLF + the single leading space) restores the input.
        line = "D" * 300
        folded = scrapers._fold(line)
        rebuilt = folded.replace("\r\n ", "")
        self.assertEqual(rebuilt, line)


class TestLooksLikeJson(unittest.TestCase):
    """Sniff a 200 body: real JSON vs. an HTML WAF challenge page."""

    class _Resp:
        def __init__(self, text):
            self.text = text

    def test_object_body_is_json(self):
        self.assertTrue(scrapers._looks_like_json(self._Resp('{"ok":1}')))

    def test_array_body_is_json(self):
        self.assertTrue(scrapers._looks_like_json(self._Resp("[1,2,3]")))

    def test_leading_whitespace_ignored(self):
        self.assertTrue(scrapers._looks_like_json(self._Resp("   \n\t[1]")))

    def test_html_body_is_not_json(self):
        self.assertFalse(scrapers._looks_like_json(
            self._Resp("<html>Just a moment…</html>")))

    def test_empty_body_is_not_json(self):
        self.assertFalse(scrapers._looks_like_json(self._Resp("")))

    def test_whitespace_only_is_not_json(self):
        self.assertFalse(scrapers._looks_like_json(self._Resp("    ")))

    def test_none_text_handled_safely(self):
        # resp.text is None -> guarded by `(resp.text or "")`, returns False.
        self.assertFalse(scrapers._looks_like_json(self._Resp(None)))

    def test_object_without_text_attr_handled_safely(self):
        # Anything lacking .text raises inside the try and is caught -> False.
        class NoText:
            pass
        self.assertFalse(scrapers._looks_like_json(NoText()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
