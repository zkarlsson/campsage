#!/usr/bin/env python3
"""
ReserveCalifornia client for CampSage — finds MAINLAND drive-up beach campgrounds
(Leo Carrillo, San Onofre, Carpinteria, El Capitán, Refugio, Pismo, …) with 2-3
consecutive nights open at the same site.

These state beaches are NOT on recreation.gov; they book through ReserveCalifornia,
whose public RDR API (the same one the website calls) is reverse-mapped here:
  • search/place                 -> nearby parks + distance from home
  • search/place (PlaceId set)   -> a park's facilities (campgrounds)
  • search/grid (FacilityId set) -> per-unit, per-night availability (IsFree)

No API key. The API has no review/rating data, so results rank by distance + soonest.
"""
import json
import time
import urllib.request
import urllib.error
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

# The live API base (read from reservecalifornia.com's own config.json -> rdrApiUrl).
RDR = "https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr"
BOOK = "https://www.reservecalifornia.com/#!park/{pid}"          # 200, deep-links the SPA
HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Origin": "https://www.reservecalifornia.com",
    "Referer": "https://www.reservecalifornia.com/",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}
GRID_CHUNK_DAYS = 28        # request availability in <=4-week windows, then merge


def _post(path, body):
    data = json.dumps(body).encode()
    last = None
    for attempt in range(config.RETRIES):
        try:
            req = urllib.request.Request(RDR + "/" + path, data=data,
                                         headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1)); continue
            if e.code == 404:
                return None
            time.sleep(0.8 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"POST {path} failed: {last}")


def is_beach_place(name):
    if any(w in name for w in config.BEACH_VETO):
        return False
    return name.endswith(" SB") or " Beach" in name or name in config.BEACH_ALLOW


def search_places(lat, lng, start_date):
    d = _post("search/place", {
        "PlaceId": 0, "Latitude": lat, "Longitude": lng, "HighlightedPlaceId": 0,
        "StartDate": start_date, "Nights": min(config.NIGHTS), "CountNearby": True,
        "NearbyLimit": 150, "Sort": "Distance", "CustomerId": "0",
        "RefreshFavourites": True, "IsADA": False, "UnitCategoryId": 1,
        "SleepingUnitId": 0, "MinVehicleLength": 0, "UnitTypesGroupIds": [],
    })
    return (d or {}).get("NearbyPlaces", []) or []


def facilities_for(place_id, start_date):
    d = _post("search/place", {
        "PlaceId": place_id, "Latitude": config.HOME_LAT, "Longitude": config.HOME_LNG,
        "HighlightedPlaceId": place_id, "StartDate": start_date, "Nights": min(config.NIGHTS),
        "CountNearby": False, "NearbyLimit": 0, "Sort": "Distance", "CustomerId": "0",
        "RefreshFavourites": True, "IsADA": False, "UnitCategoryId": 1,
        "SleepingUnitId": 0, "MinVehicleLength": 0, "UnitTypesGroupIds": [],
    })
    fac = ((d or {}).get("SelectedPlace") or {}).get("Facilities") or {}
    out = fac.values() if isinstance(fac, dict) else fac
    return [(f.get("FacilityId"), f.get("Name")) for f in out if f.get("FacilityId")]


def free_dates_by_unit(facility_id, win_start, win_end):
    """Merge grid chunks -> {unit_name: set(of 'YYYY-MM-DD' nights that are free+bookable)}."""
    units = {}
    cur = win_start
    while cur <= win_end:
        chunk_end = min(cur + timedelta(days=GRID_CHUNK_DAYS), win_end)
        d = _post("search/grid", {
            "FacilityId": facility_id, "StartDate": cur.isoformat(),
            "EndDate": chunk_end.isoformat(), "Nights": 1, "IsADA": False,
            "UnitCategoryId": 1, "SleepingUnitId": 0, "MinVehicleLength": 0,
            "UnitTypesGroupIds": [],
        })
        for u in (((d or {}).get("Facility") or {}).get("Units") or {}).values():
            if not (u.get("AllowWebBooking", True) and u.get("IsWebViewable", True)):
                continue
            name = u.get("ShortName") or u.get("Name") or str(u.get("UnitId"))
            s = units.setdefault(name, set())
            for sl in (u.get("Slices") or {}).values():
                if sl.get("IsFree") and not sl.get("IsBlocked"):
                    s.add(sl.get("Date"))
        cur = chunk_end + timedelta(days=1)
    return units


def _earliest_block(free_dates, win_start, win_end):
    """Earliest run of >=min(NIGHTS) consecutive free nights, capped at max(NIGHTS)."""
    avail = sorted(d for d in free_dates
                   if d and win_start <= date.fromisoformat(d) <= win_end)
    if not avail:
        return None
    runs, run = [], [avail[0]]
    for a, b in zip(avail, avail[1:]):
        if date.fromisoformat(b) - date.fromisoformat(a) == timedelta(days=1):
            run.append(b)
        else:
            runs.append(run); run = [b]
    runs.append(run)
    want_min, want_max = min(config.NIGHTS), max(config.NIGHTS)
    for r in runs:
        if len(r) < want_min:
            continue
        nights = min(want_max, len(r))
        start = date.fromisoformat(r[0])
        if config.WEEKENDS_ONLY and not any(
                (start + timedelta(days=i)).weekday() in (4, 5) for i in range(nights)):
            continue
        return {"start": r[0], "end": (start + timedelta(days=nights)).isoformat(),
                "nights": nights}
    return None


def _weekend_block(avail_set, win_start, win_end):
    """Earliest bookable block covering a FRIDAY *and* SATURDAY night (arrive Fri; extend to Sun if
    free). Scans all free nights, so a weekend hidden behind an earlier weekday block is still found."""
    avail = {d for d in avail_set if d and win_start <= date.fromisoformat(d) <= win_end}
    for d in sorted(avail):
        dt = date.fromisoformat(d)
        if dt.weekday() != 4 or (dt + timedelta(days=1)).isoformat() not in avail:
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


def analyze_place(place, win_start, win_end):
    pid = place.get("PlaceId")
    blocks, wk = [], []
    for fid, fname in facilities_for(pid, win_start.isoformat()):
        units = free_dates_by_unit(fid, win_start, win_end)
        for uname, dates in units.items():
            label = f"{fname} · {uname}".strip(" ·")
            blk = _earliest_block(dates, win_start, win_end)
            if blk:
                blocks.append({**blk, "site_label": label})
            wb = _weekend_block(dates, win_start, win_end)
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


def _card(place, state_beach=True):
    pid = place.get("PlaceId")
    return {
        "id": pid,
        "name": place.get("Name"),
        "parent": "California State Parks · ReserveCalifornia",
        "rating": None, "reviews": 0,                       # state system: no review scores
        "distance": round(float(place.get("MilesFromSelected") or 0), 1),
        "lat": place.get("Latitude"), "lng": place.get("Longitude"),
        "price": {}, "image": place.get("ImageUrl") or "",
        "book_url": BOOK.format(pid=pid),
        "avail_url": BOOK.format(pid=pid),
        "state_beach": state_beach,
    }


def _haversine(lat1, lng1, lat2, lng2):
    import math
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_anchor(lat, lng):
    best = None
    for label, alat, alng in config.REGION_ANCHORS:
        d = _haversine(lat, lng, alat, alng)
        if best is None or d < best[0]:
            best = (d, label)
    return best[1] if best else None


def is_campable_place(name):
    """A general state-park camping place: reuse the beach veto (drops day-use / cottages /
    trailers / OHV-dune / lake-only / historic units) but DON'T require it to be a beach."""
    return not any(w in (name or "") for w in config.BEACH_VETO)


def discover_state_parks(win_start, win_end, errors, log=lambda *_: None):
    """General CA state-park campgrounds (non-beach) via ReserveCalifornia, searched from home
    AND each region anchor (so far parks like Big Sur's are found), merged + deduped, ranked by
    distance-from-LA. Returns cards (state_park=True, no ratings) with opening info."""
    centers = [(config.HOME_LAT, config.HOME_LNG, None)]
    centers += [(alat, alng, label) for label, alat, alng in config.REGION_ANCHORS]
    raw = {}
    for lat, lng, label in centers:
        try:
            for p in search_places(lat, lng, win_start.isoformat()):
                pid = p.get("PlaceId")
                nm = p.get("Name") or ""
                if not pid or pid in raw:
                    continue
                if is_beach_place(nm):                 # beaches handled by discover_beaches
                    continue
                if not is_campable_place(nm):
                    continue
                raw[pid] = p
        except Exception as e:
            errors.append(f"stateparks search {label or 'home'}: {e}")
        time.sleep(0.3)                                # be polite: ~15 searches
    # Distance from LA + nearest region anchor (group for the per-region cap).
    for p in raw.values():
        la, lo = p.get("Latitude"), p.get("Longitude")
        try:
            p["_dist"] = _haversine(config.HOME_LAT, config.HOME_LNG, float(la), float(lo))
            p["_region"] = _nearest_anchor(float(la), float(lo))
        except (TypeError, ValueError):
            p["_dist"] = float(p.get("MilesFromSelected") or 9e9)
            p["_region"] = None
    # Keep the nearest STATE_PARK_PER_ANCHOR per region (bounds availability calls), then a
    # global cap — closest first — so a far cluster can't blow the API budget.
    per = {}
    for p in sorted(raw.values(), key=lambda p: p["_dist"]):
        if p["_dist"] > config.REGION_MAX_DISTANCE_MI:
            continue
        r = p["_region"]
        if per.get(r, 0) >= config.STATE_PARK_PER_ANCHOR:
            continue
        per[r] = per.get(r, 0) + 1
        p["_keep"] = True
    cands = sorted((p for p in raw.values() if p.get("_keep")),
                   key=lambda p: p["_dist"])[:config.STATE_PARK_MAX_ANALYZE]
    log(f"  {len(raw)} state parks found, analyzing {len(cands)} (closest per region)…")
    out = []
    with ThreadPoolExecutor(max_workers=max(2, config.MAX_WORKERS // 2)) as ex:
        futs = {ex.submit(analyze_place, p, win_start, win_end): p for p in cands}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                errors.append(f"statepark {p.get('Name')}: {e}")
                continue
            card = _card(p, state_beach=False)
            card["distance"] = round(p["_dist"], 1)           # distance from LA, not anchor
            card["state_park"] = True
            card["everyday"] = p["_dist"] <= config.MAX_DISTANCE_MI
            card["destination"] = None if card["everyday"] else (p["_region"] or "State Park")
            out.append({**card, **res})
    out.sort(key=lambda h: h["distance"])
    return out


def discover_beaches(win_start, win_end, errors):
    places = search_places(config.HOME_LAT, config.HOME_LNG, win_start.isoformat())
    cands = [p for p in places
             if is_beach_place(p.get("Name") or "")
             and float(p.get("MilesFromSelected") or 9e9) <= config.BEACH_MAX_DISTANCE]
    out = []
    with ThreadPoolExecutor(max_workers=max(2, config.MAX_WORKERS // 2)) as ex:
        futs = {ex.submit(analyze_place, p, win_start, win_end): p for p in cands}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                errors.append(f"beach {p.get('Name')}: {e}")
                continue
            out.append({**_card(p), **res})
    out.sort(key=lambda h: h["distance"])       # closest first (no ratings available)
    return out
