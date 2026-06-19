# Editing & correcting events

This map is rebuilt automatically every couple of days from each kingdom's
public calendar. **Because of that, you cannot fix an event by editing
`sca_events_clean.csv` directly — your change is erased on the next refresh.**

To make a correction that *sticks*, add a row to **`event_overrides.csv`**.
Every pipeline run re-reads that file and re-applies your corrections on top of
the fresh data, so they persist — and they keep working even if the kingdom
later tweaks the event's title, time, or wording.

The most common use is fixing a **vague or wrong location** ("the Greater
Atlanta area", "Bob's farm", a typo'd street) so the pin lands in the right
place.

---

## TL;DR

1. Open **`event_overrides.csv`** in a spreadsheet (Excel, Google Sheets, or
   LibreOffice — this handles the comma-quoting for you).
2. Add one row. Fill in **how to find the event** and **what to change**:
   - Find it by `match_event_url` (best), or by `match_source` + `match_title`.
   - Change it with `new_location` (an address that will geocode), and/or
     `new_lat` + `new_lng` (an exact pin).
3. Save, commit, and push. The next refresh applies it. Done.

---

## The columns

| Column | Purpose |
| --- | --- |
| `match_event_url` | **How to find the event (best).** The event's permalink, e.g. `https://midrealm.org/events/smurf-shoot-4/`. The most durable match — it survives changes to the title, date, and location. Copy it from the event's popup on the map or from the `event_url` column in `sca_events_clean.csv`. |
| `match_source` | **How to find the event (fallback).** The exact kingdom name, e.g. `Kingdom of Meridies`. Use when the event has no URL. |
| `match_title` | **How to find the event (fallback).** The event title. Matched **case- and punctuation-insensitively**, so small wording tweaks ("Smurf Shoot 4" vs "Smurf Shoot #4") still match. |
| `match_date` | *Optional.* `YYYY-MM-DD`. Only needed to pin **one year** of a title that repeats annually (e.g. correct *this* year's "Spring Coronation" but not next year's). Leave blank to match every instance. |
| `new_location` | **What to change.** A corrected address string. It replaces the displayed location **and** is fed to the geocoder, so write something it can resolve — ideally `City, ST, USA` or a full street address. |
| `new_lat`, `new_lng` | **What to change.** Exact coordinates in decimal degrees (e.g. `39.9526` / `-75.1652`). Use these when no address will geocode cleanly. They drop the pin precisely and skip the geocoder. |
| `note` | **Why.** A short human explanation. Always fill this in — it's the message to the next person (or future you) about what was wrong and what you did. |

> **Match rule:** if `match_event_url` is filled, it alone identifies the
> event. Otherwise **all** of the `match_source` / `match_title` / `match_date`
> fields you filled in must match. Never match on the location — that's the
> thing you're correcting.

---

## How to fix a location — two ways

### A. Give it a better address (preferred)

When the location is just vaguely worded but a real place exists, supply a
clean address in `new_location` and let the geocoder find it.

| match_event_url | match_source | match_title | match_date | new_location | new_lat | new_lng | note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | Kingdom of Meridies | Greater Savannah Gathering | | Savannah, GA, USA | | | "Greater Savannah area" wouldn't geocode; set to the city |

### B. Drop an exact pin (when nothing geocodes)

When the venue is a private home, a field with no address, or anything a
geocoder can't resolve, get the coordinates yourself and put them in
`new_lat` / `new_lng`:

1. Open [Google Maps](https://www.google.com/maps), find the spot.
2. **Right-click the exact location → click the coordinates** at the top of
   the menu (e.g. `39.9526, -75.1652`) to copy them.
3. Put the first number in `new_lat`, the second in `new_lng`.

| match_event_url | match_source | match_title | match_date | new_location | new_lat | new_lng | note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| https://midrealm.org/events/smurf-shoot-4/ | | | | | 41.8781 | -87.6298 | Org only listed "the cabbage patch"; pinned to the real site per the steward |

You can also fill in **both** `new_location` *and* `new_lat`/`new_lng`: the
text is corrected for the popup, and your exact pin is used as-is.

---

## Why corrections survive upstream edits

Kingdom stewards edit their calendar entries — fixing a typo, moving a start
time, rewording the venue. A correction keyed on something stable keeps
working through those edits:

- **`match_event_url` is the most durable.** A kingdom's per-event page URL
  (e.g. `…/events/smurf-shoot-4/`) almost never changes even when the event's
  details do. Prefer it whenever the event has one.
- **`match_title` is compared normalized** (lower-cased, punctuation stripped),
  so cosmetic title edits don't break the match.
- **Leaving `match_date` blank** matches every instance, so an annual event
  whose venue is always the same only needs one override, forever.

If a kingdom renames an event so drastically that the title no longer matches
*and* it has no URL, the override will simply stop applying (you'll see a
"matched nothing" note in the run log) — update the row and it's fixed again.

---

## Where this runs in the pipeline

`event_overrides.csv` is read by `clean_sca_events.py` as **Step 6c**, which
runs:

- **after** events are fetched, cleaned, de-duplicated, and recurring series
  merged,
- **after** carry-forward restores any kingdom whose feed failed that run, and
- **before** geocoding —

so a `new_location` is geocoded fresh, and a `new_lat`/`new_lng` pin is marked
`geocode_status = override` and left untouched by the geocoder. Nothing later
in the pipeline second-guesses a human correction.

---

## What the pipeline checks for you

You don't have to be perfect — bad input is caught, not silently mis-applied:

- **Bad coordinates are ignored.** A `new_lat`/`new_lng` that isn't a number,
  is out of range (lat must be −90…90, lng −180…180), or is only half filled
  in (lat without lng) is rejected with a `WARNING` instead of dropping a pin
  in the wrong place. If you also gave a `new_location`, that still applies.
- **A row that changes nothing is skipped** with a warning (e.g. you filled in
  the match fields but forgot the `new_*` fields).
- **A row that matches no event** prints `Override matched nothing` — re-check
  the URL or title.
- **One broken row can't break the run** — it's logged and the rest still apply.

Watch the run log for these `WARNING` lines after you push a change.

## Verifying & maintaining

- **Check it applied:** after a refresh, the run log prints a line per override,
  e.g. `Override applied to 1 event(s): …`. A `matched nothing` line means the
  match fields don't line up with any current event — re-check the URL/title.
- **Find an event's match values:** open `sca_events_clean.csv` and read the
  `event_url`, `source`, `title`, and `start` columns for the row you want.
- **Remove a correction:** delete its row (or comment it out by putting `#` at
  the very start of the first cell). The next run reverts to the upstream data.
- **Commas in a value** (addresses, notes) must be quoted in raw CSV
  (`"Savannah, GA, USA"`). Editing in a spreadsheet does this automatically —
  strongly recommended.
- Lines whose first cell starts with `#` are treated as comments and skipped.

---

## Quick reference for the maintainer

- File to edit: **`event_overrides.csv`** (committed; never auto-modified by the
  refresh job).
- Code that applies it: `clean_sca_events.py` → `apply_event_overrides()` /
  `_override_matches()` (Step 6c).
- Tests: `test_event_overrides.py`.
- A `geocode_status` of `override` means "human-pinned — do not geocode."
