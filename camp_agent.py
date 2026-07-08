#!/usr/bin/env python3
"""
CampSage — finds great California campsites with 2-3 consecutive nights open at the
SAME site, ranked closest-to-home first among well-reviewed spots, and publishes a
phone-friendly status page (served by the MarketSage dashboard at /camp).

Data source: recreation.gov public JSON endpoints (no API key):
  • search      -> ratings, review counts, lat/lng, drive distance from home
  • availability-> per-night status per site (so we can detect consecutive nights)

Pure standard library. Run:  python3 camp_agent.py
"""
import json
import re
import sys
import time
import html
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:                       # pragma: no cover
    PT = None

import config
import locations
from locations import haversine as _haversine

SEARCH_URL = "https://www.recreation.gov/api/search"
AVAIL_URL  = "https://www.recreation.gov/api/camps/availability/campground/{cid}/month"
BOOK_URL   = "https://www.recreation.gov/camping/campgrounds/{cid}"
AVAIL_PAGE = "https://www.recreation.gov/camping/campgrounds/{cid}/availability"


# ──────────────────────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────────────────────
# Global request pacing: the statewide anchor expansion multiplied availability
# fetches, and recreation.gov throttles on a CUMULATIVE trailing window, not just
# bursts (observed 2026-07-07: ~650 calls at 5.5 req/s for one city filled the quota
# and the next city's first minutes 429'd — twice, same campgrounds). 1 req/s across
# all worker threads keeps a two-city scan (~1100 calls) safely under it; the cron
# runs 3×/day so a ~20-minute scan costs nothing. 429s additionally back off long
# enough to outlive a throttle window instead of burning the retry budget in seconds.
_PACE_LOCK = None
_PACE_LAST = [0.0]
MIN_REQUEST_SPACING_S = 1.0
RATE_LIMIT_RETRIES = 5


def _pace():
    global _PACE_LOCK
    if _PACE_LOCK is None:
        import threading
        _PACE_LOCK = threading.Lock()
    with _PACE_LOCK:
        wait = _PACE_LAST[0] + MIN_REQUEST_SPACING_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _PACE_LAST[0] = time.monotonic()


def http_get_json(url, params=None):
    """GET JSON with a browser UA, timeout, global pacing, and backoff on 429/5xx."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(max(config.RETRIES, RATE_LIMIT_RETRIES)):
        _pace()
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:                       # throttled: wait the window out
                retry_after = 0
                try:
                    retry_after = int(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pass
                time.sleep(max(retry_after, min(10.0 * (attempt + 1), 45)))
                continue
            if e.code in (500, 502, 503, 504):
                if attempt >= config.RETRIES - 1:
                    break
                time.sleep(1.5 * (attempt + 1))
                continue
            if e.code == 404:
                return None
            if attempt >= config.RETRIES - 1:
                break
            time.sleep(0.8 * (attempt + 1))
        except Exception as e:
            last_err = e
            if attempt >= config.RETRIES - 1:
                break
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last_err}")


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — discover well-reviewed campgrounds near home
# ──────────────────────────────────────────────────────────────────────────────
def search_campgrounds(lat, lng, radius=None):
    """Recreation.gov campground search centered on (lat,lng) within `radius` miles.
    Called once from the scan location and once per active region anchor (the
    home-centered search is capped at 150 score-ranked results, so far destinations
    only surface via their anchor)."""
    radius = config.SEARCH_RADIUS_MI if radius is None else radius
    found, start = [], 0
    while True:
        data = http_get_json(SEARCH_URL, {
            "fq": "entity_type:campground",
            "lat": lat,
            "lng": lng,
            "radius": radius,
            "size": 50,
            "start": start,
            "sort": "score",
        })
        results = (data or {}).get("results", []) or []
        if not results:
            break
        found.extend(results)
        total = int((data or {}).get("total", len(found)) or 0)
        start += len(results)
        if start >= total or start >= 150:
            break
        time.sleep(0.3)
    return found


def discover(loc, anchors):
    """Location-centered search PLUS a small search around every ACTIVE region anchor
    (the subset within reach of `loc`), merged and deduped by campground id. Distance is
    recomputed from `loc` (the API's `distance` is relative to whatever center we
    searched), and far destination finds are tagged so the page can keep the All tab
    close to home while still surfacing them under their tab."""
    raw = {}
    for c in search_campgrounds(loc.lat, loc.lng):       # everyday home search
        cid = c.get("entity_id")
        if cid:
            raw[cid] = c
    if config.ANCHOR_SEARCH_ENABLED:
        for label, alat, alng in anchors:
            try:
                for c in search_campgrounds(alat, alng, config.REGION_SEARCH_RADIUS_MI):
                    cid = c.get("entity_id")
                    if cid and cid not in raw:
                        c["_destination"] = label        # found via a destination anchor
                        raw[cid] = c
            except Exception as e:
                log(f"  anchor search '{label}' failed: {e}")
            time.sleep(0.3)                              # be polite: ~20 searches/run
    # Explicitly seed the iconic FAR campgrounds that the score-ranked anchor search can
    # miss (e.g. Kirk Creek) via a targeted name query, so the ⭐ Sought-after tab is
    # reliable. Only meaningful when Big Sur is in reach of this location.
    if any(label == "Big Sur" for label, _, _ in anchors):
        for q in ("Kirk Creek", "Plaskett Creek", "Limekiln", "Julia Pfeiffer Burns", "Andrew Molera"):
            try:
                data = http_get_json(SEARCH_URL, {"q": q, "fq": "entity_type:campground", "size": 5})
                for c in (data or {}).get("results", []) or []:
                    cid = c.get("entity_id")
                    if cid and cid not in raw and q.split()[0].lower() in (c.get("name") or "").lower():
                        c["_destination"] = c.get("_destination") or "Big Sur"
                        raw[cid] = c
                time.sleep(0.3)
            except Exception as e:
                log(f"  marquee seed '{q}' failed: {e}")
    # Recompute distance from the scan location for consistent ranking (anchor results
    # carry a distance measured from the anchor, not from home).
    for c in raw.values():
        lat, lng = c.get("latitude"), c.get("longitude")
        if lat is not None and lng is not None:
            try:
                c["distance"] = _haversine(loc.lat, loc.lng, float(lat), float(lng))
            except (TypeError, ValueError):
                pass
    return list(raw.values())


def qualifies(c):
    """Good reviews + close enough + actually reservable + in California.
    Everyday spots must be within MAX_DISTANCE_MI; destination finds (from an anchor
    search) are allowed out to REGION_MAX_DISTANCE_MI so far places like Big Sur survive."""
    if not c.get("reservable"):
        return False
    if (c.get("state_code") or "").lower() not in ("california", "ca"):
        return False
    try:
        dist = float(c.get("distance"))
    except (TypeError, ValueError):
        return False
    # Everyday spots: within the LA cap. Destination finds (tagged by an anchor search):
    # allowed farther out, to REGION_MAX_DISTANCE_MI.
    cap = config.REGION_MAX_DISTANCE_MI if c.get("_destination") else config.MAX_DISTANCE_MI
    if dist > cap:
        return False
    rating  = c.get("average_rating")
    reviews = c.get("number_of_ratings") or 0
    if rating is None:
        return False
    rating = float(rating)
    reviews = int(reviews)
    # Primary bar, or the high-rating/low-volume "gem" tier.
    if rating >= config.MIN_RATING and reviews >= config.MIN_REVIEWS:
        return True
    if rating >= config.SOFT_MIN_RATING and reviews >= config.SOFT_MIN_REVIEWS:
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 — availability -> consecutive-night blocks
# ──────────────────────────────────────────────────────────────────────────────
def months_to_fetch(today, window_days):
    # +max(NIGHTS) so a block starting on the last in-window day is fully covered.
    end = today + timedelta(days=window_days + max(config.NIGHTS))
    months, y, m = [], today.year, today.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}-01T00:00:00.000Z")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


# Per-process availability memo: campgrounds reachable from more than one scan
# location (Big Sur, Pismo from both LA and Oakland) are fetched once per run.
_AVAIL_MEMO = {}
_AVAIL_LOCK = None  # created lazily; ThreadPoolExecutor workers share the dict


def _fetch_month(cid, mstart):
    global _AVAIL_LOCK
    if _AVAIL_LOCK is None:
        import threading
        _AVAIL_LOCK = threading.Lock()
    key = (cid, mstart)
    with _AVAIL_LOCK:
        if key in _AVAIL_MEMO:
            return _AVAIL_MEMO[key]
    data = http_get_json(AVAIL_URL.format(cid=cid), {"start_date": mstart})
    with _AVAIL_LOCK:
        _AVAIL_MEMO[key] = data
    return data


def fetch_availability(cid, months):
    """Merge the month payloads into {site_id: {meta, dates:{date->status}}}."""
    sites = {}
    for mstart in months:
        data = _fetch_month(cid, mstart)
        for sid, c in ((data or {}).get("campsites", {}) or {}).items():
            entry = sites.setdefault(sid, {
                "site": c.get("site"), "loop": c.get("loop"),
                "type": c.get("campsite_type"), "dates": {},
            })
            for iso, status in (c.get("availabilities") or {}).items():
                day = iso[:10]
                entry["dates"][day] = status
    return sites


def site_earliest_block(dates_map, win_start, win_end):
    """Earliest maximal run of Available nights in-window. Returns block or None."""
    avail = sorted(d for d, s in dates_map.items()
                   if str(s).strip().lower() == "available"
                   and win_start <= date.fromisoformat(d) <= win_end)
    if not avail:
        return None
    runs, run = [], [avail[0]]
    for prev, cur in zip(avail, avail[1:]):
        if date.fromisoformat(cur) - date.fromisoformat(prev) == timedelta(days=1):
            run.append(cur)
        else:
            runs.append(run); run = [cur]
    runs.append(run)

    want_min = min(config.NIGHTS)        # smallest acceptable stay (e.g. 2)
    want_max = max(config.NIGHTS)        # preferred length cap (e.g. 3)
    for r in runs:                       # runs are already chronological
        if len(r) < want_min:
            continue
        nights = min(want_max, len(r))
        start  = date.fromisoformat(r[0])
        if config.WEEKENDS_ONLY:
            stay = [start + timedelta(days=i) for i in range(nights)]
            if not any(d.weekday() in (4, 5) for d in stay):  # Fri/Sat night
                continue
        return {
            "start": r[0],
            "end": (start + timedelta(days=nights)).isoformat(),  # checkout morning
            "nights": nights,
            "run_len": len(r),
        }
    return None


def weekend_block(avail_set, win_start, win_end):
    """Earliest bookable block that covers a FRIDAY *and* SATURDAY night (arrive Fri; extend to Sun
    if free). Scans ALL free nights (not just the earliest run), so weekend openings hidden behind an
    earlier weekday block are still found. `avail_set` = iterable of 'YYYY-MM-DD' bookable nights."""
    avail = {d for d in avail_set if d and win_start <= date.fromisoformat(d) <= win_end}
    for d in sorted(avail):
        dt = date.fromisoformat(d)
        if dt.weekday() != 4:                                       # start on a Friday
            continue
        if (dt + timedelta(days=1)).isoformat() not in avail:       # Saturday night also free?
            continue
        nights = 3 if (max(config.NIGHTS) >= 3 and (dt + timedelta(days=2)).isoformat() in avail) else 2
        return {"start": d, "end": (dt + timedelta(days=nights)).isoformat(), "nights": nights}
    return None


def _group_openings(blocks, cap=6):
    """Group per-site blocks by (start, nights) into date options with a site count + example."""
    groups = {}
    for b in blocks:
        g = groups.setdefault((b["start"], b["nights"]),
                              {"start": b["start"], "end": b["end"], "nights": b["nights"], "sites": []})
        g["sites"].append(b["site_label"])
    ops = sorted(groups.values(), key=lambda g: (g["start"], -g["nights"]))
    for g in ops:
        g["count"] = len(g["sites"]); g["example"] = sorted(g["sites"])[0]
    return ops[:cap]


def analyze_campground(c, months, win_start, win_end):
    cid = c.get("entity_id")
    try:
        sites = fetch_availability(cid, months)
    except Exception as e:
        return {"error": str(e)}
    blocks, wk = [], []
    for sid, sdata in sites.items():
        label = f"{sdata.get('loop') or ''} #{sdata.get('site') or sid}".strip()
        blk = site_earliest_block(sdata["dates"], win_start, win_end)
        if blk:
            blocks.append({**blk, "site_label": label})
        avail = [d for d, s in sdata["dates"].items() if str(s).strip().lower() == "available"]
        wb = weekend_block(avail, win_start, win_end)
        if wb:
            wk.append({**wb, "site_label": label})
    weekend = {"has_weekend": bool(wk),
               "weekend_soonest": min((w["start"] for w in wk), default=None),
               "weekend_openings": _group_openings(wk, 4)}
    if not blocks:
        return {"openings": [], "sites_with_block": 0, **weekend}
    return {
        "openings": _group_openings(blocks, 6),
        "sites_with_block": len(blocks),
        "has_3night": any(b["nights"] >= 3 for b in blocks),
        "has_2night": any(b["nights"] >= 2 for b in blocks),
        "soonest": min(b["start"] for b in blocks),
        **weekend,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrate
# ──────────────────────────────────────────────────────────────────────────────
def fmt_date(iso):
    d = date.fromisoformat(iso)
    return d.strftime("%a %b %-d")


def now_pt():
    dt = datetime.now(PT) if PT else datetime.now()
    return dt.strftime("%a %b %-d, %-I:%M %p %Z").strip()


def assign_region(card, anchors):
    """Tag a campground with the NEAREST active destination anchor (Big Bear, etc.).
    Only the location's active anchor subset participates, so tabs match what was
    actually searched."""
    lat, lng = card.get("lat"), card.get("lng")
    if lat is None or lng is None:
        return "Other", "other"
    return locations.nearest_anchor(float(lat), float(lng), anchors)


def _base(c):
    name = c.get("name") or ""
    return {
        "id": c.get("entity_id"),
        "name": name.title() if name.isupper() else name,
        "parent": c.get("parent_name") or c.get("org_name") or "",
        "rating": round(float(c.get("average_rating")), 1),
        "reviews": int(c.get("number_of_ratings") or 0),
        "distance": round(float(c.get("distance")), 1),
        "lat": c.get("latitude"), "lng": c.get("longitude"),
        "price": (c.get("price_range") or {}),
        "image": c.get("preview_image_url") or "",
        "book_url": BOOK_URL.format(cid=c.get("entity_id")),
        "avail_url": AVAIL_PAGE.format(cid=c.get("entity_id")),
        # Far destination find (Big Sur, etc.): shown under its region tab, not in "All".
        "destination": c.get("_destination"),
        "everyday": float(c.get("distance") or 0) <= config.MAX_DISTANCE_MI,
    }


# ⭐ Most sought-after: the bucket-list campgrounds people fight over. Built from ALL analyzed
# places (INCLUDING full ones) so they ALWAYS show on their own tab — a cancellation is one tap away.
# Evidence-based "most sought-after" set — validated + expanded by a multi-agent web study
# (2026-07-01) across The Dyrt / Campendium / Hipcamp / Sunset / Campnab most-watched + the
# ReserveCalifornia "hardest to book" reporting. Big-4 LA consensus: Kirk Creek, Pfeiffer,
# Crystal Cove, Leo Carrillo. Statewide entries added 2026-07-07 with multi-location
# support (deeper NorCal curation is a follow-up — only famous names here).
MARQUEE_KW = ["kirk creek", "plaskett", "pfeiffer", "limekiln", "julia pfeiffer", "andrew molera",
              "leo carrillo", "crystal cove", "el capitan", "refugio", "carpinteria", "doheny",
              "san onofre", "san elijo", "carlsbad", "bolsa chica", "silver strand", "point mugu",
              "sycamore", "malibu creek", "thornhill", "jalama", "montana de oro", "morro bay",
              "pismo", "wheeler gorge", "gaviota", "sunset state", "new brighton", "seacliff",
              # added from the study (LA-reachable, discoverable via existing anchors):
              "serrano", "idyllwild", "palomar", "doane valley", "san clemente",
              # DESERT (now discoverable via the new Joshua Tree / Anza-Borrego anchors):
              "jumbo rocks", "indian cove", "black rock", "white tank", "ryan campground",
              "borrego palm",
              # NORCAL (discoverable via the statewide anchors; famous-only):
              "steep ravine", "emerald bay", "bliss", "mackerricher", "salt point",
              "van damme", "upper pines", "lower pines", "north pines", "tuolumne meadows"]
# Coveted-SITE intel from the 2026-07-01 study — the specific sites people fight for, so an
# opening isn't just "a site" but "the good site vs an interior dud". Keys matched in the name;
# longest (most specific) key wins so "pfeiffer big sur" and "julia pfeiffer" don't collide.
MARQUEE_SITES = {
    "kirk creek": "bluff sites right over the ocean — 9, 11, 15, 17, 19, 21, 22",
    "plaskett": "24–30 most spacious · 41–43 best ocean views · 12–17 nearest the beach",
    "pfeiffer big sur": "riverfront + big wooded even-numbered sites 6–36; 116 by the gorge",
    "julia pfeiffer": "only 2 walk-in sites — EC1 'Saddle Rock' is closest to the ocean",
    "el capitan": "blufftop 91–130 — favorites 101, 104, 109",
    "refugio": "beachfront 24–26, 29–35, 60, 61 · 52 for shade",
    "carpinteria": "oceanfront 103–115 (Santa Rosa loop) · quiet 140–155",
    "san elijo": "ocean-view bluff row 145–171 (also 1–43); interior sites hear the train",
    "carlsbad": "premium west-facing blufftop sites (stairs down to the sand)",
    "leo carrillo": "site 139 is the pick; tree-shaded canyon",
    "silver strand": "oceanfront 101–137 (RV / self-contained only)",
    "doheny": "beachfront sites 37–94",
    "san onofre": "Bluffs ocean-view 1–23, 99–119, 146–174; San Mateo 1–67 near Trestles",
    "san clemente": "Tent West 73–99 (esp. 82) for sunset ocean views",
    "thornhill": "primitive sites literally on the sand",
    "sycamore": "shady canyon sites, a short walk to the cove",
    "point mugu": "Thornhill Broome sites are right on the beach",
    "serrano": "lakeside sites 112–118",
    "limekiln": "ocean-facing sites 1 & 2 are the largest",
    "jalama": "beachfront 63 & 64 on Abalone Point",
    "crystal cove": "the blufftop row over the open coast",
    "morro bay": "select bayfront sites via the monthly reservation Draw (lottery)",
    "andrew molera": "walk-in meadow — sites 4, 20, 22 for shade",
    "montana de oro": "big private bluff sites (Islay Creek)",
    "jumbo rocks": "sites tucked among the giant boulders; superb dark-sky stargazing",
    "indian cove": "sites nestled behind the rock formations (climbers' favorite)",
    "black rock": "quieter, tree-shaded high-desert sites; good basecamp",
    "steep ravine": "the 7 rustic cabins over the ocean are the most coveted booking in the state system",
}
_MSORT = sorted(MARQUEE_SITES.items(), key=lambda x: -len(x[0]))   # longest key wins


def is_marquee_name(name):
    nm = (name or "").lower()
    return any(kw in nm for kw in MARQUEE_KW)


def cap_destination_candidates(candidates, anchors, log=lambda *_: None):
    """Bound availability fetches: the page shows at most REGION_MAX_PER_TAB spots per
    far region, so analyzing every anchor-search find is wasted API budget (and got the
    IP 429-throttled when the statewide anchors landed). Keep all everyday candidates,
    but per far region keep only the credibility-weighted best (rating × log10 reviews)
    few beyond the display cap — plus every marquee-name candidate, which the
    ⭐ Sought-after tab needs regardless of rank."""
    import math
    everyday = [c for c in candidates if not c.get("_destination")]
    dest = [c for c in candidates if c.get("_destination")]
    per_cap = config.REGION_MAX_PER_TAB + 4          # spare for full/no-opening spots
    by_region = {}
    for c in dest:
        try:
            slug = locations.nearest_anchor(float(c.get("latitude")),
                                            float(c.get("longitude")), anchors)[1]
        except (TypeError, ValueError):
            slug = "other"
        by_region.setdefault(slug, []).append(c)
    kept = list(everyday)
    for slug, group in by_region.items():
        group.sort(key=lambda c: -(float(c.get("average_rating") or 0)
                                   * math.log10(int(c.get("number_of_ratings") or 0) + 1)))
        keep = group[:per_cap] + [c for c in group[per_cap:] if is_marquee_name(c.get("name"))]
        kept.extend(keep)
    if len(kept) < len(candidates):
        log(f"  capped availability fetches: {len(candidates)} candidates -> {len(kept)} "
            f"(top {per_cap}/far region + marquee)")
    return kept


def _analyze_pass(candidates, months, win_start, win_end):
    """One parallel availability pass. Returns (cards, [(candidate, error), ...])."""
    out, failed = [], []
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        futs = {ex.submit(analyze_campground, c, months, win_start, win_end): c
                for c in candidates}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                failed.append((c, str(e)))
                continue
            if res.get("error"):
                failed.append((c, res["error"]))
                continue
            out.append({**_base(c), **res})
    return out, failed


def analyze_candidates(candidates, months, win_start, win_end, errors, retry_wait=300):
    """Fetch availability for each candidate in parallel; return a list of card dicts
    (base info merged with opening info). Items without openings keep openings=[].
    recreation.gov enforces an opaque rolling per-IP quota that a big scan can brush
    against even fully paced — so candidates that fail get ONE second-chance pass after
    the quota window slides. The availability memo makes retries cheap (already-fetched
    months are served from memory); only both-pass failures are reported as errors."""
    out, failed = _analyze_pass(candidates, months, win_start, win_end)
    if failed and retry_wait:
        log(f"  {len(failed)} availability fetches failed — retrying after "
            f"{retry_wait // 60} min quota-window pause…")
        time.sleep(retry_wait)
        more, failed = _analyze_pass([c for c, _ in failed], months, win_start, win_end)
        out += more
    for c, err in failed:
        errors.append(f"{c.get('name')}: {err}")
    return out


def run(loc, all_locs=None):
    today = date.today()
    win_start, win_end = today, today + timedelta(days=config.WINDOW_DAYS)
    months = months_to_fetch(today, config.WINDOW_DAYS)
    errors = []
    anchors = locations.active_anchors(loc)

    log(f"discovering campgrounds within {config.SEARCH_RADIUS_MI}mi of {loc.name} "
        f"+ {len(anchors)} destination anchors in range…")
    try:
        raw = discover(loc, anchors)
    except Exception as e:
        log(f"FATAL search failed: {e}")
        raise
    candidates = [c for c in raw if qualifies(c)]
    ndest = sum(1 for c in candidates if c.get("_destination"))
    log(f"  {len(raw)} campgrounds found, {len(candidates)} pass reviews+distance filter "
        f"({ndest} far-destination finds)")
    candidates = cap_destination_candidates(candidates, anchors, log)

    # Fetch availability in parallel.
    analyzed = analyze_candidates(candidates, months, win_start, win_end, errors)
    hits       = [d for d in analyzed if d.get("openings")]
    no_opening = [d for d in analyzed if not d.get("openings")]
    hits.sort(key=lambda h: h["distance"])           # closest first (default)
    no_opening.sort(key=lambda h: (-h["rating"], h["distance"]))

    # ── Beach section: MAINLAND drive-up state beaches via ReserveCalifornia ──
    # (Channel Islands / boat-in island camping intentionally excluded.)
    beach = []
    if config.BEACH_ENABLED:
        log(f"discovering mainland beach camping (ReserveCalifornia) within "
            f"{config.BEACH_MAX_DISTANCE}mi…")
        try:
            import reservecalifornia
            beach = reservecalifornia.discover_beaches(loc, win_start, win_end, errors)
            log(f"  {len(beach)} state-beach campgrounds, "
                f"{sum(1 for b in beach if b.get('openings'))} with 2-3 night openings")
        except Exception as e:
            errors.append(f"beach: {e}")

    # State parks (ReserveCalifornia, non-beach) — merged into the SAME region grid as the
    # federal sites (ratingless; e.g. Pfeiffer Big Sur under the Big Sur tab). They also get a
    # 🏕️ State Parks tab via the data-statepark flag set on their cards.
    park_full = []
    if config.STATE_PARKS_ENABLED:
        try:
            import reservecalifornia
            parks = reservecalifornia.discover_state_parks(loc, anchors, win_start, win_end,
                                                           errors, log)
            park_hits = [p for p in parks if p.get("openings")]
            # Booked-out state parks (Pfeiffer Big Sur, etc.) still surface in the
            # great-but-full / set-alert list so a cancellation is one tap away.
            park_full = sorted((p for p in parks if not p.get("openings")),
                               key=lambda h: h["distance"])
            log(f"  {len(park_hits)} state parks with openings, {len(park_full)} full (→ set-alert)")
            hits = hits + park_hits
            hits.sort(key=lambda h: h["distance"])
        except Exception as e:
            errors.append(f"stateparks: {e}")

    # Everyday closest-to-LA spots (the "All" tab, capped) + far destination finds beyond the
    # everyday radius (Big Sur, etc.), which appear only under their own region tab (capped
    # per region so one far place can't flood the page).
    everyday_hits = [h for h in hits if h.get("everyday")][:config.TOP_N_DISPLAY]
    dest_per, dest_hits = {}, []
    for h in hits:                                        # already distance-sorted
        if h.get("everyday"):
            continue
        slug = assign_region(h, anchors)[1]
        if dest_per.get(slug, 0) >= config.REGION_MAX_PER_TAB:
            continue
        dest_per[slug] = dest_per.get(slug, 0) + 1
        dest_hits.append(h)
    mountain = everyday_hits + dest_hits
    if dest_hits:
        log(f"  + {len(dest_hits)} far-destination openings "
            f"({', '.join(sorted({h.get('destination') for h in dest_hits if h.get('destination')}))})")
    beach_all = list(beach)                                 # capture BEFORE the openings filter
    if getattr(config, "SHOW_ONLY_OPENINGS", True):
        beach = [b for b in beach if b.get("openings")]
    marquee, seen = [], set()
    for p in (hits + no_opening + park_full + beach_all):
        nm = (p.get("name") or "").lower(); key = p.get("id") or p.get("name")
        if any(kw in nm for kw in MARQUEE_KW) and key not in seen:
            seen.add(key); p["marquee"] = True
            hint = next((v for k, v in _MSORT if k in nm), "")   # attach coveted-site intel
            if hint:
                p["site_hint"] = hint
            marquee.append(p)
    marquee.sort(key=lambda h: (not h.get("openings"), h.get("distance", 9e9)))   # open first, then closest
    display = mountain + beach[:config.TOP_N_DISPLAY]
    _disp = {id(c) for c in display}
    # ⭐ tab shows sought-after spots WITH a real opening only (user pref — matches SHOW_ONLY_OPENINGS).
    # Include open marquee spots that got cut from the main list by the region cap; drop the full ones.
    marquee_extra = [m for m in marquee if id(m) not in _disp and m.get("openings")]
    display = display + marquee_extra
    # Social buzz score (YouTube, no API key) for the campgrounds we'll display.
    try:
        import socialscore
        socialscore.attach_scores(display, log)
    except Exception as e:
        errors.append(f"social: {e}")
    # Tag each displayed campground with its destination region (for the place tabs).
    region_acc = {}
    for c in display:
        label, slug = assign_region(c, anchors)
        c["region"], c["region_slug"] = label, slug
        if c.get("openings"):                     # region tabs count only bookable spots (not full marquee)
            acc = region_acc.setdefault(slug, [label, 0, 9e9])
            acc[1] += 1
            acc[2] = min(acc[2], c["distance"])
    regions = sorted(({"slug": s, "label": v[0], "count": v[1]}
                      for s, v in region_acc.items()),
                     key=lambda r: region_acc[r["slug"]][2])
    log(f"  regions: {', '.join(r['label'] + '(' + str(r['count']) + ')' for r in regions)}")

    status = {
        "generated_at": now_pt(),
        "generated_epoch": int(time.time()),
        "home": loc.name,
        "slug": loc.slug,
        "lat": loc.lat,
        "lng": loc.lng,
        "window_days": config.WINDOW_DAYS,
        "nights": config.NIGHTS,
        "max_distance": config.MAX_DISTANCE_MI,
        "min_rating": config.MIN_RATING,
        "counts": {
            "scanned": len(raw),
            "qualified": len(candidates),
            "with_openings": len(hits),
            "full": len(no_opening) + len(park_full),
            "errors": len(errors),
        },
        "results": mountain + marquee_extra,   # + open sought-after spots cut by the region cap
        # Federal full (rated, top 12) + every full state park (Pfeiffer etc.) so it's alert-able.
        "also_great_but_full": [] if getattr(config, "SHOW_ONLY_OPENINGS", True) else (no_opening[:12] + park_full),
        "beach": beach[:config.TOP_N_DISPLAY],
        "beach_count": len(beach),
        "marquee_count": len(marquee),
        "marquee_open": sum(1 for m in marquee if m.get("openings")),
        "regions": regions,
        "errors": errors[:40],
    }
    loc.dir.mkdir(parents=True, exist_ok=True)
    loc.status_json.write_text(json.dumps(status, indent=2))
    render_html(status, loc, all_locs)
    log(f"DONE — {len(hits)} campgrounds with {min(config.NIGHTS)}-{max(config.NIGHTS)} "
        f"night openings, {len(no_opening)} great-but-full, {len(beach)} beach/coastal, "
        f"{len(errors)} errors")
    return status


# ──────────────────────────────────────────────────────────────────────────────
# Phone-friendly page (self-contained, sortable client-side)
# ──────────────────────────────────────────────────────────────────────────────
def load_tips():
    try:
        return json.loads(config.TIPS_JSON.read_text())
    except Exception:
        return None


def render_html(status, loc=None, all_locs=None):
    """Render a location's dashboard. `loc` defaults to a Location built from the
    status itself (doctor.py re-renders from a bare status dict); `all_locs` drives
    the location-switcher pills and may be None for a single-location setup."""
    if loc is None:
        loc = locations.Location(name=status.get("home", "Home"),
                                 slug=status.get("slug") or locations.slugify(status.get("home", "home")),
                                 lat=status.get("lat") or 0.0, lng=status.get("lng") or 0.0)

    def esc(s): return html.escape(str(s), quote=True)

    def card_html(h):
        drive = h["distance"] / 45.0
        if h.get("rating") is None:                 # state beach — no review scores
            rating_line = "<span class='beachtag'>🏖️ State beach</span> · no review scores (state-park system)"
        else:
            stars = "★" * int(round(h["rating"])) + "☆" * (5 - int(round(h["rating"])))
            price = h.get("price") or {}
            pr = ""
            if price.get("amount_min") is not None:
                lo, hi = price.get("amount_min"), price.get("amount_max")
                pr = f"${lo:g}" + (f"–${hi:g}" if hi and hi != lo else "") + "/night"
            rating_line = (f"<span class='stars'>{stars}</span> {h['rating']} "
                           f"({h['reviews']} reviews){(' · ' + pr) if pr else ''}")
        # Merge regular + weekend openings (dedup by start+nights); tag rows covering Fri+Sat so the
        # Weekends toggle can show ONLY those (and surface weekend blocks hidden behind weekday ones).
        def _covers_wknd(o):
            s = date.fromisoformat(o["start"])
            wds = {(s + timedelta(days=i)).weekday() for i in range(o["nights"])}
            return 4 in wds and 5 in wds
        seen, merged = set(), []
        for o in (h.get("openings", []) + h.get("weekend_openings", [])):
            k = (o["start"], o["nights"])
            if k in seen:
                continue
            seen.add(k); merged.append(o)
        wkrows = [o for o in merged if _covers_wknd(o)]           # keep ALL weekend rows...
        other = sorted((o for o in merged if not _covers_wknd(o)), key=lambda o: (o["start"], -o["nights"]))
        merged = sorted(wkrows + other[:max(2, 6 - len(wkrows))],  # ...+ a few earliest weekday rows
                        key=lambda o: (o["start"], -o["nights"]))
        opening_rows = []
        for o in merged:
            iswk = _covers_wknd(o)
            extra = f" +{o['count']-1} more sites" if o.get("count", 1) > 1 else ""
            opening_rows.append(
                f"<div class='op{' wkop' if iswk else ''}'><span class='nights'>{o['nights']} nights</span> "
                f"{'🗓️ ' if iswk else ''}{esc(fmt_date(o['start']))} → {esc(fmt_date(o['end']))}"
                f"<span class='site'>{esc(o['example'])}{esc(extra)}</span></div>")
        if opening_rows:
            ops = "".join(opening_rows)
            wk_badge = ("<span class='b bw'>🗓️ weekend&nbsp;✓</span>" if h.get("has_weekend") else "")
            badges = (("<span class='b b3'>3-night ✓</span>" if h.get("has_3night") else "")
                      + wk_badge
                      + f"<span class='b b2'>{h.get('sites_with_block',0)} sites open</span>")
        else:  # coastal spots can have no 2–3 night block in-window — still worth showing
            ops = "<div class='op muted'>No 2–3 night block in the next " \
                  f"{status['window_days']} days — tap the calendar for other dates.</div>"
            badges = "<span class='b b0'>check calendar</span>"
        maps = (f"https://www.google.com/maps/dir/?api=1&destination={h['lat']},{h['lng']}"
                if h.get("lat") and h.get("lng") else
                "https://www.google.com/maps/search/" + urllib.parse.quote(h["name"]))
        # Social reviews: one-tap searches that open on the user's phone (where they're
        # logged in / not blocked). Live scraping is impossible — Reddit/IG/TikTok all
        # 403 datacenter IPs — so these deep links are the reliable, honest way in.
        sq  = urllib.parse.quote(h["name"] + " camping")
        sqr = urllib.parse.quote(h["name"] + " camping review")
        sc = h.get("social_score")
        if sc:
            full = int(sc["stars"]); half = (sc["stars"] - full) >= 0.5
            glyph = "★" * full + ("½" if half else "") + "☆" * (5 - full - (1 if half else 0))
            v = sc["views"]
            vh = f"{v/1_000_000:.1f}M" if v >= 1_000_000 else (f"{v//1000}K" if v >= 1000 else str(v))
            score_line = (f"<div class='sscore'><a href='{esc(sc['url'])}' target='_blank' rel='noopener'>"
                          f"<b>{sc['stars']:.1f}</b> <span class='sst'>{glyph}</span></a> "
                          f"<span class='ssmeta'>social buzz · {sc['videos']} videos · {vh} views (YouTube)</span></div>")
        else:
            score_line = ""
        social = (
            f"{score_line}<div class='social'>💬 Reviews: "
            f"<a href='https://www.reddit.com/search/?q={sq}&sort=relevance' target='_blank' rel='noopener'>Reddit</a>"
            f"<a href='https://www.youtube.com/results?search_query={sqr}' target='_blank' rel='noopener'>YouTube</a>"
            f"<a href='https://www.tiktok.com/search?q={sq}' target='_blank' rel='noopener'>TikTok</a>"
            f"<a href='https://www.google.com/search?q={sqr}s' target='_blank' rel='noopener'>Google</a>"
            "</div>")
        soonest = esc(h.get("soonest", "9999-99-99"))
        book_label = ("Book on ReserveCalifornia →" if h.get("state_beach")
                      else "Book on Recreation.gov →")
        region_chip = (f"<span class='regionchip'>📍 {esc(h['region'])}</span>"
                       if h.get("region") else "")
        park_chip = ("<span class='parkchip'>🏖️ state beach · no reviews</span>" if h.get("state_beach")
                     else "<span class='parkchip'>🏕️ state park · no reviews</span>" if h.get("state_park")
                     else "")
        far_dest = bool(h.get("destination")) and not h.get("everyday")
        dest_chip = (f"<span class='destchip'>🚗 {h['distance']:.0f} mi · long haul</span>"
                     if far_dest else "")
        ev = 0 if far_dest else 1
        site_hint_html = (f"<div class='sitehint'>⭐ aim for: {esc(h['site_hint'])}</div>"
                          if h.get("site_hint") else "")
        return f"""
        <div class="card" data-distance="{h['distance']}" data-rating="{h['rating'] if h.get('rating') is not None else 0}" data-reviews="{h['reviews']}" data-sites="{h.get('sites_with_block',0)}" data-social="{sc['stars'] if sc else 0}" data-soonest="{soonest}" data-region="{esc(h.get('region_slug',''))}" data-beach="{1 if h.get('state_beach') else 0}" data-statepark="{1 if h.get('state_park') else 0}" data-weekend="{1 if h.get('has_weekend') else 0}" data-marquee="{1 if h.get('marquee') else 0}" data-full="{0 if h.get('openings') else 1}" data-everyday="{ev}">
          <div class="row1">
            <div class="name">{esc(h['name'])}</div>
            <div class="dist">{h['distance']:.0f} mi · ~{drive:.1f}h</div>
          </div>
          <div class="meta">{region_chip} {park_chip} {dest_chip} {esc(h['parent'])}</div>
          <div class="meta">{rating_line}</div>
          {social}
          <div class="badges">{badges}</div>
          {site_hint_html}
          <div class="ops">{ops}</div>
          <div class="btns">
            <a class="btn book" href="{esc(h['book_url'])}" target="_blank" rel="noopener">{book_label}</a>
            <a class="btn" href="{esc(h['avail_url'])}" target="_blank" rel="noopener">See calendar</a>
            <a class="btn" href="{esc(maps)}" target="_blank" rel="noopener">Directions</a>
          </div>
        </div>"""

    # One unified grid of all campgrounds (mountain + beach), closest first; tabs filter it.
    display = sorted(status["results"] + status.get("beach", []),
                     key=lambda h: h["distance"])
    grid_cards = "".join(card_html(h) for h in display) or \
        "<div class='empty'>No 2–3 night openings in range right now — cancellations appear hourly.</div>"

    # Place tabs: All · 🏖️ Beaches · one per destination region present (closest first).
    region_btns = "".join(
        f"<button data-filter='region:{esc(r['slug'])}' class='cat' onclick='tab(this)'>"
        f"{esc(r['label'])} <span class='ct'>{r['count']}</span></button>"
        for r in status.get("regions", []))
    # Separate 🏖️ Beaches and 🏕️ State Parks tabs (both ReserveCalifornia, but beaches kept
    # on their own tab so they're easy to find).
    beach_btn = (f"<button data-filter='beach' class='cat' onclick='tab(this)'>🏖️ Beaches "
                 f"<span class='ct'>{status.get('beach_count',0)}</span></button>"
                 if status.get("beach_count") else "")
    park_count = sum(1 for h in display if h.get("state_park"))
    park_btn = (f"<button data-filter='statepark' class='cat' onclick='tab(this)'>🏕️ State Parks "
                f"<span class='ct'>{park_count}</span></button>" if park_count else "")
    weekend_count = sum(1 for h in display if h.get("has_weekend"))
    # Weekends is a TOGGLE (not a category) — it stacks on top of the current tab, so you can do
    # e.g. Beaches + Weekends. Separate handler (wtoggle) so tab() doesn't clear it.
    weekend_btn = (f"<button class='wk' onclick='wtoggle(this)'>🗓️ Weekends only "
                   f"<span class='ct'>{weekend_count}</span></button>" if weekend_count else "")
    everyday_count = sum(1 for h in display
                         if not (h.get("destination") and not h.get("everyday")))
    marquee_count = sum(1 for h in display if h.get("marquee"))
    marquee_btn = (f"<button data-filter='marquee' class='cat' onclick='tab(this)'>⭐ Sought-after "
                   f"<span class='ct'>{marquee_count}</span></button>" if marquee_count else "")
    wk_prefix = f"{weekend_btn}<span class='tabsep'></span>" if weekend_btn else ""
    tabs_html = (f"{wk_prefix}"
                 f"<button data-filter='all' class='cat on' onclick='tab(this)'>All "
                 f"<span class='ct'>{everyday_count}</span></button>{marquee_btn}{beach_btn}{park_btn}{region_btns}")

    def _full_label(h):
        if h.get("rating") is not None:
            return f"{h['rating']}★ ({h['reviews']})"
        return "🏖️ state beach" if h.get("state_beach") else "🏕️ state park"
    full_rows = "".join(
        f"<li><a href='{esc(h['book_url'])}' target='_blank' rel='noopener'>{esc(h['name'])}</a> "
        f"— {_full_label(h)} · {h['distance']:.0f} mi "
        f"<a class='alert' href='{esc(h['avail_url'])}' target='_blank' rel='noopener'>check / set alert</a></li>"
        for h in status["also_great_but_full"])
    full_section = (f"<h2>Also great nearby — currently full (cancellations open hourly)</h2>"
                    f"<ul class='also'>{full_rows}</ul>") if full_rows.strip() else ""

    tips = load_tips()
    tips_html = ""
    if tips and tips.get("tips"):
        items = "".join(f"<li>{esc(t)}</li>" for t in tips["tips"][:12])
        tips_html = (f"<details open><summary>🧭 Booking concierge "
                     f"<span class='muted'>(updated {esc(tips.get('updated',''))})</span></summary>"
                     f"<ul class='tips'>{items}</ul></details>")
    else:
        tips_html = ("<details open><summary>🧭 Booking tips</summary><ul class='tips'>"
                     "<li><b>Book the second you find a spot</b> — popular CA sites vanish in minutes. "
                     "Have a recreation.gov account logged in <i>before</i> you tap Book.</li>"
                     "<li><b>Set up the booking ahead of time:</b> save your vehicle plate, the number "
                     "of people, and a payment card in your recreation.gov profile so checkout is one tap.</li>"
                     "<li><b>Cancellations drop constantly</b> — re-open this page; the soonest openings "
                     "shift daily. A 2-night block often appears even when the calendar looked full.</li>"
                     "<li><b>Rolling release:</b> many CA campgrounds release 6 months out at 7am PT. "
                     "If a spot shows 'not yet released', mark the release date.</li>"
                     "<li><b>Arrive-day strategy:</b> for first-come overflow, weekday check-ins beat weekends.</li>"
                     "</ul></details>")

    c = status["counts"]
    cnt_line = (f"{c['with_openings']} campgrounds with {min(status['nights'])}–{max(status['nights'])} "
                f"night openings · {c['qualified']} well-reviewed within {status['max_distance']} mi · "
                f"{c['full']} great-but-full")
    err_line = (f"<div class='err'>⚠ {c['errors']} fetch errors this run (auto-retries next cycle)</div>"
                if c["errors"] else "")
    # Optional health banner written by the subscription doctor (campsage_doctor.sh).
    health_banner = ""
    try:
        H = json.loads(config.HEALTH_JSON.read_text())
        if H.get("status") and H["status"] != "HEALTHY":
            health_banner = (f"<div class='hbanner'>🩺 <b>{esc(H['status'])}</b> — "
                             f"{esc(H.get('summary',''))}"
                             + (f"<div class='hdiag'>{esc(H.get('diagnosis',''))}</div>"
                                if H.get('diagnosis') else "") + "</div>")
    except Exception:
        pass
    note_html = ("<div class='note'>📍 Tabs above group spots by <b>destination</b>. "
                 "🏖️ Beaches = ReserveCalifornia state beaches (no star reviews) · ⛰️ the rest = "
                 "recreation.gov · 📺 “social buzz” = YouTube popularity, not a satisfaction "
                 "rating · island camping excluded.</div>")

    # Location switcher: one pill per saved scan origin, baked in at render time.
    # (A location added to config shows up here after the next scan re-renders — noted,
    # self-heals on the following cron tick.)
    switcher_html = ""
    if all_locs and len(all_locs) > 1:
        pills = "".join(
            f"<a class='locpill{' on' if l.slug == loc.slug else ''}' "
            f"href='/camp/{esc(l.slug)}'>📍 {esc(l.name)}</a>"
            for l in all_locs)
        switcher_html = f"<div class='locs'>{pills}</div>"

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>🏕️ CampSage — California campsites</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  body {{ margin:0; font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:#0f1411; color:#e7efe9; padding:0 0 60px; }}
  header {{ position:sticky; top:0; background:#13201a; padding:14px 16px 10px;
    border-bottom:1px solid #25382e; z-index:5; }}
  h1 {{ margin:0; font-size:20px; }}
  .sub {{ color:#8fb3a0; font-size:13px; margin-top:3px; }}
  .stamp {{ color:#6f9583; font-size:12px; margin-top:4px; }}
  .tabs {{ display:flex; gap:8px; margin-top:10px; overflow-x:auto; white-space:nowrap;
    -webkit-overflow-scrolling:touch; padding-bottom:4px; scrollbar-width:none; }}
  .tabs::-webkit-scrollbar {{ display:none; }}
  .tabs button {{ flex:0 0 auto; padding:9px 13px; border:1px solid #2d4a3b; background:#16271f;
    color:#cfe9da; border-radius:20px; font-size:14px; font-weight:600; }}
  .tabs button.on {{ background:#0e5ea8; color:#fff; border-color:#1f7fd1; }}
  .tabsep {{ flex:0 0 auto; width:1px; background:#2d4a3b; margin:4px 2px; }}
  .tabs button.wk {{ border-color:#4a3f7a; background:#221d3a; color:#c9b8ff; }}
  .tabs button.wk.on {{ background:#5a3fb0; color:#fff; border-color:#7a5fd0; }}
  .locs {{ display:flex; gap:8px; margin-top:8px; overflow-x:auto; white-space:nowrap;
    -webkit-overflow-scrolling:touch; scrollbar-width:none; }}
  .locs::-webkit-scrollbar {{ display:none; }}
  .locpill {{ flex:0 0 auto; padding:6px 12px; border:1px solid #2d4a3b; background:#16271f;
    color:#cfe9da; border-radius:20px; font-size:13px; font-weight:600; text-decoration:none; }}
  .locpill.on {{ background:#1f8a4c; color:#fff; border-color:#1f8a4c; }}
  .ct {{ display:inline-block; background:#ffffff2e; border-radius:20px;
    padding:0 7px; font-size:12px; margin-left:3px; }}
  .regionchip {{ display:inline-block; background:#23323f; color:#9fcdf0; font-size:11px;
    padding:1px 7px; border-radius:20px; border:1px solid #34506a; }}
  .destchip {{ display:inline-block; background:#3a2a16; color:#f0c98a; font-size:11px;
    padding:1px 7px; border-radius:20px; border:1px solid #6a522e; }}
  .parkchip {{ display:inline-block; background:#1c3324; color:#9fe0b6; font-size:11px;
    padding:1px 7px; border-radius:20px; border:1px solid #2f6a43; }}
  .sorts {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
  .sorts button {{ flex:1 1 28%; padding:9px 4px; border:1px solid #2d4a3b; background:#16271f;
    color:#bfe6cf; border-radius:10px; font-size:13px; white-space:nowrap; }}
  .sorts button.on {{ background:#1f8a4c; color:#fff; border-color:#1f8a4c; }}
  .wrap {{ padding:14px 16px; }}
  .card {{ background:#15211b; border:1px solid #25382e; border-radius:14px;
    padding:14px; margin-bottom:12px; }}
  .row1 {{ display:flex; justify-content:space-between; gap:10px; align-items:baseline; }}
  .name {{ font-size:17px; font-weight:650; }}
  .dist {{ color:#7fd1a3; font-size:13px; white-space:nowrap; }}
  .meta {{ color:#9bbfac; font-size:13px; margin-top:3px; }}
  .stars {{ color:#ffcf5c; letter-spacing:1px; }}
  .badges {{ margin:8px 0 2px; }}
  .b {{ display:inline-block; font-size:11px; padding:3px 8px; border-radius:20px; margin-right:6px; }}
  .b3 {{ background:#1f8a4c33; color:#7fe6a8; border:1px solid #1f8a4c66; }}
  .b2 {{ background:#2a3f33; color:#a8d8bd; }}
  .b0 {{ background:#3a3326; color:#ffcf5c; border:1px solid #6b5a2e; }}
  .bw {{ background:#2e2a4a; color:#c9b8ff; border:1px solid #4a3f7a; }}
  .beachtag {{ color:#5cc8ff; font-weight:600; }}
  .sitehint {{ margin:6px 0 2px; font-size:12.5px; line-height:1.35; color:#ffe0a3;
               background:#3a30160d; background:rgba(255,207,92,.08); border-left:3px solid #ffcf5c;
               padding:5px 9px; border-radius:0 6px 6px 0; }}
  .sscore {{ margin:8px 0 2px; font-size:14px; }}
  .sscore a {{ text-decoration:none; color:#ffcf5c; }}
  .sscore b {{ color:#ffe08a; font-size:15px; }}
  .sscore .sst {{ color:#ffcf5c; letter-spacing:1px; }}
  .ssmeta {{ color:#7f9d8c; font-size:11px; }}
  .social {{ font-size:12px; color:#7f9d8c; margin:5px 0 2px; }}
  .social a {{ display:inline-block; background:#1b2a3a; color:#9fcdf0; text-decoration:none;
    border:1px solid #2a4866; border-radius:20px; padding:3px 10px; margin:2px 5px 2px 0; }}
  .note {{ background:#13202a; border:1px solid #244055; color:#a8c6d8; font-size:13px;
    border-radius:12px; padding:10px 12px; margin:0 0 12px; }}
  .op.muted {{ color:#7f9d8c; }}
  .ops {{ margin:8px 0; }}
  .op {{ font-size:14px; padding:6px 0; border-top:1px dashed #243a2e; }}
  .nights {{ display:inline-block; background:#0e3a23; color:#7fe6a8; font-size:12px;
    padding:1px 7px; border-radius:6px; margin-right:7px; }}
  .site {{ display:block; color:#7f9d8c; font-size:12px; margin-top:2px; }}
  .btns {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
  .btn {{ flex:1; min-width:46%; text-align:center; text-decoration:none; padding:11px 8px;
    border-radius:10px; font-size:14px; background:#16271f; color:#bfe6cf;
    border:1px solid #2d4a3b; }}
  .btn.book {{ background:#1f8a4c; color:#fff; border-color:#1f8a4c; font-weight:650; min-width:100%; }}
  details {{ background:#15211b; border:1px solid #25382e; border-radius:14px; padding:8px 14px; margin-top:6px; }}
  summary {{ cursor:pointer; font-weight:600; padding:6px 0; }}
  .tips li {{ margin:7px 0; color:#c8dccf; font-size:14px; }}
  .also li {{ margin:8px 0; font-size:14px; }}
  .also a {{ color:#7fd1a3; }}
  .alert {{ font-size:12px; color:#ffcf5c; margin-left:4px; }}
  .muted {{ color:#6f9583; font-weight:400; font-size:12px; }}
  .empty {{ color:#9bbfac; padding:18px; text-align:center; }}
  .err {{ color:#e0a35a; font-size:12px; margin-top:6px; }}
  .hbanner {{ background:#3a1f1f; border:1px solid #7a3b3b; color:#ffd9d9; font-size:13px;
    border-radius:10px; padding:9px 12px; margin-top:8px; }}
  .hdiag {{ color:#f0b9b9; font-size:12px; margin-top:5px; white-space:pre-wrap; }}
  a {{ color:#7fd1a3; }}
  h2 {{ font-size:15px; color:#9bbfac; margin:22px 0 8px; }}
  h2.first {{ margin-top:4px; }}
  footer {{ color:#5d7d6c; font-size:12px; text-align:center; padding:18px; }}
</style></head>
<body>
<header>
  <h1>🏕️ CampSage <a href="/camp/{esc(loc.slug)}/map" style="font-size:14px;font-weight:600;color:#7fd1a8;text-decoration:none;vertical-align:middle;margin-left:6px;padding:3px 10px;border:1px solid #2e7d5b;border-radius:16px">🗺️ Map</a></h1>
  <div class="sub">Great California campsites · 2–3 nights at one spot · closest first</div>
  {switcher_html}
  <div class="stamp">Updated {esc(status['generated_at'])} · {esc(cnt_line)}</div>
  {err_line}
  {health_banner}
  <div class="tabs">{tabs_html}</div>
  <div class="sorts" data-target="grid">
    <button data-sort="distance" class="on" onclick="srt(this)">Closest</button>
    <button data-sort="social" onclick="srt(this)">Social buzz</button>
    <button data-sort="best" onclick="srt(this)">Best reviewed</button>
    <button data-sort="soonest" onclick="srt(this)">Soonest</button>
  </div>
</header>
<div class="wrap">
  {note_html}
  <div id="grid">{grid_cards}</div>
  {tips_html}
  {full_section}
  <footer>Data: recreation.gov + ReserveCalifornia · CampSage refreshes 3×/day · pull-to-refresh for latest</footer>
</div>
<script>
var curCat='all', wkOn=false;
function applyFilters(){{
  var reg = curCat.indexOf('region:')==0 ? curCat.slice(7) : null;
  document.querySelectorAll('#grid .card').forEach(function(c){{
    var catOk = (curCat=='marquee') ? (c.dataset.marquee=='1' && c.dataset.full!='1')
              : (((curCat=='all' && c.dataset.everyday=='1') || (curCat=='beach' && c.dataset.beach=='1') || (curCat=='statepark' && c.dataset.statepark=='1') || (reg && c.dataset.region==reg)) && c.dataset.full!='1');
    var wkOk = !wkOn || c.dataset.weekend=='1';
    c.style.display = (catOk && wkOk) ? '' : 'none';
    c.querySelectorAll('.op').forEach(function(op){{        // weekends on → show only Fri+Sat rows
      op.style.display = (wkOn && !op.classList.contains('wkop')) ? 'none' : '';
    }});
  }});
}}
function tab(btn){{                                  // category tabs (mutually exclusive)
  document.querySelectorAll('.tabs button.cat').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  curCat = btn.dataset.filter;
  applyFilters();
  window.scrollTo(0,0);
}}
function wtoggle(btn){{                              // Weekends — a toggle that STACKS on the tab
  wkOn = !wkOn;
  btn.classList.toggle('on', wkOn);
  applyFilters();
}}
function srt(btn){{
  var bar=btn.closest('.sorts');
  bar.querySelectorAll('button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  var k=btn.dataset.sort, L=document.getElementById(bar.dataset.target||'list');
  var cards=[].slice.call(L.querySelectorAll('.card'));
  function rat(c){{return parseFloat(c.dataset.rating);}}
  function rev(c){{return parseFloat(c.dataset.reviews);}}
  // "Best reviewed" = highest rating weighted by how many reviews back it up, so a
  // 4.7★ with 986 reviews beats a 4.6★ with 5 (credibility-weighted, not raw stars).
  function best(c){{return rat(c)*Math.log10(rev(c)+1);}}
  cards.sort(function(a,b){{
    if(k=='best')    return best(b)-best(a);
    if(k=='reviews') return rev(b)-rev(a) || rat(b)-rat(a);
    if(k=='rating')  return rat(b)-rat(a) || rev(b)-rev(a);   // ties -> more reviews first
    if(k=='sites')   return (parseFloat(b.dataset.sites)||0)-(parseFloat(a.dataset.sites)||0);
    if(k=='social')  return (parseFloat(b.dataset.social)||0)-(parseFloat(a.dataset.social)||0);
    if(k=='soonest') return a.dataset.soonest.localeCompare(b.dataset.soonest);
    return parseFloat(a.dataset.distance)-parseFloat(b.dataset.distance);
  }});
  cards.forEach(c=>L.appendChild(c));
}}
</script>
</body></html>"""
    loc.dir.mkdir(parents=True, exist_ok=True)
    loc.dashboard_html.write_text(page)


# ──────────────────────────────────────────────────────────────────────────────
LOG_PREFIX = ""     # set to "[<slug>] " during a location's scan


def log(msg):
    stamp = datetime.now(PT).strftime('%Y-%m-%d %H:%M:%S') if PT else datetime.now()
    line = f"[{stamp}] {LOG_PREFIX}{msg}"
    print(line, flush=True)
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    global LOG_PREFIX
    locs = locations.load_locations()      # geocodes up front; exits loudly on failure
    only = sys.argv[sys.argv.index("--location") + 1] if "--location" in sys.argv else None
    todo = [l for l in locs if l.slug == only] if only else locs
    if only and not todo:
        sys.exit(f"unknown location {only!r}; configured: {', '.join(l.slug for l in locs)}")
    for i, loc in enumerate(todo):
        if i:
            time.sleep(120)                # let the API's trailing quota window slide
        LOG_PREFIX = f"[{loc.slug}] " if len(locs) > 1 else ""
        status = run(loc, locs)
        locations.update_index(loc, status, locs)   # index valid after EVERY location
    LOG_PREFIX = ""


if __name__ == "__main__":
    main()
