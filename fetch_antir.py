"""
fetch_antir.py
--------------
An Tir is fronted by Cloudflare's Managed Challenge, which blocks every
`requests`-based fetch (including cloudscraper, curl_cffi) with a 403
"Just a moment..." page. The challenge requires a real browser that
executes JavaScript and submits the proof-of-work token back.

This module spawns a real Chromium (via Playwright) to clear the challenge
and download:
  - antir_events.ics    : the kingdom calendar feed
  - antir_groups.html   : the local-groups directory page (used by
                          build_group_locations.py)

Run manually:
    python fetch_antir.py

Or call from the pipeline:
    from fetch_antir import fetch_antir_events, fetch_antir_groups

Headless vs headed:
  - In a desktop environment (or CI with xvfb), `headless=True` works
    most of the time. Cloudflare occasionally detects headless Chromium
    and serves a CAPTCHA — in that case the script falls back to headed
    mode so a human can solve it once.
  - On a server with no display, you need xvfb-run as a wrapper:
        xvfb-run python fetch_antir.py
  - In the Claude Code sandbox, neither mode works (no spawn capability).

Cookies set by Cloudflare (cf_clearance) are saved to .antir_cookies.json
so subsequent runs can short-circuit the challenge.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
COOKIE_FILE = SCRIPT_DIR / ".antir_cookies.json"
ICS_OUT = SCRIPT_DIR / "antir_events.ics"
GROUPS_OUT = SCRIPT_DIR / "antir_groups.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _load_cookies() -> list[dict]:
    if not COOKIE_FILE.exists():
        return []
    try:
        return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_cookies(cookies: list[dict]) -> None:
    # Persist only the Cloudflare clearance + session cookies — drop nulls
    relevant = [c for c in cookies if c.get("name", "").startswith(("cf_", "__cf"))]
    COOKIE_FILE.write_text(json.dumps(relevant, indent=2), encoding="utf-8")


def _clear_challenge(page, max_seconds: int = 60) -> bool:
    """
    Wait up to max_seconds for the Cloudflare challenge page to clear.
    Checks both the title and the body content — Cloudflare sometimes
    keeps the title at "Just a moment..." briefly even after the
    challenge has solved, so we also look for the real-page marker
    (the SCA's An Tir text, "Society for Creative Anachronism").
    """
    for _ in range(max_seconds):
        time.sleep(1)
        title = page.title()
        if title and "Just a moment" not in title:
            return True
        # Sometimes content loads before title updates
        try:
            body_text = page.inner_text("body", timeout=500)
            if body_text and ("Society for Creative" in body_text
                              or "An Tir" in body_text
                              or "Kingdom" in body_text):
                return True
        except Exception:
            pass
    return False


def fetch_via_browser(headless: bool = True) -> tuple[str | None, str | None]:
    """
    Returns (ics_text, groups_html). Either may be None if that endpoint
    failed even after the Cloudflare challenge cleared.

    Prefers `patchright` (a hardened Playwright fork with built-in stealth
    that defeats most Cloudflare Managed Challenges) and falls back to
    plain `playwright` if patchright isn't installed.
    """
    try:
        from patchright.sync_api import sync_playwright  # type: ignore
        backend = "patchright"
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
            backend = "playwright"
        except ImportError:
            print("Neither patchright nor playwright is installed. Install with:",
                  file=sys.stderr)
            print("  pip install patchright && python -m patchright install chromium",
                  file=sys.stderr)
            return (None, None)
    print(f"(using {backend})")

    p = sync_playwright().start()
    # Try the user's installed Chrome first (channel='chrome'); falls back to
    # the bundled Chromium. patchright's Chromium is also pre-patched and
    # generally clears the challenge on its own.
    try:
        browser = p.chromium.launch(channel="chrome", headless=headless)
    except Exception:
        browser = p.chromium.launch(headless=headless)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 720},
        java_script_enabled=True,
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    # Restore prior CF cookies if we have them — often saves us the wait.
    saved = _load_cookies()
    if saved:
        context.add_cookies(saved)

    page = context.new_page()
    # Step 1: Visit the root to clear the Cloudflare challenge. We use
    # 'load' (not 'domcontentloaded') so we don't race the challenge JS.
    print("→ Visiting antir.org/")
    try:
        page.goto("https://antir.org/", wait_until="load", timeout=90_000)
    except Exception as exc:
        print(f"  navigation: {exc}")
    cleared = _clear_challenge(page)
    if not cleared:
        print(f"  challenge did not clear (title={page.title()!r})")
        if headless:
            print("  retrying in headed mode...")
            browser.close()
            p.stop()
            return fetch_via_browser(headless=False)
        browser.close()
        p.stop()
        return (None, None)

    # Persist the new cookies so the next run can skip the wait
    _save_cookies(context.cookies("https://antir.org"))

    # Step 2: Pull the ICS feed via the same authenticated browser context
    print("→ Fetching events.ics")
    ics_text: str | None = None
    try:
        resp = context.request.get("https://antir.org/events.ics", timeout=30_000)
        body = resp.text()
        if "VCALENDAR" in body:
            ics_text = body
            n = len(re.findall(r"BEGIN:VEVENT", body))
            print(f"   {n} events")
        else:
            print(f"   no VCALENDAR (status={resp.status}, len={len(body)})")
    except Exception as exc:
        print(f"   error: {exc}")

    # Step 3: Pull the groups HTML for build_group_locations.py to parse
    print("→ Fetching /groups/")
    groups_html: str | None = None
    for path in ("/groups/", "/branches/", "/about/branches/"):
        try:
            resp = context.request.get(f"https://antir.org{path}", timeout=30_000)
            if resp.status == 200 and "Just a moment" not in resp.text()[:300]:
                groups_html = resp.text()
                # Quick sanity check: does it look like a real group page?
                if re.search(r"Barony|Shire|Canton", groups_html, re.I):
                    print(f"   got {path} ({len(groups_html)} chars)")
                    break
        except Exception:
            continue

    browser.close()
    p.stop()
    return (ics_text, groups_html)


def fetch_antir_events() -> str | None:
    """Public entry point used by ImportMaps.py."""
    ics, _ = fetch_via_browser()
    if ics:
        ICS_OUT.write_text(ics, encoding="utf-8")
        print(f"Wrote {ICS_OUT.name}")
    return ics


def fetch_antir_groups() -> str | None:
    """Public entry point used by build_group_locations.py."""
    _, html = fetch_via_browser()
    if html:
        GROUPS_OUT.write_text(html, encoding="utf-8")
        print(f"Wrote {GROUPS_OUT.name}")
    return html


if __name__ == "__main__":
    ics, html = fetch_via_browser()
    if ics:
        ICS_OUT.write_text(ics, encoding="utf-8")
        print(f"Wrote {ICS_OUT.name}")
    if html:
        GROUPS_OUT.write_text(html, encoding="utf-8")
        print(f"Wrote {GROUPS_OUT.name}")
    if not (ics or html):
        sys.exit(1)
