"""
test_index_html_safety.py
-------------------------
Static guards on index.html's link handling. Event URLs come from third-party
calendar feeds, so a feed can carry a `javascript:`/`data:` URL; escapeHtml stops
attribute-breakout but NOT a dangerous scheme (it has no <>&"' to escape). The
front-end therefore routes every href through safeUrl(), which allows only
http(s)/mailto. These tests lock that in so a future edit can't silently
reintroduce a clickable script URL. (index.html has no JS test harness; this is a
source-level lint in the same spirit as test_csv_integrity's index.html checks.)

Run:
    python -m unittest test_index_html_safety -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

INDEX = Path(__file__).parent / "index.html"


class TestHrefSinksSanitized(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_safeurl_helper_exists_and_allowlists_schemes(self):
        self.assertIn("function safeUrl(", self.html)
        # The allowlist must be present (http/https/mailto) — a regression that
        # widened it would show up here.
        m = re.search(r"function safeUrl\(u\)\s*\{.*?\n\}", self.html, re.S)
        self.assertIsNotNone(m, "safeUrl body not found")
        body = m.group(0)
        for scheme in ("http:", "https:", "mailto:"):
            self.assertIn(scheme, body, f"safeUrl no longer allows {scheme}")

    def test_bestlinkfor_returns_through_safeurl(self):
        m = re.search(r"function bestLinkFor\(ev\)\s*\{.*?\n\}", self.html, re.S)
        self.assertIsNotNone(m, "bestLinkFor not found")
        self.assertIn("return safeUrl(", m.group(0),
                      "bestLinkFor must sanitize its returned URL")

    def test_no_dynamic_href_interpolates_an_unchecked_feed_url(self):
        # Any href="${...}" whose expression references a URL-ish feed source must
        # go through safeUrl. Sinks that interpolate an already-sanitized local var
        # (escapeHtml(safe) / escapeHtml(eventLink) / ...) don't name a feed source
        # inline and are correctly ignored.
        urlish = re.compile(
            r"ev\.\w*url|ev\.schedule|pin\.website|websiteForSource|bestLinkFor",
            re.IGNORECASE)
        bad = []
        for m in re.finditer(r'href="\$\{([^}]*)\}"', self.html):
            expr = m.group(1)
            if urlish.search(expr) and "safeUrl" not in expr:
                line = self.html[:m.start()].count("\n") + 1
                bad.append((line, expr.strip()))
        self.assertEqual(bad, [], f"unsanitized feed URL in an href: {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
