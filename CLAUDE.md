# Notes for Claude (EventScout / SCAMap)

Read README.md (pipeline), MAINTAINING.md (operations), EDITING_EVENTS.md (all
hand-edited data files) and ROADMAP.md (plan + decisions) before larger work.

## Working with the maintainer
- **UI changes (index.html etc.): test locally first, push only after the
  maintainer has tried them.** They run `python -m http.server 8766` in this
  folder and open http://localhost:8766 (localhost, so "Near me" works).
  Pipeline/data fixes and things they explicitly approve can be pushed directly.
- Explain in plain language; the maintainer is not a confident git user.
  Approvals for any future submission workflow happen on GitHub's website
  (pull requests), never via git commands.
- Links and citations for corrections must come from **official sources** (the
  event's, group's or kingdom's own site, venue site, government/OSM
  geocoders), never aggregators like allevents.in.
- Anything that waits for the maintainer's personal OK: say so clearly; don't
  push it.

## Environment (maintainer's Windows machine)
- PowerShell 5.1. `git` is not on PATH: use GitHub Desktop's bundled git,
  `%LOCALAPPDATA%\GitHubDesktop\app-<version>\resources\app\git\cmd\git.exe`.
  Pushing works through its credential manager.
- `python -c "..."` with nested quotes gets mangled by PowerShell: write a
  script file to the scratchpad and run that instead.
- **Always `git pull --rebase` before committing**: the refresh cron commits
  data every 2 days. If a rebase conflicts on generated data
  (sca_events_clean.csv, *_cache.json), take the cron's version
  (`git checkout --ours` during a rebase) and re-run anything that rewrites it
  (e.g. `python private_addresses.py`).
- The maintainer edits CSVs in LibreOffice: if git can't write a file, check
  whether it's open there; never overwrite their edits. Before writing to
  locals.csv, make sure it's not open and edit only the needed lines
  (find_calendars.apply_to_locals shows the line-level approach) so the
  rest of the file stays byte-identical.

## Data rules
- Never hand-edit sca_events_clean.csv except in an emergency; fix data via
  corrections.csv, locals.csv, wars.csv, hardcoded_events.csv, etc.
- Privacy: private_addresses.csv stores fingerprints only; the readable list
  is private_addresses.local.csv (gitignored, maintainer's machine only).
  rejected_calendars.csv (fingerprints) lists calendars ruled out by hand.
  Never commit a private address, a personal calendar ID, or real private
  data in tests (use made-up addresses).
- For pipeline refactors, prove no unintended data changes: run
  clean_sca_events.main() offline on the same input before and after and
  compare outputs (a byte-identical result is the bar).
- Run the full test suite before every push:
  `python -m unittest discover -p "test_*.py"`.

## Behaviour worth remembering (decided with the maintainer)
- Local groups' online events are imported; they never get a pin and are
  listed (with "Online" ticked) only when zoomed in (zoom 8+) near the group.
- Local events with no location are pinned at the group's locals.csv spot
  with the "This group's calendar does not include precise location
  information…" warning.
- Recurring-series merging is per calendar (source); online events are never
  de-duplicated by their "Zoom"/"Online"/"Discord" location; a barony's copy of
  its kingdom's event (same day + venue + similar title) is dropped.
- Big cross-listing pins need 4+ calendars of which 3+ are kingdom calendars.
- Search respects the filters; when it shows 0-2 results but filters hide more,
  a note under the search box explains and offers "Show all matches".
- No per-date changes inside a recurring series (would need a pipeline change;
  the maintainer decided against it).
