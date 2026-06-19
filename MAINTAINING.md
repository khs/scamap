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
latest **Actions → "Refresh events + group pins"** run and read that kingdom's
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

To re-run on demand: **Actions → Refresh events + group pins → Run workflow**.

### An Tir / hard-blocked feeds

An Tir sits behind a Cloudflare challenge that blocks datacenter IPs outright,
so it isn't in `calendars.csv`. `reference/` has the scripts from past attempts
(a Playwright browser run, and a manual-cookie `curl_cffi` approach) if anyone
wants to revisit it from a non-datacenter machine. Don't expect it to work from
the cron.

---

## Adding or removing a group

Edit **`calendars.csv`** — one row per feed, columns `id,source,type`
(`type` is `kingdom` or `baronial`). Add a row to add a group; delete the row to
remove it. No code change is needed for a normal Google-Calendar or ICS feed.
Source names must match the kingdom names used elsewhere (the colour map in
`index.html`, the home-state tables in `kingdoms.py`). After adding, run the
workflow once and check the new group's events appear.

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
