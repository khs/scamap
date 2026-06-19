"""
test_carry_forward.py
---------------------
Offline tests for clean_sca_events.carry_forward_failed_sources — the guard that
keeps a kingdom on the map when its fetch fails transiently (connection refused,
timeout, WAF 403) instead of committing 0 events and wiping it.

Run:
    python -m unittest test_carry_forward -v
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import clean_sca_events as clean

COLS = ["title", "start", "end", "location", "clean_location",
        "address_confidence", "description", "event_url", "facebook_url",
        "source", "calendar_type", "is_virtual", "lat", "lng", "geocode_status"]


def _row(title, start, source, **over):
    r = {c: "" for c in COLS}
    r.update(title=title, start=start, end=start, source=source,
             clean_location="123 Main St, Somewhere, OH 44001",
             lat="41.0", lng="-82.0", geocode_status="ok", calendar_type="kingdom")
    r.update(over)
    return r


class TestCarryForward(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = Path(self.tmp) / "sca_events_clean.csv"
        self.fails = Path(self.tmp) / "fetch_failures.json"
        self._orig = (clean.OUTPUT_FILE, clean.FETCH_FAILURES_FILE)
        clean.OUTPUT_FILE = self.out
        clean.FETCH_FAILURES_FILE = self.fails

        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        future2 = (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d")
        past = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        self.future, self.past = future, past
        # Prior committed CSV: Meridies (will fail) has 1 future + 1 past event;
        # Caid (healthy) has 1 future event.
        prior = pd.DataFrame([
            _row("Meridies War", future, "Kingdom of Meridies"),
            _row("Meridies Past Feast", past, "Kingdom of Meridies"),
            _row("Caid Coronation", future2, "Kingdom of Caid"),
        ])
        prior.to_csv(self.out, index=False)

    def tearDown(self):
        clean.OUTPUT_FILE, clean.FETCH_FAILURES_FILE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_df_without_meridies(self):
        # Simulates the wipe: this run fetched Caid fine but Meridies returned 0.
        return pd.DataFrame([_row("Caid Coronation",
                                  (datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d"),
                                  "Kingdom of Caid")], columns=COLS)

    def test_restores_future_event_of_failed_source(self):
        self.fails.write_text(json.dumps(["Kingdom of Meridies"]))
        out = clean.carry_forward_failed_sources(self._new_df_without_meridies())
        titles = set(out["title"])
        self.assertIn("Meridies War", titles)            # future event restored
        self.assertIn("Caid Coronation", titles)         # healthy source intact

    def test_does_not_restore_past_events(self):
        self.fails.write_text(json.dumps(["Kingdom of Meridies"]))
        out = clean.carry_forward_failed_sources(self._new_df_without_meridies())
        self.assertNotIn("Meridies Past Feast", set(out["title"]))

    def test_carried_rows_keep_prior_coords(self):
        self.fails.write_text(json.dumps(["Kingdom of Meridies"]))
        out = clean.carry_forward_failed_sources(self._new_df_without_meridies())
        war = out[out["title"] == "Meridies War"].iloc[0]
        self.assertEqual((war["lat"], war["lng"], war["geocode_status"]),
                         ("41.0", "-82.0", "ok"))

    def test_no_failures_is_a_noop(self):
        self.fails.write_text(json.dumps([]))
        new = self._new_df_without_meridies()
        out = clean.carry_forward_failed_sources(new)
        self.assertEqual(len(out), len(new))             # nothing added

    def test_healthy_source_not_carried_even_if_in_failures_list_stale(self):
        # If a source DIDN'T actually fail this run it won't be in df as 0 —
        # but even if it's in the failures file, an already-present event must
        # not be duplicated.
        self.fails.write_text(json.dumps(["Kingdom of Meridies", "Kingdom of Caid"]))
        out = clean.carry_forward_failed_sources(self._new_df_without_meridies())
        self.assertEqual(sum(out["title"] == "Caid Coronation"), 1)   # no dup

    def test_missing_failures_file_is_a_noop(self):
        # No fetch_failures.json at all -> behave as if nothing failed.
        new = self._new_df_without_meridies()
        out = clean.carry_forward_failed_sources(new)
        self.assertEqual(len(out), len(new))


if __name__ == "__main__":
    unittest.main(verbosity=2)
