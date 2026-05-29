"""
kingdoms.py
-----------
Single source of truth for SCA kingdom geography, shared by the pipeline
scripts (build_group_pins.py, geocode_sca_events.py, clean_sca_events.py).

These tables used to be copy-pasted into three files and drifted apart on
every edit (e.g. the Canadian-province bboxes existed only in
build_group_pins). Centralising them here keeps the geocoder, the event
cleaner, and the placeholder-pin builder in agreement.

Tables:
  STATE_BBOX           code → (lat_min, lat_max, lng_min, lng_max), US+DC+Canada
  US_STATE_NAMES       code → full state name
  CA_PROVINCE_NAMES    code → full province name
  US_STATE_ADJACENT    code → set of bordering state codes
  KINGDOM_STATES       kingdom → tuple of member state/province codes
                       (used to bbox-accept a geocode result)
  KINGDOM_HOME_STATES  kingdom → set of US member states (used to validate
                       events; expanded with US_STATE_ADJACENT)
  BARONY_HOME_STATES   barony/shire source name → set of home state(s)
  SCA_REGION_BBOXES    region name → bbox, coarse continent filter
  KINGDOM_REGIONS      kingdom → set of region names its events may fall in
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Bounding boxes (generous ~20–50 mile buffer) for US states + DC and the
# Canadian provinces SCA kingdoms reach into.
# ---------------------------------------------------------------------------
STATE_BBOX = {
    "AL": (30.1, 35.1, -88.6, -84.8), "AK": (51.0, 71.5, -180.0, -129.0),
    "AZ": (31.2, 37.1, -114.9, -109.0), "AR": (32.9, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.5, -114.0), "CO": (36.9, 41.1, -109.1, -101.9),
    "CT": (40.9, 42.1, -73.8, -71.7), "DE": (38.4, 39.9, -75.8, -75.0),
    "DC": (38.7, 39.0, -77.2, -76.9), "FL": (24.4, 31.1, -87.7, -79.9),
    "GA": (30.2, 35.1, -85.7, -80.7), "HI": (18.8, 22.3, -160.3, -154.7),
    "ID": (41.9, 49.1, -117.3, -111.0), "IL": (36.9, 42.6, -91.6, -87.4),
    "IN": (37.7, 41.9, -88.2, -84.7), "IA": (40.3, 43.6, -96.7, -90.1),
    "KS": (36.9, 40.1, -102.1, -94.5), "KY": (36.4, 39.2, -89.7, -81.9),
    "LA": (28.8, 33.1, -94.1, -88.7), "ME": (43.0, 47.6, -71.2, -66.8),
    "MD": (37.8, 39.8, -79.6, -75.0), "MA": (41.1, 42.9, -73.6, -69.8),
    "MI": (41.6, 48.4, -90.5, -82.3), "MN": (43.4, 49.5, -97.3, -89.4),
    "MS": (30.1, 35.1, -91.8, -87.9), "MO": (35.9, 40.7, -95.9, -89.0),
    "MT": (44.3, 49.1, -116.2, -103.9), "NE": (39.9, 43.1, -104.1, -95.2),
    "NV": (34.9, 42.1, -120.1, -113.9), "NH": (42.6, 45.4, -72.7, -70.5),
    "NJ": (38.8, 41.4, -75.7, -73.8), "NM": (31.2, 37.1, -109.2, -102.9),
    "NY": (40.4, 45.1, -79.9, -71.7), "NC": (33.7, 36.7, -84.4, -75.4),
    "ND": (45.8, 49.1, -104.1, -96.5), "OH": (38.3, 42.1, -84.9, -80.4),
    "OK": (33.5, 37.1, -103.1, -94.3), "OR": (41.9, 46.4, -124.7, -116.4),
    "PA": (39.6, 42.4, -80.6, -74.6), "RI": (41.0, 42.1, -71.9, -71.0),
    "SC": (32.0, 35.3, -83.4, -78.4), "SD": (42.4, 45.9, -104.1, -96.3),
    "TN": (34.9, 36.8, -90.4, -81.5), "TX": (25.7, 36.6, -106.7, -93.4),
    "UT": (36.9, 42.1, -114.1, -108.9), "VT": (42.6, 45.1, -73.5, -71.4),
    "VA": (36.4, 39.5, -83.7, -75.1), "WA": (45.4, 49.1, -124.9, -116.8),
    "WV": (37.1, 40.7, -82.7, -77.6), "WI": (42.4, 47.1, -92.9, -86.7),
    "WY": (40.9, 45.1, -111.1, -103.9),
    # Canadian provinces — Avacal (AB/SK/BC), Ealdormere (ON), An Tir (BC),
    # Tir Mara/East (QC + Atlantic Canada), Tir Righ (YT).
    "AB": (49.0, 60.0, -120.0, -110.0), "BC": (48.3, 60.0, -139.1, -114.0),
    "SK": (49.0, 60.0, -110.0, -101.4), "ON": (41.7, 56.9, -95.2, -74.3),
    "QC": (45.0, 62.6, -79.8, -57.1), "MB": (49.0, 60.0, -102.0, -88.9),
    "NS": (43.4, 47.0, -66.4, -59.7), "NB": (44.6, 48.1, -69.1, -63.8),
    "NL": (46.6, 60.5, -67.8, -52.6), "PE": (45.9, 47.1, -64.4, -61.9),
    "YT": (60.0, 69.7, -141.1, -123.8),
}

US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

CA_PROVINCE_NAMES = {
    "AB": "Alberta", "BC": "British Columbia", "SK": "Saskatchewan",
    "MB": "Manitoba", "ON": "Ontario", "QC": "Quebec", "NS": "Nova Scotia",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "PE": "Prince Edward Island", "YT": "Yukon", "NT": "Northwest Territories",
    "NU": "Nunavut",
}

# US state adjacency (shared border). Lets a baronial event held just over
# the state line in a neighbouring state still validate.
US_STATE_ADJACENT = {
    "AL": {"FL", "GA", "MS", "TN"}, "AR": {"LA", "MO", "MS", "OK", "TN", "TX"},
    "AZ": {"CA", "CO", "NM", "NV", "UT"}, "CA": {"AZ", "NV", "OR"},
    "CO": {"AZ", "KS", "NE", "NM", "OK", "UT", "WY"}, "CT": {"MA", "NY", "RI"},
    "DC": {"MD", "VA"}, "DE": {"MD", "NJ", "PA"}, "FL": {"AL", "GA"},
    "GA": {"AL", "FL", "NC", "SC", "TN"},
    "IA": {"IL", "MN", "MO", "NE", "SD", "WI"},
    "ID": {"MT", "NV", "OR", "UT", "WA", "WY"}, "IL": {"IA", "IN", "KY", "MO", "WI"},
    "IN": {"IL", "KY", "MI", "OH"}, "KS": {"CO", "MO", "NE", "OK"},
    "KY": {"IL", "IN", "MO", "OH", "TN", "VA", "WV"}, "LA": {"AR", "MS", "TX"},
    "MA": {"CT", "NH", "NY", "RI", "VT"}, "MD": {"DC", "DE", "PA", "VA", "WV"},
    "ME": {"NH"}, "MI": {"IN", "OH", "WI"}, "MN": {"IA", "ND", "SD", "WI"},
    "MO": {"AR", "IA", "IL", "KS", "KY", "NE", "OK", "TN"}, "MS": {"AL", "AR", "LA", "TN"},
    "MT": {"ID", "ND", "SD", "WY"}, "NC": {"GA", "SC", "TN", "VA"},
    "ND": {"MN", "MT", "SD"}, "NE": {"CO", "IA", "KS", "MO", "SD", "WY"},
    "NH": {"MA", "ME", "VT"}, "NJ": {"DE", "NY", "PA"},
    "NM": {"AZ", "CO", "OK", "TX", "UT"}, "NV": {"AZ", "CA", "ID", "OR", "UT"},
    "NY": {"CT", "MA", "NJ", "PA", "VT"}, "OH": {"IN", "KY", "MI", "PA", "WV"},
    "OK": {"AR", "CO", "KS", "MO", "NM", "TX"}, "OR": {"CA", "ID", "NV", "WA"},
    "PA": {"DE", "MD", "NJ", "NY", "OH", "WV"}, "RI": {"CT", "MA"},
    "SC": {"GA", "NC"}, "SD": {"IA", "MN", "MT", "ND", "NE", "WY"},
    "TN": {"AL", "AR", "GA", "KY", "MO", "MS", "NC", "VA"}, "TX": {"AR", "LA", "NM", "OK"},
    "UT": {"AZ", "CO", "ID", "NM", "NV", "WY"}, "VA": {"DC", "KY", "MD", "NC", "TN", "WV"},
    "VT": {"MA", "NH", "NY"}, "WA": {"ID", "OR"}, "WI": {"IA", "IL", "MI", "MN"},
    "WV": {"KY", "MD", "OH", "PA", "VA"}, "WY": {"CO", "ID", "MT", "NE", "SD", "UT"},
}

# Member states/provinces per kingdom, for bbox-accepting a geocode result.
# (Tuples; includes Canadian provinces where a kingdom reaches into Canada.)
KINGDOM_STATES = {
    "Kingdom of AEthelmearc":   ("PA", "WV", "NY"),
    "Kingdom of An Tir":        ("WA", "OR", "ID", "MT", "BC"),
    "Kingdom of Ansteorra":     ("OK", "TX"),
    "Kingdom of Artemisia":     ("MT", "UT", "ID", "WY"),
    "Kingdom of Atenveldt":     ("AZ",),
    "Kingdom of Atlantia":      ("VA", "MD", "NC", "SC", "DC"),
    "Kingdom of Avacal":        ("AB", "SK", "BC"),
    "Kingdom of Caid":          ("CA", "NV", "HI"),
    "Kingdom of Calontir":      ("KS", "MO", "IA", "NE"),
    "Kingdom of Ealdormere":    ("ON",),
    "Kingdom of the East":      ("CT", "DE", "ME", "MA", "NH", "NJ", "NY",
                                 "PA", "RI", "VT", "NS", "NB", "PE", "NL", "QC"),
    "Kingdom of Gleann Abhann": ("LA", "AR", "MS", "TN"),
    "Kingdom of Meridies":      ("AL", "GA", "TN", "KY", "FL"),
    "Kingdom of the Middle":    ("IL", "IN", "OH", "MI"),
    "Kingdom of Northshield":   ("MN", "WI", "ND", "SD", "MB", "ON"),  # incl. Manitoba + NW Ontario
    "Kingdom of the Outlands":  ("CO", "WY", "NM"),
    "Kingdom of Trimaris":      ("FL",),
    "Kingdom of the West":      ("CA", "NV", "AK"),
}

# US member states per kingdom, for *event* validation (expanded with
# US_STATE_ADJACENT at use sites). Sets; US-only.
KINGDOM_HOME_STATES = {
    "Kingdom of AEthelmearc":   {"PA", "WV", "NY"},
    "Kingdom of An Tir":        {"WA", "OR", "ID", "MT"},
    "Kingdom of Avacal":        {"AB", "SK", "BC"},
    "Kingdom of Ansteorra":     {"OK", "TX"},
    "Kingdom of Artemisia":     {"MT", "UT", "ID", "WY"},
    "Kingdom of Atenveldt":     {"AZ"},
    "Kingdom of Atlantia":      {"VA", "MD", "NC", "SC", "DC"},
    "Kingdom of Caid":          {"CA", "NV", "HI"},
    "Kingdom of Calontir":      {"KS", "MO", "IA", "NE"},
    "Kingdom of the East":      {"CT", "DE", "ME", "MA", "NH", "NJ", "NY",
                                  "PA", "RI", "VT"},
    "Kingdom of Gleann Abhann": {"LA", "AR", "MS", "TN"},
    "Kingdom of Meridies":      {"AL", "GA", "TN", "KY", "FL"},
    "Kingdom of the Middle":    {"IL", "IN", "OH", "MI"},
    "Kingdom of Northshield":   {"MN", "WI", "ND", "SD"},
    "Kingdom of the Outlands":  {"CO", "WY", "NM"},
    "Kingdom of Trimaris":      {"FL"},
    "Kingdom of the West":      {"CA", "NV", "AK"},
}

# Per-barony home state(s), for validating that a baronial event geocoded
# into its own area. Add entries as baronial calendars join calendars.csv.
BARONY_HOME_STATES = {
    # Atlantia — Maryland
    "Barony of Lochmere": {"MD"}, "Barony of Bright Hills": {"MD"},
    "Barony of Storvik": {"MD"}, "Barony of Dun Carraig": {"MD"},
    "Barony of Highland Foorde": {"MD"},
    # Atlantia — Virginia
    "Barony of Ponte Alto": {"VA"}, "Barony of Stierbach": {"VA"},
    "Barony of Stierbach (Workshops)": {"VA"}, "Barony of Caer Mear": {"VA"},
    "Barony of Marinus": {"VA"}, "Barony of Tir-y-Don": {"VA"},
    "Barony of Black Diamond": {"VA"},
    # Atlantia — North Carolina
    "Barony of Windmasters' Hill": {"NC"}, "Barony of Raven's Cove": {"NC"},
    "Barony of Hawkwood": {"NC"}, "Barony of Sacred Stone": {"NC"},
    # Atlantia — South Carolina
    "Barony of Nottinghill Coill": {"SC"}, "Barony of Hidden Mountain": {"SC"},
    # Atlantia — shires under Hawkwood
    "Shire of Aukesgate": {"NC"}, "Shire of Stormwall": {"NC"},
}

# Coarse continent/sub-continent filter to keep Photon from placing e.g. a
# Meridies "Atlanta Metropolitan area" event in Romania.
SCA_REGION_BBOXES = {
    "north_america": (24.0, 72.0, -170.0, -52.0),
    "hawaii":        (18.5, 22.5, -160.5, -154.5),
    "europe":        (34.5, 71.5,  -10.5,  41.0),
    "australia":     (-45.0, -9.0, 110.0, 156.0),
    "new_zealand":   (-47.5, -33.5, 165.0, 179.5),
}

# Which region(s) a kingdom's events can legitimately land in.
KINGDOM_REGIONS = {
    "Kingdom of AEthelmearc":   {"north_america"},
    "Kingdom of An Tir":        {"north_america"},
    "Kingdom of Ansteorra":     {"north_america"},
    "Kingdom of Artemisia":     {"north_america"},
    "Kingdom of Atenveldt":     {"north_america"},
    "Kingdom of Atlantia":      {"north_america"},
    "Kingdom of Avacal":        {"north_america"},
    "Kingdom of Caid":          {"north_america", "hawaii"},
    "Kingdom of Calontir":      {"north_america"},
    "Kingdom of Drachenwald":   {"europe"},
    "Kingdom of Ealdormere":    {"north_america"},
    "Kingdom of Gleann Abhann": {"north_america"},
    "Kingdom of Lochac":        {"australia", "new_zealand"},
    "Kingdom of Meridies":      {"north_america"},
    "Kingdom of the Middle":    {"north_america"},
    "Kingdom of Northshield":   {"north_america"},
    "Kingdom of the Outlands":  {"north_america"},
    "Kingdom of Trimaris":      {"north_america"},
    "Kingdom of the East":      {"north_america"},
    "Kingdom of the West":      {"north_america"},
}
