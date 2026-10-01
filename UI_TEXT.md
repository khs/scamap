# Text shown to visitors: who wrote it

Fill in **Your wording** for anything you want to replace (leave it blank to
keep the current text), then ask Claude to apply it. `N`, `X`, `<name>` etc.
stand for numbers/names the site fills in.

## A. Written by Claude (AI) — please rewrite

These were drafted by Claude in recent work sessions.

| # | Where it appears | Current text | Your wording |
| --- | --- | --- | --- |
| A1 | Button under the map key | 📍 Near me | |
| A2 | Distance dropdown (after Near me) | Show events [at any distance / within 50 miles / within 100 miles / … / within 500 miles] | |
| A3 | Near me, while locating | Finding you… | |
| A4 | Near me, browser can't do it | Your browser can't share its location. | |
| A5 | Near me, permission refused | Location sharing is turned off for this site. You can allow it in your browser's site settings. | |
| A6 | Near me, other failure | Couldn't find your location just now. Please try again. | |
| A7 | Event list checkboxes | In-person · Online | |
| A8 | Event list, nothing ticked | Tick “In-person” or “Online” above to see events. | |
| A9 | Event list count (online only) | N online events | |
| A10 | Event list, zoomed out with Online ticked | Zoom in on an area to also see local groups' online meetings there. | |
| A11 | Note under the search box (one hidden) | 1 more event matches “X” but is filtered out: … | |
| A12 | Note under the search box (several) | N more events match “X” but are filtered out: … | |
| A13 | …reason | N by your date range | |
| A14 | …reason | N by your “Near me” distance | |
| A15 | …reason | N outside the current map view | |
| A16 | …reason | N local online meeting(s) that show when you zoom in near the group | |
| A17 | …reason | N by “major events only” | |
| A18 | …reason | N because Kingdom events are unticked | |
| A19 | …reason | N because Baronial practices are unticked | |
| A20 | …reason | N online event(s) (tick “Online” to include them) | |
| A21 | …reason | N in-person event(s) (tick “In-person” to include them) | |
| A22 | …reason | N by the colours picked in the key | |
| A23 | Search note button | Show all matches | |
| A24 | Search note after clicking it | Showing every event matching “X”, with your filters set aside. | |
| A25 | …its button | Use my filters again | |
| A26 | Top of the event list after "Show all" | Showing every match, at any date or place: your filters are set aside. | |
| A27 | Event list, search with no matches | No events match your search. | |
| A28 | Event list count while showing all | N match(es) | |
| A29 | Popup link on a war pin | Event website → | |
| A30 | Popup on a war whose dates are guessed (based on your words, extended by Claude) | EventScout is unable to import the dates for X because no Kingdom calendar lists it yet. The dates shown are approximate; check the event website. | |
| A31 | Description of a guessed-date war (made by the pipeline) | <name> is usually held between <June 1> and <June 30>. Check <website> for this year's dates. | |
| A32 | Location text of An Tir/West War (wars.csv) | Lazy J Ranch, … (site of recent wars; check the war website for this year's site) | |
| A33 | Location of an event at a private address (private_addresses.py) | Private location | |
| A34 | Replaces a private address inside a description | [private location] | |
| A35 | FAQ page button | Expand all / Collapse all | |
| A36 | Link on the old About / How to Help pages (shown for a moment before they redirect) | EventScout FAQ | |

## B. Already on the site before — author not known to Claude

These existed before the recent sessions. Some are yours (the code notes
marked a few as "verbatim from the maintainer" — marked ✓ below); others may
have been written by an earlier AI session. Please check them.

| # | Where it appears | Current text | Your wording |
| --- | --- | --- | --- |
| B1 | Search box placeholder | Search by title, location, or kingdom… | |
| B2 | Filter checkboxes | Kingdom (Events) · Baronial (Practices) · Show markers for groups without calendars · Number events in the same location · Colour background by kingdom | |
| B3 | Date filter | From · To · Reset (tooltip: "Reset to today through six months out") | |
| B4 | Next to the dates | Events imported through <date> | |
| B5 | Under the map | Key · hover or tap a square to see what each colour means | |
| B6 | Key, hovering a square | <Kingdom> (events) / <Kingdom> (practices); tooltip "Filter to …" | |
| B7 | Key, after clicking squares | Showing N selected colour(s) — click a square to toggle, or “Show all”. · ✕ Show all (tooltip "Clear colour filters and show all events") | |
| B8 | Full-screen button | ⛶ Full screen / ✕ Exit full screen (tooltip "Expand the map and event list to fill your screen") | |
| B9 | Event list | Loading… · No events in this part of the map — zoom out or pan. · N in view · N mapped · N barony pins · Virtual (tag) | |
| B10 | Event list item tooltips | Click to show date, time & link · Click to show on the map | |
| B11 | Expanded online item | Open link / join → | |
| B12 | Popup links | Event page → · Event page (Facebook) → · Host events → · Facebook · <Group> website → · <Kingdom> calendar → · <Canton> (canton) → | |
| B13 | Popup / list prefix | MAYBE: | |
| B14 | "?" group pin popup | For information on events in this <barony/shire/…>, please visit their website. · 🖥 Online meeting(s) · Join → | |
| B15 | Hand-entered practice popup | EventScout is unable to import this calendar, but we believe this practice is at <place> on <day>. Please check the website: <link>. Please let us know if this event information is out of date. | |
| B16 | Hand-entered practice, vague location ✓ | This pin is placed in an approximately correct town or city, but we do not know the correct address. Check the correct location before trying to attend the practice. | |
| B17 | Hand-entered practice in the event list | The <group> does not have an automatically updating calendar — please check the website or socials. | |
| B18 | Recurring events (made by the pipeline) | "(RECURRING)" after the title; "[RECURRING — also on: <dates>, … N more through <date>]" in the description | |
| B19 | Page fails to load | Could not load map data: <error>. Make sure the data files (sca_events_clean.csv, colourschemes.csv) are in the same folder and you're serving the page via a local web server (not file://). | |

## C. Yours (for reference — no action needed)

Site notice; "Click to filter for only major events & wars (displayed with a
larger pin)"; "The location of this event is private. Please inquire with the
local group."; "This group's calendar does not include precise location
information - please check with the local group for details."; the FAQ; the
text under the map; the Choosing Your First Event page.
