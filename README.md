# SCAMap / EventScout

An interactive map of upcoming [SCA](https://www.sca.org/) events. A Python
pipeline pulls each kingdom's public calendar, cleans and geocodes the events,
and a Leaflet map (`index.html`) plots them. Hosted on GitHub Pages; the data
refreshes itself on a schedule.

## Correcting an event (start here for fixes)

If an event is in the wrong place or has a vague location, **don't edit
`sca_events_clean.csv`** — it's regenerated every run. Add a row to
**`event_overrides.csv`** instead. Full guide:

➡️ **[EDITING_EVENTS.md](EDITING_EVENTS.md)**

## Running or handing off the project

Operating it, troubleshooting a broken feed, and the owner's hand-off
checklist (repo transfer, the Nominatim contact secret, monitoring):

➡️ **[MAINTAINING.md](MAINTAINING.md)**

## How the data pipeline works

`refresh.py` runs five steps in order (each is a standalone script you can also
run on its own):

| Step | Script | What it does |
| --- | --- | --- |
| 1 | `ImportMaps.py` | Fetch every calendar in `calendars.csv` (kingdoms) and `locals.csv` (local groups) — direct ICS feeds + the scraper adapters in `scrapers.py` — expand recurring events, write `sca_events.csv`. Records any feeds that failed to fetch in `fetch_failures.json`. |
| 2 | `clean_sca_events.py` | Clean text, de-duplicate, merge recurring series, carry forward last-good events for failed feeds, **apply `event_overrides.csv`**, backfill per-event URLs → `sca_events_clean.csv`. |
| 3 | `enrich_descriptions.py` | Replace placeholder feed descriptions (Atlantia, East, Artemisia PDFs) with the real write-up from the linked event page. |
| 4 | `geocode_sca_events.py` | Geocode addresses via Nominatim (+ Photon fallback), caching results in `geocode_cache.json`. Skips events a human pinned (`geocode_status = override`). |
| 5 | `build_group_pins.py` | Placeholder pins for SCA groups so baronies with no current events still appear. |

Run the whole thing locally:

```bash
pip install -r requirements.txt
python refresh.py
```

The geocoder needs a contact email for Nominatim's User-Agent — set
`NOMINATIM_CONTACT_EMAIL` in a local `.env` (see `.env.example`).

## Automatic refresh

`.github/workflows/refresh.yml` runs `refresh.py` every two days, then commits
the updated data files and caches. `health_check.py` flags silent breakage (a
kingdom dropping to 0 events, a sharp drop, a geocode-failure spike) as
annotations in the Actions run summary.

## Key files

| File | Role |
| --- | --- |
| `calendars.csv` | Kingdom feed list — one row per kingdom (`id,source,type`). |
| `locals.csv` | Local-group registry — one row per known local group (barony/shire/canton/college/…): its calendar feed (or `No Calendar Listed`), plus type/website/social/`date_last_checked`. Add a group here, no code change needed. |
| `event_overrides.csv` | Hand-maintained event corrections (see EDITING_EVENTS.md). |
| `sca_events_clean.csv` | The cleaned, geocoded events the map serves. **Generated — don't hand-edit.** |
| `group_pins.csv`, `*.topojson` | Group placeholder pins and the kingdom-boundary overlay. |
| `*_cache.json` | Committed caches (geocode, descriptions, per-scraper) so re-runs and the cron stay cheap and polite to upstream APIs. |
| `index.html` | The Leaflet front-end (map, sidebar, filters). |
| `reference/` | One-off scripts kept for posterity (not part of the pipeline). See its README. |

## Tests

Offline unit tests across the pipeline:

```bash
python -m unittest discover -p "test_*.py"
```
