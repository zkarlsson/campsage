#!/usr/bin/env python3
"""
locations.py — CampSage's saved scan origins ("closest to X" is relative to these).

config.LOCATIONS entries are {"name": ..., "query": <zip or address>} with optional
explicit "lat"/"lng" (skips geocoding). Each location gets its own output dir under
DATA_DIR/locations/<slug>/ and a tab in the web UI; DATA_DIR/locations.json is the
index the web app and helpers read.

Also home to the single shared haversine/slugify/nearest-anchor helpers (previously
duplicated across camp_agent.py and reservecalifornia.py).
"""
import json
import math
import re
import sys
import time
from dataclasses import dataclass

import config

INDEX_FILE = config.DATA_DIR / "locations.json"
_RESERVED_SLUGS = {"data", "map"}        # would collide with /camp/<slug> routes


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "x"


def haversine(lat1, lng1, lat2, lng2):
    """Straight-line miles between two points."""
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Location:
    name: str
    slug: str
    lat: float
    lng: float
    query: str = ""

    @property
    def dir(self):
        return config.DATA_DIR / "locations" / self.slug

    @property
    def status_json(self):
        return self.dir / "status.json"

    @property
    def dashboard_html(self):
        return self.dir / "dashboard.html"


def load_locations():
    """Resolve every configured location up front (geocoding as needed). Any failure
    exits loudly — a typo'd zip must fail the whole scan visibly, not skip a city."""
    entries = getattr(config, "LOCATIONS", None)
    if not entries:                       # legacy single-home config
        return [Location(name=getattr(config, "HOME_NAME", "Home"),
                         slug=slugify(getattr(config, "HOME_NAME", "home")),
                         lat=config.HOME_LAT, lng=config.HOME_LNG)]
    out, seen = [], set()
    for e in entries:
        name = e.get("name") or e.get("query") or "?"
        slug = slugify(name)
        if slug in _RESERVED_SLUGS:
            sys.exit(f"locations: {name!r} slugs to reserved word {slug!r} — rename it")
        if slug in seen:
            sys.exit(f"locations: duplicate slug {slug!r} — rename one of the entries")
        seen.add(slug)
        lat, lng = e.get("lat"), e.get("lng")
        if lat is None or lng is None:
            import geocode
            try:
                lat, lng = geocode.geocode(e.get("query") or name)
            except RuntimeError as err:
                sys.exit(f"locations: cannot geocode {name!r}: {err}")
        out.append(Location(name=name, slug=slug, lat=float(lat), lng=float(lng),
                            query=e.get("query", "")))
    return out


def default_slug(locs=None):
    want = getattr(config, "DEFAULT_LOCATION", None)
    locs = locs or load_locations()
    if want and any(l.slug == want for l in locs):
        return want
    return locs[0].slug


def active_anchors(loc):
    """The REGION_ANCHORS within reach of this location — only these are searched
    and used for region tabs, so API load stays flat as the anchor list grows."""
    return [(label, alat, alng) for label, alat, alng in config.REGION_ANCHORS
            if haversine(loc.lat, loc.lng, alat, alng) <= config.REGION_MAX_DISTANCE_MI]


def nearest_anchor(lat, lng, anchors):
    """(label, slug) of the nearest anchor, or ("Other", "other")."""
    best = None
    for label, alat, alng in anchors:
        d = haversine(lat, lng, alat, alng)
        if best is None or d < best[0]:
            best = (d, label)
    if not best:
        return "Other", "other"
    return best[1], slugify(best[1])


def read_index():
    try:
        return json.loads(INDEX_FILE.read_text())
    except Exception:
        return None


def update_index(loc, status, locs):
    """Rewrite locations.json after EACH location's scan so a mid-run crash still
    leaves a valid, mostly-fresh index."""
    idx = read_index() or {"locations": []}
    by_slug = {e.get("slug"): e for e in idx.get("locations", [])}
    by_slug[loc.slug] = {
        "slug": loc.slug, "name": loc.name, "query": loc.query,
        "lat": loc.lat, "lng": loc.lng,
        "generated_at": status.get("generated_at"),
        "generated_epoch": status.get("generated_epoch"),
        "with_openings": (status.get("counts") or {}).get("with_openings"),
    }
    # Ordered + pruned to the CURRENT config so removed locations drop out.
    ordered = [by_slug[l.slug] for l in locs if l.slug in by_slug]
    out = {"default": default_slug(locs), "updated_epoch": int(time.time()),
           "locations": ordered}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(out, indent=2))
    return out
