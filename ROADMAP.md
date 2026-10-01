# EventScout roadmap

Priorities agreed with the maintainer (most recent first). Update this file as
things are done or decided.

## Next up (suggested order)

1. **FAQ page replacing About** — small; answers the questions people
   currently email about (why is an event wrong/missing, how locations work,
   how to get a group added, privacy). Deflects reports before they arrive.
2. **Quick wins**
   - Health check: warn when antir.ics is about to run out of events (it
     expires silently; An Tir has to be downloaded by hand).
   - "Data last updated" date shown on the map.
   - Dormant status for groups in locals.csv (keeps the row, hides the pin,
     stops find_calendars re-adding it; easy to reactivate).
3. **Reorganise the filters above the map** (full-width map stays):
   - Row "Event type": three on/off toggles — Major wars & Known World /
     Kingdom (events) / Baronial (local practices). Replaces today's two
     checkboxes + the major-events button.
   - Row "Map preferences": number events in same location, colour background
     by kingdom, show markers for groups without calendars, show events near me
     (+ distance).
   - Row "Date range".
   - Colour key stays below the map.
   - Narrow screens: collapse to three buttons ("Event type", "Map
     preferences", "Date range") that expand.
4. **Import practices for "?" groups into hardcoded_events.csv** from barony
   websites (semi-automated: extract → maintainer reviews → add). Hardcoded
   rows don't auto-update, so record the source page and a last-checked date
   and make re-checking easy.
5. **Corrections & submissions forms** (largest; needs maintainer's
   Cloudflare + GitHub-token setup). Form pages on EventScout → Cloudflare
   Worker → GitHub pull request per submission → maintainer reviews and clicks
   Merge on GitHub's website. Spam: Turnstile + rate limits. No submitter
   emails in public PRs.
   - Required checkbox: "I agree that I have not submitted any private
     addresses to EventScout, and all locations I have submitted are already
     public information elsewhere."
   - Locations via a drop-a-pin map (exact lat/lng, no geocoding) + address text.
   - Form types and destinations:
     - add a calendar link for a group → locals.csv calendar_id (PR check runs
       find_calendars.py on it)
     - "?" pin in the wrong place → locals.csv location/lat/lng
     - practice details for a group with no calendar → hardcoded_events.csv
       (free-text schedule, e.g. "every third Sunday", as that file already uses)
     - a hardcoded practice is wrong/changed/ended → edit/remove that row
     - wrong location for an event → corrections.csv, with a plain-language
       scope question: just this event / every event with this title / every
       event mentioning a keyword (a future "this address is always misread"
       scope would be new)
     - website link → group site (locals.csv website) vs event-only link
       (corrections.csv `link`) — the form asks which
     - group has gone dormant → Dormant status
     - **a group we don't know about yet** → new locals.csv row
   - NOT doing: changing one date inside a recurring series.

## Done recently
- corrections.csv replaces event_overrides.csv + location_corrections.csv
  (verified byte-identical output).
- In-person / Online checkboxes; search note with "Show all matches"; Near me
  + distance filter; major-events toggle; site notice.
- find_calendars.py (discovery + verification); My Calendar adapter;
  ~47 group calendars added; rejected_calendars.csv.
- Local online + location-less events; aggregator calendars; wars.csv;
  private_addresses.csv; map-link pins; fetch window to end of next year.
