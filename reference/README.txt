reference/
==========

One-off scripts kept here for posterity. They aren't part of the regular
event-refresh pipeline -- refresh.py never touches this directory -- but each
was useful at least once and may be useful again if a similar problem comes
back. Nothing in the live codebase imports from here.


fetch_antir.py
--------------
Headed/headless Playwright (Chromium) automation to pull An Tir's event ICS
and groups page through Cloudflare's Managed Challenge. Used during the
attempt to add An Tir to calendars.csv. Does NOT work in the Claude Code
sandbox or unattended CI because neither can spawn a real browser -- which is
why An Tir is still not in the live config. Try this from a desktop if/when
you want to revisit An Tir.


fetch_antir_with_cookie.py
--------------------------
Manual-cookie fallback for the same An Tir problem: you export the
`cf_clearance` cookie from your own browser (after you've already passed
Cloudflare once in that browser) and this script reuses it via curl_cffi with
a matching Chrome TLS fingerprint. The cookie typically lasts 4-24 hours, so
this gives a one-off snapshot of An Tir, not a sustainable feed.


find_barony_calendars.py
------------------------
Discovers Google Calendar IDs embedded as iframes on barony homepages by
decoding the base64 `src=` params. Used once to seed several baronial entries
in calendars.csv. Re-run if a batch of new baronies stand up websites.


probe_baronial_calendars.py
---------------------------
Walks the baronies in group_locations.csv for a configurable set of kingdoms
and hunts for any ICS or scrape-able calendar feed: Tribe Events `?ical=1`,
R34 plugin, MEC, Simple Calendar, embedded Google Calendar URLs. Appends
discoveries to calendars.csv. Use when you want to expand baronial coverage.
