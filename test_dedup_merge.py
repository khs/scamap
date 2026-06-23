"""
test_dedup_merge.py
-------------------
Offline tests for the deduplication and recurring-merge core of
clean_sca_events.py. This is the highest-stakes logic in the cleaner: a bug
here silently DROPS or wrongly MERGES events, so they vanish from the map.

Covers deduplicate / preferred_source (same date + location collapse, OUT OF
KINGDOM priority, geographic tie-break, kingdom-over-baronial, empty-location
passthrough, mixed date formats) and merge_recurring (weekly/monthly collapse,
location-variance split, non-recurring passthrough).

Run:
    python -m unittest test_dedup_merge -v
"""
from __future__ import annotations

import unittest

import pandas as pd

import clean_sca_events as clean

COLS = ["title", "start", "end", "location", "clean_location",
        "description", "source", "calendar_type"]


def _df(rows):
    out = []
    for r in rows:
        base = {c: "" for c in COLS}
        base.update(calendar_type="kingdom")
        base.update(r)
        out.append(base)
    return pd.DataFrame(out, columns=COLS)


class TestDeduplicate(unittest.TestCase):
    def test_out_of_kingdom_loses_to_local(self):
        df = _df([
            {"title": "Pennsic (OUT OF KINGDOM)", "start": "2026-08-01",
             "clean_location": "Cooper's Lake, PA", "source": "Kingdom of Meridies"},
            {"title": "Pennsic War", "start": "2026-08-01",
             "clean_location": "Cooper's Lake, PA", "source": "Kingdom of AEthelmearc"},
        ])
        out = clean.deduplicate(df)
        self.assertEqual(len(out), 1)
        self.assertNotIn("OUT OF KINGDOM", out.iloc[0]["title"])
        self.assertEqual(out.iloc[0]["source"], "Kingdom of AEthelmearc")

    def test_geographic_tiebreak_picks_hosting_kingdom(self):
        # Same event listed by two kingdoms; the one whose territory contains
        # the venue's state (TX -> Ansteorra) wins.
        df = _df([
            {"title": "Gulf Wars", "start": "2026-03-15",
             "clean_location": "Austin, TX 78701", "source": "Kingdom of Meridies"},
            {"title": "Gulf Wars", "start": "2026-03-15",
             "clean_location": "Austin, TX 78701", "source": "Kingdom of Ansteorra"},
        ])
        out = clean.deduplicate(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["source"], "Kingdom of Ansteorra")

    def test_baronial_and_kingdom_same_place_are_kept_separately(self):
        # Dedup is calendar_type-aware: a baronial practice is never removed by
        # a kingdom event that happens to share its date and venue — both stay.
        df = _df([
            {"title": "A&S Day", "start": "2026-05-02", "clean_location": "Community Hall, Springfield",
             "source": "Barony of Foo", "calendar_type": "baronial"},
            {"title": "A&S Day", "start": "2026-05-02", "clean_location": "Community Hall, Springfield",
             "source": "Kingdom of Foo", "calendar_type": "kingdom"},
        ])
        out = clean.deduplicate(df)
        self.assertEqual(len(out), 2)
        self.assertEqual(set(out["calendar_type"]), {"baronial", "kingdom"})

    def test_empty_location_rows_are_never_deduped(self):
        # No address means we can't be sure they're the same event — keep both.
        df = _df([
            {"title": "Mystery A", "start": "2026-06-01", "clean_location": ""},
            {"title": "Mystery B", "start": "2026-06-01", "clean_location": ""},
        ])
        out = clean.deduplicate(df)
        self.assertEqual(len(out), 2)

    def test_mixed_date_formats_same_day_collapse(self):
        # The format="mixed" guard: a date-only and a datetime value on the
        # same calendar day at the same place are one event, not two.
        df = _df([
            {"title": "Feast", "start": "2026-07-04", "clean_location": "Hall, OH",
             "source": "Kingdom of the Middle"},
            {"title": "Feast", "start": "2026-07-04 18:00:00", "clean_location": "Hall, OH",
             "source": "Kingdom of the Middle"},
        ])
        out = clean.deduplicate(df)
        self.assertEqual(len(out), 1)

    def test_different_days_same_place_kept(self):
        df = _df([
            {"title": "Practice", "start": "2026-07-04", "clean_location": "Hall, OH"},
            {"title": "Practice", "start": "2026-07-11", "clean_location": "Hall, OH"},
        ])
        out = clean.deduplicate(df)
        self.assertEqual(len(out), 2)

    def test_location_case_and_whitespace_insensitive(self):
        df = _df([
            {"title": "Event", "start": "2026-09-01", "clean_location": "  Main St, PA "},
            {"title": "Event", "start": "2026-09-01", "clean_location": "main st, pa"},
        ])
        out = clean.deduplicate(df)
        self.assertEqual(len(out), 1)


class TestMergeRecurring(unittest.TestCase):
    def _weekly(self, n, title="Fighter Practice", loc="Dojo, PA", start="2026-01-07"):
        from datetime import datetime, timedelta
        d0 = datetime.fromisoformat(start)
        return _df([
            {"title": title, "start": (d0 + timedelta(weeks=i)).strftime("%Y-%m-%d"),
             "clean_location": loc, "description": "weekly practice"}
            for i in range(n)
        ])

    def test_weekly_series_collapses_to_one_tagged_row(self):
        out = clean.merge_recurring(self._weekly(4))
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertTrue(row["title"].endswith("(RECURRING)"))
        self.assertIn("RECURRING", row["description"])
        self.assertIn("also on", row["description"])
        # earliest date kept as the base row's start
        self.assertTrue(str(row["start"]).startswith("2026-01-07"))

    def test_merged_description_lists_dates_without_leading_zeros(self):
        out = clean.merge_recurring(self._weekly(3))
        desc = out.iloc[0]["description"]
        self.assertIn("7 Jan 2026", desc)      # leading zero stripped
        self.assertIn("14 Jan 2026", desc)
        self.assertIn("21 Jan 2026", desc)

    def test_same_title_different_location_not_merged(self):
        df = _df([
            {"title": "Practice", "start": "2026-01-07", "clean_location": "Hall A, PA"},
            {"title": "Practice", "start": "2026-01-14", "clean_location": "Hall B, PA"},
        ])
        out = clean.merge_recurring(df)
        self.assertEqual(len(out), 2)
        self.assertFalse(any(t.endswith("(RECURRING)") for t in out["title"]))

    def test_non_recurring_multi_kept_separate(self):
        # Two events, same title/place, ~100 days apart -> not a cadence.
        df = _df([
            {"title": "Big Event", "start": "2026-01-01", "clean_location": "Castle, NY"},
            {"title": "Big Event", "start": "2026-04-11", "clean_location": "Castle, NY"},
        ])
        out = clean.merge_recurring(df)
        self.assertEqual(len(out), 2)
        self.assertFalse(any(t.endswith("(RECURRING)") for t in out["title"]))

    def test_single_event_passes_through_untagged(self):
        df = _df([{"title": "Solo", "start": "2026-02-02", "clean_location": "Place, VA"}])
        out = clean.merge_recurring(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["title"], "Solo")

    def test_monthly_series_collapses(self):
        from datetime import datetime, timedelta
        d0 = datetime(2026, 1, 5)
        df = _df([
            {"title": "Populace Meeting", "start": (d0 + timedelta(days=28 * i)).strftime("%Y-%m-%d"),
             "clean_location": "Library, MD"}
            for i in range(4)
        ])
        out = clean.merge_recurring(df)
        self.assertEqual(len(out), 1)
        self.assertTrue(out.iloc[0]["title"].endswith("(RECURRING)"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
