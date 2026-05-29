"""
find_barony_calendars.py
------------------------
One-off helper to discover Google Calendar IDs embedded in barony websites.
Tries each barony's homepage and a few common calendar sub-paths, decodes
base64-encoded `src=` params from embedded iframes, and prints the resulting
calendar ID along with the URL where it was found.

Output format is one row per discovered calendar, in the same CSV shape as
calendars.csv: id,source,type
"""

import base64
import re
from urllib.parse import unquote

import requests

BARONIES = [
    ("Barony of Lochmere",           "http://lochmere.atlantia.sca.org"),
    ("Barony of Bright Hills",       "http://brighthills.atlantia.sca.org"),
    ("Barony of Dun Carraig",        "http://www.duncarraig.net"),
    ("Barony of Highland Foorde",    "http://highland-foorde.atlantia.sca.org"),
    ("Barony of Windmasters' Hill",  "http://www.windmastershill.org"),
    ("Barony of Raven's Cove",       "http://ravenscove.atlantia.sca.org"),
    ("Barony of Hawkwood",           "http://hawkwood.atlantia.sca.org"),
    ("Barony of Sacred Stone",       "http://sacredstone.atlantia.sca.org"),
    ("Barony of Nottinghill Coill",  "http://www.nottinghillcoill.atlantia.sca.org"),
    ("Barony of Hidden Mountain",    "http://hiddenmountain.atlantia.sca.org"),
    ("Barony of Stierbach",          "http://stierbach.org"),
    ("Barony of Caer Mear",          "https://caermear.atlantia.sca.org"),
    ("Barony of Marinus",            "http://www.baronyofmarinus.com"),
    ("Barony of Tir-y-Don",          "http://tirydon.atlantia.sca.org"),
    # Black Diamond's host doesn't have the hyphen the directory implies
    ("Barony of Black Diamond",      "https://blackdiamond.atlantia.sca.org"),
]

# Common sub-paths where calendars are embedded. /activities.html catches
# Caer Mear's static-HTML site; the rest cover the WordPress conventions.
SUBPATHS = ["/", "/calendar/", "/calendar", "/events/", "/events",
            "/activities.html", "/activities/"]

# Iframe src= patterns from Google Calendar embeds. Two encodings show up:
#   src=Y2FsZW5kYXJfaWRAZ3JvdXAuY2FsZW5kYXIuZ29vZ2xlLmNvbQ  (base64)
#   src=calendar_id%40group.calendar.google.com             (url-encoded)
SRC_BASE64_RE = re.compile(r"src=([A-Za-z0-9_\-]{20,})(?=[&\"'])")
SRC_URLENC_RE = re.compile(r"src=([\w.-]+%40[\w.-]+\.calendar\.google\.com)")
ICS_RE        = re.compile(r"https://calendar\.google\.com/calendar/ical/([^/]+)/public/basic\.ics")


def decode_b64(s: str) -> str | None:
    """Try to decode a base64 src= param. Pads as needed. Returns the decoded
    calendar ID if it looks like one, otherwise None."""
    try:
        padded = s + "=" * (-len(s) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        if "@" in decoded and "calendar.google.com" in decoded:
            return decoded
    except Exception:
        pass
    return None


def find_calendar_ids(html: str) -> list[str]:
    """Pull every calendar ID we can find out of a page's HTML."""
    found = set()

    for m in SRC_BASE64_RE.finditer(html):
        cid = decode_b64(m.group(1))
        if cid:
            found.add(cid)

    for m in SRC_URLENC_RE.finditer(html):
        found.add(unquote(m.group(1)))

    for m in ICS_RE.finditer(html):
        found.add(unquote(m.group(1)))

    return sorted(found)


def fetch(url: str) -> str:
    try:
        r = requests.get(url, timeout=15, allow_redirects=True,
                         headers={"User-Agent": "SCA Maps Project research bot"})
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


def main():
    print("id,source,type  # CSV-format output for paste into calendars.csv")
    print()
    for name, base in BARONIES:
        all_found = set()
        winning_url = None
        for sub in SUBPATHS:
            html = fetch(base.rstrip("/") + sub)
            if not html:
                continue
            ids = find_calendar_ids(html)
            if ids:
                if winning_url is None:
                    winning_url = base.rstrip("/") + sub
                all_found.update(ids)
                # Don't waste requests hitting more subpaths once we have a hit
                break

        if not all_found:
            print(f"# {name}: no calendar found ({base})")
            continue

        # Prefer @group.calendar.google.com IDs over @import.calendar.google.com
        sorted_ids = sorted(all_found,
                            key=lambda x: (0 if "@group" in x else 1, x))
        for cid in sorted_ids:
            print(f"{cid},{name},baronial  # from {winning_url}")


if __name__ == "__main__":
    main()
