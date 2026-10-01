# Editing & correcting events

This map is rebuilt automatically every couple of days from each kingdom's
public calendar. **Because of that, you cannot fix an event by editing
`sca_events_clean.csv` directly — your change is erased on the next refresh.**

To make a correction that *sticks*, add a row to **`corrections.csv`**. Every
pipeline run re-reads it and re-applies your corrections on top of the fresh
data, so they persist — and they keep working even if the kingdom later tweaks
the event's time or wording.

---

## TL;DR

1. Open **`corrections.csv`** in a spreadsheet (Excel, Google Sheets, or
   LibreOffice — it handles the comma-quoting for you).
2. Add one row. Decide **what it applies to**:
   - `event` — one event (found by its link, or its title on a calendar);
   - `keyword` — every event on a calendar whose title or location contains a
     word (e.g. a barony's weekly "…Practice" that always lands in the wrong place).
3. Fill in the new pin (`lat`, `lng`) and/or address (`location`), and
   optionally a better `link`. Save, commit, and push. The next refresh applies it.

---

## The columns

| Column | Purpose |
| --- | --- |
| `applies_to` | `event` (one event) or `keyword` (every matching event on a calendar). |
| `calendar` | The calendar's name exactly as in the `source` column of `sca_events_clean.csv`, e.g. `Kingdom of Meridies`, `Barony of Bright Hills`. Not needed when `match` is a link. |
| `match` | **`event`:** the event's link (best — survives title and date changes), e.g. `https://midrealm.org/events/smurf-shoot-4/`, or its title. Titles match **case- and punctuation-insensitively** ("Smurf Shoot 4" = "Smurf Shoot #4"). **`keyword`:** a word or phrase found anywhere in the title or location, case-insensitively (`practice` matches "Archery Practice"). |
| `date` | *Optional, `event` only.* `YYYY-MM-DD` to correct just one year of a title that repeats (this year's "Spring Coronation" but not next year's). Blank = every instance. |
| `lat`, `lng` | An exact pin in decimal degrees. In Google Maps, **right-click the spot → click the coordinates** to copy them (e.g. `39.9526, -75.1652`). Skips the geocoder. **Required for `keyword` rows.** |
| `location` | The address to show in the popup. On an `event` row with no `lat`/`lng`, it is also geocoded, so write something that resolves (`City, ST, USA` or a street address). |
| `link` | *Optional, `event` only.* Replaces the event's link, e.g. the event's own website. Must start with `http://` or `https://`. Use official sources (the event's or group's own site), not aggregator listings. |
| `note` | **Why**, briefly — for the next person (or future you). |

Examples:

| applies_to | calendar | match | date | lat | lng | location | link | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| event | Kingdom of Meridies | Greater Savannah Gathering | | | | Savannah, GA, USA | | "Greater Savannah area" wouldn't geocode |
| event | | https://midrealm.org/events/smurf-shoot-4/ | | 41.8781 | -87.6298 | | | Pinned to the real site per the steward |
| keyword | Barony of Bright Hills | practice | | 39.416485 | -76.505546 | | | Practices have no address in the feed |

> **Which one?** A one-off event, or a placeholder location a kingdom will
> later replace ("Crown Tourney — TBA") → `event`, ideally with its link. The
> same fix for every occurrence of a series → `keyword`. An `event` row with a
> calendar but no `match` would move every event on that calendar, so the
> pipeline skips it with a warning.

---

## What happens, and what the pipeline checks

`clean_sca_events.py` applies the `event` rows (Step 6c), then the `keyword`
rows (Step 6d) — after fetching, cleaning, de-duplicating and merging recurring
series, and **before** geocoding. A pinned event is marked
`geocode_status = override` and nothing later second-guesses it; an address-only
`event` correction is geocoded fresh.

You don't have to be perfect — bad input is caught, not silently mis-applied:

- A `lat`/`lng` that isn't a number, is out of range, or is half filled in is
  ignored with a `WARNING` (a `location` on the same row still applies).
- A row that changes nothing is skipped with a warning.
- A row that matches no event prints `Override matched nothing` in the run log
  (normal once a one-off event has passed — delete the row then).
- One broken row can't break the run.

**Finding an event's values:** open `sca_events_clean.csv` and read its
`event_url`, `source`, `title` and `start`. **Removing a correction:** delete
its row; the next run reverts to the calendar's data. (The old
`event_overrides.csv` / `location_corrections.csv` files were merged into
`corrections.csv`; if an old copy reappears, it is still read.)

---

## Automatic fallback for address-less baronial events

Separately, and with **no file to edit**: if a local group's event has no
findable address (including calendars that give no location at all) and isn't
online, the pipeline pins it at that group's own coordinates in `locals.csv`
(the same spot as its "?" placeholder pin, which then disappears because the
group has events on the map). Such pins are marked `geocode_status =
ok_fallback` and `location_specificity = vague`, and the popup says: "This
group's calendar does not include precise location information - please check
with the local group for details." A pin from `corrections.csv` always wins over this fallback. If the group has no
coordinates in `locals.csv`, the event can't be placed and doesn't show.

### Local groups' online events

A local group's online meetings (Zoom, Google Meet, "virtual", …) are imported
too. They never get a map pin; with **Online** ticked above the event list
they're listed only when the map is zoomed in (zoom 8+, `LOCAL_ONLINE_MIN_ZOOM`
in `index.html`) with that group's `locals.csv` pin in view, since they matter
to people nearby. Kingdom-level online events are listed at any zoom.

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

- File to edit: **`corrections.csv`** (committed; never auto-modified by the
  refresh job) — `applies_to` = `event` (one event) or `keyword` (every match).
- Code that applies it: `clean_sca_events.py` → `_corrections_rows()`, then
  `apply_event_overrides()` (Step 6c) and `apply_location_corrections()` (Step 6d).
- Tests: `test_event_overrides.py`, `test_location_corrections.py`.
- A `geocode_status` of `override` means "human-pinned — do not geocode";
  `ok_fallback` means "auto-pinned at the barony's coords, location approximate."
