#!/usr/bin/env python3
"""
Social score for CampSage — a real, no-API-key signal from YouTube.

Live scraping of Reddit/Instagram/TikTok is impossible from a server (they 403 datacenter
IPs). YouTube's results page IS reachable, so we derive an honest "social buzz" score per
campground from how many videos people post and how many views they pull. This is a
POPULARITY signal (buzz), not a satisfaction rating — labelled as such on the page.

Score = stars 0-5 mapped from the summed views of the top videos for "<name> camping",
plus the raw video/view counts and a link. Cached on disk (buzz moves slowly) so the
3×/day scan stays light and reliable.
"""
import json
import re
import time
import urllib.parse
import urllib.request

import config

YT = "https://www.youtube.com/results?search_query="
CACHE = config.DATA_DIR / "social_cache.json"
TTL_SECONDS = 12 * 3600         # ~12h: the 7am run re-scores everything daily; 1pm/6pm reuse cache
MAX_NEW_PER_RUN = 60            # enough to refresh the whole displayed set in the morning run
TOP_VIDEOS = 12

# buzz (summed top-video views) -> stars. Absolute + explainable, so it's stable run-to-run.
_BANDS = [(2_000_000, 5.0), (600_000, 4.5), (150_000, 4.0), (40_000, 3.5),
          (10_000, 3.0), (2_500, 2.5), (500, 2.0), (1, 1.5)]


def _expand(name):
    for ab, full in ((" SB", " State Beach"), (" SP", " State Park"),
                     (" SRA", " State Recreation Area"), (" SHP", " State Historic Park")):
        if name.endswith(ab):
            return name[: -len(ab)] + full
    return name


def _stars(views):
    for thresh, st in _BANDS:
        if views >= thresh:
            return st
    return 0.0


def _fetch(name):
    query = _expand(name) + " camping"
    url = YT + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
        html = r.read().decode("utf-8", "replace")
    ids = set(re.findall(r'"videoId":"([\w-]{11})"', html))
    views = [int(v.replace(",", "")) for v in re.findall(r'(\d[\d,]{0,11}) views', html)]
    views.sort(reverse=True)
    top_views = sum(views[:TOP_VIDEOS])
    if not ids:
        return None
    return {
        "stars": _stars(top_views),
        "videos": len(ids),
        "views": top_views,
        "url": url,
        "ts": int(time.time()),
    }


def _load():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _save(cache):
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, indent=0))
    except Exception:
        pass


def attach_scores(cards, log=lambda *_: None):
    """Add a 'social_score' dict to each card, using the disk cache; fetch the stalest
    few live (bounded per run). Cards we couldn't score yet simply get no score line."""
    cache = _load()
    now = time.time()
    fresh = {k: v for k, v in cache.items()
             if isinstance(v, dict) and now - v.get("ts", 0) < TTL_SECONDS}
    # which cards need a (re)fetch — stalest / never-seen first
    need = [c for c in cards if c["name"] not in fresh]
    new = 0
    for c in need:
        if new >= MAX_NEW_PER_RUN:
            break
        try:
            sc = _fetch(c["name"])
            new += 1
            time.sleep(0.4)                       # be polite
            if sc:
                cache[c["name"]] = fresh[c["name"]] = sc
        except Exception as e:
            log(f"social: {c['name']}: {e}")
    if new:
        _save(cache)
        log(f"social: scored {new} new campground(s); {len(fresh)} cached total")
    for c in cards:
        c["social_score"] = fresh.get(c["name"])
    return cards
