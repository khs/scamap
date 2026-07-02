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

import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent

# ── Single-instance lock ────────────────────────────────────────────────
# Two refreshes running at once race the CSVs/caches and double the Nominatim
# request rate — both proven harmful (2026-07-01: a concurrent pair corrupted
# sca_events_clean.csv and got the IP 403-rate-banned). The lock file holds the
# owner's PID; a lock left behind by a dead process is reclaimed automatically.
LOCK_FILE = SCRIPT_DIR / "refresh.lock"


def _pid_running(pid: int) -> bool:
    """Best-effort 'is this PID alive?' on Windows and POSIX."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        alive = (kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                 and code.value == STILL_ACTIVE)
        kernel32.CloseHandle(handle)
        return bool(alive)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> None:
    """Take the single-instance lock or exit(2). Reclaims dead-PID locks."""
    if LOCK_FILE.exists():
        try:
            holder = int(LOCK_FILE.read_text(encoding="ascii").strip())
        except (ValueError, OSError):
            holder = -1
        if holder != os.getpid() and _pid_running(holder):
            print(f"ERROR: another refresh.py is already running (PID {holder}).\n"
                  f"Concurrent refreshes corrupt sca_events_clean.csv and get the\n"
                  f"IP rate-banned by Nominatim. Wait for it to finish — or, if it\n"
                  f"is truly gone, delete {LOCK_FILE.name} and re-run.",
                  file=sys.stderr)
            sys.exit(2)
        LOCK_FILE.unlink(missing_ok=True)   # stale lock from a dead run
    LOCK_FILE.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(release_lock)


def release_lock() -> None:
    """Remove the lock if this process owns it (idempotent, crash-safe)."""
    try:
        if (LOCK_FILE.exists()
                and LOCK_FILE.read_text(encoding="ascii").strip() == str(os.getpid())):
            LOCK_FILE.unlink()
    except OSError:
        pass

STEPS = [
    # Fill blank lat/lng for groups in locals.csv that have a location (their
    # "?"-pin coords + the baronial fallback read these). Only touches blank rows —
    # hand-placed coords are never moved. Cached, so re-runs are cheap.
    ("Geocoding local groups missing coordinates",      "geocode_locals.py"),
    ("Fetching events from kingdom/baronial calendars", "ImportMaps.py"),
    ("Cleaning, deduping, and merging recurring events", "clean_sca_events.py"),
    # Replace placeholder feed descriptions (e.g. Atlantia's "Upcoming event in
    # … Event Flyer:") with the real text from the linked event page. Cached, so
    # only new events fetch. Runs before geocoding, which preserves the column.
    ("Enriching placeholder event descriptions",         "enrich_descriptions.py"),
    # Pin Gleann Abhann's location-less kingdom events (they only carry "MS") to
    # their hosting group, read from each event page's JSON-LD organizer. Cached
    # per URL so only new events fetch. Runs before geocoding, which then leaves
    # them alone (status "ok_organizer").
    ("Placing Gleann Abhann events by hosting group",    "enrich_gleann_locations.py"),
    ("Geocoding new addresses",                          "geocode_sca_events.py"),
    # Tag multi-day kingdom "wars" with the kingdom whose territory their site
    # sits in (point-in-polygon, once here instead of in every browser), so the
    # map can size + colour the oversized war pin straight from the war_host
    # column. Runs after geocoding because it needs the events' coordinates.
    ("Tagging war host kingdoms",                        "annotate_war_hosts.py"),
    # NOTE: group placeholder-pin locations now live in locals.csv (the lat/lng
    # columns), hand-maintained rather than geocoded, so build_group_pins.py is
    # no longer part of the pipeline.
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
    acquire_lock()
    for label, script in STEPS:
        if not run_step(label, script):
            print(f"\nFAILED at step: {label}", file=sys.stderr)
            sys.exit(1)
    print("\nRefresh complete.")


if __name__ == "__main__":
    main()
