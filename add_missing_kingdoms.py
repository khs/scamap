"""
One-shot helper: re-runs just the kingdom scrapers that weren't included or
were broken when group_locations.csv was last built, and merges the new rows
into the existing CSV. Lets us iterate on individual scrapers without paying
the cost of the full ~30-minute Lochac/Middle/Northshield re-fetch.

Usage:
    python add_missing_kingdoms.py
"""
from __future__ import annotations

import csv
from pathlib import Path

from build_group_locations import (
    scrape_avacal,
    scrape_outlands,
    scrape_artemisia,
    scrape_west,
    scrape_calontir,
    scrape_ansteorra,
    scrape_drachenwald,
    scrape_antir,
    scrape_lochac,
    scrape_ealdormere,
    scrape_middle,
    scrape_atenveldt,
    scrape_northshield,
)

CSV_FILE = Path(__file__).parent / "group_locations.csv"

# Kingdom -> scraper for the runs we want to refresh
KINGDOMS_TO_REFRESH = {
    "Kingdom of Avacal":       scrape_avacal,
    "Kingdom of the Outlands": scrape_outlands,
    "Kingdom of Artemisia":    scrape_artemisia,
    "Kingdom of the West":     scrape_west,
    "Kingdom of Calontir":     scrape_calontir,
    "Kingdom of Ansteorra":    scrape_ansteorra,
    "Kingdom of Drachenwald":  scrape_drachenwald,
    "Kingdom of An Tir":       scrape_antir,
    "Kingdom of Lochac":       scrape_lochac,
    "Kingdom of Ealdormere":   scrape_ealdormere,
    "Kingdom of the Middle":   scrape_middle,
    "Kingdom of Atenveldt":    scrape_atenveldt,
    "Kingdom of Northshield":  scrape_northshield,
}


def main():
    # Read existing rows, drop any that belong to the kingdoms we're refreshing
    with open(CSV_FILE, encoding="utf-8") as f:
        existing = [row for row in csv.DictReader(f)
                    if row["kingdom"] not in KINGDOMS_TO_REFRESH]
    print(f"Keeping {len(existing)} existing rows from other kingdoms")

    new_rows: list[dict] = []
    for kingdom, scraper in KINGDOMS_TO_REFRESH.items():
        print(f"\n=== {kingdom} ===")
        try:
            results = scraper()
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue
        for entry in results:
            if len(entry) == 2:
                group, location = entry
                website = ""
            else:
                group, location, website = entry
            new_rows.append({
                "kingdom":  kingdom,
                "group":    group,
                "location": location,
                "website":  website or "",
            })

    all_rows = existing + new_rows
    # Sort by kingdom for stable output
    all_rows.sort(key=lambda r: (r["kingdom"], r["group"]))

    print(f"\nWriting {len(all_rows)} total rows to {CSV_FILE.name} "
          f"({len(new_rows)} new from refresh, {len(existing)} preserved)")
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["kingdom", "group", "location", "website"],
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(all_rows)


if __name__ == "__main__":
    main()
