"""
health_check.py
---------------
Catch *silent* pipeline breakage. The refresh cron runs unattended every two
days; if a kingdom changes its calendar feed or a scraper endpoint moves, that
kingdom's events just vanish and the map still looks fine. This turns those
invisible failures into a loud warning.

Checks (against sca_events_clean.csv, the file the public site serves):
  - CRITICAL: a kingdom configured in calendars.csv produced 0 events
             (dead feed / changed format / broken scraper).
  - WARN:    a kingdom's event count dropped sharply vs the last run.
  - WARN:    total events dropped sharply vs the last run.
  - WARN:    the geocode failure rate spiked.

Baselines are stored in health_state.json (committed, updated each run) so a
sudden collapse is caught by comparing against the previous run.

In GitHub Actions it emits ::error:: / ::warning:: annotations (visible in the
run summary) but exits 0 by default, so the data commit is never blocked. Pass
--strict to exit non-zero on a CRITICAL finding.

Usage:
    python health_check.py            # report (exit 0)
    python health_check.py --strict   # exit 1 if any CRITICAL
"""
from __future__ import annotations

import collections
import csv
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
EVENTS_FILE = SCRIPT_DIR / "sca_events_clean.csv"
CALENDARS_FILE = SCRIPT_DIR / "calendars.csv"
STATE_FILE = SCRIPT_DIR / "health_state.json"
# Written iff there are CRITICAL findings; the refresh workflow turns it into /
# updates a GitHub issue, and removes-on-clean so the issue auto-closes. Not
# committed (gitignored) — it's a per-run signal, not site data.
ALERT_FILE = SCRIPT_DIR / "health_alert.md"

IN_CI = os.getenv("GITHUB_ACTIONS") == "true"

# Collected during a run so we can also write the alert file for the workflow.
_critical_msgs: list[str] = []
_warning_msgs: list[str] = []

# Tunables
KINGDOM_DROP_FRAC = 0.60   # warn if a kingdom loses >= this fraction vs last run
TOTAL_DROP_FRAC = 0.30     # warn if total events drop by >= this fraction
MIN_BASELINE = 10          # only relative-warn when the baseline was at least this
FAILRATE_WARN = 0.08       # warn if > this fraction of geocodable events failed


def emit(level: str, msg: str) -> None:
    """level: 'error' | 'warning' | 'notice'. Uses GitHub Actions annotation
    syntax in CI (visible in the run summary), plain text locally. Errors and
    warnings are also collected for the alert file."""
    if level == "error":
        _critical_msgs.append(msg)
    elif level == "warning":
        _warning_msgs.append(msg)
    if IN_CI:
        print(f"::{level}::{msg}")
    else:
        tag = {"error": "CRITICAL", "warning": "WARN", "notice": "INFO"}[level]
        print(f"  [{tag}] {msg}")


def _write_alert_file(total: int, failed: int, geocodable: int) -> None:
    """Write health_alert.md when there are CRITICAL findings, else remove it.
    The refresh workflow opens/updates a GitHub issue from this file, and
    closes the issue when the file is gone (feeds recovered)."""
    if _critical_msgs:
        lines = [
            "The automated SCAMap refresh found feed(s) producing **0 events**.",
            "These are almost certainly broken upstream calendars or scrapers, and",
            "the affected kingdom's events are missing from the live map until fixed.",
            "",
            "**Critical — feed produced no events:**",
            *[f"- {m}" for m in _critical_msgs],
        ]
        if _warning_msgs:
            lines += ["", "**Warnings:**", *[f"- {m}" for m in _warning_msgs]]
        lines += [
            "",
            f"_This run: {total} events total, {failed}/{geocodable} geocode failures._",
            "",
            "How to diagnose and fix: see **MAINTAINING.md → \"When a feed breaks\"**.",
        ]
        ALERT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        ALERT_FILE.unlink(missing_ok=True)


def configured_kingdoms() -> list:
    if not CALENDARS_FILE.exists():
        return []
    with open(CALENDARS_FILE, encoding="utf-8") as f:
        return [r["source"] for r in csv.DictReader(f) if r.get("type") == "kingdom"]


def main(strict: bool = False) -> int:
    _critical_msgs.clear()
    _warning_msgs.clear()
    if not EVENTS_FILE.exists():
        emit("error", f"{EVENTS_FILE.name} missing — the pipeline produced no output")
        _write_alert_file(0, 0, 0)
        return 1

    with open(EVENTS_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    counts = collections.Counter(r["source"] for r in rows)
    total = len(rows)
    statuses = collections.Counter(r.get("geocode_status") for r in rows)
    geocodable = sum(v for k, v in statuses.items() if k != "skipped")
    failed = statuses.get("failed", 0)
    failrate = failed / geocodable if geocodable else 0.0

    prev = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    prev_counts = prev.get("kingdom_counts", {})
    prev_total = prev.get("total", 0)

    print("=== Pipeline health check ===")
    print(f"  total events: {total}" + (f"  (prev {prev_total})" if prev_total else "  (no baseline yet)"))
    print(f"  geocode failures: {failed}/{geocodable} ({failrate:.1%})")

    kingdoms = configured_kingdoms()
    critical = warnings = 0

    # 1. A configured kingdom with zero events — almost certainly a dead feed.
    for k in sorted(kingdoms):
        if counts.get(k, 0) == 0:
            emit("error", f"{k}: 0 events — feed/scraper likely broken")
            critical += 1

    # 2. A kingdom that lost a big share of its events vs the last run.
    for k in sorted(kingdoms):
        now, before = counts.get(k, 0), prev_counts.get(k, 0)
        if before >= MIN_BASELINE and now < before * (1 - KINGDOM_DROP_FRAC):
            emit("warning", f"{k}: events {before} -> {now} ({100 * (before - now) // before}% drop)")
            warnings += 1

    # 3. Total event count collapse.
    if prev_total >= MIN_BASELINE and total < prev_total * (1 - TOTAL_DROP_FRAC):
        emit("warning", f"total events {prev_total} -> {total} "
                        f"({100 * (prev_total - total) // prev_total}% drop)")
        warnings += 1

    # 4. Geocode failure-rate spike.
    if failrate > FAILRATE_WARN:
        emit("warning", f"geocode failure rate {failrate:.1%} exceeds {FAILRATE_WARN:.0%} "
                        f"({failed}/{geocodable})")
        warnings += 1

    # Update the baseline for next time.
    STATE_FILE.write_text(json.dumps({
        "total": total,
        "failed": failed,
        "geocodable": geocodable,
        "kingdom_counts": {k: counts.get(k, 0) for k in kingdoms},
    }, indent=2, sort_keys=True), encoding="utf-8")

    _write_alert_file(total, failed, geocodable)

    print(f"\n  {critical} critical, {warnings} warning(s). "
          f"(baseline written to {STATE_FILE.name})")
    if critical and strict:
        return 1
    return 0


if __name__ == "__main__":
    # Never let a bug in the health checker block the data commit: on an
    # unexpected error, warn and exit 0. Only --strict + a real CRITICAL exits 1.
    try:
        code = main(strict="--strict" in sys.argv)
    except Exception as exc:  # noqa: BLE001
        emit("warning", f"health_check itself errored: {exc}")
        code = 0
    sys.exit(code)
