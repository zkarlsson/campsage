#!/usr/bin/env python3
"""
locations.py — CampSage's saved scan origins ("closest to X" is relative to these).

The RUNTIME source of truth is DATA_DIR/locations_config.json (the "store"), editable
from the web UI's pill bar (add/remove) — deployment-wide, shared across users by
design. config.LOCATIONS/DEFAULT_LOCATION only SEED the store the first time; delete
the store file to re-seed. Store entries are always fully resolved (slug + lat/lng
geocoded at add time) so a geocoder outage can never break a scheduled scan.

Each location gets its own output dir under DATA_DIR/locations/<slug>/;
DATA_DIR/locations.json is the derived index the web app reads (which locations have
scan data, when, default slug).

Concurrency: the web app (2 gunicorn workers) and the scanner mutate these files from
different containers sharing one local docker volume, so cross-process safety uses
flock on sidecar lock files (correct on a same-superblock local volume; NFS-backed
volumes would need a different mechanism) plus atomic tmp+rename JSON writes:
  • locations_config.lock — every store read-modify-write and every index write
  • scan.lock            — held for a whole scan run (scans must NEVER overlap:
                           recreation.gov's per-IP quota punishes concurrent scans)

Also home to the single shared haversine/slugify/nearest-anchor helpers.
"""
import fcntl
import json
import math
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

import config

STORE_FILE = config.DATA_DIR / "locations_config.json"
INDEX_FILE = config.DATA_DIR / "locations.json"
STORE_LOCK = config.DATA_DIR / "locations_config.lock"
SCAN_LOCK = config.DATA_DIR / "scan.lock"
_RESERVED_SLUGS = {"data", "map", "locations"}   # would collide with /camp/* routes
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")


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


# ── file plumbing ─────────────────────────────────────────────────────────────
@contextmanager
def _flock(path, blocking=True):
    """Cross-process exclusive lock. Yields True if acquired (always, when
    blocking); False if non-blocking and the lock is busy."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)                     # closing the fd releases the flock


def scan_lock(blocking=True):
    """Whole-scan serialization: cron scans block; pending scans skip if busy."""
    return _flock(SCAN_LOCK, blocking)


def _write_json_atomic(path, obj):
    tmp = path.with_suffix(".tmp")       # writers all hold the store lock, no races
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ── the store ─────────────────────────────────────────────────────────────────
def read_store():
    return _read_json(STORE_FILE)


def _seed_store():
    """Build the initial store from config seeds (geocoding entries that lack
    coords). Raises RuntimeError if a seed can't be resolved."""
    import geocode
    entries = []
    for e in getattr(config, "LOCATIONS", None) or []:
        name = e.get("name") or e.get("query") or "?"
        lat, lng = e.get("lat"), e.get("lng")
        if lat is None or lng is None:
            lat, lng, _ = geocode.lookup(e.get("query") or name)
        entries.append({"slug": slugify(name), "name": name,
                        "query": e.get("query", ""), "lat": float(lat),
                        "lng": float(lng), "added_epoch": int(time.time())})
    if not entries:
        raise RuntimeError("no config.LOCATIONS to seed the location store from")
    default = getattr(config, "DEFAULT_LOCATION", None)
    if default not in {e["slug"] for e in entries}:
        default = entries[0]["slug"]
    return {"version": 1, "default": default, "locations": entries}


def load_store():
    """The store, seeding it from config on first use. Raises on seed failure."""
    store = read_store()
    if store:
        return store
    with _flock(STORE_LOCK):
        store = read_store()             # re-check under the lock
        if store:
            return store
        store = _seed_store()
        _write_json_atomic(STORE_FILE, store)
        return store


def _entry_to_location(e):
    return Location(name=e.get("name") or e["slug"], slug=e["slug"],
                    lat=float(e["lat"]), lng=float(e["lng"]),
                    query=e.get("query", ""))


def load_locations():
    """Resolve every saved location. Exits loudly on failure — a broken store or
    unresolvable seed must fail the whole scan visibly, not skip a city."""
    if not getattr(config, "LOCATIONS", None) and not read_store():
        # legacy single-home config, pre-multi-location
        return [Location(name=getattr(config, "HOME_NAME", "Home"),
                         slug=slugify(getattr(config, "HOME_NAME", "home")),
                         lat=config.HOME_LAT, lng=config.HOME_LNG)]
    try:
        store = load_store()
    except Exception as e:
        sys.exit(f"locations: cannot load/seed the location store: {e}")
    out = []
    for e in store.get("locations", []):
        try:
            out.append(_entry_to_location(e))
        except (KeyError, TypeError, ValueError) as err:
            sys.exit(f"locations: malformed store entry {e!r}: {err}")
    if not out:
        sys.exit("locations: store has no locations")
    return out


def add_location(query, name=None):
    """Validate + geocode + append. Returns the new entry dict.
    Raises ValueError with a user-facing message on any problem."""
    import geocode
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not query:
        raise ValueError("Enter a zip code or a place (e.g. 94607 or Fresno, CA).")
    if len(query) > 120 or len(name) > 40:
        raise ValueError("That's too long for a place query or label.")
    try:
        lat, lng, display = geocode.lookup(query)
    except Exception as e:
        raise ValueError(f"Couldn't locate {query!r}: {e}") from e
    if not name:
        name = (display or query).split(",")[0].strip() or query
    slug = slugify(name)
    if slug in _RESERVED_SLUGS:
        raise ValueError(f"'{name}' clashes with a reserved page name — pick another label.")
    with _flock(STORE_LOCK):
        store = read_store() or _seed_store()
        cap = getattr(config, "MAX_LOCATIONS", 6)
        if len(store["locations"]) >= cap:
            raise ValueError(f"Location limit reached ({cap}) — remove one first "
                             "(each location costs real API budget to scan).")
        existing = next((e for e in store["locations"] if e["slug"] == slug), None)
        if existing:
            raise ValueError(f"Already saved as '{existing['name']}' — "
                             "use a different label if this is a different place.")
        entry = {"slug": slug, "name": name, "query": query, "lat": lat, "lng": lng,
                 "added_epoch": int(time.time())}
        store["locations"].append(entry)
        _write_json_atomic(STORE_FILE, store)
    return entry


def remove_location(slug):
    """Remove a saved location + its index entry + its scan data.
    Raises ValueError with a user-facing message on any problem."""
    if not _SLUG_RE.match(slug or ""):
        raise ValueError("Bad location id.")
    with _flock(STORE_LOCK):
        store = read_store()
        if not store or not any(e["slug"] == slug for e in store["locations"]):
            raise ValueError("That location isn't saved.")
        if len(store["locations"]) <= 1:
            raise ValueError("Keep at least one location.")
        store["locations"] = [e for e in store["locations"] if e["slug"] != slug]
        if store.get("default") == slug:
            store["default"] = store["locations"][0]["slug"]
        _write_json_atomic(STORE_FILE, store)
        _rewrite_index_from_store(store)
    # Outside the lock: slug is regex-validated AND was present in the store.
    shutil.rmtree(config.DATA_DIR / "locations" / slug, ignore_errors=True)


def default_slug(locs=None):
    store = read_store()
    if store:
        want = store.get("default")
        slugs = [e["slug"] for e in store.get("locations", [])]
        return want if want in slugs else (slugs[0] if slugs else None)
    want = getattr(config, "DEFAULT_LOCATION", None)
    locs = locs or load_locations()
    if want and any(l.slug == want for l in locs):
        return want
    return locs[0].slug


# ── the derived index (what the web app reads) ────────────────────────────────
def read_index():
    return _read_json(INDEX_FILE)


def _rewrite_index_from_store(store, extra_entry=None):
    """Prune + order the index against the CURRENT store (caller holds STORE_LOCK).
    `extra_entry` merges in a just-finished scan's fresh row."""
    idx = read_index() or {"locations": []}
    by_slug = {e.get("slug"): e for e in idx.get("locations", [])}
    if extra_entry:
        by_slug[extra_entry["slug"]] = extra_entry
    store_slugs = [e["slug"] for e in store.get("locations", [])]
    out = {"default": store.get("default") if store.get("default") in store_slugs
                      else (store_slugs[0] if store_slugs else None),
           "updated_epoch": int(time.time()),
           "locations": [by_slug[s] for s in store_slugs if s in by_slug]}
    _write_json_atomic(INDEX_FILE, out)
    return out


def update_index(loc, status, _legacy_locs=None):
    """Record a finished scan in the index — pruned against the store as it exists
    NOW (not at scan start), so a removal that happened mid-scan stays removed.
    Called after EVERY location so a mid-run crash still leaves a valid index."""
    with _flock(STORE_LOCK):
        try:
            store = load_store()
        except Exception:
            store = {"default": loc.slug,
                     "locations": [{"slug": loc.slug, "name": loc.name}]}
        if not any(e["slug"] == loc.slug for e in store.get("locations", [])):
            return _rewrite_index_from_store(store)      # removed mid-scan: skip it
        entry = {
            "slug": loc.slug, "name": loc.name, "query": loc.query,
            "lat": loc.lat, "lng": loc.lng,
            "generated_at": status.get("generated_at"),
            "generated_epoch": status.get("generated_epoch"),
            "with_openings": (status.get("counts") or {}).get("with_openings"),
        }
        return _rewrite_index_from_store(store, extra_entry=entry)


def prune_orphan_dirs():
    """Delete location data dirs whose slug is no longer saved (e.g. a location
    removed during its own in-flight scan gets its dir recreated — this cleans it)."""
    store = read_store()
    if not store:
        return
    keep = {e["slug"] for e in store.get("locations", [])}
    base = config.DATA_DIR / "locations"
    if not base.is_dir():
        return
    for d in base.iterdir():
        if d.is_dir() and d.name not in keep and _SLUG_RE.match(d.name):
            shutil.rmtree(d, ignore_errors=True)


# ── anchors ───────────────────────────────────────────────────────────────────
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
