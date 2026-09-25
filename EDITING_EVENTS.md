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
| `new_event_url` | **What to change.** Replaces the event's link, e.g. the event's own website instead of the kingdom calendar entry. Must start with `http://` or `https://`. Use official sources (the event's or group's own site), not aggregator listings. |
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

## Fixing many events at once by keyword — `location_corrections.csv`

`event_overrides.csv` fixes **one event**. When a *whole recurring series* on a
calendar always lands in the wrong place — e.g. a barony's weekly "…Practice"
that the feed gives no address for, or that geocodes to the wrong spot — use
**`location_corrections.csv`** instead of adding a row per occurrence.

It pins **every event from a given calendar whose title contains a keyword** at
fixed coordinates, while still importing those events live (so cancellations and
time changes still flow through):

| Column | Purpose |
| --- | --- |
| `source` | The calendar's name, exactly as it appears in the `source` column of `sca_events_clean.csv` — usually the group name, e.g. `Province of the Mists` or `Barony of Bright Hills`. |
| `keywords` | A word/phrase matched **case-insensitively as a substring of the title**. `practice` matches "Archery Practice" and "Fighter Practices"; `Rockridge Bart` matches "Rockridge BART Fighter Practice". |
| `lat`, `lng` | The exact coordinates to pin matching events at (same as Google-Maps right-click coords). |
| `location` | *Optional.* An address that replaces the event's displayed location in the popup. Leave blank to keep the calendar's own text. |

Example rows:

| source | keywords | lat | lng | location |
| --- | --- | --- | --- | --- |
| Barony of Bright Hills | practice | 39.416485 | -76.505546 | |
| Province of the Mists | Rockridge Bart | 37.844514 | -122.252972 | Rockridge BART lot, Oakland, CA |

> **"Every event with X in the title" belongs here, not in `event_overrides.csv`.**
> An override row with only `match_source` filled in would move *every* event
> on that calendar, so the pipeline skips such rows with a warning.

- Matching events are pinned and marked `geocode_status = override`, so the
  geocoder leaves them alone — identical handling to an `event_overrides` pin.
- Because the match is **title keyword + source**, a virtual event (which lacks
  the physical practice's title keyword, or carries "online"/"zoom"/etc.) won't
  be pulled to the physical coordinates.
- Use `event_overrides.csv` for a single named event; use this file when the
  same fix should ride along with *every* matching event on a calendar.

Applied by `clean_sca_events.py` → `apply_location_corrections()` as **Step 6d**
(right after `event_overrides`). Tests: `test_location_corrections.py`.

### Automatic fallback for address-less baronial events

Separately, and with **no file to edit**: if a baronial event has no findable
address and doesn't look virtual, the pipeline pins it at that barony's own
coordinates (the same spot as its "?" placeholder pin) rather than dropping it or
flinging it to a state centroid. Such pins are marked `geocode_status =
ok_fallback` and `location_specificity = vague`, and the map shows a "we placed
this approximately — check with locals" note on the popup. A precise
`location_corrections` / `event_overrides` pin always wins over this fallback.

---

## Removing a private address — `private_addresses.csv`

When a site owner asks for their address to come off the map but calendars
still publish it, add it to the blocklist. Run this on your computer, writing
the address the way the calendars write it:

```bash
python private_addresses.py add "123 Example Rd" "everywhere" 42.85 -88.32 "Owner asked 2026-09"
python private_addresses.py add "The Nest" "Kingdom of Northshield" 42.85 -88.32 "Same site, venue name"
```

The arguments are: the address, the **scope**, a deliberately coarse public pin
(the town, a park), and a note.

- **Scope** = a kingdom name: only events from that kingdom's calendar or its
  local groups' calendars are affected, so a same-named venue elsewhere (a
  different "The Nest" in Texas) still shows. Use this for venue names.
  `everywhere` = any calendar; use it for a full street address, which is
  unique anyway and may be cross-listed by other kingdoms.
- On the map, affected events show **"The location of this event is private.
  Please inquire with the local group."** instead of an address, at your
  coarse pin. In descriptions the address becomes `[private location]`.
- **Where the list lives:** the committed `private_addresses.csv` stores only
  a **fingerprint** (one-way hash) of each address, so the public repo doesn't
  publish what it hides. The readable list is `private_addresses.local.csv`,
  which `add` writes **on your computer only** (git ignores it). Keep it; if
  it's lost, the fingerprints still work, but you'd only know each entry by
  its note.
- **Checking it isn't hiding too much:** `python private_addresses.py report`
  lists every entry (readable, from your local list), and every event the map
  currently shows as private. After a local `python refresh.py`, it also shows
  the exact text each entry matched in each event. Each refresh's log prints
  the event titles each entry hid.
- Every run it's re-applied before geocoding, and the last pipeline step
  scrubs unscoped entries from every committed `*_cache.json`, so a calendar
  that still publishes the address can't put it back.
- To remove an entry, delete its row from both CSVs.
- Limits: old git history still contains earlier copies; removing those needs
  a history rewrite. A fingerprint of a short, guessable phrase (a venue name)
  could be guessed, so treat it as "not displayed", not as a secret.

---

## Regional aggregator calendars — `type = aggregator` in `locals.csv`

Some calendars re-post other groups' events (a regional fighters' calendar, say).
Add them to `locals.csv` like any feed, with `type` = `aggregator` and
`location` = `No location` (so the aggregator gets no "?" pin of its own).

Each of its events is **dropped** if a group's own calendar already lists it:
same day, similar title (shared words covering at least half of the longer
title; "practice", "recurring", etc. ignored) and a compatible venue. What's
left is information the groups' own calendars don't carry, which stays on the
map under the aggregator's name.

---

## Big wars with their own websites — `wars.csv`

Gulf Wars, Pennsic and Lilies are listed on several kingdom calendars, each with
its own spelling, address (or none) and link. `wars.csv` gives each war's
permanent site and website **once**:

| Column | Purpose |
| --- | --- |
| `name` | Display name, used for the placeholder (below). |
| `title_keywords` | `\|`-separated phrases, matched as whole words in the title, case/punctuation-insensitive: `gulf wars\|gulf war`. |
| `host_kingdom` | Kingdom the event is filed under (its colour and kingdom filter). Must match the `source` spelling, e.g. `Kingdom of AEthelmearc`. |
| `location`, `lat`, `lng` | The war's permanent site. |
| `website` | The war's own site; the popup links it as "Event website →". |
| `fallback_start`, `fallback_end` | *Optional*, `MM-DD`. The usual time of year, used only when no kingdom calendar lists the war. |

What happens every run:

- **A kingdom calendar lists the war** (a kingdom event, 3+ days, title
  matches): all of that year's listings merge into **one** event. The dates
  come from the host kingdom's listing, or else the dates most kingdoms agree
  on. The site and website come from `wars.csv`. Shorter side events (e.g. a
  1-day "Gulf Wars Court") and baronial events are left alone.
- **No calendar lists it** for this year or next, and fallback dates are set:
  a placeholder spans the fallback range, and the popup warns that EventScout
  couldn't import the real dates. Once any kingdom posts the war, the real
  dates replace the placeholder automatically.

Adding a war is one row, with no code change.

---

## Quick reference for the maintainer

- File to edit: **`event_overrides.csv`** (committed; never auto-modified by the
  refresh job) — one row per event.
- Bulk-by-keyword file: **`location_corrections.csv`** (`source`, `keywords`,
  `lat`, `lng`) — one row fixes every matching event on a calendar.
- Code that applies them: `clean_sca_events.py` → `apply_event_overrides()` /
  `_override_matches()` (Step 6c) and `apply_location_corrections()` (Step 6d).
- Tests: `test_event_overrides.py`, `test_location_corrections.py`.
- A `geocode_status` of `override` means "human-pinned — do not geocode";
  `ok_fallback` means "auto-pinned at the barony's coords, location approximate."
