"""
geocoder.py
-----------
Shared low-level geocoding for the pipeline: one Nominatim + Photon client
with a single persistent cache and one rate-limit gate, used by both
geocode_sca_events.py (event addresses) and build_group_pins.py (group
region centroids).

Why share it:
  - Nominatim's bulk-geocoding policy says results MUST be cached and the
    same query must not be repeated. With a shared cache, a query resolved
    while geocoding events is reused when building group pins, and vice
    versa, and persists across scheduled runs in geocode_cache.json.
  - One throttle (_throttle) keeps the combined HTTP rate at <= 4/min no
    matter which script is calling.

The higher-level logic stays in each script (event retry-ladder vs. the
group multi-state county vote) — only the single-query primitives live here.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api"

# Contact address for the User-Agent (Nominatim policy). Pulled from the env;
# .env sets it locally, a GitHub Actions secret sets it in CI.
_CONTACT = os.getenv("NOMINATIM_CONTACT_EMAIL", "nobody@example.com")
USER_AGENT = f"SCA Maps Project ({_CONTACT})"

# Seconds between real HTTP calls — 4 requests/minute, well under Nominatim's
# 1 req/sec ceiling. Override with GEOCODE_REQUEST_DELAY for testing.
REQUEST_DELAY = float(os.getenv("GEOCODE_REQUEST_DELAY", "15.0"))

# Countries where the SCA operates — keeps Nominatim from resolving e.g.
# "Greater Savannah area" to Belize.
SCA_COUNTRY_CODES = ("us,ca,au,nz,gb,ie,fr,de,be,nl,lu,ch,at,it,es,pt,"
                     "se,no,fi,dk,is,pl,cz,sk,hu,si,hr,ee,lv,lt,bg,ro,gr,mt,cy")

# ── Persistent cache ───────────────────────────────────────────────────
_CACHE_FILE = SCRIPT_DIR / "geocode_cache.json"
_FAIL_TTL = 30 * 86400          # retry a cached failure after 30 days
_MISS = object()


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  WARNING: could not read {_CACHE_FILE.name}: {e}")
    return {}


_cache: dict = _load_cache()
_dirty = False


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry is None:
        return _MISS
    if entry.get("lat") is None and time.time() - entry.get("ts", 0) > _FAIL_TTL:
        return _MISS   # stale failure → retry
    return (entry.get("lat"), entry.get("lng"))


def _cache_put(key: str, result: tuple) -> None:
    global _dirty
    _cache[key] = {"lat": result[0], "lng": result[1], "ts": int(time.time())}
    _dirty = True


def save_cache() -> None:
    """Persist the cache to disk if it changed. Call at end of a run."""
    if _dirty:
        _CACHE_FILE.write_text(
            json.dumps(_cache, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )


# ── Rate-limit gate ────────────────────────────────────────────────────
_last_http = 0.0


def _throttle() -> None:
    global _last_http
    elapsed = time.time() - _last_http
    if 0 < elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_http = time.time()


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


# ── Query primitives ───────────────────────────────────────────────────
def nominatim(query: str, session: requests.Session, *,
              countrycodes: str = SCA_COUNTRY_CODES,
              require_in_name: str = "",
              addressdetails: bool = False) -> tuple:
    """One Nominatim lookup, cached. Returns (lat, lng) or (None, None).

    require_in_name: reject a result whose display_name doesn't contain this
    substring (used by the group-pin county vote to avoid near-miss matches).
    """
    key = f"nom:{query}|{require_in_name}|{countrycodes}|{int(addressdetails)}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    params = {"q": query, "format": "json", "limit": 1, "countrycodes": countrycodes}
    if addressdetails:
        params["addressdetails"] = 1
    result = (None, None)
    _throttle()
    try:
        r = session.get(NOMINATIM_URL, params=params, timeout=15)
        r.raise_for_status()
        results = r.json()
        if results:
            top = results[0]
            ok = True
            if require_in_name:
                ok = require_in_name.lower() in (top.get("display_name") or "").lower()
            if ok:
                result = (float(top["lat"]), float(top["lon"]))
    except Exception as e:
        print(f"    WARNING: Nominatim error for '{query[:60]}': {e}")
    _cache_put(key, result)
    return result


def photon(query: str, session: requests.Session) -> tuple:
    """One Photon lookup, cached. Returns (lat, lng) or (None, None)."""
    key = f"pho:{query}"
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached
    result = (None, None)
    _throttle()
    try:
        r = session.get(PHOTON_URL, params={"q": query, "limit": 1}, timeout=15)
        r.raise_for_status()
        features = r.json().get("features", [])
        if features:
            coords = features[0]["geometry"]["coordinates"]  # [lng, lat]
            result = (float(coords[1]), float(coords[0]))
    except Exception as e:
        print(f"    WARNING: Photon error for '{query[:60]}': {e}")
    _cache_put(key, result)
    return result
