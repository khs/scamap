"""
private_addresses.py
--------------------
Blocklist for addresses whose owners asked to be taken off the map (a private
home that hosts practices, say) but which upstream calendars still publish.

The blocklist must not itself publish the address, so the committed
private_addresses.csv stores only a one-way FINGERPRINT of it: the SHA-256 of
its words, lower-cased and joined ("123 Example Rd" -> "123|example|rd").
Matching slides a window of that many words over each event's text and
compares fingerprints, so case, spacing and punctuation differences between
calendars don't matter ("123 Example Rd." == "123  example rd").

The readable list lives ONLY on the maintainer's computer, in
private_addresses.local.csv (gitignored), which `add` keeps in step.

Commands (run from the project folder):
    python private_addresses.py add "<address as calendars write it>" \
        "<kingdom, or 'everywhere'>" <public lat> <public lng> "<note>"
    python private_addresses.py report     # what's blocked, and which events it hides
    python private_addresses.py            # scrub committed files (last refresh step)

private_addresses.csv columns:
    fingerprint   SHA-256 of the address's words (see above)
    words         how many words it has (the matching window size)
    scope         a kingdom ("Kingdom of Northshield"): only events from that
                  kingdom's calendars or its local groups' calendars are touched,
                  so a same-named venue elsewhere still shows. Blank = everywhere
                  (right for a full street address, which is unique anyway).
    replace_with  text put in place of the address in descriptions
    lat, lng      a deliberately coarse public pin for affected events
    note          who asked and when — no identifying detail

Affected events get location_specificity="private": the map shows "The
location of this event is private" instead of an address, at the coarse pin.

Used in two places, so a re-import can never bring the address back:
  * clean_sca_events (Step 6g) scrubs events and pins them before the geocoder
    ever sees the address;
  * the last refresh.py step scrubs every file the cron commits
    (sca_events_clean.csv, and all *_cache.json for unscoped entries — caches
    carry no calendar, so a scoped entry can't be judged there).

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
LOCAL_FILE = SCRIPT_DIR / "private_addresses.local.csv"   # gitignored plaintext
FIELDS = ["fingerprint", "words", "scope", "replace_with", "lat", "lng", "note"]
TEXT_COLUMNS = ("title", "location", "clean_location", "description")
PRIVATE_LOCATION = "Private location"
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
                   "scope": (r.get("scope") or "").strip(),
                   "replace_with": (r.get("replace_with") or "").strip() or "[private location]",
                   "note": (r.get("note") or "").strip()}
            if _find(row["replace_with"], row):
                # It would match again every run and grow without end.
                print(f"  WARNING: private_addresses.csv row {fp[:8]}…: replace_with "
                      f"contains the address itself — using '[private location]'")
                row["replace_with"] = "[private location]"
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


def load_source_kingdoms(locals_path: Path | None = None) -> dict:
    """Local group name -> its kingdom, from locals.csv. Kingdom calendars are
    their own kingdom (handled in event_kingdom)."""
    path = locals_path or SCRIPT_DIR / "locals.csv"
    out = {}
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                g, k = (r.get("group") or "").strip(), (r.get("kingdom") or "").strip()
                if g and k:
                    out.setdefault(g, k)
    return out


def event_kingdom(source: str, source_kingdoms: dict) -> str:
    source = str(source or "").strip()
    return source if source.startswith("Kingdom of ") else source_kingdoms.get(source, "")


def _in_scope(row: dict, kingdom: str) -> bool:
    return not row["scope"] or row["scope"].lower() == kingdom.lower()


def apply_to_events(df, rows: list[dict] | None = None, source_kingdoms: dict | None = None):
    """Scrub event text; mark affected events private and pin them at the
    row's coarse public spot (geocode_status "override": never geocoded)."""
    rows = load() if rows is None else rows
    if not rows:
        return df
    source_kingdoms = load_source_kingdoms() if source_kingdoms is None else source_kingdoms
    counts: dict = {}
    for idx in df.index:
        kingdom = event_kingdom(df.at[idx, "source"] if "source" in df.columns else "",
                                source_kingdoms)
        active = [r for r in rows if _in_scope(r, kingdom)]
        hit = None
        for col in TEXT_COLUMNS:
            if col in df.columns and active:
                new, h = scrub_text(str(df.at[idx, col]), active)
                if h:
                    df.at[idx, col] = new
                    hit = hit or h
        if not hit:
            continue
        counts.setdefault(hit["fingerprint"], []).append(str(df.at[idx, "title"]))
        for col in ("location", "clean_location"):
            if col in df.columns:
                df.at[idx, col] = PRIVATE_LOCATION
        if "location_specificity" in df.columns:
            df.at[idx, "location_specificity"] = "private"
        if hit["coords"]:
            df.at[idx, "lat"], df.at[idx, "lng"] = hit["coords"]
            df.at[idx, "geocode_status"] = "override"
        else:                               # no public pin given: show no pin
            df.at[idx, "lat"] = df.at[idx, "lng"] = ""
            df.at[idx, "geocode_status"] = "skipped"
    # Per-entry counts + titles in the run log, so over-matching is visible
    # (titles are public; the address is not printed).
    for fp, titles in counts.items():
        print(f"  Private entry {fp[:8]}…: {len(titles)} event(s): "
              + "; ".join(sorted(set(titles)))[:300])
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
    """Scrub sca_events_clean.csv (all entries, scope-aware) and every
    *_cache.json (unscoped entries only) in place."""
    rows = load() if rows is None else rows
    if not rows:
        return 0
    changed = 0
    global_rows = [r for r in rows if not r["scope"]]
    for path in sorted(directory.glob("*_cache.json")) if global_rows else []:
        raw = path.read_text(encoding="utf-8")
        original = json.loads(raw)
        scrubbed = _scrub_json(original, global_rows)
        if scrubbed == original:
            continue
        path.write_text(json.dumps(scrubbed, **_json_format(raw, original)), encoding="utf-8")
        changed += 1
        print(f"  scrubbed {path.name}")
    events = directory / "sca_events_clean.csv"
    if events.exists() and _mentions(events.read_text(encoding="utf-8"), rows):
        import pandas as pd
        df = pd.read_csv(events, dtype=str, keep_default_na=False)
        before = df.copy()
        apply_to_events(df, rows)
        if not df.equals(before):
            df.to_csv(events, index=False, quoting=csv.QUOTE_ALL)
            changed += 1
            print(f"  scrubbed {events.name}")
    return changed


def add(text: str, scope: str, lat: str, lng: str, note: str = "",
        replace_with: str = "[private location]",
        path: Path | None = None, local_path: Path | None = None) -> None:
    """Append a fingerprinted row to the committed blocklist, and the readable
    address to the maintainer's own (gitignored) local list."""
    path, local_path = path or CSV_FILE, local_path or LOCAL_FILE
    scope = "" if scope.strip().lower() in ("", "everywhere", "all", "*") else scope.strip()
    fp, n = fingerprint_text(text)
    if not n:
        raise SystemExit("That text has no words to match.")
    for p, header, row in ((path, FIELDS, [fp, n, scope, replace_with, lat, lng, note]),
                           (local_path, ["address", "fingerprint", "scope", "note"],
                            [text, fp, scope, note])):
        new = not p.exists()
        with open(p, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)
    print(f"Added a {n}-word entry ({fp[:8]}…, scope: {scope or 'everywhere'}) to "
          f"{path.name}; the readable address is in {local_path.name} (not committed).")


def report(directory: Path = SCRIPT_DIR) -> None:
    """Print every blocklist entry (readable where this computer has the local
    list) and the events it currently hides, so over-blocking is easy to spot."""
    rows = load(directory / CSV_FILE.name)
    local = {}
    lp = directory / LOCAL_FILE.name
    if lp.exists():
        with open(lp, encoding="utf-8", newline="") as f:
            local = {r["fingerprint"]: r["address"] for r in csv.DictReader(f)}
    import pandas as pd
    events = pd.read_csv(directory / "sca_events_clean.csv", dtype=str, keep_default_na=False)
    private = events[events.get("location_specificity", "") == "private"]
    # The raw import (not committed) still has the original text, so it shows
    # which entry each event matched and the surrounding words.
    raw_path = directory / "sca_events.csv"
    raw = pd.read_csv(raw_path, dtype=str, keep_default_na=False) if raw_path.exists() else None
    sk = load_source_kingdoms(directory / "locals.csv")
    print(f"{len(rows)} blocklist entr{'y' if len(rows) == 1 else 'ies'}:\n")
    for r in rows:
        name = local.get(r["fingerprint"], f"(fingerprint {r['fingerprint'][:8]}…; "
                                           f"address not on this computer)")
        print(f"- {name}\n    scope: {r['scope'] or 'everywhere'} | pin: "
              f"{', '.join(r['coords']) if r['coords'] else 'none'} | {r['note']}")
        if raw is not None:
            for _, e in raw.iterrows():
                if not _in_scope(r, event_kingdom(e["source"], sk)):
                    continue
                for col in ("title", "location", "description"):
                    spans = _find(str(e.get(col, "")), r)
                    if spans:
                        a, b = spans[0]
                        ctx = str(e[col])[max(0, a - 30):b + 30].replace("\n", " ")
                        print(f"    matches: {e['start'][:10]} {e['source']} | {e['title']} "
                              f"[{col}: …{ctx}…]")
                        break
    print(f"\n{len(private)} event(s) currently shown as private on the map:")
    for _, e in private.iterrows():
        print(f"  {e['start'][:10]}  {e['source']}  |  {e['title']}")
    if raw is None:
        print("\n(Run `python refresh.py` first to see which text each entry matched.)")


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "add":
        add(*sys.argv[2:7])
    elif sys.argv[1:] == ["report"]:
        report()
    elif len(sys.argv) > 1:
        raise SystemExit(__doc__)
    else:
        print(f"Private-address scrub: {scrub_files()} file(s) changed.")
