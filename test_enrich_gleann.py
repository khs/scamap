"""
test_enrich_gleann.py
---------------------
Offline tests for enrich_gleann_locations.py — the step that pins Gleann Abhann's
location-less kingdom events to their hosting group, read from each event page's
JSON-LD organizer. No network: parsing, name normalisation, and group resolution
are exercised directly. Catches the apostrophe/HTML-entity mismatch that would
otherwise drop a group ("Dragoun&#8217;s Weal" vs "Dragoun's Weal").

Run:
    python -m unittest test_enrich_gleann -v
"""
from __future__ import annotations

import unittest

import enrich_gleann_locations as eg


class TestNormalise(unittest.TestCase):
    def test_clean_unescapes_and_folds_apostrophes(self):
        self.assertEqual(eg._clean("Dragoun&#8217;s Weal"), "Dragoun's Weal")
        self.assertEqual(eg._clean("Dragoun’s Weal"), "Dragoun's Weal")

    def test_full_is_lowercased_whole_name(self):
        self.assertEqual(eg._full("Shire of Dragoun&#8217;s Weal"), "shire of dragoun's weal")

    def test_norm_strips_group_type_prefix(self):
        self.assertEqual(eg._norm("Province of Lagerdamm"), "lagerdamm")
        self.assertEqual(eg._norm("Barony of Axemoor"), "axemoor")
        self.assertEqual(eg._norm("Shire of Tor an Riogh"), "tor an riogh")


class TestParseOrganizer(unittest.TestCase):
    def test_parses_jsonld_organizer_name(self):
        html = ('<script type="application/ld+json">{"@type":"Event",'
                '"organizer":{"@type":"Person","name":"Province of Lagerdamm",'
                '"url":""}}</script>')
        self.assertEqual(eg.parse_organizer(html), "Province of Lagerdamm")

    def test_parses_organizer_list(self):
        html = '{"organizer":[{"@type":"Person","name":"Barony of Grey Niche"}]}'
        self.assertEqual(eg.parse_organizer(html), "Barony of Grey Niche")

    def test_entity_apostrophe_is_folded(self):
        html = '{"organizer":{"name":"Shire of Dragoun&#8217;s Weal"}}'
        self.assertEqual(eg.parse_organizer(html), "Shire of Dragoun's Weal")

    def test_none_when_no_organizer(self):
        self.assertIsNone(eg.parse_organizer("<html><body>no json-ld</body></html>"))


class TestResolve(unittest.TestCase):
    def setUp(self):
        def entry(name, lat, lng, loc):
            return {eg._full(name): (name, lat, lng, loc),
                    eg._norm(name): (name, lat, lng, loc)}
        self.groups = {}
        self.groups.update(entry("Province of Lagerdamm", "35.25", "-92.68", "Conway, AR"))
        self.groups.update(entry("Shire of Dragoun's Weal", "32.5", "-93.7", "Bossier City, LA"))

    def test_resolves_full_name(self):
        self.assertEqual(eg.resolve("Province of Lagerdamm", self.groups)[0],
                         "Province of Lagerdamm")

    def test_resolves_through_entity_apostrophe(self):
        # The page's organizer carries the HTML entity; it must still resolve to
        # the straight-quote group from locals.csv.
        hit = eg.resolve("Shire of Dragoun&#8217;s Weal", self.groups)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "Shire of Dragoun's Weal")

    def test_unknown_group_returns_none(self):
        self.assertIsNone(eg.resolve("Barony of Nowhere", self.groups))

    def test_empty_returns_none(self):
        self.assertIsNone(eg.resolve("", self.groups))


class TestLoadGroupsRealLocals(unittest.TestCase):
    """Light check against the committed locals.csv: the Gleann groups the scrape
    relies on must be present with coordinates."""

    def test_finds_known_gleann_groups_with_coords(self):
        groups = eg.load_groups()
        self.assertTrue(groups, "no Gleann Abhann groups parsed from locals.csv")
        for name in ("Barony of Axemoor", "Province of Lagerdamm"):
            self.assertIn(eg._norm(name), groups, f"{name} missing from locals.csv")
            g = groups[eg._norm(name)]
            self.assertTrue(g[1] and g[2], f"{name} has no coordinates")


if __name__ == "__main__":
    unittest.main(verbosity=2)
