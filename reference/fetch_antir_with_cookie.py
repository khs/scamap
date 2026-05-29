"""
fetch_antir_with_cookie.py
--------------------------
Manual-cookie fallback for antir.org. Use this when patchright/playwright
can't pass Cloudflare's Managed Challenge automatically.

How to use:
  1. In your regular browser (Chrome/Edge/Firefox), visit https://antir.org/
     and wait for the page to load past "Just a moment...".
  2. Open DevTools (F12) → Application tab → Cookies → https://antir.org
  3. Copy the *value* of the `cf_clearance` cookie (a long random string).
  4. Either paste it into .antir_cf_clearance.txt in this directory, or
     pass it as the first command-line argument:
         python fetch_antir_with_cookie.py "<the-cf_clearance-value>"
  5. The script uses curl_cffi with matching Chrome TLS fingerprint + that
     cookie to fetch antir.org/events.ics and antir.org/groups/.

Cookie lifetime: typically 4–24 hours depending on Cloudflare's risk score
for your IP. Re-export if you start getting 403s again.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
COOKIE_FILE = SCRIPT_DIR / ".antir_cf_clearance.txt"
ICS_OUT = SCRIPT_DIR / "antir_events.ics"
GROUPS_OUT = SCRIPT_DIR / "antir_groups.html"

# Must match the Chrome version the cookie was set under
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome131"


def _load_clearance() -> str:
    if len(sys.argv) > 1 and sys.argv[1]:
        val = sys.argv[1].strip()
        # Save for next time
        COOKIE_FILE.write_text(val, encoding="utf-8")
        return val
    if COOKIE_FILE.exists():
        return COOKIE_FILE.read_text(encoding="utf-8").strip()
    print(
        "No cf_clearance cookie available. Either:\n"
        f"  1. Save it to {COOKIE_FILE.name}, or\n"
        "  2. Pass it as the first argument:\n"
        '       python fetch_antir_with_cookie.py "<cookie-value>"',
        file=sys.stderr,
    )
    sys.exit(2)


def main():
    try:
        from curl_cffi import requests
    except ImportError:
        print("curl_cffi not installed. Install with: pip install curl_cffi",
              file=sys.stderr)
        sys.exit(1)

    clearance = _load_clearance()
    cookies = {"cf_clearance": clearance}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Step 1: ICS feed
    print("→ Fetching antir.org/events.ics")
    r = requests.get(
        "https://antir.org/events.ics",
        cookies=cookies, headers=headers,
        impersonate=IMPERSONATE, timeout=30,
    )
    body = r.text
    if "VCALENDAR" in body:
        n = len(re.findall(r"BEGIN:VEVENT", body))
        ICS_OUT.write_text(body, encoding="utf-8")
        print(f"   {n} events → {ICS_OUT.name}")
    else:
        print(f"   ICS fetch failed: status={r.status_code}, len={len(body)}")
        if "Just a moment" in body[:300]:
            print("   (Cloudflare blocked us — cookie may have expired; "
                  "re-export from your browser.)")

    # Step 2: Groups page
    print("→ Fetching antir.org/groups/")
    r = requests.get(
        "https://antir.org/groups/",
        cookies=cookies, headers=headers,
        impersonate=IMPERSONATE, timeout=30,
    )
    body = r.text
    if r.status_code == 200 and "Barony" in body:
        GROUPS_OUT.write_text(body, encoding="utf-8")
        groups = len(re.findall(
            r"(?:Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+",
            body, re.I,
        ))
        print(f"   {groups} group mentions → {GROUPS_OUT.name}")
    else:
        print(f"   Groups fetch failed: status={r.status_code}")


if __name__ == "__main__":
    main()
