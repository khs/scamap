"""
measure_date_anomalies.py
-------------------------
One-shot empirical probe: for every KINGDOM feed in calendars.csv, obtain each
event's start/end dates from the RAW upstream feed (bypassing the scrapers'
built-in date sanitization where possible) and count date anomalies:

  (a) year < 1900 or > 2100
  (b) end < start
  (c) unparseable / missing dates
  (d) total events seen

For direct ICS feeds we fetch + parse with icalendar. For scraper-prefixed
feeds we hit the same raw JSON/ICS the scraper consumes, but read the date
fields ourselves WITHOUT the scraper's drop/clamp logic, so we see the true
upstream anomaly rate. EventPrime/NuEvent/SimCal/MEC derive dates from per-event
HTML pages (too slow / no bulk raw feed); for those we fall back to running the
scraper and parsing its (already-sanitized) ICS output, and note that caveat.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, date
from pathlib import Path

import requests
from icalendar import Calendar

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scrapers  # noqa: E402

HEADERS = scrapers.HTTP_HEADERS


def year_of(dt):
    if isinstance(dt, (datetime, date)):
        return dt.year
    return None


def classify(start, end):
    """Return list of anomaly tags for a (start, end) pair of date/datetime/None."""
    tags = []
    if start is None:
        tags.append("missing_start")
    if end is None:
        tags.append("missing_end")
    for label, dt in (("start", start), ("end", end)):
        y = year_of(dt)
        if y is not None and (y < 1900 or y > 2100):
            tags.append(f"bad_year_{label}({y})")
    if isinstance(start, (datetime, date)) and isinstance(end, (datetime, date)):
        # normalize to comparable types
        try:
            s = start if isinstance(start, datetime) else datetime(start.year, start.month, start.day)
            e = end if isinstance(end, datetime) else datetime(end.year, end.month, end.day)
            if s.tzinfo and not e.tzinfo:
                e = e.replace(tzinfo=s.tzinfo)
            if e.tzinfo and not s.tzinfo:
                s = s.replace(tzinfo=e.tzinfo)
            if e < s:
                tags.append("end_before_start")
        except Exception:
            pass
    return tags


def to_naive(v):
    """icalendar vDDDTypes .dt -> date or datetime."""
    return v


# ---- Per-source RAW date extractors. Each yields (name, start, end) tuples. ----

def from_ics_text(text):
    cal = Calendar.from_ical(text)
    for comp in cal.walk("VEVENT"):
        summary = str(comp.get("summary", "(untitled)"))
        ds = comp.get("dtstart")
        de = comp.get("dtend")
        start = ds.dt if ds is not None else None
        end = de.dt if de is not None else (ds.dt if ds is not None else None)
        yield summary, start, end


def from_ics_url(url):
    r = scrapers._http_get(url)
    r.raise_for_status()
    body = r.text
    if "BEGIN:VCALENDAR" not in body[:2000].upper() and "BEGIN:VCALENDAR" not in body.upper():
        raise RuntimeError(f"non-ICS response (looks like HTML/{r.status_code})")
    yield from from_ics_text(body)


def parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y%m%d", "%Y%m%dT%H%M%SZ"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def from_calon_json_raw(url):
    """Raw calon JSON: read start_date/end_date WITHOUT the scraper's drop/clamp."""
    r = scrapers._http_get(url)
    r.raise_for_status()
    records = r.json()
    for rec in records:
        name = rec.get("event_name", "(untitled)")
        sd = rec.get("start_date")
        ed = rec.get("end_date") or rec.get("start_date")
        yield name, parse_iso(sd), parse_iso(ed), (sd, ed)


def from_drachenwald_json_raw(url):
    r = scrapers._http_get(url)
    r.raise_for_status()
    records = r.json()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("type") != "event":
            continue
        if str(rec.get("status", "")).strip().lower() == "cancelled":
            continue
        name = rec.get("event-name", "(untitled)")
        sd = (rec.get("start-date") or "").strip()
        ed = (rec.get("end-date") or "").strip() or sd
        yield name, parse_iso(sd), parse_iso(ed), (sd, ed)


def from_tribe_rest_raw(site_url):
    api = site_url.rstrip("/") + "/wp-json/tribe/events/v1/events"
    page = 1
    while True:
        r = requests.get(api, params={"per_page": 50, "page": page},
                         timeout=scrapers.HTTP_TIMEOUT, headers=HEADERS)
        if r.status_code == 404:
            break
        r.raise_for_status()
        data = r.json()
        batch = data.get("events", []) if isinstance(data, dict) else []
        if not batch:
            break
        for e in batch:
            name = e.get("title", "(untitled)")
            sd = e.get("start_date")
            ed = e.get("end_date")
            yield name, parse_iso(sd), parse_iso(ed), (sd, ed)
        if len(batch) < 50:
            break
        page += 1


def via_scraper_ics(calendar_id, name):
    """Fallback: run the scraper, parse its (sanitized) ICS. Caveat noted."""
    ics = scrapers.maybe_scrape(calendar_id, name)
    if ics is None:
        return
    yield from from_ics_text(ics)


def main():
    csv_path = ROOT / "calendars.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    kingdoms = [r for r in rows if r["type"] == "kingdom"]
    print(f"{len(kingdoms)} kingdom feeds\n")

    results = []
    for r in kingdoms:
        cid = r["id"].strip()
        name = r["source"]
        rec = {"name": name, "id": cid, "total": 0, "bad_year": [],
               "end_before_start": [], "unparseable": [], "error": None,
               "raw": True, "method": ""}
        print(f"=== {name} ===")
        try:
            triples = []  # (summary, start, end, raw_strings_or_None)
            if cid.startswith("calon-json:"):
                rec["method"] = "raw calon JSON"
                triples = list(from_calon_json_raw(cid[len("calon-json:"):]))
            elif cid.startswith("drachenwald-json:"):
                rec["method"] = "raw drachenwald JSON"
                triples = list(from_drachenwald_json_raw(cid[len("drachenwald-json:"):]))
            elif cid.startswith("tribe-rest:"):
                rec["method"] = "raw tribe REST"
                triples = list(from_tribe_rest_raw(cid[len("tribe-rest:"):]))
            elif cid.startswith(("simcal:", "nuevent:", "mec-rest:", "eventprime:")):
                rec["method"] = "scraper ICS (sanitized; per-event HTML, no bulk raw feed)"
                rec["raw"] = False
                for s, st, en in via_scraper_ics(cid, name):
                    triples.append((s, st, en, None))
            else:
                rec["method"] = "direct ICS"
                url = (cid if cid.startswith("http")
                       else f"https://calendar.google.com/calendar/ical/{cid}/public/basic.ics")
                for s, st, en in from_ics_url(url):
                    triples.append((s, st, en, None))

            for tup in triples:
                summary, start, end = tup[0], tup[1], tup[2]
                raw = tup[3] if len(tup) > 3 else None
                rec["total"] += 1
                tags = classify(start, end)
                if any(t.startswith("missing_") for t in tags):
                    rec["unparseable"].append((summary, raw if raw else (start, end)))
                if any(t.startswith("bad_year_") for t in tags):
                    rec["bad_year"].append((summary, raw if raw else (start, end), tags))
                if "end_before_start" in tags:
                    rec["end_before_start"].append((summary, str(start), str(end)))
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  ERROR: {rec['error']}")

        print(f"  method={rec['method']} total={rec['total']} "
              f"bad_year={len(rec['bad_year'])} "
              f"end<start={len(rec['end_before_start'])} "
              f"unparseable={len(rec['unparseable'])}")
        for s, raw, tags in rec["bad_year"][:5]:
            print(f"     BAD YEAR: {s!r} {raw} {tags}")
        for s, st, en in rec["end_before_start"][:5]:
            print(f"     END<START: {s!r} start={st} end={en}")
        for s, raw in rec["unparseable"][:5]:
            print(f"     UNPARSEABLE: {s!r} {raw}")
        results.append(rec)
        print()

    print("\n==================== SUMMARY ====================")
    for rec in results:
        flag = "ERROR" if rec["error"] else (
            "ANOMALY" if (rec["bad_year"] or rec["end_before_start"] or rec["unparseable"]) else "clean")
        print(f"  [{flag:7}] {rec['name']:32} total={rec['total']:4} "
              f"bad_year={len(rec['bad_year'])} end<start={len(rec['end_before_start'])} "
              f"unparseable={len(rec['unparseable'])} ({rec['method']})")


if __name__ == "__main__":
    main()
