"""
clean_sca_events.py
-------------------
Cleans the raw sca_events.csv produced by fetch_sca_events.py and outputs
a cleaned version ready for geocoding.

Run this after fetch_sca_events.py. It reads sca_events.csv and writes
sca_events_clean.csv in the same directory.

What this script does:
  1. Unescape any remaining ICS backslash sequences in all fields
  2. Remove non-events (deadlines, reports due, cancelled events, etc.)
  3. Clean descriptions: strip HTML tags, keep plain-text event/facebook URLs,
     remove personal info (emails, phone numbers, autocrat contact blocks),
     blank out placeholder "no description yet" text
  4. Clean locations: strip venue name prefix, leaving just the street address.
     If location is empty, attempt to extract a city/state from the description.
     Adds an address_confidence column: "high", "low", or "empty"
  5. Deduplicate: same start date + same location = duplicate.
     Prefer the entry from the kingdom that geographically hosts the event.
     Events explicitly marked "OUT OF KINGDOM" lose priority to non-OOK duplicates.
  6. Merge recurring events: same title + same location, occurring regularly
     (weekly or monthly). Collapses into one row with dates listed in description.
     If location changes between occurrences, keeps them separate.

Output columns (same as input, plus):
  clean_location     - best-guess street address (may be empty)
  address_confidence - "high" | "low" | "empty"

Requirements:
    pip install pandas beautifulsoup4 lxml

Usage:
    python clean_sca_events.py
"""

import csv
import re
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).parent
INPUT_FILE   = SCRIPT_DIR / "sca_events.csv"
OUTPUT_FILE  = SCRIPT_DIR / "sca_events_clean.csv"

# Titles containing any of these (case-insensitive) are not real events
NON_EVENT_PATTERNS = [
    r"\bdeadline\b",
    r"\breports?\s+due\b",
    r"\bsubmissions?\s+due\b",
    r"\bpolling\s+(opens|closes)\b",
    r"\bregistration\s+closes\b",
    r"\blast\s+day\s+to\b",
    r"\breminder\b",
    r"\bdue\s+date\b",
    r"^cancelled\b",
]

# Titles containing these indicate the event was cancelled — remove entirely
CANCELLED_PATTERNS = [
    r"^cancelled\b",
    r"^\[cancelled\]",
    r"^canceled\b",
]

# Descriptions matching any of these are placeholder text — blank them out
PLACEHOLDER_DESC_PATTERNS = [
    r"no description yet",
    r"no information yet",
    r"details (for this event )?have not (been )?(given|provided|submitted)",
    r"information (is )?coming soon",
    r"details to (be )?announced",
    r"contact us if you are the autocrat",
    r"The details for this event have not been given yet",
    r"PENDING HOST GROUP SUBMISSION",
]

# If a description contains any of these substrings it's almost certainly a
# Tribe Events Calendar widget that bled into the ICS export rather than real
# event content. Gleann Abhann's calendar does this — every event's description
# is the calendar's footer/sidebar text rather than the event description. Blank.
WIDGET_BOILERPLATE_MARKERS = [
    "Add to calendar Google Calendar iCalendar Outlook 365 Outlook Live",
    "+ Google Map - Arts & Sciences",
]

# AEthelmearc's calendar uses template placeholder locations like "Default R3,
# 18515" when no real venue is set. Treat these as no-location.
TEMPLATE_LOCATION_RE = re.compile(
    r"^\s*Default\s+R\d+\s*,?\s*\d{5}?\s*$",
    re.IGNORECASE,
)

# Regex to extract "City, ST" from a description when location field is empty
# e.g. "hosted in Burkburnett, TX" or "will be held in Austin, Texas"
CITY_STATE_RE = re.compile(
    r"(?:in|at|held at|hosted (?:in|at)|located in|location[:\s]+)\s+"
    r"([A-Z][a-zA-Z\s]{2,30}),\s*([A-Z]{2})\b"
)

# ---------------------------------------------------------------------------
# Kingdom → US states/regions mapping for deduplication priority
# ---------------------------------------------------------------------------

STATE_TO_KINGDOM = {
    # Kingdom of AEthelmearc (PA, WV, western NY)
    "PA": "Kingdom of AEthelmearc",
    "WV": "Kingdom of AEthelmearc",
    # Kingdom of Ansteorra (OK, TX)
    "OK": "Kingdom of Ansteorra",
    "TX": "Kingdom of Ansteorra",
    # Kingdom of Artemisia (MT, UT, southern ID, western WY)
    "MT": "Kingdom of Artemisia",
    "UT": "Kingdom of Artemisia",
    # Kingdom of Atenveldt (AZ, NM)
    "AZ": "Kingdom of Atenveldt",
    "NM": "Kingdom of Atenveldt",
    # Kingdom of Atlantia (MD, VA, NC, SC)
    "MD": "Kingdom of Atlantia",
    "VA": "Kingdom of Atlantia",
    "NC": "Kingdom of Atlantia",
    "SC": "Kingdom of Atlantia",
    # Kingdom of Caid (southern CA, HI, NV)
    "HI": "Kingdom of Caid",
    # Kingdom of Calontir (KS, MO, IA, NE)
    "KS": "Kingdom of Calontir",
    "MO": "Kingdom of Calontir",
    "IA": "Kingdom of Calontir",
    "NE": "Kingdom of Calontir",
    # Kingdom of the East (CT, DE, ME, MA, NH, NJ, RI, VT, eastern NY)
    "CT": "Kingdom of the East",
    "DE": "Kingdom of the East",
    "ME": "Kingdom of the East",
    "MA": "Kingdom of the East",
    "NH": "Kingdom of the East",
    "NJ": "Kingdom of the East",
    "RI": "Kingdom of the East",
    "VT": "Kingdom of the East",
    # Kingdom of Gleann Abhann (MS, AR, most of LA)
    "MS": "Kingdom of Gleann Abhann",
    "AR": "Kingdom of Gleann Abhann",
    # Kingdom of Meridies (AL, most of GA, most of TN, parts of KY/FL)
    "AL": "Kingdom of Meridies",
    # Kingdom of the Middle (IL, IN, OH, MI)
    "IL": "Kingdom of the Middle",
    "IN": "Kingdom of the Middle",
    "OH": "Kingdom of the Middle",
    "MI": "Kingdom of the Middle",
    # Kingdom of Northshield (MN, WI, ND, SD)
    "MN": "Kingdom of Northshield",
    "WI": "Kingdom of Northshield",
    "ND": "Kingdom of Northshield",
    "SD": "Kingdom of Northshield",
    # Kingdom of the Outlands (CO, most of WY)
    "CO": "Kingdom of the Outlands",
    # Kingdom of An Tir (OR, WA, northern ID, most of BC)
    "OR": "Kingdom of An Tir",
    "WA": "Kingdom of An Tir",
    # Kingdom of Lochac (Australia, New Zealand)
    "NSW": "Kingdom of Lochac",
    "VIC": "Kingdom of Lochac",
    "QLD": "Kingdom of Lochac",
    "SA":  "Kingdom of Lochac",
    "TAS": "Kingdom of Lochac",
    "ACT": "Kingdom of Lochac",
    "NT":  "Kingdom of Lochac",
}


# ---------------------------------------------------------------------------
# Step 0: Unescape remaining ICS sequences
# ---------------------------------------------------------------------------

def unescape_ics(text: str) -> str:
    """Fix any remaining ICS backslash escapes not caught by the fetcher."""
    if not isinstance(text, str):
        return text
    text = text.replace("\\,", ",")
    text = text.replace("\\;", ";")
    text = text.replace("\\n", " ").replace("\\N", " ")
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Step 1: Remove non-events
# ---------------------------------------------------------------------------

def is_non_event(title: str) -> bool:
    """Return True if the title indicates this is not a real event."""
    if not isinstance(title, str):
        return False
    for pattern in NON_EVENT_PATTERNS:
        if re.search(pattern, title.strip(), re.IGNORECASE):
            return True
    return False


def is_cancelled(title: str) -> bool:
    """Return True if the event is explicitly marked as cancelled."""
    if not isinstance(title, str):
        return False
    for pattern in CANCELLED_PATTERNS:
        if re.search(pattern, title.strip(), re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Step 2: Clean descriptions
# ---------------------------------------------------------------------------

def is_placeholder_description(desc: str) -> bool:
    """Return True if the description is a placeholder and should be blanked."""
    if not isinstance(desc, str) or not desc.strip():
        return True
    for pattern in PLACEHOLDER_DESC_PATTERNS:
        if re.search(pattern, desc.strip(), re.IGNORECASE):
            return True
    return False


def unwrap_redirect(url: str) -> str:
    """Strip Facebook l.php and Google redirect wrappers to get the real URL."""
    from urllib.parse import unquote
    fb_match = re.search(r"[?&]u=(https?[^&]+)", url)
    if fb_match:
        return unquote(fb_match.group(1))
    g_match = re.search(r"[?&]q=(https?[^&]+)", url)
    if g_match:
        return unquote(g_match.group(1))
    return url


def extract_urls_from_html(html: str) -> dict:
    """
    Parse HTML and extract the first event website URL and first Facebook URL.
    Returns a dict with keys 'event_url' and 'facebook_url' (either may be None).
    """
    soup = BeautifulSoup(html, "lxml")
    event_url = None
    facebook_url = None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        link_text = a.get_text(strip=True)

        if not event_url and re.search(r"event\s+web", link_text, re.IGNORECASE):
            event_url = unwrap_redirect(href)

        if not facebook_url and ("facebook.com" in href or "fb.com" in href):
            facebook_url = unwrap_redirect(href)

    # Fallback: first non-Facebook link
    if not event_url:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "facebook.com" not in href and "fb.com" not in href:
                candidate = unwrap_redirect(href)
                if candidate.startswith("http"):
                    event_url = candidate
                    break

    return {"event_url": event_url, "facebook_url": facebook_url}


PLAIN_URL_RE = re.compile(r"https?://[^\s<>'\")\]]+")


def extract_urls_from_text(text: str) -> dict:
    """
    Pull the first event-flyer-ish URL and first Facebook URL out of a
    plain-text description (after HTML stripping). Returns the same
    {event_url, facebook_url} shape as extract_urls_from_html.
    """
    event_url = None
    facebook_url = None
    for m in PLAIN_URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:)")
        is_fb = "facebook.com" in url or "fb.com" in url
        if is_fb and not facebook_url:
            facebook_url = url
        elif not is_fb and not event_url:
            event_url = url
        if event_url and facebook_url:
            break
    return {"event_url": event_url, "facebook_url": facebook_url}


def clean_description(desc: str) -> tuple:
    """
    Clean a description field. Returns (cleaned_text, urls_dict) where
    urls_dict has keys event_url and facebook_url (either may be None).

      - Blank out placeholder text entirely
      - Blank out calendar-widget boilerplate (Tribe Events Calendar etc.)
      - If it contains HTML, strip tags and extract useful URLs
      - Remove personal info: emails, phone numbers, autocrat contact blocks
      - Collapse whitespace
    """
    if not isinstance(desc, str) or not desc.strip():
        return ("", {"event_url": None, "facebook_url": None})

    urls = {"event_url": None, "facebook_url": None}

    if re.search(r"<[a-z]", desc, re.IGNORECASE):
        urls = extract_urls_from_html(desc)
        soup = BeautifulSoup(desc, "lxml")
        desc = soup.get_text(separator=" ")

    # Tribe Events Calendar widget boilerplate (Gleann Abhann does this) —
    # the description is the calendar widget's HTML, not event content. The
    # URLs in the boilerplate point to the wrong event, so drop them too.
    # Normalise whitespace first so tabs/multi-spaces in the feed don't
    # break the marker match.
    desc_normalised = re.sub(r"\s+", " ", desc)
    if any(marker in desc_normalised for marker in WIDGET_BOILERPLATE_MARKERS):
        return ("", {"event_url": None, "facebook_url": None})

    # Blank placeholder descriptions after stripping HTML
    if is_placeholder_description(desc):
        return ("", urls)

    # If no URLs from HTML, try plain-text URL extraction
    if not urls["event_url"] and not urls["facebook_url"]:
        urls = extract_urls_from_text(desc)

    # Remove URLs from the body since we're surfacing them separately
    desc = PLAIN_URL_RE.sub("", desc)

    # Remove email addresses
    desc = re.sub(r"[\w.+\-]+@[\w\-]+\.\w+", "", desc)

    # Remove phone numbers (various formats)
    desc = re.sub(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}", "", desc)

    # Remove autocrat/contact lines
    desc = re.sub(
        r"(autocrat|reservations?\s*(clerk)?|event\s*steward|contact|troll|"
        r"marshal\s*in\s*charge|feastcrat|royal\s*liaison|minister\s*of|"
        r"merchant\s*li[ae]son|coordinator)\s*[:\-].*",
        "",
        desc,
        flags=re.IGNORECASE,
    )

    # Remove P.O. Box lines
    desc = re.sub(r"P\.?\s*O\.?\s*Box\s+\d+[\w\s,\.]*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"c/o\s+[\w\s]+,[\w\s,]+\d{5}", "", desc, flags=re.IGNORECASE)

    # Collapse whitespace
    desc = re.sub(r"\s{2,}", " ", desc).strip(" .,;|")

    return (desc, urls)


# ---------------------------------------------------------------------------
# Step 3: Clean locations + extract city from description if empty
# ---------------------------------------------------------------------------

STREET_ADDRESS_RE = re.compile(
    r"\b\d+\s+[\w\s.]+(?:road|rd|street|st|avenue|ave|drive|dr|lane|ln|"
    r"blvd|boulevard|way|court|ct|place|pl|highway|hwy|parkway|pkwy|"
    r"circle|cir|trail|trl|run|pike|path|row)\b",
    re.IGNORECASE,
)
HIGHWAY_RE = re.compile(r"\b\d+\s+[A-Z]{1,3}-\d+\b")
ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b|\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b")


def _strip_leading_venue(part: str) -> str:
    """
    Given a chunk like "Burger's Lake 1200 Meandering Rd", return just the
    address starting from the first house-number-then-street-name pattern:
    "1200 Meandering Rd". Returns the original part if no match.
    """
    m = STREET_ADDRESS_RE.search(part)
    if m:
        return part[m.start():].strip()
    m = HIGHWAY_RE.search(part)
    if m:
        return part[m.start():].strip()
    return part


PLACEHOLDER_LOCATIONS = {
    "tbd", "tba", "to be announced", "to be determined", "tbc",
    "n/a", "na", "none", "unknown", "pending", "see description",
    "see facebook", "see website", "see event page",
}

# When a location is just a bare kingdom name (e.g. "Kingdom of Northshield"
# — usually appears on out-of-kingdom events on another kingdom's calendar),
# Nominatim happily matches it to some random place ("Northshield" → Scotland,
# "Atlantia" → a town in Italy). Rewrite to a state-level fallback so the
# event gets a marker in roughly the right region.
KINGDOM_FALLBACK_LOCATION = {
    "Kingdom of AEthelmearc":   "Pennsylvania, USA",
    "Kingdom of An Tir":        "Oregon, USA",
    "Kingdom of Ansteorra":     "Texas, USA",
    "Kingdom of Artemisia":     "Utah, USA",
    "Kingdom of Atenveldt":     "Arizona, USA",
    "Kingdom of Atlantia":      "Virginia, USA",
    "Kingdom of Avacal":        "Alberta, Canada",
    "Kingdom of Caid":          "Los Angeles, California, USA",
    "Kingdom of Calontir":      "Missouri, USA",
    "Kingdom of Drachenwald":   "Berlin, Germany",
    "Kingdom of Ealdormere":    "Ontario, Canada",
    "Kingdom of Gleann Abhann": "Mississippi, USA",
    "Kingdom of Lochac":        "Sydney, Australia",
    "Kingdom of Meridies":      "Atlanta, Georgia, USA",
    "Kingdom of Northshield":   "Minnesota, USA",
    "Kingdom of the East":      "Albany, New York, USA",
    "Kingdom of the Middle":    "Indianapolis, Indiana, USA",
    "Kingdom of the Outlands":  "Colorado, USA",
    "Kingdom of the West":      "Berkeley, California, USA",
    "Kingdom of Trimaris":      "Florida, USA",
}

# Matches "Kingdom of <Name>", "the Kingdom of <Name>", etc.
KINGDOM_NAME_RE = re.compile(
    r"^\s*(?:the\s+)?kingdom\s+of\s+(.+?)\s*$",
    re.IGNORECASE,
)

# 2-letter US state code → full state name. Used to rewrite locations like
# "LA" into "Louisiana, USA" — Nominatim interprets bare "LA" as Laos.
US_STATE_NAMES = {
    "AL": "Alabama",       "AK": "Alaska",         "AZ": "Arizona",
    "AR": "Arkansas",      "CA": "California",     "CO": "Colorado",
    "CT": "Connecticut",   "DE": "Delaware",       "DC": "District of Columbia",
    "FL": "Florida",       "GA": "Georgia",        "HI": "Hawaii",
    "ID": "Idaho",         "IL": "Illinois",       "IN": "Indiana",
    "IA": "Iowa",          "KS": "Kansas",         "KY": "Kentucky",
    "LA": "Louisiana",     "ME": "Maine",          "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan",       "MN": "Minnesota",
    "MS": "Mississippi",   "MO": "Missouri",       "MT": "Montana",
    "NE": "Nebraska",      "NV": "Nevada",         "NH": "New Hampshire",
    "NJ": "New Jersey",    "NM": "New Mexico",     "NY": "New York",
    "NC": "North Carolina","ND": "North Dakota",   "OH": "Ohio",
    "OK": "Oklahoma",      "OR": "Oregon",         "PA": "Pennsylvania",
    "RI": "Rhode Island",  "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee",     "TX": "Texas",          "UT": "Utah",
    "VT": "Vermont",       "VA": "Virginia",       "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin",      "WY": "Wyoming",
}


def clean_location(raw: str) -> tuple:
    """
    Attempt to strip the venue name from a location string, leaving a geocodable address.
    Returns (clean_address, confidence) where confidence is "high", "low", or "empty".
    """
    if not isinstance(raw, str) or not raw.strip():
        return ("", "empty")

    raw = unescape_ics(raw)

    # Placeholder strings like "TBD" — treat as empty so we don't geocode them
    if raw.strip().lower().rstrip(".") in PLACEHOLDER_LOCATIONS:
        return ("", "empty")

    # AEthelmearc-style "Default R3, 18515" template locations — not real
    if TEMPLATE_LOCATION_RE.match(raw.strip()):
        return ("", "empty")

    # Two-letter US state abbreviation alone (e.g. "LA", "TX") and bare
    # kingdom names ("Kingdom of Northshield") are both fallbacks of last
    # resort. We return ("", "empty") here so that description-based
    # extraction gets a chance to find a real venue first; if it doesn't,
    # apply_geographic_fallback() (called from main) will apply the
    # state/kingdom rewrite. This ordering lets a real street address in
    # the description win over a coarse state-level marker.
    bare = raw.strip()
    if bare in US_STATE_NAMES or KINGDOM_NAME_RE.match(bare):
        return ("", "empty")

    parts = [p.strip() for p in raw.split(",")]

    if not parts:
        return ("", "empty")

    # Case 1: first chunk starts with a street number (anchored match)
    first = parts[0]
    if STREET_ADDRESS_RE.match(first) or HIGHWAY_RE.match(first):
        return (raw, "high")

    # Case 2: a later chunk starts with a street number — drop everything before it
    for i, part in enumerate(parts[1:], start=1):
        if STREET_ADDRESS_RE.match(part.strip()) or HIGHWAY_RE.match(part.strip()):
            clean = ", ".join(parts[i:])
            return (clean, "high")

    # Case 3: the first chunk contains a street number embedded mid-string
    # (e.g. "Burger's Lake 1200 Meandering Rd") — strip the venue prefix
    if STREET_ADDRESS_RE.search(first) or HIGHWAY_RE.search(first):
        stripped_first = _strip_leading_venue(first)
        if stripped_first != first:
            cleaned_parts = [stripped_first] + parts[1:]
            return (", ".join(cleaned_parts), "high")

    if ZIP_RE.search(raw):
        return (raw, "low")

    return (raw, "low")


DESC_LABELED_LOCATION_RE = re.compile(
    # Match "Location:", "Venue:", "Address:", "Held at:" followed by a value
    # ending at the next field label, a sentence break, or end of line. The
    # value typically looks like "Mandt Center, 400 Mandt Parkway, Stoughton,
    # WI 53589" — venue name optional, but city/state should be there.
    r"(?:^|\s)(?:Location|Venue|Address|Held at|Site)\s*[:\-]\s*"
    r"(.+?)"
    r"(?=\s+(?:Website|URL|Event|Dates?|Time|Cost|Fees?|Autocrat|Stewards?|Contact|Hosted|Map|Direction|Notes?|Description)[:\-]"
    r"|\s+https?://"
    r"|[.!?]\s+[A-Z]"
    r"|$)",
    re.IGNORECASE,
)

# A full "<number> <street>, <city>, <ST> <zip>" pattern anywhere in text
DESC_FULL_ADDRESS_RE = re.compile(
    r"\b\d+\s+[\w\s.'\-]+?(?:road|rd|street|st|avenue|ave|drive|dr|"
    r"lane|ln|blvd|boulevard|way|court|ct|place|pl|highway|hwy|parkway|"
    r"pkwy|circle|cir|trail|trl|run|pike|path|row)"
    r"[\s.,]+[\w\s.'\-]+?,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?",
    re.IGNORECASE,
)


def extract_location_from_description(desc: str) -> tuple:
    """
    Try, in order of decreasing specificity, to find a real address in
    a description. Returns (address_string, confidence) where confidence
    is "high" (full street address), "low" (city/state), or "empty".

    The kingdom-of-X case is the motivating example: Caid's listing for
    "Known World Armored Combat Collegium (Kingdom of Northshield)" has
    `LOCATION: kingdom of Northshield` but the description spells out
    "Location: Mandt Center, 400 Mandt Parkway, Stoughton, WI 53589".
    Pull that out rather than fall back to "Minnesota".
    """
    if not isinstance(desc, str) or not desc.strip():
        return ("", "empty")

    # 1. Look for a free-floating "<num> <street>, <city>, <ST> <zip>" — the
    #    most reliable shape, works regardless of any "Location:" label.
    m = DESC_FULL_ADDRESS_RE.search(desc)
    if m:
        return (m.group(0).strip().rstrip(".,;"), "high")

    # 2. Look for an explicit "Location:"/"Venue:"/"Address:" label. The
    #    value is more flexible (may be just "Venue Name, City, ST"), so
    #    only mark "high" if it contains a street-address pattern; else "low".
    m = DESC_LABELED_LOCATION_RE.search(desc)
    if m:
        candidate = m.group(1).strip().rstrip(".,;")
        if DESC_FULL_ADDRESS_RE.search(candidate):
            return (candidate, "high")
        if CITY_STATE_RE.search(candidate) or re.search(r",\s*[A-Z]{2}\b", candidate):
            return (candidate, "low")

    # 3. Fall back to the old "<verb> in <City>, <ST>" pattern.
    m = CITY_STATE_RE.search(desc)
    if m:
        return (f"{m.group(1).strip()}, {m.group(2).strip()}", "low")

    return ("", "empty")


def apply_geographic_fallback(raw_location: str) -> tuple:
    """
    Last-resort fallback for rows where clean_location came up empty and
    description extraction also failed. If the raw location was just a
    state code or a kingdom name, expand it to a state/region-level
    geocodable string so the event still gets a marker (just an imprecise
    one). Returns (address_string, "low") or ("", "empty").
    """
    if not isinstance(raw_location, str) or not raw_location.strip():
        return ("", "empty")
    bare = raw_location.strip()
    if bare in US_STATE_NAMES:
        return (f"{US_STATE_NAMES[bare]}, USA", "low")
    m = KINGDOM_NAME_RE.match(bare)
    if m:
        normalized = "Kingdom of " + m.group(1).strip()
        for known, fallback in KINGDOM_FALLBACK_LOCATION.items():
            if known.lower() == normalized.lower() or \
               known.lower().replace("kingdom of ", "kingdom of the ") == normalized.lower() or \
               known.lower() == normalized.lower().replace("kingdom of the ", "kingdom of "):
                return (fallback, "low")
    return ("", "empty")


# ---------------------------------------------------------------------------
# Group → location lookup
# ---------------------------------------------------------------------------
# Many SCA calendars publish events as "Event Name (GroupName)" with no
# LOCATION or DESCRIPTION (e.g. Caid's 71-of-85). For those, we look up
# GroupName in group_locations.csv (built by build_group_locations.py) and
# fall back to the group's region centroid. Result is "low" confidence —
# specific to a region/county but not a real venue.

# Match parenthetical at the end of a title: "Academia (Nordwache)" → "Nordwache"
TITLE_GROUP_TAG_RE = re.compile(r"\(([^()]+)\)\s*$")

_group_location_cache: dict | None = None


def _normalize_group_key(name: str) -> str:
    """
    Normalise a group name for lookup. Strips diacritics, smart quotes, and
    punctuation so that "Barony of Thor's Mountain" matches "Thors Mountain"
    in event descriptions and "Shire of Owl's Nest" matches "Owls Nest".
    Returns lowercase alphanumerics + single spaces only.
    """
    if not name:
        return ""
    # Normalise smart-quote / accent variants to ASCII equivalents
    n = (name.lower()
              .replace("’", "'").replace("‘", "'")
              .replace("“", '"').replace("”", '"'))
    # Remove all punctuation (but keep spaces)
    n = re.sub(r"[^\w\s]", "", n, flags=re.UNICODE)
    # Collapse whitespace
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _load_group_locations() -> dict[str, str]:
    """Load group_locations.csv into a {normalized_name: location} dict.
    Each group gets two entries: the full name ("barony of nordwache") and
    the short name ("nordwache") so callers can look up either form."""
    global _group_location_cache
    if _group_location_cache is not None:
        return _group_location_cache
    path = SCRIPT_DIR / "group_locations.csv"
    cache: dict[str, str] = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                group = (row.get("group") or "").strip()
                loc   = (row.get("location") or "").strip()
                if not group or not loc:
                    continue
                full = _normalize_group_key(group)
                short = re.sub(r"^(barony|shire|canton|stronghold|college|province)\s+of\s+(?:the\s+)?",
                               "", full)
                cache[full] = loc
                if short and short != full:
                    cache[short] = loc
    _group_location_cache = cache
    return cache


def lookup_group_location(title: str, description: str = "") -> tuple:
    """
    Resolve an event to a known SCA group's region via three patterns:
      1. Title parenthetical: "Academia (Nordwache)", "Crown Tourney (Host: Lyondemere)"
      2. Description has "Hosted by Barony of X"
      3. Title contains a known short group name as a word ("Calafia Anniversary")
    Returns (region, "low") on hit, ("", "empty") on miss.

    All lookups go through _normalize_group_key so apostrophes and case
    variations (e.g. "Owl's Nest" vs "Owls Nest") match consistently.
    """
    table = _load_group_locations()

    # 1. Parenthetical at end of title; strip leading "Host:" / "Hosted by:"
    if isinstance(title, str):
        m = TITLE_GROUP_TAG_RE.search(title)
        if m:
            tag = m.group(1).strip()
            tag = re.sub(r"^\s*(?:Hosted\s+by[:\s]+|Host[:\s]+)", "", tag,
                         flags=re.IGNORECASE).strip()
            tag_norm = _normalize_group_key(tag)
            if tag_norm in table:
                return (table[tag_norm], "low")

    # 2. "Hosted by <Group>" in description
    if isinstance(description, str) and description:
        m = HOSTED_BY_RE.search(description)
        if m:
            group_norm = _normalize_group_key(m.group(1))
            if group_norm in table:
                return (table[group_norm], "low")
            short = re.sub(r"^(barony|shire|canton|stronghold|college|province)\s+of\s+(?:the\s+)?",
                           "", group_norm)
            if short in table:
                return (table[short], "low")

    # 3. Title contains a known SHORT group name as a word. Many calendars
    #    use the bare name at the start: "Altavia Anniversary", "Calafia Yule".
    #    Only the SHORT names (no "Barony of" prefix) are checked here so we
    #    don't accidentally match a city name like "Athens" appearing in an
    #    unrelated event title.
    if isinstance(title, str):
        title_norm = _normalize_group_key(title)
        for word in title_norm.split():
            if len(word) >= 5 and word in table:
                if word not in {"county", "valley", "lake", "river", "creek", "ridge"}:
                    return (table[word], "low")

    return ("", "empty")


# Matches a full group name in descriptions, used by lookup_group_location.
# Captures both "Hosted by …" (Meridies) and "Hosted By: …" (AEthelmearc).
HOSTED_BY_RE = re.compile(
    r"Hosted\s+(?:by|by:)\s*(?:the\s+)?((?:Barony|Shire|Canton|Stronghold|College)\s+of\s+(?:the\s+)?[\w''\.\- ]+?)"
    r"(?=\s*[.,;|]|\s+PENDING\b|\s+\(|\s+email\b|\s*$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Step 4: Deduplication (with OUT OF KINGDOM priority)
# ---------------------------------------------------------------------------

def is_out_of_kingdom(title: str) -> bool:
    """Return True if the title explicitly marks this as an out-of-kingdom event."""
    if not isinstance(title, str):
        return False
    return bool(re.search(r"out\s+of\s+kingdom", title, re.IGNORECASE))


def extract_state(location: str) -> str | None:
    """Try to extract a US/AU state abbreviation from a location string."""
    if not isinstance(location, str):
        return None
    match = re.search(r",\s*([A-Z]{2,3})\s*[,\d]", location)
    return match.group(1) if match else None


def preferred_source(group: pd.DataFrame) -> pd.Series:
    """
    Given a group of duplicate events, return the single best row to keep.

    Priority order:
      1. Drop any rows explicitly marked "OUT OF KINGDOM" if non-OOK rows exist
      2. Prefer the row from the kingdom that geographically hosts the event
      3. Fall back to kingdom-type calendar over baronial
      4. Fall back to first row
    """
    # 1. Prefer non-OOK entries if available
    non_ook = group[~group["title"].apply(is_out_of_kingdom)]
    candidates = non_ook if not non_ook.empty else group

    # 2. Prefer geographically appropriate kingdom
    location = candidates.iloc[0]["clean_location"] or candidates.iloc[0]["location"]
    state = extract_state(str(location))
    preferred_kingdom = STATE_TO_KINGDOM.get(state) if state else None

    if preferred_kingdom:
        match = candidates[candidates["source"] == preferred_kingdom]
        if not match.empty:
            return match.iloc[0]

    # 3. Prefer kingdom-type over baronial
    kingdom_rows = candidates[candidates["calendar_type"] == "kingdom"]
    if not kingdom_rows.empty:
        return kingdom_rows.iloc[0]

    return candidates.iloc[0]


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove duplicate events (same start date + same clean_location).
    Keeps the most geographically appropriate, non-OOK source.
    """
    # format="mixed" is required: the column mixes "YYYY-MM-DD" and
    # "YYYY-MM-DD HH:MM:SS" values, and pandas 2.x silently returns NaT
    # for one of those without it — which would group all NaT rows into
    # a single key and drop them entirely.
    df["_start_date"] = pd.to_datetime(df["start"], errors="coerce", format="mixed").dt.date
    df["_loc_key"] = df["clean_location"].fillna("").str.strip().str.lower()

    has_loc = df["_loc_key"] != ""
    df_with_loc = df[has_loc].copy()
    df_no_loc   = df[~has_loc].copy()

    kept_rows = []
    for _, group in df_with_loc.groupby(["_start_date", "_loc_key"]):
        kept_rows.append(preferred_source(group))

    if kept_rows:
        df_deduped = pd.DataFrame(kept_rows)
    else:
        df_deduped = pd.DataFrame(columns=df.columns)

    result = pd.concat([df_deduped, df_no_loc], ignore_index=True)
    result = result.drop(columns=["_start_date", "_loc_key"])
    return result


# ---------------------------------------------------------------------------
# Step 5: Merge recurring events
# ---------------------------------------------------------------------------

def is_recurring(dates: list) -> bool:
    """Return True if dates follow a consistent weekly or monthly pattern."""
    if len(dates) < 2:
        return False
    sorted_dates = sorted(dates)
    gaps = [(sorted_dates[i+1] - sorted_dates[i]).days
            for i in range(len(sorted_dates)-1)]
    avg_gap = sum(gaps) / len(gaps)
    all_similar = all(abs(g - avg_gap) <= 3 for g in gaps)
    return all_similar and (6 <= avg_gap <= 32)


def merge_recurring(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse recurring events (same title + same clean_location, weekly/monthly)
    into a single row. Appends date list to description.
    If location varies across occurrences, keeps them as separate rows.
    """
    df["_start_dt"]  = pd.to_datetime(df["start"], errors="coerce", format="mixed")
    df["_loc_key_r"] = df["clean_location"].fillna("").str.strip().str.lower()

    result_rows = []
    for (title, loc_key), group in df.groupby(["title", "_loc_key_r"]):
        if len(group) == 1:
            result_rows.append(group.iloc[0].drop(["_start_dt", "_loc_key_r"]))
            continue

        dates = [d.date() for d in group["_start_dt"].dropna()]

        if not is_recurring(dates):
            for _, row in group.iterrows():
                result_rows.append(row.drop(["_start_dt", "_loc_key_r"]))
            continue

        # Merge into one row
        base = group.sort_values("_start_dt").iloc[0].copy()
        date_strs = ", ".join(
            d.strftime("%d %b %Y").lstrip("0") for d in sorted(dates)
        )
        base["title"]       = base["title"] + " (RECURRING)"
        base["description"] = str(base["description"]).rstrip() + \
                              f" [RECURRING — also on: {date_strs}]"
        result_rows.append(base.drop(["_start_dt", "_loc_key_r"]))

    return pd.DataFrame(result_rows).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Geocode result validation (used by Step 6)
# ---------------------------------------------------------------------------

# Rough bounding boxes for US states + DC, used to verify that a carried-over
# geocode result actually lies inside the state named in its address.
US_STATE_BBOX = {
    "AL": (30.1, 35.1, -88.6, -84.8), "AK": (51.0, 71.5, -180.0, -129.0),
    "AZ": (31.2, 37.1, -114.9, -109.0), "AR": (32.9, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.5, -114.0), "CO": (36.9, 41.1, -109.1, -101.9),
    "CT": (40.9, 42.1, -73.8, -71.7), "DE": (38.4, 39.9, -75.8, -75.0),
    "DC": (38.7, 39.0, -77.2, -76.9), "FL": (24.4, 31.1, -87.7, -79.9),
    "GA": (30.2, 35.1, -85.7, -80.7), "HI": (18.8, 22.3, -160.3, -154.7),
    "ID": (41.9, 49.1, -117.3, -111.0), "IL": (36.9, 42.6, -91.6, -87.4),
    "IN": (37.7, 41.9, -88.2, -84.7), "IA": (40.3, 43.6, -96.7, -90.1),
    "KS": (36.9, 40.1, -102.1, -94.5), "KY": (36.4, 39.2, -89.7, -81.9),
    "LA": (28.8, 33.1, -94.1, -88.7), "ME": (43.0, 47.6, -71.2, -66.8),
    "MD": (37.8, 39.8, -79.6, -75.0), "MA": (41.1, 42.9, -73.6, -69.8),
    "MI": (41.6, 48.4, -90.5, -82.3), "MN": (43.4, 49.5, -97.3, -89.4),
    "MS": (30.1, 35.1, -91.8, -87.9), "MO": (35.9, 40.7, -95.9, -89.0),
    "MT": (44.3, 49.1, -116.2, -103.9), "NE": (39.9, 43.1, -104.1, -95.2),
    "NV": (34.9, 42.1, -120.1, -113.9), "NH": (42.6, 45.4, -72.7, -70.5),
    "NJ": (38.8, 41.4, -75.7, -73.8), "NM": (31.2, 37.1, -109.2, -102.9),
    "NY": (40.4, 45.1, -79.9, -71.7), "NC": (33.7, 36.7, -84.4, -75.4),
    "ND": (45.8, 49.1, -104.1, -96.5), "OH": (38.3, 42.1, -84.9, -80.4),
    "OK": (33.5, 37.1, -103.1, -94.3), "OR": (41.9, 46.4, -124.7, -116.4),
    "PA": (39.6, 42.4, -80.6, -74.6), "RI": (41.0, 42.1, -71.9, -71.0),
    "SC": (32.0, 35.3, -83.4, -78.4), "SD": (42.4, 45.9, -104.1, -96.3),
    "TN": (34.9, 36.8, -90.4, -81.5), "TX": (25.7, 36.6, -106.7, -93.4),
    "UT": (36.9, 42.1, -114.1, -108.9), "VT": (42.6, 45.1, -73.5, -71.4),
    "VA": (36.4, 39.5, -83.7, -75.1), "WA": (45.4, 49.1, -124.9, -116.8),
    "WV": (37.1, 40.7, -82.7, -77.6), "WI": (42.4, 47.1, -92.9, -86.7),
    "WY": (40.9, 45.1, -111.1, -103.9),
}

_STATE_FROM_ADDR_RE = re.compile(r",\s*([A-Z]{2})(?:[\s,]|$)")
_US_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")

# Map from source name → home state(s) and US state adjacency, used together
# to validate that a baronial event geocoded into a sensible region. Kept
# in sync with geocode_sca_events.py's tables; consider extracting these to
# a shared module if more files need them.
_BARONY_HOME_STATES = {
    "Barony of Lochmere": {"MD"}, "Barony of Bright Hills": {"MD"},
    "Barony of Storvik": {"MD"}, "Barony of Dun Carraig": {"MD"},
    "Barony of Highland Foorde": {"MD"},
    "Barony of Ponte Alto": {"VA"}, "Barony of Stierbach": {"VA"},
    "Barony of Stierbach (Workshops)": {"VA"}, "Barony of Caer Mear": {"VA"},
    "Barony of Marinus": {"VA"}, "Barony of Tir-y-Don": {"VA"},
    "Barony of Black Diamond": {"VA"},
    "Barony of Windmasters' Hill": {"NC"}, "Barony of Raven's Cove": {"NC"},
    "Barony of Hawkwood": {"NC"}, "Barony of Sacred Stone": {"NC"},
    "Barony of Nottinghill Coill": {"SC"}, "Barony of Hidden Mountain": {"SC"},
    "Shire of Aukesgate": {"NC"}, "Shire of Stormwall": {"NC"},
}

_US_STATE_ADJACENT = {
    "AL": {"FL","GA","MS","TN"}, "AR": {"LA","MO","MS","OK","TN","TX"},
    "AZ": {"CA","CO","NM","NV","UT"}, "CA": {"AZ","NV","OR"},
    "CO": {"AZ","KS","NE","NM","OK","UT","WY"}, "CT": {"MA","NY","RI"},
    "DC": {"MD","VA"}, "DE": {"MD","NJ","PA"}, "FL": {"AL","GA"},
    "GA": {"AL","FL","NC","SC","TN"}, "IA": {"IL","MN","MO","NE","SD","WI"},
    "ID": {"MT","NV","OR","UT","WA","WY"}, "IL": {"IA","IN","KY","MO","WI"},
    "IN": {"IL","KY","MI","OH"}, "KS": {"CO","MO","NE","OK"},
    "KY": {"IL","IN","MO","OH","TN","VA","WV"}, "LA": {"AR","MS","TX"},
    "MA": {"CT","NH","NY","RI","VT"}, "MD": {"DC","DE","PA","VA","WV"},
    "ME": {"NH"}, "MI": {"IN","OH","WI"}, "MN": {"IA","ND","SD","WI"},
    "MO": {"AR","IA","IL","KS","KY","NE","OK","TN"}, "MS": {"AL","AR","LA","TN"},
    "MT": {"ID","ND","SD","WY"}, "NC": {"GA","SC","TN","VA"},
    "ND": {"MN","MT","SD"}, "NE": {"CO","IA","KS","MO","SD","WY"},
    "NH": {"MA","ME","VT"}, "NJ": {"DE","NY","PA"},
    "NM": {"AZ","CO","OK","TX","UT"}, "NV": {"AZ","CA","ID","OR","UT"},
    "NY": {"CT","MA","NJ","PA","VT"}, "OH": {"IN","KY","MI","PA","WV"},
    "OK": {"AR","CO","KS","MO","NM","TX"}, "OR": {"CA","ID","NV","WA"},
    "PA": {"DE","MD","NJ","NY","OH","WV"}, "RI": {"CT","MA"},
    "SC": {"GA","NC"}, "SD": {"IA","MN","MT","ND","NE","WY"},
    "TN": {"AL","AR","GA","KY","MO","MS","NC","VA"}, "TX": {"AR","LA","NM","OK"},
    "UT": {"AZ","CO","ID","NM","NV","WY"}, "VA": {"DC","KY","MD","NC","TN","WV"},
    "VT": {"MA","NH","NY"}, "WA": {"ID","OR"}, "WI": {"IA","IL","MI","MN"},
    "WV": {"KY","MD","OH","PA","VA"}, "WY": {"CO","ID","MT","NE","SD","UT"},
}


def _acceptable_states(source: str):
    home = _BARONY_HOME_STATES.get(source)
    if not home:
        return None
    acceptable = set(home)
    for s in home:
        acceptable |= _US_STATE_ADJACENT.get(s, set())
    return acceptable


def _coord_state(lat: float, lng: float):
    for code, (lat_min, lat_max, lng_min, lng_max) in US_STATE_BBOX.items():
        if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
            return code
    return None


def _invalidate_misgeocoded_rows(df: pd.DataFrame) -> int:
    """
    Blank carried-over coords and set geocode_status="failed" for rows that
    fail either of these checks:
      - Address names a US state but coords are outside its bounding box
      - Source is a tracked barony but coords are in a non-adjacent state

    Returns the number of rows invalidated.
    """
    invalidated = 0
    for idx, row in df.iterrows():
        lat_s = str(row.get("lat", ""))
        lng_s = str(row.get("lng", ""))
        loc   = str(row.get("clean_location", ""))
        source = str(row.get("source", ""))
        if not lat_s or not lng_s:
            continue
        try:
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            continue

        bad = False

        # Check 1: address-state vs result-state (US addresses only)
        if loc and _US_ZIP_RE.search(loc):
            m = _STATE_FROM_ADDR_RE.search(loc)
            if m:
                state = m.group(1).upper()
                box = US_STATE_BBOX.get(state)
                if box and not (box[0] <= lat <= box[1] and box[2] <= lng <= box[3]):
                    bad = True

        # Check 2: baronial source vs result-state
        if not bad:
            acceptable = _acceptable_states(source)
            if acceptable:
                actual = _coord_state(lat, lng)
                if actual and actual not in acceptable:
                    bad = True

        if bad:
            df.at[idx, "lat"] = ""
            df.at[idx, "lng"] = ""
            df.at[idx, "geocode_status"] = "failed"
            invalidated += 1
    return invalidated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Reading {INPUT_FILE.name} ...")
    df = pd.read_csv(INPUT_FILE, dtype=str)
    print(f"  {len(df)} rows loaded\n")

    # Step 0: Unescape remaining ICS sequences
    print("Step 0: Unescaping ICS sequences ...")
    for col in ["title", "location", "description"]:
        df[col] = df[col].apply(unescape_ics)

    # Step 1: Remove non-events, cancelled events, and past events
    print("Step 1: Removing non-events, cancelled events, and past events ...")
    before = len(df)
    df = df[~df["title"].apply(is_non_event)]
    df = df[~df["title"].apply(is_cancelled)]
    removed_filter = before - len(df)

    # Drop events whose END date is in the past. Fall back to start date if
    # no end. Use format="mixed" because the column mixes dates and datetimes.
    today = pd.Timestamp.today().normalize()
    end_parsed   = pd.to_datetime(df["end"],   errors="coerce", format="mixed")
    start_parsed = pd.to_datetime(df["start"], errors="coerce", format="mixed")
    effective_end = end_parsed.fillna(start_parsed)
    # Keep rows where effective_end is in the future OR couldn't be parsed
    keep = effective_end.isna() | (effective_end >= today)
    before_past = len(df)
    df = df[keep]
    removed_past = before_past - len(df)
    print(f"  Removed {removed_filter} non-events/cancelled, "
          f"{removed_past} past events. {len(df)} remain.\n")

    # Step 2: Clean descriptions and extract event/Facebook URLs
    print("Step 2: Cleaning descriptions and extracting URLs ...")
    cleaned_descs = df["description"].apply(clean_description)
    df["description"]  = cleaned_descs.apply(lambda x: x[0])
    df["event_url"]    = cleaned_descs.apply(lambda x: x[1].get("event_url") or "")
    df["facebook_url"] = cleaned_descs.apply(lambda x: x[1].get("facebook_url") or "")
    blanked = (df["description"] == "").sum()
    with_event_url = (df["event_url"] != "").sum()
    print(f"  {blanked} descriptions blanked (placeholder/widget/empty). "
          f"{with_event_url} event URLs extracted.\n")

    # Step 3: Clean locations; fall back to description extraction if empty
    print("Step 3: Cleaning locations ...")
    cleaned = df["location"].apply(clean_location)
    df["clean_location"]     = cleaned.apply(lambda x: x[0])
    df["address_confidence"] = cleaned.apply(lambda x: x[1])

    # For rows with no address, try extracting an address from description.
    # Description extraction takes precedence over the geographic fallback so
    # that "Location: Mandt Center, 400 Mandt Parkway, Stoughton, WI 53589"
    # buried in a Caid event description beats the "kingdom of Northshield →
    # Minnesota" coarse fallback.
    empty_mask = df["address_confidence"] == "empty"
    extracted = df.loc[empty_mask, "description"].apply(extract_location_from_description)
    df.loc[empty_mask, "clean_location"]     = extracted.apply(lambda x: x[0])
    df.loc[empty_mask, "address_confidence"] = extracted.apply(lambda x: x[1])
    pulled_from_desc = (df.loc[empty_mask, "address_confidence"] != "empty").sum()
    print(f"  recovered {pulled_from_desc} addresses from descriptions")

    # If the title ends in "(GroupName)" OR the description has
    # "Hosted by GROUP", look the group up in group_locations.csv. Common
    # patterns: Caid "Academia (Nordwache)", Meridies "Hosted by Barony of
    # South Downs". Falls back to the group's region centroid.
    still_empty = df["address_confidence"] == "empty"
    via_group = df.loc[still_empty].apply(
        lambda r: lookup_group_location(r["title"], r["description"]), axis=1)
    if len(via_group):
        df.loc[still_empty, "clean_location"]     = via_group.apply(lambda x: x[0])
        df.loc[still_empty, "address_confidence"] = via_group.apply(lambda x: x[1])
    pulled_from_group = (df.loc[still_empty, "address_confidence"] != "empty").sum()
    print(f"  recovered {pulled_from_group} addresses from group_locations.csv")

    # Last-resort fallback: bare state codes ("LA") and bare kingdom names
    # ("Kingdom of Northshield") get expanded to a state/region marker.
    still_empty = df["address_confidence"] == "empty"
    fallback = df.loc[still_empty, "location"].apply(apply_geographic_fallback)
    df.loc[still_empty, "clean_location"]     = fallback.apply(lambda x: x[0])
    df.loc[still_empty, "address_confidence"] = fallback.apply(lambda x: x[1])
    pulled_from_fallback = (df.loc[still_empty, "address_confidence"] != "empty").sum()
    print(f"  applied {pulled_from_fallback} state/kingdom-level fallbacks")

    conf_counts = df["address_confidence"].value_counts()
    print(f"  high confidence:  {conf_counts.get('high', 0)}")
    print(f"  low confidence:   {conf_counts.get('low', 0)}")
    print(f"  empty:            {conf_counts.get('empty', 0)}\n")

    # Step 4: Deduplicate (OOK-aware, geography-aware)
    print("Step 4: Deduplicating (same date + location, OOK-aware) ...")
    ook_count = df["title"].apply(is_out_of_kingdom).sum()
    print(f"  {ook_count} events marked OUT OF KINGDOM (will lose priority to duplicates)")
    before = len(df)
    df = deduplicate(df)
    print(f"  Removed {before - len(df)} duplicates. {len(df)} remain.\n")

    # Step 5: Merge recurring events
    print("Step 5: Merging recurring events ...")
    before = len(df)
    df = merge_recurring(df)
    after = len(df)
    print(f"  Collapsed {before - after} rows into recurring entries. {after} remain.\n")

    # Step 6: Preserve any existing geocoding results from a prior run.
    # Match by (title, start, clean_location) — stable across re-cleans as long
    # as cleaning logic doesn't change the clean_location for those rows.
    # Also validate that the carried-over coords are in the right state — if
    # not, drop them so they get re-geocoded with the improved validator.
    if OUTPUT_FILE.exists():
        print("Step 6: Preserving prior geocoding results ...")
        try:
            prior = pd.read_csv(OUTPUT_FILE, dtype=str).fillna("")
            geocoded_cols = [c for c in ("lat", "lng", "geocode_status") if c in prior.columns]
            if geocoded_cols:
                key_cols = ["title", "start", "clean_location"]
                prior_keyed = prior[key_cols + geocoded_cols].drop_duplicates(subset=key_cols)
                merged = df.merge(prior_keyed, on=key_cols, how="left")
                for col in geocoded_cols:
                    df[col] = merged[col].fillna("")

                # Validate carried-over coords against the address's state
                invalidated = _invalidate_misgeocoded_rows(df)
                carried = (df["geocode_status"].isin(["ok", "ok_retry", "ok_photon"])).sum()
                print(f"  Carried over {carried} prior geocoding results, "
                      f"invalidated {invalidated} that were in the wrong state.\n")
            else:
                print("  No prior geocoding columns found.\n")
        except Exception as e:
            print(f"  Could not merge prior results: {e}\n")

    # Reorder columns
    col_order = [
        "title", "start", "end", "location", "clean_location",
        "address_confidence", "description", "event_url", "facebook_url",
        "source", "calendar_type", "is_virtual",
        "lat", "lng", "geocode_status",
    ]
    df = df[[c for c in col_order if c in df.columns]]

    # Save
    df.to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
    print(f"Saved {len(df)} cleaned events to '{OUTPUT_FILE.name}'")
    print(f"\nSummary:")
    print(f"  Non-virtual (mappable): {(df['is_virtual'] == 'False').sum()}")
    print(f"  Virtual:                {(df['is_virtual'] == 'True').sum()}")
    print(f"  High-confidence addresses: {(df['address_confidence'] == 'high').sum()}")
    print(f"  Low-confidence addresses:  {(df['address_confidence'] == 'low').sum()}")
    print(f"  No address:                {(df['address_confidence'] == 'empty').sum()}")


if __name__ == "__main__":
    main()
