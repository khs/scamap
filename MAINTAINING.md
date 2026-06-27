# Maintaining SCAMap

This is the operator's guide for whoever runs the map. It's built to need almost
no attention: the data refreshes itself and recovers from most failures on its
own. Read [README.md](README.md) first for what the project is and how the
pipeline fits together; this doc is about **keeping it running and handling the
two things that ever land on your desk** — a person reporting a problem, and a
feed breaking.

---

## The short version

- **Day to day there is nothing to do.** A GitHub Actions cron rebuilds the data
  every 2 days and commits it; GitHub Pages serves it.
- **You act on exactly one signal:** a GitHub **issue labelled `pipeline-health`**
  opens automatically when a kingdom's feed produces 0 events, and closes itself
  when the feed recovers. Watch the repo so you're emailed when one opens.
- **Almost every "my event is wrong" report is not yours to fix** — the map
  mirrors the kingdom's own calendar, so the fix is made there. The public
  [About](about.html) and [How to Help](how-to-help.html) pages already say this,
  which deflects most reports before they reach you.

---

## When someone reports a wrong or missing event

Work down this list; you'll almost always stop at step 1.

1. **Is the event wrong on the kingdom's own calendar too?** Then it's not a map
   problem. Point them at the official calendar and tell them to have their
   group's calendar steward / webminister / seneschal fix it there — it reaches
   the map within a couple of days. (This is exactly what the About page tells
   them, so you can usually just link it.)

2. **Is only the location/pin wrong**, and the kingdom can't or won't improve the
   address? Add a correction yourself — this is the one case you actually touch.
   Edit **`event_overrides.csv`** following **[EDITING_EVENTS.md](EDITING_EVENTS.md)**:
   give a better address, or drop an exact pin. It persists and survives the
   kingdom editing the event.

3. **Is the event genuinely missing**, though it's on the kingdom calendar? Check
   whether that whole kingdom is dark (see the next section) — a broken feed
   drops all of a kingdom's events at once.

You never need to hand-edit `sca_events_clean.csv`; it's regenerated every run.

---

## When a feed breaks (the `pipeline-health` issue opens)

The issue body lists which kingdom(s) produced 0 events. To diagnose, open the
latest **Actions → "Refresh events"** run and read that kingdom's
`Fetching […]` line. Common causes, most to least likely:

- **The site is just down or slow this run.** The pipeline already carries that
  kingdom's last-good upcoming events forward, so the map isn't blank. It
  usually fixes itself on the next run and the issue auto-closes. Only dig in if
  it stays broken across several runs.
- **The kingdom changed or moved its calendar feed.** Find the new feed and
  update that kingdom's row in **`calendars.csv`** (`id,source,type`). For a
  Google Calendar that's the calendar's ICS id/URL; for a WordPress site it may
  need a scraper prefix — `scrapers.py` lists the supported prefixes, and
  `reference/probe_baronial_calendars.py` can hunt for a feed.
- **A WAF is blocking the run** (a `403`, or a `non-JSON 200` challenge in the
  log). The pipeline automatically retries through a real browser TLS
  fingerprint (`curl_cffi`), which clears Calontir and the Middle. If a *new*
  kingdom is hard-blocked even so, it's in the same boat as An Tir (see below) —
  it may not be scrapable from CI.
- **A malformed upstream date.** Handled automatically now (one bad event is
  skipped instead of taking down the feed), so this shouldn't reach you — but if
  the log mentions it, that's why only one event went missing, not the kingdom.

To re-run on demand: **Actions → Refresh events → Run workflow**.

### Updating An Tir

An Tir sits behind Cloudflare, which blocks the cron's datacenter IP — so the
cron **can't** fetch it automatically. But a normal **web browser** passes
Cloudflare with no trouble, and An Tir publishes a standard calendar file. So
keeping An Tir on the map is just: download that file in your browser and drop
it into the repo. It needs no scripts and no code — the pipeline reads it like
any other feed.

**To update An Tir (takes about a minute, do it whenever you like — monthly is
plenty):**

1. In your browser, go to **https://antir.org/events.ics** . Wait for the
   "Just a moment…" Cloudflare screen to pass. The browser will download a file
   (usually named `events.ics`), or show calendar text you can save with
   **Ctrl-S / ⌘-S**.
2. **Save / rename it to `antir.ics`** and put it in the SCAMap project folder,
   replacing the placeholder `antir.ics` that's already there.
   - Doing it on GitHub instead? Open `antir.ics` in the repo → the pencil
     (Edit) → paste in the downloaded file's contents → Commit.
3. Commit and push (`git add antir.ics && git commit -m "Update An Tir" &&
   git push`), or just commit on GitHub.

That's it. The next refresh (within two days) folds An Tir's events into the
map, geocoded and coloured like every other kingdom. Until you first add real
data, An Tir simply shows nothing — that's expected, and it does **not** trip
the health alarm.

How it works: `calendars.csv` has the row `file:antir.ics,Kingdom of An Tir,
kingdom`. The `file:` prefix tells the pipeline to read that committed file
instead of fetching a URL, so it works from the cron with no network.

If your browser won't give you a clean `.ics` (some open it in a calendar app),
`reference/fetch_antir_with_cookie.py` is a fallback: it uses a `cf_clearance`
cookie you copy from your browser's DevTools to pull `antir.org/events.ics` via
`curl_cffi`. Its header comment has the step-by-step; save its output over
`antir.ics`.

---

## Adding or removing a group

Two files, one row per feed; no code change is needed for a normal
Google-Calendar or ICS feed:

- **Kingdoms → `calendars.csv`** — columns `id,source,type` (`type` is
  `kingdom`). Add or delete a row.
- **Local groups → `locals.csv`** — a registry of every local group we know of
  (baronies, shires, cantons, colleges, …), columns
  `kingdom,group,type,calendar_id,website,social,date_last_checked,location,lat,lng`.
  One row per group; `calendar_id` is its feed, or one of the recognised "no
  feed" notes — `No Calendar Listed` (we haven't looked), `No Calendar Available`
  (checked, it has none), or `Practices Added Manually` (no feed, but you've
  hand-entered its practices in `hardcoded_events.csv`) — all of which keep the
  row, skip the import, and still show a "?" pin so you can track it. Put `No
  location` in
  `calendar_id` on a *secondary* feed for a group to import its events without a
  second pin. `location`/`lat`/`lng` place that "?" pin (the map reads them
  directly — no geocoding). Only `group` + a real `calendar_id` drive the
  import; `type`/`website`/`social` are reference info. Bump `date_last_checked`
  (YYYY-MM-DD) whenever you re-verify a row. This is the one place to manage
  local groups.

For either file, the `id`/`calendar_id` is the Google-Calendar ICS id/URL; a
WordPress site may need a scraper prefix (`scrapers.py` lists them, and
`reference/probe_baronial_calendars.py` can hunt for a feed). Source/group
names must match the names used elsewhere (the colour map in `index.html`, the
home-state tables in `kingdoms.py`). After adding, run the workflow once and
check the new group's events appear.

---

## Publishing your change (no Claude, no credits)

Everything here runs locally with the free scripts — you never need Claude to
ship an update. Which scripts depends on what you edited:

**You added events to `hardcoded_events.csv`** (a static practice page, with
hand-entered `lat`/`lng`). The map reads this file *directly* in the browser —
no pipeline, no geocoding. Just commit and push it:

```bash
git pull --rebase
git add hardcoded_events.csv
git commit -m "Add Foo practice events"
git push
```

**You added or fixed a feed in `locals.csv`** (or `calendars.csv`). Run the
pipeline once so the new feed's events are imported and its addresses geocoded,
then push what changed:

```bash
git pull --rebase                  # take the cron's latest first
python refresh.py                  # imports every feed, geocodes only NEW addresses
git add locals.csv calendars.csv sca_events_clean.csv \
        geocode_cache.json description_cache.json
git commit -m "Add/refresh local calendars"
git push
```

(Drop `calendars.csv` from the `add` if you didn't touch it. Adding the two
`*_cache.json` files is what keeps the next run — and the cron — from redoing
work you already paid for.)

`refresh.py` is **incremental**: `geocode_cache.json` remembers every address it
has ever resolved and the geocoder skips rows already marked done, so a re-run
only calls Nominatim for genuinely *new* addresses — it does **not** re-geocode
the whole map. That's exactly the re-attempt path when you've only *fixed a
broken link*: correct the `calendar_id`, re-run `refresh.py`, and just that
feed's events get (re)imported and geocoded while everything else is served from
cache. (A stray trailing space in a pasted ICS URL is fine — the importer trims
it before fetching.)

Prefer not to run anything? Commit the `locals.csv` edit on its own: the 2-day
cron runs `refresh.py` in GitHub Actions (on its own IP) and imports the new
feed within ~48 hours. Running it yourself just makes it instant.

---

## Editing the kingdom-colour overlay

The "Colour background by kingdom" overlay is driven by
**`territory_kingdoms.csv`** — one row per region, columns
`type,id,name,kingdom,parent kingdom`. Leave `parent kingdom` blank for a
kingdom; set it (to a `Kingdom of …`) when `kingdom` is a **principality** — the
region is then painted in the *parent's* colour while the hover label names both
("Principality of Oertha, Kingdom of the West").

**Read at runtime** (edit a row, reload the page — instant):

- **`type=state` / `type=county`** — US regions, by 2- or 5-digit FIPS. A
  `county` row overrides its state's default.
- **`type=province`** (Canada, ISO like `CA-ON`) — the **default** colour for
  every census division in that province.
- **`type=cd`** — a Canadian **census division**, by its 4-digit `CDUID`. It
  overrides that division's province default — this is how you draw sub-province
  splits (NW Ontario → Northshield, eastern BC → Avacal, …). Find a CDUID by
  searching the division name in `canada-cd.topojson`, or on Statistics Canada's
  census-division reference map.

**Read by `build_world_kingdoms.py`** (re-run `python build_world_kingdoms.py`
to regenerate `world-kingdoms.topojson`, then reload):

- **`type=country`** (by name) — the non-US, non-Canada world layer. Adding a
  *brand-new* country needs the re-run (it fetches the polygon from Natural Earth).

Notes:

- **Excel strips leading zeros** from FIPS (`02`→`2`) when it saves the CSV; the
  code zero-pads on read, so it still works — don't bother re-padding by hand.
- Canada is now census-division level, so most sub-province splits *can* be
  drawn via `type=cd`. The world layer is whole-country, built by
  `build_world_kingdoms.py` from Natural Earth **50m** admin-0 (50m follows the
  coastline closely, so the overlay paints land, not the territorial sea — the
  coarser 110m used to bleed colour across estuaries like the Thames and the
  Wadden Sea). Two notes for editing it:
  - **Overseas pieces are dropped.** `CLIP_TO_EUROPE` (`France`, `Spain`,
    `Portugal`, `Norway`) keeps only the polygons inside `EUROPE_BBOX`, so
    far-flung islands — French Guiana / Guadeloupe / Martinique / Réunion /
    Mayotte, the Canaries, the Azores + Madeira, Svalbard — are left unassigned
    rather than coloured across an ocean. Add another country to that set if you
    assign one with similar outliers. A real group on one can be added explicitly
    from admin-1 data instead.
  - A handful of estuary/archipelago city *centroids* (e.g. central Stockholm,
    the Copenhagen waterfront) sit on water at 50m and so read as unassigned;
    that's correct (it's water) and invisible at the overlay's zoom.
- The Canada boundaries come from **Statistics Canada's 2021 Census cartographic
  boundary file** (open licence — keep the "Statistics Canada" credit in the map
  attribution). To refresh them (rare — the divisions barely change): download
  *Census divisions → Cartographic Boundary File → Shapefile* from StatCan's
  boundary-files page, then run it through mapshaper (`-proj wgs84 -simplify 12%
  keep-shapes -filter-fields CDUID,CDNAME,PRUID -rename-layers cd -o
  format=topojson canada-cd.topojson`).
- **Changing the colours** (both the marker/pin colours *and* this overlay): edit
  **`colourschemes.csv`** — one row per kingdom, columns
  `kingdom,event_color,practice_color`. `event_color` is the kingdom's pin +
  background colour; `practice_color` is the (lighter) shade used for that
  kingdom's baronial events and practices. Hex with or without a leading `#` both
  work. It's read at runtime, so edit a hex, reload the page, and the whole map
  (pins, overlay, and the legend under the map) retints — no code change.
  Principalities aren't listed; they inherit their parent kingdom's colour. If the
  file is missing or a hex is malformed the map shows a load error rather than
  reverting silently, and `test_csv_integrity.py` guards it so a typo can't ship.

---

## Monitoring

- **The only alarm:** GitHub issues labelled `pipeline-health`. One opens when a
  configured kingdom hits 0 events and closes when every kingdom recovers. So
  the whole monitoring story is: **watch this repo** (Watch → All Activity, or at
  least Issues) and act when one opens.
- `health_check.py` runs at the end of every refresh and writes that signal; it
  also keeps `health_state.json`, the last run's per-kingdom counts, for
  reference.
- Nothing pages you, emails members, or needs a dashboard. Silence means healthy.

---

## Running it yourself (for debugging)

```bash
pip install -r requirements.txt
# put a real contact address in .env for Nominatim's User-Agent:
#   NOMINATIM_CONTACT_EMAIL=you@example.org   (see .env.example)
python refresh.py                       # full pipeline
python -m unittest discover -p "test_*.py"   # the test suite
```

From a normal home/residential connection every feed works, including the ones
the cron has to fight a WAF for — so local runs are the easiest way to confirm a
feed is alive.

---

## Handoff checklist (for the person handing the project over)

The code and docs travel with the repo, but a few things only the current owner
can transfer. Do these so the new maintainer is fully self-sufficient and
**nothing routes back to you**:

- [ ] **Transfer the GitHub repository** to the new maintainer (or an org they
      control): *Settings → General → Danger Zone → Transfer*. The cron, GitHub
      Pages, and the `pipeline-health` issues all follow the repo automatically.
- [ ] **Reset the Nominatim contact secret.** *Settings → Secrets and variables →
      Actions → `NOMINATIM_CONTACT_EMAIL`* → set it to the new maintainer's
      address. Nominatim (OpenStreetMap) requires a real contact in every
      request; any rate-limit or abuse notice goes to whatever's set here, so it
      must not stay yours.
- [ ] **Make sure the new owner Watches the repo** (All Activity, or at least
      Issues) so the automatic health-alert issues actually reach them.
- [ ] **Confirm GitHub Pages is still serving** after the transfer: *Settings →
      Pages*.
- [ ] **(Optional) Update the GitHub links** in `README.md`, `about.html`, and
      `how-to-help.html` if the repo URL changes on transfer. GitHub redirects
      the old URL, so this is cosmetic.
- [ ] Nothing in the site or code references a personal email, so there's no
      inbox to migrate — just don't add one.

Once the repo is transferred and the secret is reset, you're out of the loop:
member reports go to the kingdoms (per the public pages), and breakage opens an
issue on the new owner's repo. You won't be emailed.

---

## Map of the docs

- **[README.md](README.md)** — what the project is, the pipeline, key files.
- **[EDITING_EVENTS.md](EDITING_EVENTS.md)** — how to correct an event's location.
- **MAINTAINING.md** (this file) — operating, troubleshooting, handoff.
- **`reference/README.txt`** — one-off scripts kept for posterity.
