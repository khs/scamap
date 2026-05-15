"""
refresh.py
----------
Runs the full pipeline end-to-end: fetch → clean → geocode. Use this to
refresh the data on a schedule (cron, Windows Task Scheduler, GitHub Action).

The cleaner preserves prior geocoding results, so the geocoder only hits
Nominatim/Photon for events that are new or had their address change. A
typical incremental refresh takes 1–3 minutes total once the cache is warm.

Exit code is non-zero if any step fails.

Usage:
    python refresh.py
"""

import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

STEPS = [
    ("Fetching events from kingdom/baronial calendars", "ImportMaps.py"),
    ("Cleaning, deduping, and merging recurring events", "clean_sca_events.py"),
    ("Geocoding new addresses",                          "geocode_sca_events.py"),
    # Group-level placeholder pins (so baronies without events still show on
    # the map with a "visit their website" link). Cached; only geocodes new
    # or relocated groups. Safe to skip if group_locations.csv is missing.
    ("Building group placeholder pins",                  "build_group_pins.py"),
]


def run_step(label: str, script: str) -> bool:
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    started = time.time()
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script)],
        cwd=SCRIPT_DIR,
        env={"PYTHONIOENCODING": "utf-8", **__import__("os").environ},
    )
    elapsed = time.time() - started
    print(f"\n[{label}] finished in {elapsed:.0f}s (exit {result.returncode})")
    return result.returncode == 0


def main():
    for label, script in STEPS:
        if not run_step(label, script):
            print(f"\nFAILED at step: {label}", file=sys.stderr)
            sys.exit(1)
    print("\nRefresh complete.")


if __name__ == "__main__":
    main()
