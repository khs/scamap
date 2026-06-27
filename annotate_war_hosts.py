"""
annotate_war_hosts.py
---------------------
Post-geocode pipeline step: tag each multi-day kingdom "war" with the kingdom
whose TERRITORY its site sits in (its "hosting kingdom") in a `war_host` column on
sca_events_clean.csv. The map reads that column directly to size + colour the
oversized war pin — so the heavy point-in-polygon lookup runs ONCE here, not in
every visitor's browser.

A "war" is a kingdom-level event 5+ days long with "war"/"wars" in its title
(word-boundary, so "Warlord"/"Steward" don't count). `war_host` is blank for
everything else, and for a war whose site falls outside the mapped territories.

Run standalone (after geocoding) or via refresh.py:
    python annotate_war_hosts.py
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from kingdom_geo import kingdom_at_point

OUTPUT_FILE = Path(__file__).parent / "sca_events_clean.csv"
WAR_RE = re.compile(r"\bwars?\b", re.IGNORECASE)
MIN_WAR_DAYS = 5.0


def _parse(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        try:
            return datetime.fromisoformat(s[:10])
        except ValueError:
            return None


def is_war(title, start, end, calendar_type) -> bool:
    """Kingdom-level event, 5+ days long, with 'war'/'wars' in the title."""
    if (calendar_type or "").strip() != "kingdom":
        return False
    if not WAR_RE.search(title or ""):
        return False
    s, e = _parse(start), _parse(end or start)
    if not s or not e:
        return False
    return (e - s).total_seconds() / 86400.0 >= MIN_WAR_DAYS


def war_host_for(row: dict) -> str:
    """The hosting kingdom for a war row, or '' (non-war / no coords / off-map)."""
    if not is_war(row.get("title"), row.get("start"), row.get("end"),
                  row.get("calendar_type")):
        return ""
    try:
        lat, lng = float(row.get("lat")), float(row.get("lng"))
    except (TypeError, ValueError):
        return ""
    return kingdom_at_point(lat, lng) or ""


def main():
    if not OUTPUT_FILE.exists():
        print(f"  (no {OUTPUT_FILE.name} — nothing to annotate)")
        return
    df = pd.read_csv(OUTPUT_FILE, dtype=str, keep_default_na=False)
    df["war_host"] = [war_host_for(r) for r in df.to_dict("records")]
    df.to_csv(OUTPUT_FILE, index=False, quoting=csv.QUOTE_ALL)
    tagged = df[df["war_host"] != ""]
    hosts = sorted(tagged["war_host"].unique())
    print(f"Annotated war_host on {len(tagged)} war listing(s); hosts: {hosts}")


if __name__ == "__main__":
    main()
