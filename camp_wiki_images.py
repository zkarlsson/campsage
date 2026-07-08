#!/usr/bin/env python3
"""
camp_wiki_images.py — enrich CampSage state parks/beaches (whose ReserveCalifornia image URLs are
dead 404s) with a GALLERY of real photos from Wikimedia Commons via Wikipedia's free, keyless API.

Uses the media-list endpoint to pull up to 5 scenic photos per park (filtered to real jpg/png, skips
maps/logos/icons), best resolution per image. Caches {site name -> [image urls]} at
~/campsage/wiki_images.json. The /camp/map route reads it as a gallery for beach/park pins. Idempotent
(skips names already cached), keyless, read-only. Cron: daily after the morning scan.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

import config

STATUS = config.STATUS_JSON
CACHE = config.DATA_DIR / "wiki_images.json"
UA = {"User-Agent": "CampSage/1.0 (personal campsite finder)"}
MAX_IMGS = 5
_SKIP = re.compile(r"(map|logo|icon|locator|seal|diagram|coat_of_arms|flag|svg)", re.I)


def _expand(name):
    n = re.sub(r"\bSB\b", "State Beach", name)
    n = re.sub(r"\bSP\b", "State Park", n)
    n = re.sub(r"\bSRA\b", "State Recreation Area", n)
    n = re.sub(r"\bSHP\b", "State Historic Park", n)
    return re.sub(r"\s+", " ", n).strip()


def _candidates(name):
    full = _expand(name)
    cands = [full]
    stripped = re.sub(r"\s+(?:Moro\s+|Lower\s+|Upper\s+)?(?:Campground|Camp|Loop|Group|Area|CG).*$",
                      "", full, flags=re.I).strip()
    if stripped and stripped != full:
        cands.append(stripped)
    return cands


def _get(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=10).read())


def _best_src(item):
    """Highest-resolution valid Wikimedia thumbnail URL from a media-list item's srcset."""
    best, bw = None, 0
    for s in item.get("srcset", []):
        src = s.get("src", "")
        m = re.search(r"/(\d+)px-", src)
        w = int(m.group(1)) if m else 0
        if w >= bw:
            bw, best = w, src
    if not best:
        return None
    return ("https:" + best) if best.startswith("//") else best


def _fname(url):
    """Normalized filename for de-duping (strip the size + path)."""
    m = re.search(r"/([^/]+?)(?:/\d+px-[^/]+)?$", url or "")
    return (m.group(1) if m else url or "").lower()


def _wiki_images(name):
    """Return up to MAX_IMGS photo URLs: the reliable summary thumbnail FIRST (guaranteed baseline),
    then extra scenic photos from the media-list. Never regresses below the single-image version."""
    for title in _candidates(name):
        t = title.replace(" ", "_")
        out, seen = [], set()
        # 1) reliable primary — the summary thumbnail (this is what worked for ~23 sites before)
        try:
            s = _get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(t))
            th = (s.get("thumbnail") or {}).get("source")
            if th:
                out.append(th); seen.add(_fname(th))
        except Exception:
            pass
        # 2) extras from the media-list (skip near-dupes of the primary)
        try:
            j = _get("https://en.wikipedia.org/api/rest_v1/page/media-list/" + urllib.parse.quote(t))
            for m in j.get("items", []):
                if m.get("type") != "image":
                    continue
                ttl = m.get("title", "")
                if not re.search(r"\.(jpe?g|png)$", ttl, re.I) or _SKIP.search(ttl):
                    continue
                src = _best_src(m)
                if src and _fname(src) not in seen:
                    out.append(src); seen.add(_fname(src))
                if len(out) >= MAX_IMGS:
                    break
        except Exception:
            pass
        if out:
            return out
    return []


def _status_files():
    """Every scanned location's status.json (multi-location), else the legacy flat one."""
    import locations
    idx = locations.read_index()
    if idx:
        files = [config.DATA_DIR / "locations" / e["slug"] / "status.json"
                 for e in idx.get("locations", []) if e.get("slug")]
        return [f for f in files if f.exists()]
    return [STATUS] if STATUS.exists() else []


def run():
    files = _status_files()
    if not files:
        print("no status.json yet"); return 1
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    # migrate any old string-format entries to lists
    cache = {k: ([v] if isinstance(v, str) and v else (v if isinstance(v, list) else []))
             for k, v in cache.items()}

    # Union of names across all locations — the cache is keyed by name, so a park
    # reachable from two locations is fetched once.
    targets = []
    for f in files:
        d = json.loads(f.read_text())
        for arr in (d.get("beach") or [], d.get("results") or []):
            for s in arr:
                img = s.get("image") or ""
                if (not img) or ("cali-content" in img):
                    targets.append(s.get("name", ""))
    targets = [t for t in dict.fromkeys(targets) if t and t not in cache]

    added = 0
    for name in targets:
        imgs = _wiki_images(name)
        cache[name] = imgs
        if imgs:
            added += 1
        print(f"  {name:<30} -> {len(imgs)} photo(s)")
        time.sleep(0.3)                          # be polite to Wikipedia (avoid 429)
    CACHE.write_text(json.dumps(cache, indent=2))
    total = sum(len(v) for v in cache.values())
    print(f"[camp_wiki_images] {added}/{len(targets)} parks newly enriched; "
          f"cache {len(cache)} sites / {total} photos")
    return 0


if __name__ == "__main__":
    sys.exit(run())
