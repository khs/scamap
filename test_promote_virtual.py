"""
test_promote_virtual.py
-----------------------
Offline tests for clean_sca_events.promote_virtual_baronials — the
reclassification that moves virtual baronial business-meetings/practices out of
a kingdom feed into the "Baronial" filter on the map. This had a real
mis-classification bug this project (virtual baronial meetings showing as
kingdom events), and it governs which tab an event lands in.

Run:
    python -m unittest test_promote_virtual -v
"""
from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import clean_sca_events as clean

COLS = ["title", "source", "calendar_type", "is_virtual"]


def _df(rows):
    out = []
    for r in rows:
        base = {c: "" for c in COLS}
        base.update(calendar_type="kingdom", is_virtual="True")
        base.update(r)
        out.append(base)
    return pd.DataFrame(out, columns=COLS)


class TestPromoteVirtualBaronials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.nonexistent = Path(self.tmp) / "nope.csv"     # disables name-match path

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _groups_csv(self, rows):
        p = Path(self.tmp) / "group_locations.csv"
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["group", "kingdom"])
            w.writeheader()
            w.writerows(rows)
        return p

    def test_group_prefix_title_promoted(self):
        df = _df([{"title": "Barony of Tarnmists Business Meeting (Virtual)",
                   "source": "Kingdom of the West"}])
        n = clean.promote_virtual_baronials(df, self.nonexistent)
        self.assertEqual(n, 1)
        self.assertEqual(df.iloc[0]["calendar_type"], "baronial")

    def test_baronial_keyword_title_promoted(self):
        df = _df([{"title": "Fettburg Baronial Meeting", "source": "Kingdom of Ealdormere"},
                  {"title": "Skrael Fighter Practice", "source": "Kingdom of Ealdormere"}])
        n = clean.promote_virtual_baronials(df, self.nonexistent)
        self.assertEqual(n, 2)
        self.assertTrue((df["calendar_type"] == "baronial").all())

    def test_non_virtual_event_never_promoted(self):
        df = _df([{"title": "Barony of X Business Meeting", "source": "Kingdom of the West",
                   "is_virtual": "False"}])
        n = clean.promote_virtual_baronials(df, self.nonexistent)
        self.assertEqual(n, 0)
        self.assertEqual(df.iloc[0]["calendar_type"], "kingdom")

    def test_kingdom_level_virtual_not_promoted(self):
        # "Officer/Council/Populace Meeting" is deliberately NOT a baronial keyword.
        df = _df([{"title": "Kingdom Officer Meeting", "source": "Kingdom of the West"},
                  {"title": "Curia (Online)", "source": "Kingdom of the West"}])
        n = clean.promote_virtual_baronials(df, self.nonexistent)
        self.assertEqual(n, 0)
        self.assertTrue((df["calendar_type"] == "kingdom").all())

    def test_already_baronial_not_counted(self):
        df = _df([{"title": "Barony of X Meeting", "source": "Kingdom of the West",
                   "calendar_type": "baronial"}])
        n = clean.promote_virtual_baronials(df, self.nonexistent)
        self.assertEqual(n, 0)

    def test_known_group_shortname_match(self):
        # Title has no prefix/keyword, but matches a known group of the same
        # kingdom (diacritic/spacing-insensitive).
        groups = self._groups_csv([
            {"group": "Barony of Aarnimetsä", "kingdom": "Kingdom of Drachenwald"},
        ])
        df = _df([{"title": "Aarnimetsa Populace Gathering", "source": "Kingdom of Drachenwald"}])
        n = clean.promote_virtual_baronials(df, groups)
        self.assertEqual(n, 1)
        self.assertEqual(df.iloc[0]["calendar_type"], "baronial")

    def test_shortname_match_is_kingdom_scoped(self):
        # The same short-name in a DIFFERENT kingdom must not match.
        groups = self._groups_csv([
            {"group": "Barony of Foobar", "kingdom": "Kingdom of the West"},
        ])
        df = _df([{"title": "Foobar Gathering", "source": "Kingdom of Ealdormere"}])
        n = clean.promote_virtual_baronials(df, groups)
        self.assertEqual(n, 0)

    def test_no_virtual_kingdom_rows_returns_zero(self):
        df = _df([{"title": "Barony of X Meeting", "source": "Kingdom of the West",
                   "is_virtual": "False", "calendar_type": "kingdom"}])
        self.assertEqual(clean.promote_virtual_baronials(df, self.nonexistent), 0)


class TestNormNameAndGroupLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_norm_name_strips_diacritics_and_punct(self):
        self.assertEqual(clean._norm_name("Aarnimetsä"), "aarnimetsa")
        self.assertEqual(clean._norm_name("Winter's Gate"), "wintersgate")
        self.assertEqual(clean._norm_name(""), "")

    def test_load_groups_strips_prefix_and_min_length(self):
        p = Path(self.tmp) / "g.csv"
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["group", "kingdom"])
            w.writeheader()
            w.writerows([
                {"group": "Barony of Foobar", "kingdom": "Kingdom of the West"},
                {"group": "Shire of Qy", "kingdom": "Kingdom of the West"},  # too short -> dropped
            ])
        got = clean._load_groups_by_kingdom(p)
        self.assertIn("foobar", got["Kingdom of the West"])
        self.assertNotIn("qy", got["Kingdom of the West"])

    def test_load_groups_missing_file(self):
        self.assertEqual(clean._load_groups_by_kingdom(Path(self.tmp) / "nope.csv"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
