"""
enrich_descriptions.py
----------------------
Some kingdom calendar feeds ship a placeholder description and keep the real
event write-up on a linked event page. The Kingdom of Atlantia's Google Calendar
is the main offender: every event's description is just

    "Upcoming event in <Group> Event Flyer:"

with the actual text on https://atlantia.sca.org/event/?event_id=XXXX .

This step fetches those pages and swaps the placeholder for the page's
"Description:" field. Results are cached in description_cache.json (committed,
like geocode_cache.json) so re-runs and the unattended cron don't re-fetch.
Network failures are non-fatal — the event keeps its placeholder and we try
again next run. Trivial page text ("Coming soon", empty) is intentionally NOT
cached, so an event picks up its real description automatically once Atlantia
publishes it.

Run AFTER clean_sca_events.py and BEFORE geocode_sca_events.py — the geocoder
preserves the description column when it rewrites the CSV.

Usage:
    python enrich_descriptions.py
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

SCRIPT_DIR = Path(__file__).parent
EVENTS_FILE = SCRIPT_DIR / "sca_events_clean.csv"
CACHE_FILE = SCRIPT_DIR / "description_cache.json"

# Atlantia: the placeholder we replace, e.g. "Upcoming event in Storvik Event
# Flyer:". Anchored at the start so a genuine write-up that merely mentions a
# flyer is never clobbered.
PLACEHOLDER_RE = re.compile(r"^\s*Upcoming event in .*Event Flyer", re.IGNORECASE)
ATLANTIA_HOST = "atlantia.sca.org/event"
EAST_HOST = "eastkingdom.org/EventDetails.php"
# Artemisia publishes per-event flyers as Google Drive PDFs. Their flyer
# template always (so far) contains a "Facebook Event Page/Link" label near
# the top, with the actual description text immediately after.
ARTEMISIA_DRIVE_HOST = "drive.google.com/file/d/"
_DRIVE_ID_RE = re.compile(r"/file/d/([^/]+)")
_ARTEMISIA_FB_MARKER_RE = re.compile(
    r"\bF[ae]cebook\s+(?:Event\s+(?:Page|Link)|Page\s+for\s+this\s+Event)\b",
    re.IGNORECASE,
)
# Page text this short / this generic isn't a real description.
TRIVIAL = {"", "coming soon", "tbd", "tba", "n/a"}
MIN_DESC_LEN = 12

REQUEST_DELAY = 1.5        # seconds between fetches — be polite to the kingdom site
REQUEST_TIMEOUT = 30
USER_AGENT = "SCAMap event aggregator (+https://github.com/khs/scamap)"


def extract_atlantia_description(html: str) -> str | None:
    """Pull the value of the 'Description:' field from an Atlantia event page.

    The page lays out fields as
        <div class="labelDiv">Description:</div> <div>VALUE</div>
    so we find the Description label and read its sibling value div.
    """
    soup = BeautifulSoup(html, "lxml")
    for label in soup.select("div.labelDiv"):
        if label.get_text(strip=True).rstrip(":").strip().lower() == "description":
            value = label.find_next_sibling("div")
            if value is not None:
                return re.sub(r"\s+", " ", value.get_text(" ", strip=True)).strip()
    return None


def extract_east_description(html: str) -> str | None:
    """Pull the body of an eastkingdom.org/EventDetails.php page.

    The real description sits in `<div class="eventDetailsContent">`, right
    after the event header. East's feed only ships the URL as the description,
    so we have to fetch the page to get any actual text."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("div.eventDetailsContent")
    if el is None:
        return None
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def extract_artemisia_description(pdf_bytes: bytes) -> str | None:
    """Pull the event description out of an Artemisia Sage flyer PDF.

    Artemisia's flyers always carry a 'Facebook Event Page/Link' label near
    the top, with the description prose immediately after. We anchor on that
    exact label phrase and take everything that follows; if the label isn't
    present we fail silent rather than ship arbitrary PDF text as a description.
    The kingdom's near-term events have detailed flyers and far-future ones
    often just say "TBD" -- we re-fetch every pipeline run so a flyer that
    starts as TBD picks up real content automatically when it's published.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception:                                  # noqa: BLE001
        return None
    m = _ARTEMISIA_FB_MARKER_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", text[m.end():]).strip() or None


def fetch_description(url: str, session: requests.Session) -> str | None:
    """Fetch `url` and run the right extractor for its host."""
    # Artemisia: PDF download via Drive direct-download URL.
    if ARTEMISIA_DRIVE_HOST in url:
        m = _DRIVE_ID_RE.search(url)
        if not m:
            return None
        dl_url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
        resp = session.get(dl_url, timeout=REQUEST_TIMEOUT,
                           headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            return None                                # Drive served HTML, not the PDF
        return extract_artemisia_description(resp.content)

    # HTML extractors: Atlantia + East.
    resp = session.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    # The pages are UTF-8; requests guesses latin-1 when the header is silent.
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    if ATLANTIA_HOST in url:
        return extract_atlantia_description(resp.text)
    if EAST_HOST in url:
        return extract_east_description(resp.text)
    return None


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, indent=0, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def is_real(text: str | None) -> bool:
    return bool(text) and text.strip().lower() not in TRIVIAL and len(text.strip()) >= MIN_DESC_LEN


def main() -> int:
    if not EVENTS_FILE.exists():
        print("enrich_descriptions: no sca_events_clean.csv — nothing to do")
        return 0

    with open(EVENTS_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames
    if not fieldnames or "description" not in fieldnames:
        print("enrich_descriptions: unexpected CSV shape — skipping")
        return 0

    cache = load_cache()
    session = requests.Session()
    fetched = replaced = failed = 0

    for r in rows:
        desc = (r.get("description") or "")
        url = (r.get("event_url") or "").strip()
        if not url:
            continue
        # Atlantia: trigger only when the placeholder is present (a real write-up
        # in the description column means the page has already been enriched).
        is_atlantia = ATLANTIA_HOST in url and PLACEHOLDER_RE.match(desc)
        # East: the Google Calendar feed ships only the URL as the description,
        # so trigger when the description is empty OR is itself a URL.
        is_east = (EAST_HOST in url
                   and (not desc.strip() or desc.strip().startswith("http")))
        # Artemisia: flyer-PDF URLs on Drive. Trigger when the column is empty,
        # is just the PDF filename, or otherwise too short to be a real desc.
        is_artemisia = (ARTEMISIA_DRIVE_HOST in url
                        and (not desc.strip()
                             or desc.strip().lower().endswith(".pdf")
                             or len(desc.strip()) < 50))
        if not (is_atlantia or is_east or is_artemisia):
            continue

        # Cache by URL for HTML extractors (those pages rarely change). Skip the
        # cache for Artemisia PDFs -- a "save the date" flyer that initially
        # says TBD often gets the real prose added as the event approaches, and
        # we want each pipeline run to pick up that update.
        use_cache = not is_artemisia
        if use_cache and url in cache:
            real = cache[url]
        else:
            try:
                real = fetch_description(url, session)
                fetched += 1
                time.sleep(REQUEST_DELAY)
            except Exception as exc:  # noqa: BLE001 — network is best-effort
                print(f"  WARN  could not fetch {url}: {exc}")
                failed += 1
                continue
            # Cache only real descriptions (and only for cacheable sources), so
            # "Coming soon" events are retried next run and pick up their text
            # once it's published.
            if use_cache and is_real(real):
                cache[url] = real
                save_cache(cache)

        if is_real(real):
            r["description"] = real.strip()
            replaced += 1

    with open(EVENTS_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"enrich_descriptions: fetched {fetched}, replaced {replaced}, failed {failed} "
          f"(cache now {len(cache)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
