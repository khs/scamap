"""
build_group_locations.py
------------------------
Scrapes each kingdom's "local groups" directory and writes a CSV mapping
SCA group names (Barony of X, Shire of Y, etc.) to a geocodable region
and the group's official website URL.

Two consumers of this CSV:
  1. clean_sca_events.py — falls back to the region when an event has only
     "(GroupName)" in its title and no real location field.
  2. build_group_pins.py — geocodes the region and emits group_pins.csv,
     used by the map to render placeholder pins for groups that have a
     website but no events currently on the map.

Re-run this script periodically (quarterly?) to pick up new groups or
relocations. Output is committed to the repo as group_locations.csv.

Each kingdom-specific scraper is a single function returning a list of
(group_name, location_text, website_url) tuples. Add a new function to
register more kingdoms.

Output columns:
    kingdom, group, location, website

Usage:
    python build_group_locations.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Default Windows console encoding (cp1252) crashes on Old Norse names and
# zero-width spaces lurking in scraped HTML. Force UTF-8 stdout so progress
# prints don't kill a long-running scrape mid-kingdom.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


SCRIPT_DIR = Path(__file__).parent
OUTPUT_FILE = SCRIPT_DIR / "group_locations.csv"

HDRS = {"User-Agent": "Mozilla/5.0 (compatible; SCA Maps research bot)"}
TIMEOUT = 20


# ── Seat-map override helpers ───────────────────────────────────────────
# Several kingdom directories give us reliable group *names* and websites but
# only a generic kingdom/region as the location, which lands every group on a
# single centroid. For those kingdoms we keep a hand-verified group→city seat
# map and prefer it over the scraped region. Lookups are apostrophe- and
# case-insensitive so curly/straight quotes and "Barony Of"/"Barony of"
# capitalization drift in scraped names still match.
def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ",
                  s.replace("’", "'").replace("‘", "'")).strip().lower()


def _seat_lookup(city_map: dict, name: str, default: str) -> str:
    if name in city_map:
        return city_map[name]
    n = _norm_key(name)
    for k, v in city_map.items():
        if _norm_key(k) == n:
            return v
    return default


# ---------------------------------------------------------------------------
# Per-kingdom scrapers
# ---------------------------------------------------------------------------

def _extract_group_region(body_text: str, group_name: str) -> str:
    """Try a battery of regex patterns to find a barony/shire's region. Each
    pattern aims to match one of the common ways SCA group pages describe
    where they're located. We try them in order of specificity."""
    # Strip the leading "Barony of" / "Shire of" prefix for matching
    short = re.sub(r"^(Barony|Shire|Canton|Stronghold|College)\s+(?:of\s+)?(?:the\s+)?",
                   "", group_name, flags=re.IGNORECASE)

    patterns = [
        # "The Barony of Altavia includes the San Fernando Valley, ..."
        rf"(?:The\s+)?(?:Barony|Shire|Canton|Stronghold|College)\s+of\s+(?:the\s+)?{re.escape(short)}\s+(?:includes|encompasses|covers|comprises|consists\s+of)\s+([^.;\n]{{4,180}})",
        # "Altavia is located in ..."
        rf"{re.escape(short)}\s+is\s+(?:located|based|situated)\s+in\s+([^.;\n]{{4,180}})",
        # "We are located in ..."
        r"(?:we\s+are\s+|the\s+(?:barony|shire)\s+is\s+)(?:located|based|situated)\s+in\s+([^.;\n]{4,180})",
        # "located in / based in / situated in"
        r"(?:located|situated|based)\s+in\s+([^.;\n]{4,150})",
        # "consists of / comprises / encompasses"
        r"(?:encompasses?|comprises?|consists\s+of|covers)\s+([^.;\n]{4,180})",
        # "in the <something> area"
        r"in\s+the\s+([\w\s]{4,80}\s+(?:area|region|valley|county))",
    ]
    for p in patterns:
        m = re.search(p, body_text, re.IGNORECASE)
        if not m:
            continue
        loc = m.group(1).strip().rstrip(",.;")
        # Drop obvious bad matches (like "the Kingdom of Caid, which is part of ...")
        if re.search(r"\bkingdom of\b", loc, re.IGNORECASE) and "," not in loc[:30]:
            continue
        # Trim repeated whitespace and parenthesised asides
        loc = re.sub(r"\s+", " ", loc)
        # Cut at the first hard separator that doesn't look like part of an address
        loc = re.split(r"\s+(?:and\s+is|, and\s+is|which\s+is|, which|, where)\b", loc)[0].rstrip(",.;")
        return loc
    return ""


# Caid (Southern California, Southern Nevada, Hawaii) seat map. The sca-caid.org
# nav gives accurate *area* descriptions, but several geocode to the wrong
# same-named place — Altavia ("San Fernando Valley") -> San Diego, Western Seas
# ("The Hawaiian Islands") -> Ventura CA, Dun Or ("Antelope Valley") -> Antelope
# near Sacramento, Lyondemere -> open ocean. Pin each to a clean city seat.
CAID_CITY = {
    "Barony of Altavia":      "Van Nuys, California",      # San Fernando Valley
    "Barony of Calafia":      "San Diego, California",
    "Barony of Dreiburgen":   "Redlands, California",       # Inland Empire ("San Bernardino" hits the desert county centroid)
    "Barony of Dun Or":       "Lancaster, California",     # Antelope Valley
    "Barony of Gyldenholt":   "Santa Ana, California",     # Orange County
    "Barony of Lyondemere":   "Long Beach, California",    # South Bay / Long Beach
    "Barony of Naevehjem":    "Ridgecrest, California",
    "Barony of Nordwache":    "Fresno, California",        # Madera/Fresno/Kings/Tulare
    "Barony of Starkhafn":    "Las Vegas, Nevada",         # Clark County
    "Barony of Western Seas": "Honolulu, Hawaii",
    "Barony of Wintermist":   "Bakersfield, California",   # Kern County
    "Barony of the Angels":   "Los Angeles, California",
    "Shire of Al-Sahid":      "Victorville, California",   # Barstow/Victorville
    "Shire of Carreg Wen":    "Lompoc, California",
    "Shire of Darach":        "Ventura, California",       # Ventura County
    "Shire of the Isles":     "Santa Barbara, California",
}


def scrape_caid() -> list[tuple[str, str, str]]:
    """
    Caid's groups directory at places.sca-caid.org/caid/. Scraped area text is
    accurate but several entries geocode to the wrong same-named place, so we
    override with a hand-verified city seat from CAID_CITY before returning.

    Strategy:
      1. Pull the canonical list of group names (and website URLs) from
         the directory page.
      2. Fetch any single sub-page (e.g. /wintermist/) — its left-nav lists
         every group with its region text glued in front of the name.
      3. For each known group name, locate it in the nav block and grab the
         text immediately preceding it as the region. Reading a known list
         of names avoids the fragile multi-word-name regex matching.
    """
    root = "https://places.sca-caid.org/caid/"
    r = requests.get(root, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    group_link_re = re.compile(r"^(Barony|Shire|Canton|Stronghold|College)( of)?\b")
    group_names = []
    group_websites: dict[str, str] = {}
    seen = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if group_link_re.match(text) and text not in seen:
            seen.add(text)
            group_names.append(text)
            # The link target is often the barony's own site (after redirects)
            href = a["href"]
            full = href if href.startswith("http") else "https://places.sca-caid.org" + href
            group_websites[text] = full

    # Pull the nav block from one of the sub-pages
    nav_url = "https://places.sca-caid.org/wintermist/"
    rn = requests.get(nav_url, timeout=TIMEOUT, headers=HDRS)
    nav_soup = BeautifulSoup(rn.text, "lxml")
    nav_text = ""
    for li in nav_soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        if text.startswith("Regions ") and "Barony of" in text and len(text) > 200:
            nav_text = text[len("Regions"):].strip()
            break

    # Account for spelling variants between the directory and the nav. The
    # directory has "Naevehjem" but the wintermist nav uses "Naevehjen".
    aliases = [
        ("Barony of Naevehjem", "Barony of Naevehjen"),
    ]
    for canonical, variant in aliases:
        if variant in nav_text and canonical not in nav_text:
            nav_text = nav_text.replace(variant, canonical)

    # Sort group names longest-first so "Barony of Dun Or" matches before
    # "Barony of Dun" would (preventing the latter from eating the suffix).
    out = []
    if nav_text:
        names_sorted = sorted(group_names, key=len, reverse=True)
        # Find each group's start index in the nav text
        positions = []
        for name in names_sorted:
            idx = nav_text.find(name)
            if idx >= 0:
                positions.append((idx, name))
        positions.sort()

        # Each group's region is the text from the previous group's end (or 0)
        # up to this group's start. If a region accidentally contains another
        # "Barony/Shire of …" substring (means an upstream entry wasn't matched —
        # possibly due to a spelling variant in the nav), keep only the text
        # after that last occurrence.
        prev_end = 0
        inner_group_re = re.compile(r"\b(Barony|Shire|Canton|Stronghold|College)\s+of\s+\w")
        for start, name in positions:
            region = nav_text[prev_end:start].strip().rstrip(",.;")
            # Truncate at the last embedded "(Barony|Shire) of …" to drop
            # the previous-but-unmatched group's name and trailing material
            inner = list(inner_group_re.finditer(region))
            if inner:
                last = inner[-1]
                tail = region[last.start():]
                # Drop the embedded "<Type> of <Word>" prefix from the tail
                tail = re.sub(r"^\w+\s+of\s+\S+\s*", "", tail)
                region = tail.strip().rstrip(",.;") or region
            if region and len(region) <= 200:
                out.append((name, region, group_websites.get(name, "")))
                print(f"  {name}: {region[:80]}")
            prev_end = start + len(name)

    # For any groups not found in the nav, try the old per-page scrape.
    # The link from the directory often redirects to the barony's actual
    # website — follow it and capture the final URL.
    found_names = {n for n, _, _ in out}
    for name in group_names:
        if name in found_names:
            continue
        try:
            for link in soup.find_all("a", string=name):
                href = link["href"]
                full = href if href.startswith("http") else "https://places.sca-caid.org" + href
                rg = requests.get(full, timeout=TIMEOUT, headers=HDRS, allow_redirects=True)
                # Update website to wherever the redirect ended up
                group_websites[name] = rg.url
                sg = BeautifulSoup(rg.text, "lxml")
                main = (sg.select_one("article") or sg.select_one(".entry-content")
                        or sg.select_one("main") or sg.body)
                body = main.get_text(" ", strip=True) if main else ""
                location = _extract_group_region(body, name)
                if location:
                    out.append((name, location, group_websites.get(name, "")))
                    print(f"  {name} (per-page): {location[:80]}")
                else:
                    print(f"  {name}: not found")
                break
        except requests.RequestException as exc:
            print(f"  {name}: fetch failed: {exc}")

    # Prefer a clean, correctly-geocoding city seat over the scraped area text.
    out = [(name, _seat_lookup(CAID_CITY, name, region), web)
           for name, region, web in out]
    return out


def scrape_meridies() -> list[tuple[str, str, str]]:
    """
    Meridies publishes per-state group directories at
    /home/find-my-group/{state}/. Each group is laid out in a Gutenberg
    "wp-block-columns" container. The group name is a hyperlink, the
    Seneschal: row is the second paragraph, and the third paragraph is the
    region. We grab name + website (from the anchor href) + region.
    """
    states = ["alabama", "florida", "georgia", "kentucky", "tennessee"]
    name_prefix_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province)\b",
        re.IGNORECASE,
    )

    out: dict[str, tuple[str, str]] = {}   # name -> (region, website)
    for state in states:
        url = f"https://meridies.org/home/find-my-group/{state}/"
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"  WARNING: failed to fetch {state}: {exc}")
            continue
        soup = BeautifulSoup(r.text, "lxml")

        for col in soup.select(".wp-block-columns"):
            paragraphs = col.select(".wp-block-column p")
            texts = [p.get_text(" ", strip=True) for p in paragraphs]
            texts = [t for t in texts if t]
            if len(texts) < 3:
                continue
            name = texts[0]
            if not name_prefix_re.match(name):
                continue
            # First <p>'s anchor is the website link (the group name is hyperlinked)
            first_a = paragraphs[0].find("a", href=True)
            website = first_a["href"] if first_a else ""
            # Drop mailto: and javascript: false-positives
            if website.startswith(("mailto:", "javascript:")):
                website = ""

            region = texts[-1].rstrip(",.;")
            if (4 <= len(region) <= 200
                and "seneschal" not in region.lower()
                and "@meridies" not in region.lower()):
                if name not in out:
                    out[name] = (region, website)

    items = [(name, loc, web) for name, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_aethelmearc() -> list[tuple[str, str, str]]:
    """
    AEthelmearc's group directory uses Divi's team-member layout. Each
    group is laid out as:
        <h4>Barony of X</h4>
        <p>City, ST</p>
        <div>... officer + Web/Facebook links ...</div>
    We grab the h4 + immediately-following <p>, and pluck the [Web] link
    out of the following details block.
    """
    url = "https://aethelmearc.org/groups/local-groups/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    group_name_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College)\s+of\s+",
        re.IGNORECASE,
    )

    seen: dict[str, tuple[str, str]] = {}    # name -> (region, website)
    for h in soup.find_all("h4"):
        name = h.get_text(" ", strip=True)
        if not group_name_re.match(name):
            continue
        region, website = "", ""
        # First <p> sibling is the region; subsequent divs/paragraphs have
        # the [Web] anchor.
        sib = h.find_next_sibling()
        while sib is not None:
            if not region and sib.name == "p":
                cand = sib.get_text(" ", strip=True).rstrip(",.;")
                if cand and 4 <= len(cand) <= 120 and "email" not in cand.lower():
                    region = cand
            if not website:
                # Look for an anchor whose visible text is "Web"
                for a in sib.find_all("a", href=True) if hasattr(sib, "find_all") else []:
                    text = a.get_text(strip=True)
                    if text.lower() in {"web", "website", "[ web ]"}:
                        website = a["href"]
                        break
            # Stop at the next group's <h4>
            if sib.name == "h4":
                break
            sib = sib.find_next_sibling()
            if region and website:
                break
        if region and name not in seen:
            seen[name] = (region, website)

    out = [(name, loc, web) for name, (loc, web) in sorted(seen.items())]
    for g, loc, web in out:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return out


def scrape_atlantia() -> list[tuple[str, str, str]]:
    """
    Atlantia's group directory at atlantia.sca.org/newcomers/local-groups/
    lays out each state as an `<h3>STATE</h3>` (with USPS state codes — MD,
    VA, NC, SC, GA) followed by one `<p>` per group. Each `<p>` looks like:

        Barony of <Name> Seneschal: … Chatelain: … Located in: <Region>

    with the first `<a>` linking to the group's website (anchor text = the
    bare name). We pull the type+name from the opening words, the region
    from "Located in:", and the website from the first link.
    """
    url = "https://atlantia.sca.org/newcomers/local-groups/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")
    main = soup.select_one("article") or soup.select_one("main") or soup.body

    # Atlantia uses USPS codes for the state headings on this page
    state_codes = {"MD", "VA", "NC", "SC", "GA"}
    type_prefix_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+(?:the\s+)?",
        re.IGNORECASE,
    )

    out: dict[str, tuple[str, str]] = {}    # name -> (region, website)

    # Walk through children of main, tracking the current state heading
    for el in main.find_all(["h3", "p"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name == "h3":
            continue  # state heading — we don't actually need it; "Located in:" gives finer info
        # Must look like a group entry
        if not type_prefix_re.match(text):
            continue
        # Pull the group's full name = text up to "Seneschal" / "Chatelain" / "Located in"
        name_match = re.match(
            r"^((?:Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+(?:the\s+)?[\w' \-]+?)"
            r"\s+(?:Seneschal|Chatelain|Located\s+in)\b",
            text, flags=re.IGNORECASE,
        )
        if not name_match:
            continue
        name = re.sub(r"\s+", " ", name_match.group(1)).strip()

        # Region = text after "Located in:" up to end of paragraph
        region = ""
        loc_match = re.search(
            r"Located\s+in[:\s]+([^.;\n]{4,500})",
            text, flags=re.IGNORECASE,
        )
        if loc_match:
            region = loc_match.group(1).strip().rstrip(",.;")

        # Website = first anchor that doesn't start with "mailto:"
        website = ""
        for a in el.find_all("a", href=True):
            href = a["href"]
            if not href.startswith(("mailto:", "javascript:")):
                website = href
                break

        if region and name not in out:
            out[name] = (region, website)

    items = [(name, loc, web) for name, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_east() -> list[tuple[str, str, str]]:
    """
    East Kingdom's branches at eastkingdom.org/branches/ — flat text listing
    where each entry reads "(Barony|Shire|…) of (Name) - (Region)". Almost
    every group is anchored with a link to its eastkingdom.org subdomain
    (e.g. <slug>.eastkingdom.org), so we collect those first and then merge
    with regions parsed from the same page body.
    """
    url = "https://www.eastkingdom.org/branches/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")
    main = soup.select_one("article") or soup.select_one("main") or soup.body

    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding|Crown\s+Principality)"
        r"\s+of\s+(?:the\s+)?[\w'\- ]+$",
        re.IGNORECASE,
    )
    # Pass 1: pull every anchored group → website mapping
    websites: dict[str, str] = {}
    for a in main.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not group_re.match(text):
            continue
        text = re.sub(r"\s+", " ", text).strip()
        href = a["href"]
        if href.startswith(("http://", "https://")) and text not in websites:
            websites[text] = href

    # Pass 2: pull (name, region) pairs from the body text
    out: dict[str, tuple[str, str]] = {}
    entry_re = re.compile(
        r"((?:Barony|Shire|Canton|Stronghold|College|Province|Riding|Crown\s+Principality)"
        r"\s+of\s+(?:the\s+)?[\w'\- ]+?)"
        r"\s+[-–—]\s+"
        r"([^\n]{4,120}?)"
        r"(?=\s+(?:Barony|Shire|Canton|Stronghold|College|Province|Riding|Crown\s+Principality)\s+of\s|\s+The\s+(?:Northern|Central|Southern|Tir)\s+Region|\s+Crown\s+Principality|$)",
        re.IGNORECASE,
    )
    body = main.get_text(" ", strip=True) if main else ""
    for m in entry_re.finditer(body):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        region = re.sub(r"\s+", " ", m.group(2)).strip().rstrip(",.;")
        region = re.sub(r"\(DORMANT\)|\(dormant\)", "", region).strip().rstrip(",.;")
        if "dormant" in name.lower() or "dormant" in region.lower():
            continue
        if 4 <= len(region) <= 120 and name not in out:
            out[name] = (region, websites.get(name, ""))

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_gleann_abhann() -> list[tuple[str, str, str]]:
    """
    Gleann Abhann's local-groups page is built with Elementor. Each entry
    is its own deeply-nested div containing an <h5> with the group name and
    the region/officer info as plain text. We find each h5, walk a couple
    levels up to the container, and extract the region from the container's
    text between the group name and "Seneschal".
    """
    url = "https://gleannabhann.net/local-groups/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+",
        re.IGNORECASE,
    )
    out: dict[str, tuple[str, str]] = {}
    for h in soup.find_all("h5"):
        name = h.get_text(" ", strip=True)
        if not group_re.match(name):
            continue
        # Walk up to find the smallest ancestor whose text includes both
        # the group name AND the "Seneschal" marker.
        container = h.parent
        while container is not None:
            text = container.get_text(" ", strip=True)
            if name in text and "Seneschal" in text:
                break
            container = container.parent
        if container is None:
            continue
        text = container.get_text(" ", strip=True)
        # Region is between the group name and "Seneschal"
        idx_name = text.find(name)
        idx_sen  = text.find("Seneschal", idx_name)
        if idx_name < 0 or idx_sen <= idx_name:
            continue
        region = text[idx_name + len(name):idx_sen].strip().rstrip(",.;")
        # Strip official-website-list trailing words if any
        region = re.sub(r"\s+Official\s+(?:Website|Facebook).*$", "",
                         region, flags=re.IGNORECASE).strip().rstrip(",.;")
        # Pull website from any anchor in the container
        website = ""
        for a in container.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("http://", "https://")) and "gleannabhann.net" not in href \
                    and "facebook" not in href.lower() and "mailto:" not in href:
                website = href
                break
        if 4 <= len(region) <= 200 and name not in out:
            out[name] = (region, website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_trimaris() -> list[tuple[str, str, str]]:
    """
    Trimaris's groups page uses paragraphs of the form:
        <p><b>Barony of X</b> Region words Website: <a>URL</a> Contact: ...</p>
    We grab each <b> matching a group name, then read the parent <p>'s text
    and pull out the region (between the group name and "Website:"/"Contact:"
    /"Email:") plus the first <a> href as the website.
    """
    url = "https://trimaris.org/local-groups-gatherings/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+",
        re.IGNORECASE,
    )
    out: dict[str, tuple[str, str]] = {}
    for b in soup.find_all("b"):
        name = b.get_text(" ", strip=True)
        if not group_re.match(name) or "INACTIVE" in name.upper():
            continue
        # The parent <p> has the full entry text; strip the group name prefix
        parent = b.find_parent("p") or b.parent
        if parent is None:
            continue
        full = parent.get_text(" ", strip=True)
        if name in full:
            tail = full.split(name, 1)[1]
        else:
            tail = full
        # Region ends at the first "Website:" / "Contact:" / "Email:" marker
        region = re.split(
            r"\s+(?:Website|Contact|Email|Officers?|Seneschal|MKA)\s*:",
            tail, maxsplit=1,
        )[0].strip().rstrip(",.;")
        if region.upper().startswith("INACTIVE"):
            continue
        if not region or len(region) < 4 or len(region) > 200:
            region = "Florida"
        # Website is the first non-mailto anchor in the parent
        website = ""
        for a in parent.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("http://", "https://")) and "trimaris.org" not in href \
                    and "facebook" not in href.lower():
                website = href
                break
        if name not in out:
            out[name] = (region, website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_avacal() -> list[tuple[str, str, str]]:
    """
    Avacal's /branches/ page — small kingdom in Alberta + Saskatchewan + BC.
    Page is a WP grid: each group lives in a div.wp-block-column with the bare
    name (no "Barony of"/"Shire of" prefix) in a centered <p>, followed by
    region / officer / website lines. The first three columns are baronies
    (we know because the body text contains "The Barony of X serves...");
    everything else is a shire.
    """
    url = "https://avacal.org/branches/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")
    out: dict[str, tuple[str, str]] = {}

    for col in soup.select("div.wp-block-column"):
        first_p = col.select_one("p.has-text-align-center")
        if not first_p:
            continue
        name = first_p.get_text(" ", strip=True)
        if len(name) > 30 or len(name) < 3 or not all(c.isalpha() or c.isspace() for c in name):
            continue

        # All text in the column tells us city/region; the descriptive
        # "The Barony of X serves..." text marks it as a barony.
        text = col.get_text(" ", strip=True)
        is_barony = re.search(rf"The Barony of {re.escape(name)} ", text, re.IGNORECASE) is not None
        full = f"{'Barony' if is_barony else 'Shire'} of {name}"

        # Find region: line right after the name, but strip officer titles
        parts = [p.get_text(" ", strip=True) for p in col.find_all(["p"])]
        parts = [p for p in parts if p and p != name]
        region = ""
        for p in parts:
            if any(kw in p for kw in ["Seneschal", "Baron and Baroness", "Officer",
                                        "Website", "Facebook"]):
                continue
            if "The Barony" in p or "The Shire" in p:
                continue
            if len(p) > 4 and len(p) <= 100:
                region = p.rstrip(",.;")
                break
        if not region or len(region) < 4:
            region = "Alberta, Canada"

        # Website: external http(s) link that isn't avacal.org
        website = ""
        for a in col.find_all("a", href=True):
            href = a["href"]
            if (href.startswith(("http://", "https://"))
                    and "avacal.org" not in href
                    and "google.com/url" not in href     # WP exports broken redirect URLs
                    and "mailto:" not in href):
                website = href
                break

        if full not in out:
            out[full] = (region, website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:80]}  ({web[:60]})")
    return items


# Hand-curated "City, Country" seats for Drachenwald groups whose scraped
# region text is a bare province name or multi-city list that Nominatim
# can't resolve. City chosen as the named seat or the province capital.
DRACHENWALD_CITY = {
    "Barony of Eplaheimr":          "Athlone, Ireland",
    "Canton of Drei Eichen":        "Köln, Germany",
    "Canton of Hukka":              "Helsinki, Finland",
    "Canton of Humalasalo":         "Tampere, Finland",
    "Canton of Meadowmarsh":        "Frankfurt, Germany",
    "Canton of Miehonlinna":        "Kouvola, Finland",
    "Canton of Roterde":            "Dortmund, Germany",
    "Canton of Turmstadt":          "Nürnberg, Germany",
    "Canton of Vielburgen":         "Kaiserslautern, Germany",
    "College of St John of Rila":   "Kyustendil, Bulgaria",
    "Shire of Baggeholm":           "Karlskrona, Sweden",     # Blekinge
    "Shire of Dun in Mara":         "Dublin, Ireland",
    "Shire of Fjellom":             "Östersund, Sweden",      # Jämtland
    "Shire of Frostheim":           "Luleå, Sweden",          # Norrbotten
    "Shire of Glen Rathlin":        "Belfast, United Kingdom",
    "Shire of Gyllengran":          "Sundsvall, Sweden",      # Medelpad
    "Shire of Löghammar":           "Västerås, Sweden",       # Västmanland
    "Shire of Mynydd Gwyn":         "Cardiff, United Kingdom",
    "Shire of Reengarda":           "Skellefteå, Sweden",
    "Shire of Ulvberget":           "Skövde, Sweden",         # Skaraborg
    "Shire of Örehus":              "Helsingborg, Sweden",
    # Baronies that already geocode but pin to country centroid — sharpen them
    "Barony of Aarnimetsä":         "Helsinki, Finland",
    "Barony of Gotvik":             "Gothenburg, Sweden",
    "Barony of Knights Crossing":   "Kaiserslautern, Germany",
    "Barony of Styringheim":        "Visby, Sweden",          # Gotland
}


def scrape_drachenwald() -> list[tuple[str, str, str]]:
    """
    Drachenwald spans Europe + parts of Africa & the Middle East. Their
    /groups/ page has TWO useful structures:

    1. A summary block at the top of the page that lists each Barony with
       its country in parentheses:
         <b>Aarnimetsä</b> (Finland)
         <b>Ad Flumen Caerulum</b> (Austria)
         ...
       We harvest the (country) for each barony from this list.

    2. Per-group blocks lower on the page, each with:
         <h2|h3>Barony of X</h2>
         <strong>Region:</strong>  <text node — the region we want>
         <br>
         <strong>Online:</strong>  <a href="..."> website </a>

       Walking `find_next_sibling()` skips text nodes, so we work off the
       parent's raw text instead and split on "Region:" / "Online:".

    For unknown groups we fall back to the barony-summary country, or to
    "Europe" if that fails too.
    """
    url = "https://drachenwald.sca.org/groups/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    # Pass 1: Summary list of baronies → country mapping
    barony_country: dict[str, str] = {}
    for b in soup.find_all("b"):
        bare = b.get_text(" ", strip=True)
        if not bare or " " in bare and len(bare) > 30:
            continue
        # Next sibling text node should be " (Country)"
        nxt = b.next_sibling
        if nxt is None:
            continue
        nxt_text = nxt if isinstance(nxt, str) else nxt.get_text(" ", strip=True)
        m = re.match(r"\s*\(([^)]{2,200})\)", nxt_text)
        if m:
            barony_country[bare.lower()] = m.group(1).strip()

    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+",
        re.IGNORECASE,
    )
    out: dict[str, tuple[str, str]] = {}

    # Pass 2: Walk each group heading, then read the parent's text from
    # *just after the heading* up to the next heading. That text contains
    # the "Region:" / "Online:" pairs we want.
    headings = [h for h in soup.find_all(["h2", "h3"])
                if group_re.match(h.get_text(" ", strip=True))]

    for idx, h in enumerate(headings):
        name = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip()
        # Gather raw HTML between this heading and the next heading
        block_parts: list[str] = []
        for el in h.next_elements:
            if el is h:
                continue
            if hasattr(el, "name") and el.name in {"h2", "h3"} and el is not h:
                break
            if isinstance(el, str):
                block_parts.append(str(el))
        block_text = re.sub(r"\s+", " ", " ".join(block_parts)).strip()

        # Region: text between "Region:" and the next label ("Online:",
        # "Seneschal:", etc.) or end-of-block.
        region = ""
        rm = re.search(
            r"Region\s*:\s*(.+?)(?=\s*(?:Online|Seneschal|Webminister|Chronicler|Twitter|Facebook)\s*:|\s*$)",
            block_text, re.IGNORECASE,
        )
        if rm:
            region = rm.group(1).strip().rstrip(",.;")
            # Drop trailing labels that leaked through
            region = re.split(r"\s+(?:Online|Seneschal|Officers?|Web|@)\b",
                              region, maxsplit=1)[0].strip().rstrip(",.;")

        # Fallback: try the summary list (look up by bare name)
        if not region:
            bare = re.sub(r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+(?:the\s+)?",
                          "", name, flags=re.IGNORECASE).strip().lower()
            region = barony_country.get(bare, "")

        # Prefer a hand-curated "City, Country" seat — Drachenwald's region
        # text is full of bare Swedish/Finnish province names ("Blekinge",
        # "Jämtland", "Helsinki area") that Nominatim can't place without a
        # country, so the geocoder was leaving 21 groups pinless.
        if name in DRACHENWALD_CITY:
            region = DRACHENWALD_CITY[name]

        # Online (website) — first http(s) anchor in the block
        website = ""
        # We need to look at the live siblings for anchors, since block_text has them stripped
        sib = h.find_next_sibling()
        for _ in range(8):
            if sib is None or (hasattr(sib, "name") and sib.name in {"h2", "h3"}):
                break
            if hasattr(sib, "find_all"):
                for a in sib.find_all("a", href=True):
                    href = a["href"]
                    if (href.startswith(("http://", "https://"))
                            and "drachenwald.sca.org" not in href
                            and "mailto:" not in href
                            and "facebook.com" not in href.lower()
                            and not website):
                        website = href
                        break
            if website:
                break
            sib = sib.find_next_sibling()

        if name not in out:
            out[name] = (region or "Europe", website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Known seats for Ealdormere groups. The kingdom's /branches/ page rarely
# specifies a city in the body text, so without these every group geocodes
# to the geographic centre of Ontario and stacks on top of itself.
EALDORMERE_CITY = {
    # Baronies
    "Barony of Ben Dunfirth":       "Hamilton, Ontario, Canada",
    "Barony of Ramshaven":          "Guelph, Ontario, Canada",
    "Barony of Rising Waters":      "Niagara Falls, Ontario, Canada",
    "Barony of Septentria":         "Toronto, Ontario, Canada",
    "Barony of Skraeling Althing":  "Ottawa, Ontario, Canada",
    # Cantons under Septentria (Toronto area)
    "Canton of Ardchreag":          "Scarborough, Ontario, Canada",
    "Canton of Eoforwic":           "Toronto, Ontario, Canada",
    "Canton of Greyfells":          "Etobicoke, Ontario, Canada",
    "Canton of Northgeatham":       "Aurora, Ontario, Canada",
    "Canton of Skeldergate":        "Etobicoke, Ontario, Canada",
    "Canton of Vest Yorvik":        "Newmarket, Ontario, Canada",
    "Canton of Petrea Thule":       "Sault Ste. Marie, Ontario, Canada",
    # Cantons under Skraeling Althing (Ottawa-Carleton area)
    "Canton of Bryniau Tywynnog":   "Renfrew County, Ontario, Canada",
    "Canton of Caldrithig":         "Carleton Place, Ontario, Canada",
    "Canton of Monadh":             "Almonte, Ontario, Canada",
    "Canton of Beremere":           "Belleville, Ontario, Canada",
    # Strongholds under Skraeling Althing (Ottawa Valley)
    "Stronghold of Greyfells":      "Pembroke, Ontario, Canada",
    "Stronghold of Tor Brant":      "Arnprior, Ontario, Canada",
    # Shires
    "Shire of Bastille du Lac":     "Sarnia, Ontario, Canada",
    "Shire of Champcorbeau":        "Sault Ste. Marie, Ontario, Canada",
    "Shire of Trinovantia Nova":    "London, Ontario, Canada",
    "Shire of Ulfheim":             "Sudbury, Ontario, Canada",  # approx — northern Ontario
}


def scrape_ealdormere() -> list[tuple[str, str, str]]:
    """
    Ealdormere's /branches/ page lists each group in a <p> as
    "<Group Name> <ealdormere.ca/slug/> <officer info>". The group name is in
    a <strong> tag, the website is the slug link, and officer info follows.
    The page rarely names a city, so we fall back to a hand-maintained seat
    map (EALDORMERE_CITY) before defaulting to the Ontario centroid.
    """
    url = "https://ealdormere.ca/branches/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    out: dict[str, tuple[str, str]] = {}
    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+",
        re.IGNORECASE,
    )
    for strong in soup.find_all("strong"):
        name = strong.get_text(" ", strip=True)
        # Normalize zero-width spaces and curly apostrophes in scraped names
        name = name.replace("​", "").replace("’", "'").replace("‘", "'")
        # Some <strong> tags glue a URL onto the name, e.g.
        # "Stronghold of Tor Brant skraelingalthing.com/wp/torbrant/".
        # Cut everything from the first domain-looking token onward.
        name = re.split(r"\s+(?:https?://|\S+\.(?:com|ca|org|net)\b)", name)[0]
        name = re.sub(r"\s+", " ", name).strip()
        if not group_re.match(name):
            continue
        # Walk up to enclosing <p> to find website (the next anchor)
        parent = strong.find_parent("p") or strong.parent
        website = ""
        for a in parent.find_all("a", href=True) if parent else []:
            href = a["href"]
            if href.startswith(("http://", "https://")) and "mailto:" not in href:
                website = href
                break
        # Always prefer a hand-curated city when one exists — body extraction
        # often grabs the seneschal/website/email line instead of a real
        # location, and that doesn't geocode.
        hardcoded = EALDORMERE_CITY.get(name)
        if hardcoded:
            region = hardcoded
        else:
            # Fallback: try to extract a region from the parent paragraph
            ptext = parent.get_text(" ", strip=True) if parent else ""
            region = ""
            if ptext.startswith(name):
                rest = ptext[len(name):].strip()
                rest = re.sub(r"https?://\S+|ealdormere\.ca/\S+", "", rest)
                rest = re.split(
                    r"\s+(?:Lord|Lady|THL|THLord|THLady|Baron|Baroness|Master|Mistress|Officers?|Seneschal|Chatelaine)\b",
                    rest, maxsplit=1,
                )[0].strip().rstrip(",.;")
                # Drop body text that's a URL/email/officer name rather than a place
                if (3 <= len(rest) <= 200 and "@" not in rest
                        and "http" not in rest and "skraelingalthing" not in rest.lower()):
                    region = rest
        # Drop any remaining zero-width characters from the final region string
        if region:
            region = region.replace("​", "").replace(" ", " ")
            region = re.sub(r"\s+", " ", region).strip()
        if name not in out:
            out[name] = (region or "Ontario, Canada", website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_lochac() -> list[tuple[str, str, str]]:
    """
    Lochac's /groups/ page only links a handful of groups directly, but the
    SCA's Lochac wiki publishes one subdomain per group (e.g. ildhafn.lochac
    .sca.org). We extract all distinct <prefix>.lochac.sca.org links from the
    /groups/ page, filter out the officer subdomains, and visit each group's
    homepage to learn its full name and region.
    """
    url = "https://lochac.sca.org/groups/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)

    # All group-ish subdomains (excluding officer roles + the kingdom itself)
    OFFICER_SUBS = {
        "www","lochac","baronage","chivalry","constable","defense",
        "hospitaller","laurels","seneschal","seneschaldb","herald","scribes",
    }
    subs = set()
    for m in re.finditer(r"https?://([\w-]+)\.lochac\.sca\.org", r.text):
        s = m.group(1).lower()
        if s not in OFFICER_SUBS:
            subs.add(s)

    out: dict[str, tuple[str, str]] = {}
    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+(?:the\s+)?",
        re.IGNORECASE,
    )

    for sub in sorted(subs):
        site = f"https://{sub}.lochac.sca.org/"
        try:
            rg = requests.get(site, timeout=TIMEOUT, headers=HDRS,
                              allow_redirects=True)
            if rg.status_code != 200:
                continue
            sg = BeautifulSoup(rg.text, "lxml")
            # Title is usually "<Group Name> | Lochac" or similar
            title = (sg.find("title").get_text(strip=True) if sg.find("title") else "")
            name_m = re.search(
                r"(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+"
                r"(?:the\s+)?[\w' \-]+",
                title, re.IGNORECASE,
            )
            name = re.sub(r"\s+", " ", name_m.group(0)).strip() if name_m else ""
            if not name:
                # Fall back to first <h1>/<h2> on the page
                for h in sg.find_all(["h1", "h2"]):
                    htext = h.get_text(" ", strip=True)
                    if group_re.match(htext):
                        name = htext
                        break
            if not name:
                continue
            # Normalise mangled characters in the name (e.g. "Barony of Kraé
            # Glas" arriving as "Kra<replacement-char>") so the city lookup
            # and downstream dedup match cleanly.
            name = (name.replace("�", "e")     # replacement char → best guess
                        .replace("’", "'").replace("‘", "'"))
            name = re.sub(r"\s+", " ", name).strip()
            # Hand-curated seat always wins — the body text on Lochac group
            # pages is wildly inconsistent ("your insurance, by signing up",
            # "Schlaepher Park, 41C Ostrich Farm Road, Pukekohe", multi-
            # sentence blurbs) and rarely geocodes.
            region = LOCHAC_CITY.get(name, "")
            if not region:
                body = sg.get_text(" ", strip=True)[:3000]
                for pat in [
                    r"(?:located|based|situated)\s+in\s+([^.;\n]{4,120})",
                    r"(?:covers|encompasses|comprises)\s+([^.;\n]{4,120})",
                    r"(?:Region|Location)\s*[:\-]\s*([^.;\n]{4,120})",
                ]:
                    m = re.search(pat, body, re.IGNORECASE)
                    if m:
                        region = m.group(1).strip().rstrip(",.;")
                        break
            if not region:
                region = "Australia"   # last-resort default
            if name not in out:
                out[name] = (region, site)
        except requests.RequestException:
            continue

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Known seats for Lochac groups, used as a fallback when the per-subdomain
# scrape can't find a "located in ..." sentence. Keeps Aneala in Perth and
# Rowany in Sydney instead of stacking everyone in central Australia.
LOCHAC_CITY = {
    # Baronies
    "Barony of Aneala":        "Perth, Western Australia",
    "Barony of Ildhafn":       "Auckland, New Zealand",
    "Barony of Innilgard":     "Adelaide, South Australia",
    "Barony of Krae Glas":     "Eltham, Victoria, Australia",
    "Barony of Kraé Glas":     "Eltham, Victoria, Australia",   # accented spelling
    "Barony of Mordenvale":    "Newcastle, New South Wales, Australia",
    "Barony of Politarchopolis": "Canberra, Australia",
    "Barony of River Haven":   "Brisbane, Queensland, Australia",
    "Barony of Rowany":        "Sydney, New South Wales, Australia",
    "Barony of Southron Gaard": "Christchurch, New Zealand",
    "Barony of St Florian de la Riviere": "Townsville, Queensland, Australia",
    "Barony of Stormhold":     "Melbourne, Victoria, Australia",
    "Barony of Ynys Fawr":     "Hobart, Tasmania, Australia",
    # Shires
    "Shire of Adora":          "Nowra, New South Wales, Australia",
    "Shire of Bordescros":     "Albury, New South Wales, Australia",
    "Shire of Darton":         "Wollongong, New South Wales, Australia",
    "Shire of Dismal Fogs":    "Blackheath, New South Wales, Australia",
    "Shire of Mountain's Edge": "Hamilton, New Zealand",
    "Shire of Okewaite":       "Wellington, New Zealand",
    "Shire of Saint Florian":  "Townsville, Queensland, Australia",
    "Shire of Torlyon":        "Geelong, Victoria, Australia",
    # Cantons
    "Canton of Burnfield":     "Melbourne, Victoria, Australia",
    "Canton of Okewaite":      "Wellington, New Zealand",
    "Canton of Stowe-on-the-Wowld": "Melbourne, Victoria, Australia",
    # Colleges (all attached to universities in major cities)
    "College of Blessed Herman the Cripple": "Sydney, New South Wales, Australia",
    "College of St Basil the Great":         "Brisbane, Queensland, Australia",
    "College of St Christina the Astonishing": "Melbourne, Victoria, Australia",
    "College of St Monica":                  "Wollongong, New South Wales, Australia",
    "College of St Ursula":                  "Sydney, New South Wales, Australia",
}


def scrape_antir() -> list[tuple[str, str, str]]:
    """
    An Tir is behind Cloudflare's Managed Challenge — no automated client
    (cloudscraper, curl_cffi, patchright) can clear it. Instead we parse a
    locally-saved copy of https://antir.org/branches/ — save the page from
    your normal browser into the project root as
        "All Branches – Kingdom of An Tir.html"
    or in Downloads with that exact filename. Re-save when the kingdom adds
    or dissolves a branch (probably annually).

    The HTML lists every branch as an <h2> followed by a "Branch Website"
    anchor and a "Learn More About This Branch" anchor whose URL path tells
    us which principality the branch belongs to:
        /branches/kingdom-of-an-tir/summits/<name>/        → Summits
        /branches/kingdom-of-an-tir/principality-of-tir-righ/<name>/ → Tir Righ
        /branches/kingdom-of-an-tir/<name>/                → mainland An Tir

    For region (geocoding target) we use a hardcoded city map for the
    major baronies (their seats are stable), and fall back to a per-
    principality default for shires/cantons.
    """
    # Look for the file in two places: project root or user's Downloads.
    candidate_paths = [
        SCRIPT_DIR / "All Branches – Kingdom of An Tir.html",
        Path.home() / "Downloads" / "All Branches – Kingdom of An Tir.html",
    ]
    html_path = next((p for p in candidate_paths if p.exists()), None)
    if html_path is None:
        print("  (no local An Tir HTML found; save the page from your browser "
              "as 'All Branches – Kingdom of An Tir.html' to enable this scraper)")
        return []

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "lxml")

    # Known seats for every An Tir group we can place. Without these the
    # cantons and shires all stack at the principality centroid — 12 in
    # Vancouver, 11 in Seattle, 9 in Eugene. Cross-referenced against
    # antir.org's branch directory and the kingdom map "An Tir During the
    # Reign of Kjartan II & Sha'ya II". Update when groups relocate or new
    # ones charter.
    BARONY_CITY = {
        # ── Tir Righ (BC, Yukon, NW WA) ─────────────────────────────────
        "Principality of Tir Righ":  "Burnaby, BC, Canada",  # offset from Lions Gate
        "Barony of Lions Gate":      "Vancouver, BC, Canada",
        "Barony of Seagirt":         "Victoria, BC, Canada",
        "Barony of Stromgard":       "Surrey, BC, Canada",
        "Shire of Appledore":        "Prince Rupert, BC, Canada",
        "Shire of Coill Mhór":       "Williams Lake, BC, Canada",
        "Shire of Cold Keep":        "Whitehorse, YT, Canada",
        "Shire of Danescombe":       "Kelowna, BC, Canada",
        "Shire of Hartwood":         "Courtenay, BC, Canada",
        "Shire of Krakafjord":       "Vernon, BC, Canada",
        "Shire of Lionsdale":        "Squamish, BC, Canada",
        "Shire of Ramsgaard":        "Kamloops, BC, Canada",
        "Shire of Thornwold":        "Powell River, BC, Canada",
        "Shire of Tir Bannog":       "Prince George, BC, Canada",
        # ── Main An Tir (Puget Sound / Idaho / MT) ──────────────────────
        "Barony of Madrone":         "Seattle, WA",
        "Barony of Aquaterra":       "Bremerton, WA",   # Kitsap
        "Barony of Dragons Laire":   "Silverdale, WA",  # Kitsap-north
        "Barony of Glymm Mere":      "Olympia, WA",
        "Barony of Wyewood":         "Federal Way, WA",
        "Barony of Wealdsmere":      "Lynnwood, WA",
        "Barony of Three Mountains": "Portland, OR",
        "Barony of Vulcanfeldt":     "Spokane, WA",
        "Barony of Wastekeep":       "Kennewick, WA",
        "Barony of Blatha an Oir":   "Whitefish, MT",
        # Cantons of Madrone, College & shire groups in WA — spread across
        # the Puget Sound region rather than stacking in downtown Seattle.
        "Canton of Akornebir":       "Mount Vernon, WA",
        "Canton of Caladphort":      "Oak Harbor, WA",
        "Canton of Crows Gate":      "Tacoma, WA",
        "Canton of Kaldor Ness":     "Edmonds, WA",
        "Canton of Misty Ridge":     "Issaquah, WA",
        "Canton of Porte de l'Eau":  "Tukwila, WA",
        "College of Cranehaven":     "Bellingham, WA",  # Western Washington Univ
        "College of Lyonsmarche":    "Seattle, WA",     # Seattle U/SPU area
        "Shire of Hauksgarðr":       "Bothell, WA",
        "Shire of River's Bend":     "Renton, WA",
        # ── Summits (Southern Oregon) ───────────────────────────────────
        "Principality of the Summits": "Springfield, OR",  # offset from Adiantum
        "Barony of Adiantum":        "Eugene, OR",
        "Barony of Terra Pomaria":   "Salem, OR",
        "Barony of Glyn Dwfn":       "Medford, OR",
        "Barony of Dragon's Mist":   "Hillsboro, OR",
        "Shire of Briaroak":         "Roseburg, OR",
        "Shire of Coeur du Val":     "Corvallis, OR",
        "Shire of Corvaria":         "Bend, OR",
        "Shire of Mountain Edge":    "Florence, OR",
        "Shire of Myrtle Holt":      "Coos Bay, OR",
        "Shire of Southmarch":       "Ashland, OR",
        "Shire of Tymberhavene":     "Klamath Falls, OR",
    }
    PRINCIPALITY_DEFAULT = {
        "Summits":   "Eugene, OR",
        "Tir Righ":  "Vancouver, BC, Canada",
        "":          "Seattle, WA",   # main An Tir
    }

    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding|Principality)"
        r"\s+of\s+",
        re.IGNORECASE,
    )

    out: dict[str, tuple[str, str]] = {}
    for h in soup.find_all("h2"):
        name = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip()
        # Normalize curly apostrophes
        name = name.replace("’", "'").replace("‘", "'")
        if not group_re.match(name):
            continue
        if "Dissolved" in name or "dissolved" in name:
            continue

        # Walk forward through siblings to find the two anchors
        branch_website = ""
        learn_more = ""
        sib = h
        for _ in range(8):
            sib = sib.find_next_sibling()
            if sib is None or (hasattr(sib, "name") and sib.name == "h2"):
                break
            if not hasattr(sib, "find_all"):
                continue
            for a in sib.find_all("a", href=True):
                tx = a.get_text(" ", strip=True)
                href = a["href"]
                if "Branch Website" in tx and not branch_website:
                    branch_website = href
                elif "Learn More" in tx and not learn_more:
                    learn_more = href

        # Derive principality from the Learn More URL
        principality = ""
        if "summits" in learn_more.lower():
            principality = "Summits"
        elif "principality-of-tir-righ" in learn_more.lower():
            principality = "Tir Righ"

        # Pick a region: known barony seat first, then principality default
        region = BARONY_CITY.get(name) or PRINCIPALITY_DEFAULT[principality]

        if name not in out:
            out[name] = (region, branch_website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Ansteorra (Oklahoma + Texas) seat map. The /groups/ pages often describe a
# whole multi-county region (or just "Texas"), so without this many groups land
# on the OKC / central-Texas centroid. Cities verified against ansteorra.org
# group pages. League City stands in for Loch Soilleir's Clear Lake/NASA area
# so it doesn't collide with Stargate (Houston); Graywood=Lufkin so it doesn't
# collide with Rosenfeld (Tyler).
ANSTEORRA_CITY = {
    "Barony of Bjornsborg":      "San Antonio, Texas",
    "Barony of Bonwicke":        "Lubbock, Texas",
    "Barony of Bordermarch":     "Beaumont, Texas",
    "Barony of Bryn Gwlad":      "Austin, Texas",
    "Barony of Elfsea":          "Fort Worth, Texas",
    "Barony of Loch Soilleir":   "League City, Texas",      # Clear Lake / SE Houston
    "Barony of Namron":          "Norman, Oklahoma",
    "Barony of Northkeep":       "Tulsa, Oklahoma",
    "Barony of Wiesenfeuer":     "Oklahoma City, Oklahoma",
    "Barony of the Eldern Hills": "Lawton, Oklahoma",
    "Barony of the Stargate":    "Houston, Texas",
    "Barony of the Steppes":     "Dallas, Texas",
    "Canton of Chemin Noir":     "Bartlesville, Oklahoma",
    "Canton of Glaslyn":         "Denton, Texas",
    "Canton of Myrgenfeld":      "Guthrie, Oklahoma",
    "Canton of Skorragarðr":     "Shawnee, Oklahoma",       # dormant
    "Province of Mooneschadowe": "Stillwater, Oklahoma",
    "Riding of Marata":          "Enid, Oklahoma",
    "Shire of Adlersruhe":       "Amarillo, Texas",
    "Shire of Brad Leah":        "Wichita Falls, Texas",
    "Shire of Ffynnon Gath":     "San Marcos, Texas",
    "Shire of Graywood":         "Lufkin, Texas",           # Deep East TX
    "Shire of Rosenfeld":        "Tyler, Texas",
    "Shire of Seawinds":         "Corpus Christi, Texas",
    "Shire of the Shadowlands":  "College Station, Texas",
    "Stronghold of Hellsgate":   "Killeen, Texas",          # Fort Hood area
}


def scrape_ansteorra() -> list[tuple[str, str, str]]:
    """
    Ansteorra (Oklahoma + Texas) — /groups/ page links every group as
    /ansteorra.org/<slug>. Each group has its own static page. We visit
    each page to read its full name from <title> and any location/region
    descriptive text from the body, then prefer a hand-verified city seat
    from ANSTEORRA_CITY so multi-county descriptions don't centroid-stack.
    """
    url = "https://ansteorra.org/groups/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+"
        r"(?:the\s+)?[\w' \-]+$",
        re.IGNORECASE,
    )
    # Pull only the *real* group anchors: skip mailing-list and college pages.
    candidate_links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).strip()
        href = a["href"]
        if not group_re.match(text):
            continue
        # Drop mailing-list and meta-group pages (College of Heralds, etc.)
        if any(s in href for s in ["lists.ansteorra.org", "/heraldry", "/scribal"]):
            continue
        # Drop secondary anchors for the same barony (Dance, Heralds, Officers
        # sub-list pages — they all hang off lists.ansteorra.org)
        if text not in candidate_links:
            candidate_links[text] = href

    out: dict[str, tuple[str, str]] = {}
    for name, group_url in sorted(candidate_links.items()):
        if not group_url.startswith("http"):
            group_url = "https://ansteorra.org" + group_url
        region = ""
        try:
            rg = requests.get(group_url, timeout=TIMEOUT, headers=HDRS,
                              allow_redirects=True)
            if rg.status_code == 200:
                sg = BeautifulSoup(rg.text, "lxml")
                # Title sometimes contains the full name (e.g. "Barony of Raven's
                # Fort — Ansteorra"). Only accept a longer name if the match
                # starts at the very beginning of the title — otherwise we
                # grab descriptive text like "Skorragarðr – A Canton of the
                # Barony of Namron in the Principality of Vindheim".
                title = (sg.find("title").get_text(strip=True) if sg.find("title") else "")
                tm = re.match(
                    r"((?:Barony|Shire|Canton|Stronghold|College|Province|Riding)"
                    r"\s+of\s+(?:the\s+)?[\w' \-]+?)(?:\s+[–—\-]|\s+in\s+|$)",
                    title, re.IGNORECASE,
                )
                if tm and len(tm.group(1)) > len(name):
                    name = re.sub(r"\s+", " ", tm.group(1)).strip()
                # Region from body
                main = (sg.select_one("article") or sg.select_one(".entry-content")
                        or sg.select_one("main") or sg.body)
                body = main.get_text(" ", strip=True) if main else ""
                for pat in [
                    r"(?:Counties?\s+included\s*:\s*)([^.;\n]{4,200})",
                    r"(?:located|based|situated)\s+in\s+([^.;\n]{4,150})",
                    r"(?:covers|encompasses|comprises|includes)\s+([^.;\n]{4,150})",
                    r"Region\s*[:\-]\s*([^.;\n]{4,150})",
                    r"(?:the cities? of)\s+([^.;\n]{4,150})",
                ]:
                    rm = re.search(pat, body, re.IGNORECASE)
                    if rm:
                        cand = rm.group(1).strip().rstrip(",.;")
                        # Drop obvious false hits like "Ansteorra"
                        if 4 <= len(cand) <= 200 and "ansteorra" not in cand.lower():
                            region = cand
                            break
        except requests.RequestException:
            pass
        if not region:
            # Ansteorra fallback: Texas (covers most of it)
            region = "Texas"
        if name not in out:
            out[name] = (_seat_lookup(ANSTEORRA_CITY, name, region), group_url)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Atenveldt (Arizona) seat map. The homepage only links group names — no city —
# so without this all nine baronies/shires land on the Arizona centroid.
# Cities verified against atenveldt.org group pages.
ATENVELDT_CITY = {
    "Barony of Atenveldt":       "Phoenix, Arizona",        # central Phoenix
    "Barony of Mons Tonitrus":   "Sierra Vista, Arizona",   # Cochise County
    "Barony of Ered Sul":        "Flagstaff, Arizona",
    "Barony of Granite Mountain": "Prescott, Arizona",
    "Barony of Sun Dragon":      "Glendale, Arizona",       # west Phoenix valley
    "Barony of Tir Ysgithr":     "Tucson, Arizona",
    "Barony of Twin Moons":      "Mesa, Arizona",           # east Phoenix valley
    "Shire of Burning Sands":    "Yuma, Arizona",
    "Shire of Windale":          "Kingman, Arizona",
}


def scrape_atenveldt() -> list[tuple[str, str, str]]:
    """Atenveldt lists their baronies right on the kingdom homepage as links.
    The homepage carries no city, so we prefer a hand-verified seat from
    ATENVELDT_CITY (else fall back to the Arizona centroid)."""
    url = "https://www.atenveldt.org/"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")
    out: dict[str, tuple[str, str]] = {}
    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+",
        re.IGNORECASE,
    )
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not group_re.match(text):
            continue
        if text not in out:
            out[text] = (_seat_lookup(ATENVELDT_CITY, text, "Arizona"), a["href"])

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:80]}  ({web[:60]})")
    return items


# Northshield seat map. Northshield spans MN, WI, the Dakotas, the western UP
# of Michigan, plus Manitoba and NW Ontario — but its OP group pages carry no
# reliable location, so the scraper defaulted every group to "Wisconsin",
# stacking 25 groups on the WI centroid. Cities verified against each group's
# site and the northshield.org/local-groups directory.
NORTHSHIELD_CITY = {
    "Barony of Caer Anterth Mawr": "Milwaukee, Wisconsin",
    "Barony of Castel Rouge":      "Winnipeg, Manitoba, Canada",
    "Barony of Jararvellir":       "Madison, Wisconsin",
    "Barony of Nordskogen":        "Minneapolis, Minnesota",
    "Barony of Windhaven":         "Appleton, Wisconsin",
    "Canton of Coille Stoirmeil":  "Tomah, Wisconsin",
    "Canton of Nordleigh":         "Northfield, Minnesota",
    "College of Svaty Sebesta":    "Brookings, South Dakota",   # SDSU
    "Shire of Border Downs":       "Sioux Falls, South Dakota",
    "Shire of Coldedernhale":      "Pierre, South Dakota",
    "Shire of Darkstone":          "Ashland, Wisconsin",
    "Shire of Dreibrucken":        "Grand Forks, North Dakota",
    "Shire of Falcon's Keep":      "Stevens Point, Wisconsin",
    "Shire of Inner Sea":          "Duluth, Minnesota",
    "Shire of Korsvag":            "Fargo, North Dakota",
    "Shire of Mare Amethystinum":  "Thunder Bay, Ontario, Canada",
    "Shire of Midewinde":          "Minot, North Dakota",
    "Shire of Rivenwood Tower":    "Mankato, Minnesota",
    "Shire of Rockhaven":          "St. Cloud, Minnesota",
    "Shire of Rokeclif":           "La Crosse, Wisconsin",
    "Shire of Schattentor":        "Rapid City, South Dakota",
    "Shire of Silfren Mere":       "Rochester, Minnesota",
    "Shire of Skerjastrond":       "Marquette, Michigan",       # western UP
    "Shire of Trewint":            "Aberdeen, South Dakota",
    "Shire of Vilku Urvas":        "Grand Rapids, Minnesota",
}


def scrape_northshield() -> list[tuple[str, str, str]]:
    """
    Northshield's homepage doesn't host a structured directory but it links
    each group as /op/groups/?groupid=N. We harvest those links from the
    homepage and dereference each one for the proper name + region, then prefer
    a hand-verified city seat from NORTHSHIELD_CITY (the OP pages rarely name a
    location, so the scraped default is the WI centroid for nearly everyone).
    """
    home = "https://northshield.org/"
    r = requests.get(home, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    # Each group gets its own ?groupid=N link from the homepage
    group_links: dict[str, str] = {}    # groupid -> name from the link text
    group_re = re.compile(r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+",
                          re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a["href"]
        m = re.search(r"groupid=(\d+)", href)
        if m and group_re.match(text):
            group_links[m.group(1)] = (text, href if href.startswith("http") else home + href.lstrip("/"))

    out: dict[str, tuple[str, str]] = {}
    for gid, (name, group_url) in sorted(group_links.items()):
        # Try to fetch the group's OP page for region / external website
        region = "Wisconsin"   # kingdom default
        website = group_url
        try:
            rg = requests.get(group_url, timeout=TIMEOUT, headers=HDRS)
            if rg.status_code == 200:
                sg = BeautifulSoup(rg.text, "lxml")
                main = (sg.select_one("article") or sg.select_one(".entry-content")
                        or sg.select_one("main") or sg.body)
                body = main.get_text(" ", strip=True) if main else ""
                # Look for a location hint
                for pat in [
                    r"(?:located|based|situated)\s+in\s+([^.;\n]{4,150})",
                    r"(?:covers|encompasses|comprises)\s+([^.;\n]{4,150})",
                    r"Region\s*[:\-]\s*([^.;\n]{4,150})",
                ]:
                    m = re.search(pat, body, re.IGNORECASE)
                    if m:
                        cand = m.group(1).strip().rstrip(",.;")
                        if 4 <= len(cand) <= 200 and "northshield" not in cand.lower():
                            region = cand
                            break
                # External website if linked
                for a in sg.find_all("a", href=True):
                    href = a["href"]
                    if (href.startswith(("http://", "https://"))
                        and "northshield.org" not in href
                        and "facebook" not in href.lower()
                        and "mailto:" not in href):
                        website = href
                        break
        except requests.RequestException:
            pass
        if name not in out:
            out[name] = (_seat_lookup(NORTHSHIELD_CITY, name, region), website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Middle Kingdom seat map. The Midrealm publishes no machine-readable group
# directory, and its event feed only attributes a "Hosted by <group>" with no
# location, so (like Lochac and Ealdormere) we hand-maintain the group→city
# table below. Cities verified against MiddleWiki (middlewiki.midrealm.org).
# This replaced an event-text regex scraper that emitted truncated junk names
# ("Barony of the", "Shire of Dark") and stacked ~30 groups on the kingdom
# centroid via the generic "Indiana, Illinois, Ohio…" kingdom-wide blurb.
MIDDLE_CITY = {
    # Baronies
    "Barony of Andelcrag":         "Grand Rapids, Michigan",      # western Lower Michigan
    "Barony of Ayreton":           "Chicago, Illinois",
    "Barony of Brendoken":         "Akron, Ohio",
    "Barony of Carraig Ban":       "DeKalb, Illinois",
    "Barony of the Cleftlands":    "Cleveland, Ohio",
    "Barony of Cynnabar":          "Ann Arbor, Michigan",
    "Barony of Fenix":             "Cincinnati, Ohio",
    "Barony of the Flame":         "Louisville, Kentucky",
    "Barony of Flaming Gryphon":   "Dayton, Ohio",
    "Barony of Illiton":           "Peoria, Illinois",
    "Barony of Northwoods":        "Lansing, Michigan",
    "Barony of Red Spears":        "Toledo, Ohio",
    "Barony of Rivenstar":         "Lafayette, Indiana",
    "Barony of Roaring Wastes":    "Detroit, Michigan",
    "Barony of Shattered Crystal": "Belleville, Illinois",        # SW Illinois / Metro East
    "Barony of Sternfeld":         "Indianapolis, Indiana",
    # Cantons
    "Canton of Ealdnordwuda":      "East Lansing, Michigan",
    "Canton of Hrothgeirsfjordr":  "Toledo, Ohio",
    "Canton of Pferdestadt":       "Delaware, Ohio",
    "Canton of Rimsholt":          "Grand Rapids, Michigan",
    "Canton of Vanished Wood":     "Chicago, Illinois",           # Chicago suburbs
    # Riding
    "Riding of Hawkland Moor":     "Rochester Hills, Michigan",   # N. Oakland/Macomb
    # Province
    "Province of Tree-Girt-Sea":   "Chicago, Illinois",
    # Shires
    "Shire of Dark River":         "Moline, Illinois",            # Quad Cities
    "Shire of Dernehealde":        "Athens, Ohio",
    "Shire of Donnershafen":       "Traverse City, Michigan",     # northern Lower Michigan
    "Shire of Dragonsmark":        "Lexington, Kentucky",
    "Shire of Falcon's Quarry":    "Elyria, Ohio",                # Lorain County
    "Shire of Lochmorrow":         "Peoria, Illinois",            # canton of Illiton
    "Shire of Mugmort":            "Lancaster, Ohio",
    "Shire of Mynydd Seren":       "Bloomington, Indiana",
    "Shire of Narrental":          "Logansport, Indiana",
    "Shire of Ravenslake":         "Crystal Lake, Illinois",      # Lake/McHenry, NW Chicago
    "Shire of Rivenvale":          "Youngstown, Ohio",
    "Shire of Rokkehealden":       "Chicago, Illinois",           # Chicago suburbs
    "Shire of Shadowed Stars":     "Fort Wayne, Indiana",
    "Shire of Stormvale":          "Flint, Michigan",
    "Shire of Swordcliff":         "Springfield, Illinois",
    "Shire of Tirnewydd":          "Columbus, Ohio",
}

# Groups with a confirmed-live X.midrealm.org subdomain (slug). Others fall
# back to the kingdom homepage so the pin's "website" link is never dead.
MIDDLE_SLUG = {
    "Barony of Andelcrag": "andelcrag", "Barony of Ayreton": "ayreton",
    "Barony of Brendoken": "brendoken", "Barony of Cynnabar": "cynnabar",
    "Barony of Fenix": "fenix", "Barony of Illiton": "illiton",
    "Barony of Northwoods": "northwoods", "Barony of Red Spears": "redspears",
    "Barony of Rivenstar": "rivenstar", "Barony of Sternfeld": "sternfeld",
    "Canton of Ealdnordwuda": "ealdnordwuda", "Canton of Pferdestadt": "pferdestadt",
    "Canton of Rimsholt": "rimsholt", "Shire of Dernehealde": "dernehealde",
    "Shire of Dragonsmark": "dragonsmark", "Shire of Mugmort": "mugmort",
    "Shire of Narrental": "narrental", "Shire of Rokkehealden": "rokkehealden",
    "Shire of Swordcliff": "swordcliff",
}


def scrape_middle() -> list[tuple[str, str, str]]:
    """
    Middle Kingdom — emit the hand-maintained MIDDLE_CITY seat table. The
    Midrealm publishes no group directory and its event feed gives no
    locations, so a static table (verified against MiddleWiki) is both more
    accurate and more stable than scraping event text. See MIDDLE_CITY.
    """
    items: list[tuple[str, str, str]] = []
    for name in sorted(MIDDLE_CITY):
        city = MIDDLE_CITY[name]
        slug = MIDDLE_SLUG.get(name)
        website = f"https://{slug}.midrealm.org/" if slug else "https://www.midrealm.org/"
        items.append((name, city, website))
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Calontir (KS, MO, NE, IA, N. AR) seat map. The group list is derived from
# event-JSON host names, whose homepages describe a multi-county region (or
# default to "Missouri"), so groups stacked on the MO centroid. Cities verified
# against each group's site + Wikipedia. Note several are NOT in Missouri
# (Vatavia=KS, Coeur d'Ennui=IA, Mag Mor/Lonely Tower=NE).
CALONTIR_CITY = {
    "Barony of Vatavia":          "Wichita, Kansas",
    "Barony of Coeur d'Ennui":    "Des Moines, Iowa",
    "Barony of Forgotten Sea":    "Kansas City, Missouri",
    "Barony of Mag Mor":          "Lincoln, Nebraska",
    "Barony of Three Rivers":     "St. Louis, Missouri",
    "Barony of the Lonely Tower": "Omaha, Nebraska",
    "Shire of Cum An Iolair":     "Overland Park, Kansas",
    "Shire of Heraldshill":       "Mason City, Iowa",
    "Shire of Lost Moor":         "St. Joseph, Missouri",
}
# 'Lilies War' is Calontir's annual war *event* (Smithville Lake, MO), not a
# branch — the event-JSON scraper mis-enumerated it as a host. Drop it.
CALONTIR_DROP = {"shire of lilies war"}


def scrape_calontir() -> list[tuple[str, str, str]]:
    """
    Calontir's `calon_vload.php` event JSON (the same endpoint our event
    scraper uses) ships a `group_name` and `primary_website` with every
    event. Enumerate distinct hosts, then visit each website to read its
    full barony/shire/canton name from <title>, preferring a hand-verified
    city seat from CALONTIR_CITY and dropping event-only hosts (CALONTIR_DROP).
    """
    try:
        r = requests.get(
            "https://www.calontir.org/wp-content/plugins/calon/calon_vload.php?didi=50",
            timeout=TIMEOUT, headers=HDRS,
        )
        events = r.json()
    except Exception:
        return []
    sites: dict[str, str] = {}
    for e in events:
        gn = (e.get("group_name") or "").strip()
        ws = (e.get("primary_website") or "").strip()
        # Skip non-group hosts (whole-kingdom entries)
        if not gn or gn in {"Calontir", "Northshield", "the Outlands", "Æthelmearc"}:
            continue
        if not ws.startswith("http"):
            ws = "https://" + ws
        if gn not in sites:
            sites[gn] = ws

    out: dict[str, tuple[str, str]] = {}
    for short, site in sorted(sites.items()):
        try:
            rg = requests.get(site, timeout=TIMEOUT, headers=HDRS)
            sg = BeautifulSoup(rg.text, "lxml")
            title_text = sg.find("title").get_text(strip=True) if sg.find("title") else ""
            m = re.search(
                r"(Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+(?:the\s+)?[\w' \-]+",
                title_text, re.IGNORECASE,
            )
            name = m.group(0).strip() if m else f"Shire of {short}"
            # Find region in the homepage body
            body = sg.get_text(" ", strip=True)[:3000]
            region = "Missouri"
            for pat in [
                r"(?:located|based|situated)\s+in\s+([^.;\n]{4,150})",
                r"(?:covers|encompasses|comprises)\s+([^.;\n]{4,150})",
            ]:
                mm = re.search(pat, body, re.IGNORECASE)
                if mm:
                    region = mm.group(1).strip().rstrip(",.;")
                    break
            if _norm_key(name) in CALONTIR_DROP:
                continue
            if name not in out:
                out[name] = (_seat_lookup(CALONTIR_CITY, name, region), site)
        except requests.RequestException:
            continue

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_outlands() -> list[tuple[str, str, str]]:
    """
    Kingdom of the Outlands' /local-groups page (note: outlands.org, not
    outlandskingdom.org) renders each group as a WP post card. The clean
    structure is:
      <li class="wp-block-post ...">
        <a href="/branches/<slug>/"></a>          (image link, empty text)
        <h2 class="wp-block-post-title">Barony of Aarquelle</h2>
        <p>Pueblo, CO</p>
        <a href="https://aarquelle.outlands.org">Visit Website</a>
      </li>
    """
    url = "https://www.outlands.org/local-groups"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+",
        re.IGNORECASE,
    )
    out: dict[str, tuple[str, str]] = {}
    for h2 in soup.select("h2.wp-block-post-title"):
        name = h2.get_text(" ", strip=True)
        if not group_re.match(name):
            continue
        # Walk up to the enclosing post container (li.wp-block-post)
        li = h2.parent
        for _ in range(8):
            if li is None:
                break
            cls = li.get("class") or []
            if any("wp-block-post" in c and c != "wp-block-post-title" for c in cls):
                break
            li = li.parent
        if li is None:
            continue
        # City/state = the text in the post minus the name and "Visit Website"
        post_text = li.get_text(" | ", strip=True)
        region = post_text.replace(name, "", 1).replace("Visit Website", "")
        region = re.sub(r"\s*\|\s*", " ", region).strip(" |,.")
        if not region:
            region = "Colorado"
        # External "Visit Website" link
        website = ""
        for a in li.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            if "visit" in text or "website" in text:
                website = href
                break
        # Fallback: any external link
        if not website:
            for a in li.find_all("a", href=True):
                href = a["href"]
                if href.startswith(("http://", "https://")) and "outlands.org/branches" not in href:
                    website = href
                    break
        if name not in out:
            out[name] = (region, website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


def scrape_artemisia() -> list[tuple[str, str, str]]:
    """
    Artemisia's /groups page is a Google Sites layout. Each group line reads
    "The Barony of X" (or Shire of X) followed by "(City, ST and second city, ST)"
    and then an Artemisia-hosted Google Sites URL printed as text. The website
    is occasionally an external link; otherwise we fall back to the printed URL.
    Artemisia spans MT+UT+ID+WY; the region is taken from the parenthesised
    city/state text.
    """
    url = "https://www.artemisia.sca.org/groups"
    r = requests.get(url, timeout=TIMEOUT, headers=HDRS)
    soup = BeautifulSoup(r.text, "lxml")

    # Each group's name appears in a span as "The (Barony|Shire) of X"
    name_re = re.compile(
        r"^The\s+(?P<type>Barony|Shire|Canton|Stronghold|College|Province|Riding)\s+of\s+(?P<rest>.+)$",
        re.IGNORECASE,
    )
    out: dict[str, tuple[str, str]] = {}
    for node in soup.find_all(string=name_re):
        # Strip leading "The "
        name_match = name_re.match(str(node).strip())
        if not name_match:
            continue
        name = f"{name_match.group('type').title()} of {name_match.group('rest').strip()}"
        # Skip if it's a stray sentence fragment (rest too long)
        if len(name_match.group("rest")) > 40:
            continue

        # Walk up to the enclosing block-level container to find the region
        container = node.parent
        region = ""
        website = ""
        for _ in range(6):
            if container is None:
                break
            text = container.get_text(" | ", strip=True)
            rm = re.search(r"\(([^)]{4,200})\)", text)
            if rm and not region:
                region = rm.group(1).strip()
            # Find external website anchor
            if not website:
                for a in container.find_all("a", href=True):
                    href = a["href"]
                    if (href.startswith(("http://", "https://"))
                            and "artemisia.sca.org" not in href
                            and "google.com/search" not in href
                            and "mailto:" not in href):
                        website = href
                        break
            # As a last resort, accept a Google Sites artemisia URL printed
            # as plain text (no anchor) — fall back to the kingdom subpage
            if region and website:
                break
            container = container.parent
        if not website:
            # Look for the URL printed as text within the same row
            container = node.parent
            for _ in range(6):
                if container is None:
                    break
                ctext = container.get_text(" ", strip=True)
                um = re.search(r"https://sites\.google\.com/artemisia\.sca\.org/[\w\-/]+", ctext)
                if um:
                    website = um.group(0)
                    break
                container = container.parent
        if not region:
            region = "Utah"
        if name not in out:
            out[name] = (region, website)

    items = [(g, loc, web) for g, (loc, web) in sorted(out.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Kingdom of the West seat map. The Mists/Cynagua/Oertha directory pages only
# tell us which Principality a group belongs to, so without this every group
# lands on the principality centroid — 28 NorCal groups stacked on one rural
# point and 7 Oertha groups on the Alaska centroid. Cities below are a
# representative seat for each group's county coverage, verified against the
# three Principality group directories (mists/cynagua/oertha.westkingdom.org).
WEST_CITY = {
    # Principality of the Mists — San Francisco Bay Area
    "Principality of the Mists":   "San Francisco, California",
    "Province of the Mists":       "Oakland, California",        # N. Alameda Co.
    "Province of Southern Shores": "San Jose, California",       # central Santa Clara
    "Barony of Darkwood":          "Santa Cruz, California",     # Santa Cruz/Monterey
    "Barony of Westermark":        "Fremont, California",        # San Mateo/S. Alameda
    "Canton of Hawk's Haven":      "Hollister, California",      # San Benito Co.
    "Canton of Montagne du Roi":   "Monterey, California",
    "College of Saint Katherine":  "Berkeley, California",       # UC Berkeley
    "College of St. David":        "Santa Cruz, California",     # UC Santa Cruz
    "Shire of Caldarium":          "San Rafael, California",     # Marin Co.
    "Shire of Cloondara":          "San Francisco, California",
    "Shire of Crosston":           "Sunnyvale, California",      # N. Santa Clara
    "Shire of Teufelberg":         "Antioch, California",        # E. Contra Costa
    "Shire of Vinhold":            "Napa, California",
    "Shire of Wolfscairn":         "Petaluma, California",       # N. Marin/W. Sonoma
    # Principality of Cynagua — Sacramento Valley, Sierra foothills, N. Nevada
    "Principality of Cynagua":     "Sacramento, California",
    "Province of Golden Rivers":   "Sacramento, California",     # Sacramento Co.
    "Province of Silver Desert":   "Reno, Nevada",              # Washoe Co., NV
    "Barony of Fettburg":          "Stockton, California",       # San Joaquin Co.
    "Barony of Rivenoak":          "Chico, California",          # Glenn/Butte Co.
    "Shire of Belogor":            "Yreka, California",          # Siskiyou/Modoc Co.
    "Shire of Canale":             "Modesto, California",        # S. Stanislaus/Merced
    "Shire of Champclair":         "Vacaville, California",      # E. Solano Co.
    "Shire of Danegeld Tor":       "Roseville, California",      # NE Sacramento/Placer
    "Shire of Fendrake Marsh":     "Fallon, Nevada",            # Churchill/Lyon, NV
    "Shire of Mont d'Or":          "Grass Valley, California",   # Nevada Co.
    "Shire of Mountain's Gate":    "Placerville, California",    # El Dorado Co.
    "Shire of Thistletorr":        "Colusa, California",         # Colusa/Sutter Co.
    "Shire of Vakkerfjell":        "Marysville, California",     # Yuba Co.
    "Shire of Windy Meads":        "Davis, California",          # Yolo Co.
    # Principality of Oertha — Alaska
    "Principality of Oertha":      "Anchorage, Alaska",
    "Barony of Eskalya":           "Anchorage, Alaska",
    "Barony of Selveirgard":       "Wasilla, Alaska",            # Mat-Su Valley
    "Barony of Winter's Gate":     "Fairbanks, Alaska",
    "College of Saint Boniface":   "Fairbanks, Alaska",          # UA Fairbanks
    "Shire of Earngyld":           "Juneau, Alaska",             # SE Alaska
    "Shire of Hrafnafjordr":       "Kenai, Alaska",              # Kenai Peninsula
    "Shire of Pavlok Gorod":       "Kodiak, Alaska",             # Kodiak Island
}


def scrape_west() -> list[tuple[str, str, str]]:
    """
    Kingdom of the West is organised as three Principalities (Cynagua, Mists,
    Oertha) plus "The Marches" (smaller groups in Northern California). The
    Kingdom-level /groups/ page only links the Principalities themselves, so
    we scrape each Principality's directory in turn.

    The directory pages give us reliable group *names* and websites but no
    city — only which Principality a group is in. We therefore override the
    generic principality region with a hand-curated seat from WEST_CITY (see
    above) before returning, falling back to the principality default for any
    group not yet mapped.

    Sources:
      Mists  : https://mists.westkingdom.org/branches-groups/   (anchor list)
      Cynagua: https://cynagua.westkingdom.org/wpp/places/      (anchor list)
      Oertha : https://sites.google.com/westkingdom.org/oertha/groups-of-oertha
               (text-only on a Google Sites page; we parse names + cut at
               the first descriptive sentence)
    """
    out: dict[str, tuple[str, str]] = {}
    # Anchor-text regex: matches a complete group name in the *anchor's text*.
    # Limited to ~4 words after "of" to avoid grabbing description text. Allows
    # ASCII + Unicode (curly) apostrophes and dots (for "St.").
    APOS = r"'‘’"
    group_re = re.compile(
        r"^(Barony|Shire|Canton|Stronghold|College|Province|Principality|Riding)\s+of\s+"
        rf"(?:the\s+)?[A-Z][\w{APOS}.\-]+(?:\s+[A-Za-z{APOS}\-.]+){{0,3}}\s*$",
        re.IGNORECASE,
    )

    # Principality of Mists — anchor list on /branches-groups/
    try:
        r = requests.get("https://mists.westkingdom.org/branches-groups/",
                         timeout=TIMEOUT, headers=HDRS)
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            href = a["href"]
            m = group_re.match(text)
            if not m:
                continue
            # Skip award-reference links etc.
            if any(s in href.lower() for s in ["heralds.westkingdom", "awards_by_region"]):
                continue
            name = re.sub(r"\s+", " ", m.group(0)).strip().rstrip(",.;")
            if name not in out:
                out[name] = ("Northern California", href)
    except requests.RequestException:
        pass

    # Principality of Cynagua — anchor list on /wpp/places/
    try:
        r = requests.get("https://cynagua.westkingdom.org/wpp/places/",
                         timeout=TIMEOUT, headers=HDRS)
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            m = group_re.match(text)
            if not m:
                continue
            name = re.sub(r"\s+", " ", m.group(0)).strip().rstrip(",.;")
            href = a["href"]
            # Skip kingdom-level / award reference links
            if any(s in href.lower() for s in ["heralds.westkingdom", "awards_by_region",
                                                "?page_id=", "wpp/about", "wpp/things"]):
                continue
            if name not in out:
                website = href if href.startswith("http") else ""
                out[name] = ("Northern Central Valley, California", website)
        # Also pull text-mode groups with parenthesised region (some entries
        # have a mailto link but no anchor with the name)
        body = soup.get_text(" ", strip=True)
        for m in re.finditer(
            r"((?:Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+(?:the\s+)?"
            r"[A-Z][\w'.\-]+(?:\s+[A-Za-z'\-.]+){0,3})\s*\(([^)]{4,150})\)",
            body,
        ):
            name = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",.;")
            region = m.group(2).strip()
            if name not in out:
                out[name] = (region, "")
    except requests.RequestException:
        pass

    # Principality of Oertha — text-only Google Sites page. The body, when
    # split by newline, exposes each group name on its own line; we strip the
    # optional "The " prefix and accept anything matching the standard pattern.
    try:
        r = requests.get(
            "https://sites.google.com/westkingdom.org/oertha/groups-of-oertha",
            timeout=TIMEOUT, headers=HDRS,
        )
        soup = BeautifulSoup(r.text, "lxml")
        body = soup.get_text("\n", strip=True)
        line_re = re.compile(
            r"^(?:The\s+)?(Barony|Shire|Canton|Stronghold|College|Province)\s+of\s+"
            r"[\w\"'\- ]+$",
            re.IGNORECASE,
        )
        for line in body.split("\n"):
            line = line.strip()
            if not line_re.match(line):
                continue
            name = re.sub(r"^The\s+", "", line, flags=re.IGNORECASE).strip()
            # Google Sites mangled the apostrophe in "Winter's" to a double-quote
            name = name.replace('"', "'")
            # Title-case the article words but preserve the rest
            type_m = re.match(r"^(Barony|Shire|Canton|Stronghold|College|Province)", name, re.I)
            if type_m:
                name = type_m.group(0).title() + name[type_m.end():]
            if name and name not in out:
                out[name] = ("Alaska", "")
    except requests.RequestException:
        pass

    # Normalize curly apostrophes to ASCII, then override the generic
    # principality region with a hand-curated city when we have one.
    normalized: dict[str, tuple[str, str]] = {}
    for name, (loc, web) in out.items():
        clean = name.replace("’", "'").replace("‘", "'")
        normalized[clean] = (WEST_CITY.get(clean, loc), web)

    items = [(g, loc, web) for g, (loc, web) in sorted(normalized.items())]
    for g, loc, web in items:
        print(f"  {g}: {loc[:60]}  ({web[:60]})")
    return items


# Registry of scrapers — adding a kingdom is one function + one entry here
SCRAPERS = {
    "Kingdom of Atlantia":      scrape_atlantia,
    "Kingdom of Caid":          scrape_caid,
    "Kingdom of Meridies":      scrape_meridies,
    "Kingdom of AEthelmearc":   scrape_aethelmearc,
    "Kingdom of the East":      scrape_east,
    "Kingdom of Gleann Abhann": scrape_gleann_abhann,
    "Kingdom of Trimaris":      scrape_trimaris,
    "Kingdom of Avacal":        scrape_avacal,
    "Kingdom of Atenveldt":     scrape_atenveldt,
    "Kingdom of Drachenwald":   scrape_drachenwald,
    "Kingdom of Ealdormere":    scrape_ealdormere,
    "Kingdom of Lochac":        scrape_lochac,
    "Kingdom of Northshield":   scrape_northshield,
    "Kingdom of the Middle":    scrape_middle,
    "Kingdom of Calontir":      scrape_calontir,
    "Kingdom of the Outlands":  scrape_outlands,
    "Kingdom of Artemisia":     scrape_artemisia,
    "Kingdom of the West":      scrape_west,
    "Kingdom of Ansteorra":     scrape_ansteorra,
    "Kingdom of An Tir":        scrape_antir,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    all_rows = []
    for kingdom, scraper in SCRAPERS.items():
        print(f"\n=== {kingdom} ===")
        try:
            results = scraper()
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue
        for entry in results:
            # Scrapers return 3-tuples; tolerate legacy 2-tuples too
            if len(entry) == 2:
                group, location = entry
                website = ""
            else:
                group, location, website = entry
            all_rows.append({
                "kingdom":  kingdom,
                "group":    group,
                "location": location,
                "website":  website or "",
            })

    print(f"\nWriting {len(all_rows)} groups to {OUTPUT_FILE.name}")
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["kingdom", "group", "location", "website"],
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(all_rows)


if __name__ == "__main__":
    main()
