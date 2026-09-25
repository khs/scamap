"""
private_addresses.py
--------------------
Blocklist for addresses whose owners asked to be taken off the map (a private
home that hosts practices, say) but which upstream calendars still publish.

The blocklist must not itself publish the address, so private_addresses.csv
stores only a one-way FINGERPRINT of it: the SHA-256 of its words, lower-cased
and joined ("123 Example Rd" -> "123|example|rd"). Matching slides a window of
that many words over each event's text and compares fingerprints, so spacing,
case and punctuation differences between calendars don't matter
("123 Example Rd." == "123  example rd").

Add an entry (prints nothing sensitive; the address is never written down):
    python private_addresses.py add "<address as calendars write it>" \
        "<text to show instead>" <public lat> <public lng> "<note>"

private_addresses.csv columns:
    fingerprint   SHA-256 of the address's words (see above)
    words         how many words it has (the matching window size)
    replace_with  what to show instead (e.g. the town)
    lat, lng      a deliberately coarse public pin, used for every event that
                  mentioned the address
    note          who asked and when — no identifying detail

Used in two places, so a re-import can never bring the address back:
  * clean_sca_events (Step 6g) scrubs events and pins them before the geocoder
    ever sees the address;
  * `python private_addresses.py` (the last refresh.py step) scrubs every file
    the cron commits: sca_events_clean.csv and all *_cache.json.

Old git history still holds earlier copies; removing those needs a rewrite.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CSV_FILE = SCRIPT_DIR / "private_addresses.csv"
FIELDS = ["fingerprint", "words", "replace_with", "lat", "lng", "note"]
TEXT_COLUMNS = ("title", "location", "clean_location", "description")
_WORD_RE = re.compile(r"[A-Za-z]+|\d+")
_JOINER_RE = re.compile(r"^[\s,.]*$")   # allowed between words of one address


def fingerprint(words: list[str]) -> str:
    return hashlib.sha256("|".join(w.lower() for w in words).encode("utf-8")).hexdigest()


def fingerprint_text(text: str) -> tuple[str, int]:
    words = _WORD_RE.findall(text)
    return fingerprint(words), len(words)


def load(path: Path | None = None) -> list[dict]:
    path = path or CSV_FILE
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            fp = (r.get("fingerprint") or "").strip().lower()
            if not fp or fp.startswith("#"):
                continue
            try:
                n = int(r.get("words") or "")
            except ValueError:
                print(f"  WARNING: private_addresses.csv row {fp[:8]}… has no word count — skipped")
                continue
            try:
                lat, lng = float(r.get("lat") or ""), float(r.get("lng") or "")
                coords = (str(lat), str(lng)) if -90 <= lat <= 90 and -180 <= lng <= 180 else None
            except ValueError:
                coords = None
            row = {"fingerprint": fp, "words": n, "coords": coords,
                   "replace_with": (r.get("replace_with") or "").strip() or "[address withheld]"}
            if _find(row["replace_with"], row):
                # It would match again every run and grow without end.
                print(f"  WARNING: private_addresses.csv row {fp[:8]}…: replace_with "
                      f"contains the address itself — using '[address withheld]'")
                row["replace_with"] = "[address withheld]"
            rows.append(row)
    return rows


def _find(s: str, row: dict) -> list[tuple[int, int]]:
    """(start, end) character spans in `s` where the row's address appears."""
    toks = list(_WORD_RE.finditer(s))
    n, spans, i = row["words"], [], 0
    while i + n <= len(toks):
        win = toks[i:i + n]
        if (all(_JOINER_RE.match(s[a.end():b.start()]) for a, b in zip(win, win[1:]))
                and fingerprint([m.group() for m in win]) == row["fingerprint"]):
            spans.append((win[0].start(), win[-1].end()))
            i += n
        else:
            i += 1
    return spans


def scrub_text(s: str, rows: list[dict]) -> tuple[str, dict | None]:
    """Replace every blocklisted address in `s`. Returns (text, first row hit)."""
    hit = None
    for row in rows:
        spans = _find(s, row)
        if not spans:
            continue
        for a, b in reversed(spans):
            s = s[:a] + row["replace_with"] + s[b:]
        # "<venue> <street> <town>" -> one mention of the replacement, not three.
        rep = re.escape(row["replace_with"])
        s = re.sub(rf"{rep}(?:[\s,.]*{rep})+", row["replace_with"], s)
        hit = hit or row
    return s, hit


def apply_to_events(df, rows: list[dict] | None = None):
    """Scrub event text; pin every affected event at the row's public coords
    and mark it "override" so the geocoder leaves it alone."""
    rows = load() if rows is None else rows
    if not rows:
        return df
    n = 0
    for idx in df.index:
        hit = None
        for col in TEXT_COLUMNS:
            if col in df.columns:
                new, h = scrub_text(str(df.at[idx, col]), rows)
                if h:
                    df.at[idx, col] = new
                    hit = hit or h
        if hit:
            n += 1
            if hit["coords"]:
                df.at[idx, "lat"], df.at[idx, "lng"] = hit["coords"]
                df.at[idx, "geocode_status"] = "override"
            else:                           # no public pin given: show no pin
                df.at[idx, "lat"] = df.at[idx, "lng"] = ""
                df.at[idx, "geocode_status"] = "skipped"
    if n:
        print(f"  Withheld a private address on {n} event(s) (private_addresses.csv).")
    return df


def _mentions(s: str, rows) -> bool:
    return any(_find(s, r) for r in rows)


def _scrub_json(obj, rows):
    if isinstance(obj, str):
        return scrub_text(obj, rows)[0]
    if isinstance(obj, list):
        return [_scrub_json(v, rows) for v in obj]
    if isinstance(obj, dict):
        # A key that IS the address (the geocoder's query cache) is dropped
        # outright rather than rewritten into a misleading cache entry.
        return {k: _scrub_json(v, rows) for k, v in obj.items() if not _mentions(k, rows)}
    return obj


# How the pipeline's writers serialise their caches (geocoder.py, scrapers.py,
# enrich_descriptions.py, ...). A scrubbed cache is written back in whichever
# of these reproduces the original, so the commit diff is just the scrub.
_JSON_FORMATS = [
    dict(separators=(",", ":"), sort_keys=True),                   # geocoder
    dict(indent=0, ensure_ascii=False, sort_keys=True),            # scrapers / descriptions
    dict(indent=2, sort_keys=True),                                # maplink / gleann
    dict(indent=2, sort_keys=True, ensure_ascii=False),
    dict(indent=2),
]


def _json_format(raw: str, data) -> dict:
    for fmt in _JSON_FORMATS:
        if json.dumps(data, **fmt) == raw:
            return fmt
    return _JSON_FORMATS[1]


def scrub_files(rows: list[dict] | None = None, directory: Path = SCRIPT_DIR) -> int:
    """Scrub sca_events_clean.csv and every *_cache.json in place."""
    rows = load() if rows is None else rows
    if not rows:
        return 0
    changed = 0
    for path in sorted(directory.glob("*_cache.json")):
        raw = path.read_text(encoding="utf-8")
        original = json.loads(raw)
        scrubbed = _scrub_json(original, rows)
        if scrubbed == original:
            continue
        path.write_text(json.dumps(scrubbed, **_json_format(raw, original)), encoding="utf-8")
        changed += 1
        print(f"  scrubbed {path.name}")
    events = directory / "sca_events_clean.csv"
    if events.exists() and _mentions(events.read_text(encoding="utf-8"), rows):
        import pandas as pd
        df = pd.read_csv(events, dtype=str, keep_default_na=False)
        apply_to_events(df, rows)
        df.to_csv(events, index=False, quoting=csv.QUOTE_ALL)
        changed += 1
        print(f"  scrubbed {events.name}")
    return changed


def add(text: str, replace_with: str, lat: str, lng: str, note: str = "",
        path: Path | None = None) -> None:
    """Append a fingerprinted row. The address itself is never written."""
    path = path or CSV_FILE
    fp, n = fingerprint_text(text)
    if not n:
        raise SystemExit("That text has no words to match.")
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([fp, n, replace_with, lat, lng, note])
    print(f"Added a {n}-word blocklist entry ({fp[:8]}…) to {path.name}.")


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "add":
        add(*sys.argv[2:7])
    elif len(sys.argv) > 1:
        raise SystemExit(__doc__)
    else:
        print(f"Private-address scrub: {scrub_files()} file(s) changed.")
