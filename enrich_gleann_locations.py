"""
enrich_gleann_locations.py
--------------------------
Gleann Abhann's kingdom calendar feed leaves most events with just "MS" as the
location, so they all geocode to the Mississippi state centroid and stack on a
single spot. The feed's ICS export does NOT carry a usable organizer — it stamps
one constant "Barony of Axemoor" on every event — but each event's own web page
carries the real hosting group as JSON-LD: "organizer":{"name":"Shire of …"}.

This step reads that per-event organizer from the event page, matches it to a
Gleann Abhann group in locals.csv, and pins the event at that group's known
coordinates — a much better guess than the state centroid. Only VAGUE events
(address_confidence != "high") are touched, so an event that does carry a real
street address keeps it.

Organizers are cached per event URL in gleann_organizer_cache.json, so re-runs
only fetch genuinely new events (and the step works offline once the cache is
warm). Relocated rows are marked geocode_status="ok_organizer" so the geocoder
leaves them alone, and their clean_location becomes the hosting group's area so
the popup matches the pin.

Runs in refresh.py after cleaning and before geocoding.
"""
from __future__ import annotations

import csv
import functools
import html
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

print = functools.partial(print, flush=True)

# Windows console defaults to cp1252; force UTF-8 so group names with non-ASCII
# characters and the punctuation in our log lines print without mangling.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).parent
EVENTS_FILE = SCRIPT_DIR / "sca_events_clean.csv"
LOCALS_FILE = SCRIPT_DIR / "locals.csv"
CACHE_FILE = SCRIPT_DIR / "gleann_organizer_cache.json"

SOURCE = "Kingdom of Gleann Abhann"
FETCH_DELAY_S = 1.2          # be polite to gleannabhann.net between page fetches

# The Events Calendar plugin embeds JSON-LD on each event page; the organizer is
# an object (or a list of them) carrying a "name".
ORGANIZER_RE = re.compile(r'"organizer"\s*:\s*(\[.*?\]|\{.*?\})', re.S)
NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
PREFIX_RE = re.compile(
    r"^(?:barony|shire|canton|province|college|stronghold|riding|march|"
    r"crown principality|principality)\s+of\s+", re.I)

# Gleann Abhann sits in MS / LA / AR / western TN / the Florida panhandle. A group
# whose locals.csv coordinates fall outside this generous box is a data error
# (e.g. a stray California coordinate) — skip it so a single bad row can't fling an
# event across the continent; the event keeps the state centroid until it's fixed.
GLEANN_BBOX = (28.0, 38.0, -96.0, -83.0)   # (lat_min, lat_max, lng_min, lng_max)


def _in_region(lat, lng) -> bool:
    try:
        la, lo = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    return (GLEANN_BBOX[0] <= la <= GLEANN_BBOX[1]
            and GLEANN_BBOX[2] <= lo <= GLEANN_BBOX[3])


def _clean(s: str) -> str:
    """Unescape HTML entities and fold curly apostrophes/backticks to a plain '
    so "Dragoun&#8217;s Weal" matches a locals.csv "Dragoun's Weal"."""
    s = html.unescape(s or "").strip()
    return s.replace("’", "'").replace("‘", "'").replace("`", "'")


def _full(name: str) -> str:
    """Match key: the whole name, normalised + lowercased."""
    return _clean(name).lower().strip()


def _norm(name: str) -> str:
    """Match key: the name minus its SCA group-type prefix ("Shire of …")."""
    return PREFIX_RE.sub("", _clean(name)).lower().strip()


def load_groups(path: Path = LOCALS_FILE) -> dict:
    """Gleann Abhann groups from locals.csv -> {match_key: (group, lat, lng, location)},
    keyed by both the full name and the prefix-stripped name so "Province of
    Lagerdamm" and a bare "Lagerdamm" both resolve."""
    groups: dict = {}
    if not path.exists():
        return groups
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if "Gleann" not in (r.get("kingdom") or ""):
                continue
            name = (r.get("group") or "").strip()
            lat, lng = (r.get("lat") or "").strip(), (r.get("lng") or "").strip()
            if not name or not lat or not lng:
                continue
            if not _in_region(lat, lng):
                print(f"  WARNING: {name} has out-of-region coords in locals.csv "
                      f"({lat},{lng}); skipped — its events stay at the state centroid.")
                continue
            entry = (name, lat, lng, (r.get("location") or "").strip())
            groups.setdefault(_full(name), entry)
            groups.setdefault(_norm(name), entry)
    return groups


def resolve(organizer: str, groups: dict):
    """Resolve a JSON-LD organizer name to its (group, lat, lng, location) entry,
    or None if no Gleann Abhann group matches."""
    if not organizer:
        return None
    return groups.get(_full(organizer)) or groups.get(_norm(organizer))


def parse_organizer(html_text: str):
    """Pull the first JSON-LD organizer name out of an event page's HTML, or None."""
    m = ORGANIZER_RE.search(html_text or "")
    if not m:
        return None
    names = NAME_RE.findall(m.group(1))
    return _clean(names[0]) if names else None


def fetch_organizer(url: str):
    """Fetch an event page and return its organizer name. Returns "" if the page
    loaded but had no organizer (cache it; don't refetch), or None if the fetch
    itself failed (leave uncached so a later run retries)."""
    try:
        from curl_cffi import requests as creq
        resp = creq.get(url, impersonate="chrome", timeout=30)
    except Exception:
        try:
            import requests
            resp = requests.get(
                url,
                headers={"User-Agent":
                         "SCAMap event aggregator (+https://github.com/khs/scamap)"},
                timeout=30)
        except Exception as e:
            print(f"    fetch failed for {url}: {e}")
            return None
    # An HTTP error is a failed fetch, not "page has no organizer" — neither
    # curl_cffi nor requests raise on 4xx/5xx, so check the status. Returning None
    # (uncached) stops a transient 403/429/500 or a Cloudflare challenge from
    # permanently stranding the event at the state centroid (same failure-cache
    # bug class fixed in geocoder.py).
    if getattr(resp, "status_code", 200) >= 400:
        print(f"    fetch failed for {url}: HTTP {resp.status_code}")
        return None
    return parse_organizer(resp.text) or ""


def main():
    if not EVENTS_FILE.exists():
        print(f"{EVENTS_FILE.name} not found — run clean_sca_events first.")
        return
    groups = load_groups()
    if not groups:
        print("No Gleann Abhann groups with coordinates in locals.csv — nothing to do.")
        return

    cache: dict = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    df = pd.read_csv(EVENTS_FILE, dtype=str).fillna("")
    for col in ("lat", "lng", "geocode_status", "clean_location", "address_confidence",
                "event_url", "source"):
        if col not in df.columns:
            df[col] = ""

    is_gleann = df["source"] == SOURCE
    # Only relocate vague events; ones that carry a real street address ("high")
    # keep their own, more precise, location.
    vague = is_gleann & (df["address_confidence"] != "high") & (df["event_url"].str.len() > 0)
    idxs = df[vague].index.tolist()
    print(f"Gleann Abhann: {int(is_gleann.sum())} events, "
          f"{len(idxs)} vague to relocate; cache has {len(cache)} URL(s).")

    fetched = relocated = unmatched = 0
    for idx in idxs:
        url = str(df.at[idx, "event_url"]).strip()
        if not url:
            continue
        if url in cache:
            organizer = cache[url]
        else:
            organizer = fetch_organizer(url)
            if organizer is not None:        # page loaded (a name, or "" for none)
                cache[url] = organizer
                # Flush after every fetch so an interrupted run keeps the pages it
                # already paid the polite 1.2s/page for.
                CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True),
                                      encoding="utf-8")
            fetched += 1
            time.sleep(FETCH_DELAY_S)
        hit = resolve(organizer, groups)
        if not hit:
            if organizer:
                unmatched += 1
                print(f"    unmatched organizer: {organizer!r}")
            continue
        group, lat, lng, loc = hit
        df.at[idx, "lat"] = lat
        df.at[idx, "lng"] = lng
        df.at[idx, "geocode_status"] = "ok_organizer"
        if loc:
            df.at[idx, "clean_location"] = loc       # popup matches the new pin
        relocated += 1

    CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    df.to_csv(EVENTS_FILE, index=False, quoting=csv.QUOTE_ALL)
    print(f"  fetched {fetched} new page(s); relocated {relocated} event(s) to their "
          f"hosting group; {unmatched} organizer(s) had no matching group.")


if __name__ == "__main__":
    main()
