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
like geocode_cache.json), each entry stamped with the time it was fetched.
Rather than caching forever, a cached description is re-validated against the
live page on a cadence keyed to how soon the event is: monthly for events more
than a month out, weekly inside the final month, daily inside the final week.
Event pages get edited as the date nears (venue, schedule, and fees get
finalised), and those kingdoms' servers send no ETag/Last-Modified, so a timed
re-fetch is the only way to catch the edits — but we spend the effort where it
matters and leave far-off events nearly untouched.

Network failures are non-fatal — the event keeps its last good description (or
its placeholder) and we try again next run. Trivial page text ("Coming soon",
empty) is intentionally NOT cached, so an event picks up its real description
automatically once the kingdom publishes it.

Artemisia is the exception: its flyer PDFs (see extract_artemisia_description)
are re-pulled every run and never cached, because a "save the date" PDF that
initially says TBD gets the real prose dropped in as the event approaches.

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
from datetime import datetime
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

# Re-fetch cadence for cached descriptions, keyed on how soon the event is.
# Event pages get edited as the date nears (venue, schedule, fees finalised),
# so we re-validate aggressively in the run-up and rarely for far-off events.
# An event that first appears a year out is re-checked ~11 times monthly, then
# ~3 times weekly, then daily in its final week — under 2x the work of a flat
# monthly poll, with the extra effort spent where edits actually happen.
# (tier_threshold_days, max_cache_age_days), ordered nearest-first.
REFRESH_TIERS = (
    (7, 1),     # < 1 week out  -> re-fetch when the cached copy is > 1 day old
    (30, 7),    # < 1 month out -> ... > 1 week old
)
DEFAULT_REFRESH_DAYS = 30    # >= 1 month out, or undated -> monthly
PAST_REFRESH_DAYS = 3650     # already happened -> details are settled; freeze it
# NB: the refresh.yml cron runs every 2 days, which caps the "daily" tier at
# every-other-day in practice — fine, and not worth a faster global schedule.

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


def cache_get(cache: dict, url: str) -> tuple[str | None, float]:
    """Return (text, fetched_ts) for a cached URL.

    Tolerates the legacy `{url: "text"}` format by reporting ts=0, so those
    entries read as maximally stale and re-validate (and upgrade to the new
    `{url: {"text", "ts"}}` shape) on the next eligible run."""
    entry = cache.get(url)
    if entry is None:
        return None, 0.0
    if isinstance(entry, str):              # legacy format
        return entry, 0.0
    return entry.get("text"), float(entry.get("ts") or 0)


def cache_put(cache: dict, url: str, text: str, ts: float) -> None:
    cache[url] = {"text": text, "ts": int(ts)}


def days_until_event(start: str, today: datetime) -> int | None:
    """Whole days from `today` to the event's start date (negative if past).

    The CSV `start` column is ISO-ish: "2026-08-01" or "2026-08-01 00:00:00"."""
    s = (start or "").strip()
    if not s:
        return None
    try:
        when = datetime.fromisoformat(s)
    except ValueError:
        try:
            when = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return (when.date() - today.date()).days


def refresh_interval_days(days_until: int | None) -> int:
    """Max age a cached description may reach before re-fetch, by event proximity."""
    if days_until is None:
        return DEFAULT_REFRESH_DAYS
    if days_until < 0:
        return PAST_REFRESH_DAYS
    for threshold, max_age in REFRESH_TIERS:
        if days_until < threshold:
            return max_age
    return DEFAULT_REFRESH_DAYS


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
    now_ts = time.time()
    today = datetime.now()
    fetched = replaced = failed = revalidated = 0

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

        # Artemisia PDFs are never cached -- they're re-pulled every run (see the
        # module docstring). The HTML sources are cached, but a cached copy is
        # only reused while it's fresher than the event's proximity tier allows;
        # past that we re-validate it against the live page.
        use_cache = not is_artemisia
        cached_text, cached_ts = cache_get(cache, url) if use_cache else (None, 0.0)
        if cached_text is not None:
            max_age = refresh_interval_days(days_until_event(r.get("start", ""), today))
            if (now_ts - cached_ts) < max_age * 86400:
                if is_real(cached_text):                 # still fresh -> reuse
                    r["description"] = cached_text.strip()
                    replaced += 1
                continue
            revalidated += 1                             # cache hit, but past its tier

        try:
            real = fetch_description(url, session)
            fetched += 1
            time.sleep(REQUEST_DELAY)
        except Exception as exc:  # noqa: BLE001 — network is best-effort
            print(f"  WARN  could not fetch {url}: {exc}")
            failed += 1
            # Don't lose a good prior description to a transient fetch error:
            # keep the stale copy (with its old timestamp, so we retry next run).
            if is_real(cached_text):
                r["description"] = cached_text.strip()
                replaced += 1
            continue

        if is_real(real):
            # Cache only real descriptions (and only for cacheable sources), so
            # "Coming soon" pages are retried next run and pick up their text
            # once published. Stamp the fetch time to drive the refresh cadence.
            if use_cache:
                cache_put(cache, url, real, now_ts)
                save_cache(cache)
            r["description"] = real.strip()
            replaced += 1
        elif is_real(cached_text):
            # Re-validation came back empty/placeholder but we have good prior
            # text -- keep it, and reset its clock so a one-off empty response
            # doesn't make us re-fetch every single run.
            cache_put(cache, url, cached_text, now_ts)
            save_cache(cache)
            r["description"] = cached_text.strip()
            replaced += 1

    with open(EVENTS_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"enrich_descriptions: fetched {fetched} ({revalidated} cadence re-validations), "
          f"replaced {replaced}, failed {failed} (cache now {len(cache)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
