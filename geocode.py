#!/usr/bin/env python3
"""
geocode.py — keyless zip/address → (lat, lng) for CampSage's saved locations.

  • 5-digit US ZIP  -> api.zippopotam.us (keyless, no UA requirements)
  • anything else   -> Nominatim (OpenStreetMap) with a descriptive UA and a polite
                       delay between uncached calls, restricted to the US

Results are cached forever in DATA_DIR/geocode_cache.json (places don't move), so
Nominatim sees roughly one request per configured location — ever. A location that
can't be geocoded raises RuntimeError: a typo'd zip should fail the scan loudly,
not silently scan a subset. Locations with explicit lat/lng in config bypass this
module entirely (the escape hatch if either service is ever down).
"""
import json
import re
import time
import urllib.parse
import urllib.request

import config

CACHE_FILE = config.DATA_DIR / "geocode_cache.json"
UA = {"User-Agent": "CampSage/1.0 (personal campsite finder)"}
NOMINATIM_DELAY_S = 1.1          # Nominatim usage policy: max 1 req/s
_last_nominatim = 0.0


def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(cache):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _zippopotam(zip5):
    d = _fetch_json(f"https://api.zippopotam.us/us/{zip5}")
    places = d.get("places") or []
    if not places:
        raise RuntimeError(f"zippopotam returned no places for zip {zip5}")
    p = places[0]
    label = f"{p.get('place name')}, {p.get('state abbreviation')} {zip5}"
    return float(p["latitude"]), float(p["longitude"]), label


def _nominatim(query):
    global _last_nominatim
    wait = NOMINATIM_DELAY_S - (time.monotonic() - _last_nominatim)
    if wait > 0:
        time.sleep(wait)
    q = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1,
                                "countrycodes": "us"})
    d = _fetch_json(f"https://nominatim.openstreetmap.org/search?{q}")
    _last_nominatim = time.monotonic()
    if not d:
        raise RuntimeError(f"Nominatim found no match for {query!r}")
    return float(d[0]["lat"]), float(d[0]["lon"]), d[0].get("display_name", query)


def lookup(query):
    """(lat, lng, display) for a zip or address string. Cached forever; raises on failure."""
    key = re.sub(r"\s+", " ", str(query)).strip().lower()
    if not key:
        raise RuntimeError("empty geocode query")
    cache = _load_cache()
    hit = cache.get(key)
    if hit:
        return hit["lat"], hit["lng"], hit.get("display", query)
    try:
        if re.fullmatch(r"\d{5}", key):
            lat, lng, display = _zippopotam(key)
        else:
            lat, lng, display = _nominatim(key)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"geocoding {query!r} failed: {e}") from e
    cache[key] = {"lat": lat, "lng": lng, "display": display, "ts": int(time.time())}
    _save_cache(cache)
    return lat, lng, display


def geocode(query):
    """(lat, lng) — see lookup()."""
    lat, lng, _ = lookup(query)
    return lat, lng


if __name__ == "__main__":
    import sys
    for q in sys.argv[1:] or ["94607"]:
        print(q, "->", geocode(q))
