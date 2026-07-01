"""One-time cleanup so the new Canadian-postal geocoding takes effect.

The An Tir / BC events are cached at the bogus point 48.5293305, -123.4666294
with status 'ok_retry' (a success status), so a normal refresh would carry them
forward and never re-geocode. Blank those rows + purge the poisoned cache keys so
the refresh re-runs them through the new postal-code path. Safe to re-run.
"""
import csv
import json
from pathlib import Path

HERE = Path(__file__).parent
EVENTS = HERE / "sca_events_clean.csv"
CACHE = HERE / "geocode_cache.json"
BOGUS_LAT, BOGUS_LNG = "48.5293305", "-123.4666294"
BOGUS_LAT_F = 48.5293305

# 1. Blank the mislanded rows so the geocoder re-attempts them.
with open(EVENTS, encoding="utf-8", newline="") as f:
    reader = csv.DictReader(f)
    fields = reader.fieldnames
    rows = list(reader)
blanked = 0
for r in rows:
    if r.get("lat", "").strip() == BOGUS_LAT and r.get("lng", "").strip() == BOGUS_LNG:
        r["lat"] = ""
        r["lng"] = ""
        r["geocode_status"] = ""
        blanked += 1
with open(EVENTS, "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
print(f"Blanked {blanked} mislanded BC row(s) in {EVENTS.name}")

# 2. Purge cache keys that resolved to the bogus point.
cache = json.loads(CACHE.read_text(encoding="utf-8"))
poisoned = [k for k, v in cache.items() if isinstance(v, dict) and v.get("lat") == BOGUS_LAT_F]
for k in poisoned:
    del cache[k]
CACHE.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True), encoding="utf-8")
print(f"Purged {len(poisoned)} poisoned cache key(s) from {CACHE.name}")
