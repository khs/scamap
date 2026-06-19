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
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

import kingdoms


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR   = Path(__file__).parent
INPUT_FILE   = SCRIPT_DIR / "sca_events.csv"
OUTPUT_FILE  = SCRIPT_DIR / "sca_events_clean.csv"
# Written by ImportMaps.py: sources whose fetch failed this run (see Step 6).
FETCH_FAILURES_FILE = SCRIPT_DIR / "fetch_failures.json"
# Hand-maintained corrections applied every run (see EDITING_EVENTS.md and the
# apply_event_overrides step). Lets a human fix a vague/wrong event location
# permanently, keyed on a stable identifier so it survives upstream edits.
OVERRIDES_FILE = SCRIPT_DIR / "event_overrides.csv"

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


# Lochac's calendar prefixes every event description with an autocrat contact
# block — "Steward:<name> Email:<addr> Website:<url>" — where only the steward
# name varies and Email/Website are usually blank or a redacted tail (".au").
# It's noise on the map, so strip the leading block and keep the real text.
_STEWARD_BOILERPLATE_RE = re.compile(
    r"^\s*Steward:.*?\bEmail:\S*\s*(?:Website:\S*\s*)?",
    re.IGNORECASE,
)


def strip_steward_boilerplate(text: str) -> str:
    """Remove a leading 'Steward:… Email:… [Website:…]' block (Lochac feed)."""
    if not isinstance(text, str) or not text:
        return text
    return _STEWARD_BOILERPLATE_RE.sub("", text, count=1).strip()


# AEthelmearc's calendar prepends one or more "Additional Notes on <topic>:
# <text>" segments to event descriptions (Pet Policy, Flames, Alcohol Policy,
# Weapons, …). They're logistics boilerplate from a calendar-plugin form, not
# part of the actual event description, so strip them.
_AENOTE_HEADER_RE = re.compile(r"\bAdditional Notes on [^:]+:", re.IGNORECASE)
# Phrases that mark the start of the REAL event description right after the
# boilerplate. Anchored to a sentence boundary so "welcome" inside note text
# ("service dogs are welcome.") can't be mistaken for the sentinel "Welcome".
_AENOTE_SENTINELS = (
    # Strong openers — formal/archaic phrases unlikely to appear naturally in
    # the body of a real description. "Welcome", "Join us", "Please join" are
    # NOT here: they false-positive deep in real descriptions ("...invites you
    # to join us in preparation!" matched mid-paragraph for Myrkfaelinn).
    r"Royal Progress:", r"Hark[!,]", r"Lordes\b", r"Lords and Ladies",
    r"Greetings", r"Unto\s", r"Come one", r"Come all",
    r"Announcing", r"All are cordially invited",
    r"The (?:Barony|Shire|Canton|Stronghold|College|Province|Principality|Riding) of",
)
_AENOTE_SENTINEL_RE = re.compile(
    r"(?:(?<=\.\s)|\A)(?:" + "|".join(_AENOTE_SENTINELS) + r")",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"\.\s+(?=[A-Z][a-z])")


def _ae_block_end(text: str, last_header_pos: int) -> int:
    """Find where an AN block (ending at last_header_pos) terminates.

    A known sentinel (Royal Progress:, Hark!, Lordes, Greetings, Unto, etc.)
    always wins when it matches — it's the strongest signal of the real desc
    starting. Otherwise pick the EARLIEST of: a ". <Capital>" sentence end, a
    lowercase+Capital run-on (when the feed drops the last note's period), or
    a section marker (" — Directions:" etc.)."""
    after_colon = text.find(":", last_header_pos) + 1
    sent = _AENOTE_SENTINEL_RE.search(text, after_colon)
    if sent:
        return sent.start()
    candidates = []
    sent_end = _SENTENCE_END_RE.search(text, after_colon)
    if sent_end: candidates.append(sent_end.end())
    runon = re.search(r"(?<=[a-z])\s+(?=[A-Z][a-z]{2,})", text[after_colon:])
    if runon: candidates.append(after_colon + runon.end())
    section = re.search(r"\s+[—-]\s+(?=[A-Z][a-z]+:)", text[after_colon:])
    if section: candidates.append(after_colon + section.start())
    return min(candidates) if candidates else len(text)


def strip_aethelmearc_notes(text: str) -> str:
    """Strip every 'Additional Notes on …' boilerplate block (AEthelmearc).

    Each block is a run of consecutive notes (next header within ~400 chars).
    The block's end is the earliest of: a known sentinel (Royal Progress:,
    Hark!, Lordes, Greetings, Unto, Announcing, "The Barony of …"), a period +
    capital-word sentence boundary, a lowercase+Capital run-on (some feeds
    drop the last note's terminating period), or a section marker ("— Directions:").
    Handles leading boilerplate, "Hosted by: <group>" prefix + boilerplate,
    AND inline AN blocks deep in the description (Road to Rouen has both).
    """
    if not isinstance(text, str) or not text:
        return text
    headers = [m.start() for m in _AENOTE_HEADER_RE.finditer(text)]
    if not headers:
        return text
    # Group into blocks: consecutive headers within ~400 chars belong together;
    # a wider gap means the next note is a separate inline block.
    blocks = [[headers[0]]]
    for h in headers[1:]:
        if h - blocks[-1][-1] <= 400:
            blocks[-1].append(h)
        else:
            blocks.append([h])
    cuts = [(b[0], _ae_block_end(text, b[-1])) for b in blocks]
    # Apply cuts in reverse so earlier positions stay valid.
    result = text
    for start, end in reversed(cuts):
        result = result[:start] + " " + result[end:]
    return re.sub(r"\s{2,}", " ", result).strip()


# Group-type prefix at the start of a title — strongly indicates a baronial-host
# event ("Barony of Tarnmists Business Meeting (Virtual)").
_BARONIAL_PREFIX_RE = re.compile(
    r"^(Barony|Shire|Canton|Stronghold|College|Province|Principality|Riding|Hamlet)\s+of\s+",
    re.IGNORECASE,
)
# Keyword hits for clearly-baronial recurring meetings/practices whose title
# doesn't carry the group prefix: "Ravenshore Business Meeting", "Fettburg
# Baronial Meeting", "Skrael and Friends A&S Evenings". These are the regular
# local activities (sword practice, A&S nights, business/officer meetings) that
# belong under the "Baronial" filter rather than "Kingdom (Events)".
_BARONIAL_KEYWORD_RE = re.compile(
    r"\b(Business Meeting|Baronial Meeting|Baronial|"
    r"A&S Evenings?|A&S Nights?|Sewing Circle|Drumming Circle|Bardic Circle|"
    r"Choir Practice|Dance Practice|Pell Night|Fighter Practice|Fencing Practice|"
    r"Archery Practice)\b",
    re.IGNORECASE,
)
# "Officer/Council/Populace Meeting" deliberately NOT here -- they can be kingdom-
# level too (e.g. "K. Officer Meeting", "MoD Council meeting"). The keywords above
# are tight enough that a kingdom-level event won't be caught.


def _norm_name(s: str) -> str:
    """Lowercase + diacritic-strip + alphanumeric-only — for loose name matches
    so "Aarnimetsä"/"Winter's Gate"/etc. line up across spellings."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _load_groups_by_kingdom(group_locations_path: Path) -> dict:
    """{kingdom: set(normalized short-names without 'Barony of'/etc. prefix)}."""
    out: dict = {}
    if not group_locations_path.exists():
        return out
    with open(group_locations_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            bare = _BARONIAL_PREFIX_RE.sub("", row.get("group", "") or "").strip()
            bn = _norm_name(bare)
            if len(bn) >= 4:
                out.setdefault(row.get("kingdom", ""), set()).add(bn)
    return out


def promote_virtual_baronials(df, group_locations_path: Path) -> int:
    """Reclassify virtual events from kingdom feeds to calendar_type=baronial
    when their title clearly references a baronial group. The West & Ealdormere
    kingdom feeds aggregate baronial business meetings that would otherwise be
    filed as 'kingdom' — so the "Baronial" filter on the map should govern them.

    A title qualifies if it (a) starts with a group-type prefix
    ("Barony of …"/"Shire of …"), (b) contains a Business-Meeting / Baronial
    keyword, or (c) matches a known group short-name from the same kingdom
    (loose: diacritic- and spacing-insensitive). Returns count promoted.
    """
    groups_by_kingdom = _load_groups_by_kingdom(group_locations_path)
    is_kingdom_virtual = (
        (df["calendar_type"] == "kingdom")
        & (df["is_virtual"].astype(str) == "True")
    )
    if not is_kingdom_virtual.any():
        return 0

    def is_baronial(row) -> bool:
        title = row.get("title") or ""
        if _BARONIAL_PREFIX_RE.match(title):
            return True
        if _BARONIAL_KEYWORD_RE.search(title):
            return True
        norm_title = _norm_name(title)
        return any(sn in norm_title for sn in groups_by_kingdom.get(row.get("source", ""), ()))

    mask = is_kingdom_virtual & df.apply(is_baronial, axis=1)
    n = int(mask.sum())
    if n:
        df.loc[mask, "calendar_type"] = "baronial"
    return n


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
US_STATE_BBOX = kingdoms.STATE_BBOX

_STATE_FROM_ADDR_RE = re.compile(r",\s*([A-Z]{2})(?:[\s,]|$)")
_US_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")

# Map from source name → home state(s) and US state adjacency, used together
# to validate that a baronial event geocoded into a sensible region. Kept
# in sync with geocode_sca_events.py's tables; consider extracting these to
# a shared module if more files need them.
_BARONY_HOME_STATES = kingdoms.BARONY_HOME_STATES

_US_STATE_ADJACENT = kingdoms.US_STATE_ADJACENT


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


def _load_fetch_failures() -> set:
    """Sources whose fetch failed this run, per ImportMaps' fetch_failures.json."""
    try:
        return set(json.loads(FETCH_FAILURES_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def carry_forward_failed_sources(df: pd.DataFrame) -> pd.DataFrame:
    """Re-attach the last-good FUTURE events for any source that failed to fetch
    this run, so a transient outage / WAF block doesn't wipe a kingdom off the
    map until the next good run.

    Only still-upcoming events are carried (anything already in the past is left
    to drop), so a source that stays down simply fades out as its events pass,
    rather than freezing a stale snapshot forever. Carried rows keep the prior
    run's coordinates and geocode_status, so they aren't re-geocoded.
    """
    failed = _load_fetch_failures()
    if not failed or not OUTPUT_FILE.exists():
        return df
    try:
        prior = pd.read_csv(OUTPUT_FILE, dtype=str).fillna("")
    except Exception as e:
        print(f"  Could not read prior events for carry-forward: {e}")
        return df
    if prior.empty or "source" not in prior.columns:
        return df

    today   = datetime.now().strftime("%Y-%m-%d")
    present = set(zip(df.get("source", []), df.get("title", []), df.get("start", [])))
    keep = []
    for _, row in prior.iterrows():
        if row.get("source") not in failed:
            continue
        if str(row.get("start", ""))[:10] < today:          # already past
            continue
        if (row.get("source"), row.get("title"), row.get("start")) in present:
            continue                                          # already present
        keep.append(row)

    if not keep:
        print(f"  Carry-forward: {len(failed)} source(s) failed, no future "
              f"last-good events to restore.")
        return df

    carried = pd.DataFrame(keep).reindex(columns=df.columns, fill_value="")
    by_src  = carried["source"].value_counts().to_dict()
    note    = ", ".join(f"{s} ({n})" for s, n in sorted(by_src.items()))
    print(f"  Carry-forward: restored {len(carried)} last-good event(s) for "
          f"failed source(s): {note}")
    return pd.concat([df, carried], ignore_index=True)


# ---------------------------------------------------------------------------
# Hand-maintained event overrides (event_overrides.csv)
# ---------------------------------------------------------------------------
# The pipeline rebuilds every event from the upstream feeds on each run, so a
# manual edit to sca_events_clean.csv would be wiped the next run. This applies
# a committed, human-edited correction file INSTEAD, every run, so a fix to a
# vague or wrong event location is permanent. Full handoff docs in
# EDITING_EVENTS.md; the format is also described in event_overrides.csv itself.
#
# Each override row carries MATCH fields (how to find the event) and NEW fields
# (what to change). Matching is deliberately location-INDEPENDENT — the whole
# point is the location is wrong — and tolerant of minor upstream edits:
#   - match_event_url : the event's permalink. Most stable; survives title,
#                       date, and location edits. Preferred when the event
#                       has a URL.
#   - match_source + match_title [+ match_date] : for events with no URL.
#                       Title is compared normalized (case/punctuation-
#                       insensitive) so small wording tweaks still match;
#                       match_date (YYYY-MM-DD) is optional and pins one
#                       instance of an annually-repeating title.

OVERRIDE_COLUMNS = [
    "match_event_url", "match_source", "match_title", "match_date",
    "new_location", "new_lat", "new_lng", "note",
]


def _load_overrides() -> list[dict]:
    """Read event_overrides.csv. Skips blank rows, `#` comment rows, and any
    row with no usable match key (so an empty row can't match every event)."""
    if not OVERRIDES_FILE.exists():
        return []
    out = []
    try:
        with open(OVERRIDES_FILE, encoding="utf-8", newline="") as f:
            for raw in csv.DictReader(f):
                row = {k: (raw.get(k) or "").strip() for k in OVERRIDE_COLUMNS}
                if row["match_event_url"].startswith("#"):
                    continue                                    # comment line
                if not (row["match_event_url"] or row["match_source"]
                        or row["match_title"]):
                    continue                                    # no match key
                out.append(row)
    except Exception as e:
        print(f"  WARNING: could not read {OVERRIDES_FILE.name}: {e}")
    return out


def _valid_override_coords(lat_s: str, lng_s: str):
    """Return (lat_str, lng_str) if BOTH are present, numeric, and in valid
    ranges (lat -90..90, lng -180..180); else None. A typo'd or half-filled
    pin is rejected here rather than placed somewhere wrong on the map."""
    if not (lat_s and lng_s):
        return None
    try:
        lat, lng = float(lat_s), float(lng_s)
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    return (lat_s, lng_s)


def _override_matches(ov: dict, row) -> bool:
    """True if override `ov` applies to event `row`. URL match wins outright;
    otherwise ALL of the provided source/title/date fields must match."""
    if ov["match_event_url"]:
        return str(row.get("event_url", "")).strip() == ov["match_event_url"]
    if ov["match_source"] and str(row.get("source", "")).strip() != ov["match_source"]:
        return False
    if ov["match_title"] and (_normalize_title_for_match(row.get("title", ""))
                              != _normalize_title_for_match(ov["match_title"])):
        return False
    if ov["match_date"] and str(row.get("start", ""))[:10] != ov["match_date"]:
        return False
    return True


def apply_event_overrides(df: pd.DataFrame) -> pd.DataFrame:
    """Apply each correction in event_overrides.csv to every matching event.

    new_location  -> replaces the displayed location AND the geocoder input,
                     and clears any coords so the corrected address is geocoded
                     fresh (unless new_lat/new_lng are also given).
    new_lat+new_lng -> pins the event to exact coordinates and marks it
                     geocode_status="override" so the geocoder leaves it alone.
    Both may be given: corrected text plus an exact pin (use this when even the
    fixed address won't geocode cleanly).
    """
    overrides = _load_overrides()
    if not overrides:
        return df
    applied = 0
    for ov in overrides:
        label = (ov["note"] or ov["match_title"] or ov["match_event_url"]
                 or ov["match_source"])
        try:
            new_loc = ov["new_location"]
            lat_s, lng_s = ov["new_lat"], ov["new_lng"]
            coords = _valid_override_coords(lat_s, lng_s)

            # Sanity checks BEFORE matching, so the warning fires even if the
            # match is wrong too.
            if (lat_s or lng_s) and coords is None:
                print(f"  WARNING: override '{label}' has invalid or incomplete "
                      f"coordinates (lat={lat_s!r}, lng={lng_s!r}) — ignoring the pin")
            if not new_loc and coords is None:
                print(f"  WARNING: override '{label}' changes nothing "
                      f"(no new_location, no valid pin) — skipping")
                continue

            mask = df.apply(lambda r: _override_matches(ov, r), axis=1)
            n = int(mask.sum())
            if n == 0:
                print(f"  Override matched nothing (check the match fields): {label}")
                continue

            for idx in df[mask].index:
                if new_loc:
                    df.at[idx, "location"]           = new_loc
                    df.at[idx, "clean_location"]     = new_loc
                    df.at[idx, "address_confidence"] = "high"
                    if coords is None:            # corrected address -> re-geocode
                        df.at[idx, "lat"] = ""
                        df.at[idx, "lng"] = ""
                        df.at[idx, "geocode_status"] = ""
                if coords is not None:            # exact pin -> skip the geocoder
                    df.at[idx, "lat"]            = coords[0]
                    df.at[idx, "lng"]            = coords[1]
                    df.at[idx, "geocode_status"] = "override"
            applied += n
            print(f"  Override applied to {n} event(s): {label}")
        except Exception as e:                    # one bad row can't break cleaning
            print(f"  WARNING: override '{label}' failed to apply: {e}")
    if applied:
        print(f"  Applied {applied} event change(s) from {OVERRIDES_FILE.name}.")
    return df


# ---------------------------------------------------------------------------
# Per-kingdom URL backfill from their public REST APIs
# ---------------------------------------------------------------------------

def _normalize_title_for_match(title: str) -> str:
    """Lowercase + strip HTML entities + reduce punctuation to single dashes.
    Used to match titles from our ICS feed against titles on the kingdom's
    own website, where curly quotes, &#038; ampersands, etc. don't match."""
    import html, re
    s = html.unescape(str(title or "")).lower()
    # Collapse anything non-alphanumeric (including curly quotes, hyphens,
    # asterisks, ellipses) down to single spaces, then squeeze spaces.
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


# Kingdoms whose own WordPress sites expose Tribe Events Calendar's REST
# endpoint. For each one, we fetch every published event and match it
# against our cleaned data by normalized title — recovering the per-event
# WordPress URL that the upstream Google-Calendar import strips out.
# Probed and confirmed working as of May 2026.
KINGDOMS_WITH_TRIBE_API = {
    "Kingdom of AEthelmearc":   "https://aethelmearc.org",
    "Kingdom of Atenveldt":     "https://atenveldt.org",
    "Kingdom of Gleann Abhann": "https://gleannabhann.net",
    "Kingdom of the Outlands":  "https://www.outlands.org",
    "Kingdom of Trimaris":      "https://trimaris.org",
    # The West is itself a tribe-rest source, so events carry an ICS URL: —
    # but ImportMaps drops that property, so we re-fetch the per-event
    # permalink here just like the Google-Calendar kingdoms above. Without
    # this, West "Save the Date" entries (which have no URL in their body)
    # fall back to linking the whole kingdom calendar.
    "Kingdom of the West":      "https://westkingdom.org",
}


def _fetch_tribe_events(base_url: str) -> dict:
    """Return {normalized_title: url} for every published Tribe Events
    record on `base_url`. Walks pages until exhausted. Returns {} on error.

    We pass an explicit wide date window: the Tribe REST API otherwise
    defaults to a short upcoming window (~the next handful of events), which
    misses far-future "Save the Date" entries — exactly the West rows that
    most need a real per-event URL. Window spans recent past (to catch events
    still in our data) through a few years out."""
    import requests
    from datetime import date, timedelta
    today = date.today()
    win_start = (today - timedelta(days=90)).isoformat()
    win_end   = (today + timedelta(days=1095)).isoformat()
    lookup: dict[str, str] = {}
    page = 1
    while True:
        try:
            r = requests.get(
                f"{base_url}/wp-json/tribe/events/v1/events",
                params={"per_page": 50, "page": page, "status": "publish",
                        "start_date": win_start, "end_date": win_end},
                timeout=45,
                headers={"User-Agent": "SCA Maps Project (URL backfill)"},
            )
            if r.status_code != 200:
                break
            data = r.json()
        except Exception as exc:
            print(f"    WARNING: {base_url} page {page} failed: {exc}")
            break
        events = data.get("events", []) or []
        for ev in events:
            norm = _normalize_title_for_match(ev.get("title", ""))
            url = ev.get("url", "")
            if norm and url and norm not in lookup:
                lookup[norm] = url
        # The Tribe REST API caps per_page at 50 and reports total_pages; walk
        # every page. (The old `len < 100` check stopped after page 1, so all
        # far-future events — e.g. next year's Save-the-Dates — were missed.)
        total_pages = data.get("total_pages") or 0
        if not events or (total_pages and page >= total_pages):
            break
        page += 1
    return lookup


def augment_event_urls_from_tribe_apis(df) -> dict:
    """In place: for every kingdom in KINGDOMS_WITH_TRIBE_API, fill the
    event_url field on matching rows. Returns {kingdom: filled_count}."""
    filled: dict[str, int] = {}
    for kingdom, base in KINGDOMS_WITH_TRIBE_API.items():
        mask = (df["source"] == kingdom) & (df.get("event_url", "") == "")
        if not mask.any():
            filled[kingdom] = 0
            continue
        lookup = _fetch_tribe_events(base)
        if not lookup:
            filled[kingdom] = 0
            continue
        n = 0
        for idx in df[mask].index:
            norm = _normalize_title_for_match(df.at[idx, "title"])
            if norm in lookup:
                df.at[idx, "event_url"] = lookup[norm]
                n += 1
        filled[kingdom] = n
    return filled


# Backwards-compatible wrapper (older code paths call this name)
def augment_aethelmearc_event_urls(df) -> None:
    augment_event_urls_from_tribe_apis(df)


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
    # Prefer the URL from the ICS VEVENT (captured by ImportMaps; populated by
    # scrapers.py's _emit_calendar). When the feed doesn't carry one, fall back
    # to whatever the description-text URL extractor found. Without this, every
    # MEC-scraped Middle event linked to the kingdom calendar instead of its
    # own page (midrealm.org/events/smurf-shoot-4/).
    extracted_url = cleaned_descs.apply(lambda x: x[1].get("event_url") or "")
    if "event_url" in df.columns:
        raw_url = df["event_url"].fillna("").astype(str)
        df["event_url"] = raw_url.where(raw_url.str.strip() != "", extracted_url)
    else:
        df["event_url"] = extracted_url
    df["facebook_url"] = cleaned_descs.apply(lambda x: x[1].get("facebook_url") or "")
    # Lochac's feed prefixes every description with a "Steward:… Email:… Website:…"
    # autocrat block — strip it, keeping the real event description.
    lochac = df["source"] == "Kingdom of Lochac"
    if lochac.any():
        df.loc[lochac, "description"] = (
            df.loc[lochac, "description"].apply(strip_steward_boilerplate)
        )
        print(f"  stripped Steward/Email/Website boilerplate from "
              f"{int(lochac.sum())} Lochac descriptions")
    aeth = df["source"] == "Kingdom of AEthelmearc"
    if aeth.any():
        before = df.loc[aeth, "description"].astype(str)
        after  = before.apply(strip_aethelmearc_notes)
        changed = int((before != after).sum())
        df.loc[aeth, "description"] = after
        if changed:
            print(f"  stripped 'Additional Notes on …' boilerplate from "
                  f"{changed} AEthelmearc descriptions")
    # Promote virtual baronial business meetings from kingdom feeds into the
    # baronial type, so the "Baronial" map filter governs them instead of the
    # "Kingdom" one (mostly aggregated West/Ealdormere baronies).
    promoted = promote_virtual_baronials(df, SCRIPT_DIR / "group_locations.csv")
    if promoted:
        print(f"  promoted {promoted} virtual events from kingdom -> baronial "
              f"(aggregated baronial business meetings)")
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

    # Step 6b: a source that failed to fetch this run (network refused, timeout,
    # WAF 403) produced zero events above and would vanish from the map. Restore
    # its last-good upcoming events so a transient blip doesn't blank a kingdom.
    df = carry_forward_failed_sources(df)

    # Step 6c: apply hand-maintained corrections from event_overrides.csv (run
    # AFTER carry-forward so restored events get corrected too, and AFTER the
    # geocode merge/invalidation so a human pin is never second-guessed). See
    # EDITING_EVENTS.md.
    print("Step 6c: Applying event_overrides.csv corrections ...")
    df = apply_event_overrides(df)

    # Step 7: For kingdoms whose calendars import from a Google Calendar
    # (and therefore lose the original WordPress event URL), backfill the
    # event_url field from the kingdom site's Tribe Events REST API.
    print("Step 7: Backfilling per-kingdom event URLs from Tribe REST APIs ...")
    try:
        per_kingdom = augment_event_urls_from_tribe_apis(df)
        for k, n in per_kingdom.items():
            short = k.replace("Kingdom of ", "")
            print(f"  {short:18s} filled {n} event_urls")
        print()
    except Exception as e:
        print(f"  WARNING: backfill step failed: {e}\n")

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
