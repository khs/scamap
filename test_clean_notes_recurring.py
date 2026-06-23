"""
test_clean_notes_recurring.py
-----------------------------
Offline tests for two PURE helpers in clean_sca_events.py:

  * strip_aethelmearc_notes -- removes AEthelmearc's "Additional Notes on …"
    logistics boilerplate that the source calendar prepends/inlines into event
    descriptions, while preserving the real write-up that follows.
  * is_recurring -- decides whether a list of dates forms a consistent
    weekly/biweekly/monthly cadence.

No network, no file IO -- both functions are pure. Expected outputs were
determined empirically by running the functions, not guessed.

Run:
    python -m unittest test_clean_notes_recurring -v
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

import clean_sca_events as clean


# ---------------------------------------------------------------------------
# strip_aethelmearc_notes
# ---------------------------------------------------------------------------
class TestStripAethelmearcNotes(unittest.TestCase):
    def test_leading_pet_policy_block_dropped_real_text_kept(self):
        # A leading "Additional Notes on Pet Policy: …" block followed by the
        # real description (which opens with the "Greetings" sentinel). The
        # boilerplate goes; the write-up survives verbatim.
        text = ("Additional Notes on Pet Policy: Only service animals are "
                "permitted on site. Greetings to all gentles! Join us for the "
                "Spring Coronation with tournaments, a feast, and dancing.")
        out = clean.strip_aethelmearc_notes(text)
        self.assertEqual(
            out,
            "Greetings to all gentles! Join us for the Spring Coronation with "
            "tournaments, a feast, and dancing.")
        # boilerplate fully gone
        self.assertNotIn("Pet Policy", out)
        self.assertNotIn("service animals", out)

    def test_inline_block_ended_by_royal_progress_sentinel(self):
        # An "Additional Notes on …" block buried mid-description, terminated by
        # the strong "Royal Progress:" sentinel. The surrounding real text on
        # both sides is preserved; only the note run is excised.
        text = ("The annual war is upon us. Additional Notes on Alcohol Policy: "
                "No outside alcohol permitted on site. Royal Progress: Their "
                "Majesties will hold court at noon.")
        out = clean.strip_aethelmearc_notes(text)
        self.assertEqual(
            out,
            "The annual war is upon us. Royal Progress: Their Majesties will "
            "hold court at noon.")
        self.assertNotIn("Alcohol Policy", out)

    def test_inline_block_ended_by_hark_sentinel(self):
        text = ("Additional Notes on Weapons: All weapons must pass inspection. "
                "Hark! The tourney begins at dawn.")
        self.assertEqual(clean.strip_aethelmearc_notes(text),
                         "Hark! The tourney begins at dawn.")

    def test_inline_block_ended_by_unto_sentinel(self):
        text = ("Additional Notes on Flames: No open flames allowed. Unto the "
                "populace of the Kingdom, greetings.")
        self.assertEqual(clean.strip_aethelmearc_notes(text),
                         "Unto the populace of the Kingdom, greetings.")

    def test_block_ended_by_the_barony_of_sentinel(self):
        text = ("Additional Notes on Parking: Limited parking available. The "
                "Barony of Thescorre invites you to a day of revelry.")
        self.assertEqual(clean.strip_aethelmearc_notes(text),
                         "The Barony of Thescorre invites you to a day of revelry.")

    def test_consecutive_note_headers_form_one_block(self):
        # Two back-to-back "Additional Notes on …" headers (well within the
        # ~400-char grouping window) are stripped as a single block, leaving the
        # real description that starts at the "Greetings" sentinel.
        text = ("Additional Notes on Pet Policy: Service dogs only. Additional "
                "Notes on Alcohol Policy: None permitted. Greetings to all who "
                "attend this fine event.")
        out = clean.strip_aethelmearc_notes(text)
        self.assertEqual(out, "Greetings to all who attend this fine event.")
        self.assertNotIn("Pet Policy", out)
        self.assertNotIn("Alcohol Policy", out)

    def test_block_ended_by_sentence_boundary_when_no_sentinel(self):
        # No known sentinel: the block ends at the first ". <Capital word>"
        # sentence boundary, so the following real sentence is preserved.
        text = ("Additional Notes on Parking: Park in the north lot only. The "
                "tournament field opens at nine.")
        self.assertEqual(clean.strip_aethelmearc_notes(text),
                         "The tournament field opens at nine.")

    def test_no_headers_is_unchanged_passthrough(self):
        # No "Additional Notes on" header anywhere -> returned byte-for-byte,
        # even though it contains sentinel-like phrases ("Come one, come all").
        text = ("Come one, come all to the grand tournament. There will be "
                "archery, fencing, and a feast.")
        self.assertEqual(clean.strip_aethelmearc_notes(text), text)

    def test_ordinary_text_with_sentinel_words_not_clobbered(self):
        # Real text that happens to contain note-ish words ("welcome", "join
        # us") but no header must be left exactly as-is.
        text = "Service dogs are welcome. Please join us in preparation!"
        self.assertEqual(clean.strip_aethelmearc_notes(text), text)

    def test_non_string_and_empty_passthrough(self):
        # Guard clause: non-str / empty inputs are returned unchanged.
        self.assertIsNone(clean.strip_aethelmearc_notes(None))
        self.assertEqual(clean.strip_aethelmearc_notes(""), "")
        self.assertEqual(clean.strip_aethelmearc_notes(123), 123)


# ---------------------------------------------------------------------------
# is_recurring
# ---------------------------------------------------------------------------
def _series(*gaps, start=date(2026, 1, 1)):
    """Build a date list starting at `start`, stepping by each gap in `gaps`.
    N gaps -> N+1 dates."""
    dates = [start]
    for g in gaps:
        dates.append(dates[-1] + timedelta(days=g))
    return dates


class TestIsRecurring(unittest.TestCase):
    def test_weekly_four_dates_is_recurring(self):
        self.assertTrue(clean.is_recurring(_series(7, 7, 7)))   # 4 dates

    def test_weekly_five_dates_is_recurring(self):
        self.assertTrue(clean.is_recurring(_series(7, 7, 7, 7)))

    def test_biweekly_is_recurring(self):
        self.assertTrue(clean.is_recurring(_series(14, 14, 14)))

    def test_monthly_28_day_gaps_is_recurring(self):
        self.assertTrue(clean.is_recurring(_series(28, 28, 28)))

    def test_monthly_30_day_gaps_is_recurring(self):
        self.assertTrue(clean.is_recurring(_series(30, 30, 30)))

    def test_monthly_31_day_gaps_is_recurring(self):
        # 31 is within tolerance of the avg and avg<=32, so still recurring.
        self.assertTrue(clean.is_recurring(_series(31, 31, 31)))

    def test_gaps_within_tolerance_is_recurring(self):
        # 7, 9, 5 -> avg 7, every gap within +/-3 of avg, avg in [6,32].
        self.assertTrue(clean.is_recurring(_series(7, 9, 5)))

    def test_weekly_skipping_a_week_is_recurring(self):
        # 7, 7, 14 -> a weekly series that skips one week. The avg test alone
        # rejects it (14 is >3 off the ~9.33 avg), but the skip-tolerant path
        # keeps it as one (RECURRING) row instead of splitting off a stray pin.
        self.assertTrue(clean.is_recurring(_series(7, 7, 14)))

    def test_weekly_with_holiday_skip_pattern_is_recurring(self):
        # A real practice cadence that skips a holiday week twice: 7,14,7,7,14,7.
        self.assertTrue(clean.is_recurring(_series(7, 14, 7, 7, 14, 7)))

    def test_monthly_skipping_a_month_is_recurring(self):
        # 28, 56, 28 -> monthly with one month skipped (56 = 2x the base).
        self.assertTrue(clean.is_recurring(_series(28, 56, 28)))

    def test_irregular_large_gap_not_recurring(self):
        # 7, 30, 7 -> a 30-day gap is neither ~weekly nor a small multiple of
        # the 7-day base, so the series is not a clean cadence.
        self.assertFalse(clean.is_recurring(_series(7, 30, 7)))

    def test_average_gap_below_six_not_recurring(self):
        # Consistent 5-day cadence but avg < 6 -> rejected.
        self.assertFalse(clean.is_recurring(_series(5, 5, 5)))

    def test_average_gap_above_thirty_two_not_recurring(self):
        # Consistent 35-day cadence but avg > 32 -> rejected.
        self.assertFalse(clean.is_recurring(_series(35, 35, 35)))

    def test_average_gap_exactly_six_is_recurring(self):
        # Lower bound is inclusive (6 <= avg_gap).
        self.assertTrue(clean.is_recurring(_series(6, 6, 6)))

    def test_average_gap_exactly_thirty_two_is_recurring(self):
        # Upper bound is inclusive (avg_gap <= 32).
        self.assertTrue(clean.is_recurring(_series(32, 32, 32)))

    def test_two_dates_with_in_range_gap_is_recurring(self):
        # BEHAVIOR PIN (flagged): a 2-date series with a single in-range gap
        # returns True, because with one gap the avg equals that gap and the
        # "all within tolerance" check is trivially satisfied. The docstring
        # / intent ("consistent pattern") arguably wants >=3 dates, but the
        # code accepts 2. We pin the CURRENT behavior, not the "correct" one.
        self.assertTrue(clean.is_recurring(_series(7)))          # 2 dates, 7-day gap
        self.assertTrue(clean.is_recurring(_series(14)))         # 2 dates, biweekly

    def test_two_dates_out_of_range_gap_not_recurring(self):
        # A single out-of-range gap (e.g. 3 days, avg < 6) is still rejected.
        self.assertFalse(clean.is_recurring(_series(3)))

    def test_single_date_not_recurring(self):
        self.assertFalse(clean.is_recurring([date(2026, 1, 1)]))

    def test_empty_list_not_recurring(self):
        self.assertFalse(clean.is_recurring([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
