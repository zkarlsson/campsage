#!/usr/bin/env python3
"""CampSage self-test — validates the whole pipeline end to end, for every saved
location (or one via --location <slug>). Exit 0 = all pass."""
import json, os, subprocess, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import date, timedelta
from pathlib import Path
import config

# In the docker-compose stack the scheduler is supercronic (crontab.scan) and the
# concierge/doctor helpers (Claude-subscription, host-side) are not deployed.
IN_CONTAINER = os.environ.get("CAMPSAGE_CONTAINER") == "1"

PT = "https://www.recreation.gov"
UA = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
ok = True

def fetch_json(url):
    """GET JSON with backoff on 429/5xx — the live API rate-limits bursts."""
    for attempt in range(5):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=30).read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 * (attempt + 1)); continue
            raise

def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

def skip(name, why):
    # A live-API rate-limit (429) blocks re-verification but is NOT a CampSage fault — warn, don't fail.
    print(f"  [SKIP] {name} — {why}")


def http_body(url):
    return urllib.request.urlopen(url, timeout=15).read().decode()


def location_entries():
    """[(slug, name, lat, lng, status_path, page_url)] from locations.json, or the
    legacy flat layout as a single pseudo-location."""
    try:
        idx = json.loads((config.DATA_DIR / "locations.json").read_text())
        return [(e["slug"], e.get("name", e["slug"]), e.get("lat"), e.get("lng"),
                 config.DATA_DIR / "locations" / e["slug"] / "status.json",
                 f"http://127.0.0.1:5001/camp/{e['slug']}")
                for e in idx.get("locations", [])], idx
    except Exception:
        return [("", "legacy", None, None, config.STATUS_JSON,
                 "http://127.0.0.1:5001/camp")], None


def check_status_invariants(p, s, status_path):
    """Sections 2/3b/3c: status.json shape, distances, beaches, regions — all
    origin-relative, so identical for every location."""
    try:
        age_h = (time.time() - s.get("generated_epoch", 0)) / 3600
        check(f"{p}status.json present + parses", True)
        check(f"{p}status.json fresh (<24h)", age_h < 24, f"{age_h:.1f}h old")
        check(f"{p}has campgrounds with openings", s["counts"]["with_openings"] > 0,
              f"{s['counts']['with_openings']} found")
        # results = everyday spots (closest-first, within the everyday radius) followed by
        # far-destination finds (anchor search, region tabs only, within the outer cap).
        everyday = [h for h in s["results"] if h.get("everyday", True)]
        dest = [h for h in s["results"] if not h.get("everyday", True)]
        check(f"{p}everyday results sorted closest-first",
              all(everyday[i]["distance"] <= everyday[i+1]["distance"]
                  for i in range(len(everyday)-1)))
        check(f"{p}everyday results within max distance",
              all(h["distance"] <= config.MAX_DISTANCE_MI for h in everyday))
        check(f"{p}destination finds within outer cap",
              all(h["distance"] <= config.REGION_MAX_DISTANCE_MI for h in dest))
        # State parks (ReserveCalifornia) carry no review scores — rating is None by design.
        check(f"{p}all rated spots meet min rating",
              all(h["rating"] >= config.MIN_RATING
                  for h in s["results"] if h.get("rating") is not None))
        check(f"{p}openings respect night bounds",
              all(min(config.NIGHTS) <= o["nights"] <= max(config.NIGHTS)
                  for h in s["results"] for o in h["openings"]))
        check(f"{p}no fetch errors", s["counts"]["errors"] == 0, f"{s['counts']['errors']} errors")
    except Exception as e:
        check(f"{p}status.json invariants", False, str(e))

    # beach section = MAINLAND state beaches (ReserveCalifornia), no islands
    try:
        b = s.get("beach", [])
        check(f"{p}beach section populated", len(b) > 0, f"{s.get('beach_count')} state beaches")
        check(f"{p}beach is all state beaches", all(h.get("state_beach") for h in b))
        check(f"{p}beach has NO island camping",
              not any("island" in (h["name"] or "").lower() for h in b))
        check(f"{p}beach within max distance",
              all(h["distance"] <= config.BEACH_MAX_DISTANCE for h in b))
        check(f"{p}beach sorted closest-first",
              all(b[i]["distance"] <= b[i+1]["distance"] for i in range(len(b)-1)))
        check(f"{p}beach has 2-3 night openings",
              sum(1 for h in b if h.get("openings")) > 0,
              f"{sum(1 for h in b if h.get('openings'))} with openings")
        check(f"{p}beach openings respect night bounds",
              all(min(config.NIGHTS) <= o["nights"] <= max(config.NIGHTS)
                  for h in b for o in h.get("openings", [])))
    except Exception as e:
        check(f"{p}beach section populated", False, str(e))

    # destination regions (place tabs)
    try:
        regs = s.get("regions", [])
        check(f"{p}regions computed", len(regs) > 0,
              ", ".join(f"{r['label']}({r['count']})" for r in regs[:6]) + ("…" if len(regs) > 6 else ""))
        check(f"{p}every displayed campground has a region",
              all(h.get("region_slug") for h in (s["results"] + s.get("beach", []))))
        check(f"{p}region counts add up",
              sum(r["count"] for r in regs) == len(s["results"]) + len(s.get("beach", [])))
    except Exception as e:
        check(f"{p}regions computed", False, str(e))


def check_live_reverify(p, s):
    """Section 3: a claimed opening is independently real (merge months — a block can
    cross a boundary). Federal only — state parks/beaches live on ReserveCalifornia."""
    try:
        h = next(h for h in s["results"]
                 if not h.get("state_park") and not h.get("state_beach") and h.get("openings"))
        o = h["openings"][0]
        need = [(date.fromisoformat(o["start"]) + timedelta(days=i)).isoformat()
                for i in range(o["nights"])]
        merged = {}  # site_id -> {date: status}, across every month the block touches
        for ms in sorted({(n[:7] + "-01T00:00:00.000Z") for n in need}):
            q = urllib.parse.urlencode({"start_date": ms})
            d = fetch_json(f"{PT}/api/camps/availability/campground/{h['id']}/month?{q}")
            for sid, c in d["campsites"].items():
                merged.setdefault(sid, {}).update(c.get("availabilities", {}))
        real = sum(1 for av in merged.values()
                   if all(str(av.get(n + "T00:00:00Z", "")).lower() == "available" for n in need))
        check(f"{p}top opening is genuinely available", real > 0,
              f"{real} sites open for {h['name']} {o['start']}")
    except Exception as e:
        if "429" in str(e):
            skip(f"{p}top opening is genuinely available", "API rate-limited (try again in a minute)")
        else:
            check(f"{p}top opening is genuinely available", False, str(e))

    # independently re-confirm one beach opening via the live ReserveCalifornia API
    try:
        import reservecalifornia as rc
        bo = next((h for h in s.get("beach", []) if h.get("openings")), None)
        if bo:
            o = bo["openings"][0]
            ws = date.fromisoformat(o["start"]); we = date.fromisoformat(o["end"])
            need = {(ws + timedelta(days=i)).isoformat() for i in range(o["nights"])}
            ok_unit = False
            for fid, _ in rc.facilities_for(bo["id"], ws.isoformat(), bo.get("lat"), bo.get("lng")):
                for dates in rc.free_dates_by_unit(fid, ws, we - timedelta(days=1)).values():
                    if need <= dates:
                        ok_unit = True; break
                if ok_unit:
                    break
            check(f"{p}a beach opening is genuinely available (ReserveCalifornia)", ok_unit,
                  f"{bo['name']} {o['nights']}N from {o['start']}")
    except Exception as e:
        check(f"{p}beach opening re-verification", False, str(e))


def check_page(p, url, other_slugs):
    """Section 4: the dashboard serves and has its UI parts (+ switcher links)."""
    try:
        body = http_body(url)
        check(f"{p}dashboard serves", "CampSage" in body and "card" in body)
        check(f"{p}shows Beaches tab", "data-filter='beach'" in body or "🏖️ Beaches" in body)
        check(f"{p}shows place tabs + region chips",
              "function tab(" in body and body.count("data-region=") >= 10
              and "regionchip" in body)
        check(f"{p}shows social score (number + stars)",
              "social buzz" in body and "class='sscore'" in body)
        check(f"{p}shows social review links",
              "reddit.com/search" in body and "youtube.com/results" in body
              and body.count("💬 Reviews") >= 10)
        if other_slugs:
            check(f"{p}location switcher links present",
                  all(f"/camp/{s2}" in body for s2 in other_slugs))
    except Exception as e:
        check(f"{p}dashboard serves", False, str(e))


print("CampSage self-test")
want = sys.argv[sys.argv.index("--location") + 1] if "--location" in sys.argv else None
entries, index = location_entries()
if want:
    entries = [e for e in entries if e[0] == want] or sys.exit(f"unknown location {want!r}")
multi = len(entries) > 1 or (index and len(index.get("locations", [])) > 1)

# 1. live endpoints reachable (from the first location's coords)
try:
    lat = entries[0][2] or 36.78
    lng = entries[0][3] or -119.42
    q = urllib.parse.urlencode({"fq": "entity_type:campground", "lat": lat,
                                "lng": lng, "radius": 50, "size": 1})
    d = fetch_json(f"{PT}/api/search?{q}")
    check("recreation.gov search API", int(d.get("total", 0)) > 0, f"{d.get('total')} results")
except Exception as e:
    check("recreation.gov search API", False, str(e))

# 1b. locations index sanity (multi-location layout only)
if index is not None:
    idx_slugs = [e[0] for e in entries]
    check("locations.json lists every configured location",
          bool(idx_slugs), ", ".join(idx_slugs))
    try:
        r = urllib.request.urlopen("http://127.0.0.1:5001/camp", timeout=15)
        check("/camp lands on the default location",
              r.geturl().rstrip("/").endswith(index.get("default", "")))
    except Exception as e:
        check("/camp lands on the default location", False, str(e))
    try:
        # Route precedence: the static /camp/data route must win over /camp/<slug>.
        r = urllib.request.urlopen("http://127.0.0.1:5001/camp/data", timeout=15)
        check("/camp/data resolves (route precedence)", r.status == 200)
    except Exception as e:
        check("/camp/data resolves (route precedence)", False, str(e))

# 2-4 per location
for slug, name, lat, lng, status_path, page_url in entries:
    p = f"[{slug}] " if (multi and slug) else ""
    if multi:
        print(f"  — {name} —")
    try:
        s = json.loads(status_path.read_text())
    except Exception as e:
        check(f"{p}status.json present + parses", False, str(e))
        continue
    check_status_invariants(p, s, status_path)
    check_live_reverify(p, s)
    other = [e[0] for e in entries if e[0] and e[0] != slug]
    check_page(p, page_url, other)

# 5. booking tips + health + schedule presence
if IN_CONTAINER:
    # Concierge/doctor are Claude-subscription host-side helpers, not part of the container
    # stack — the page degrades gracefully without their JSON. Scheduler is supercronic.
    skip("booking_tips.json present", "concierge not deployed in container stack")
    skip("doctor health present", "doctor not deployed in container stack")
    try:
        cron = Path("/app/crontab.scan").read_text()
        check("scan schedule installed (supercronic)",
              "camp_agent.py" in cron and "camp_wiki_images.py" in cron)
    except Exception as e:
        check("scan schedule installed (supercronic)", False, str(e))
else:
    check("booking_tips.json present", config.TIPS_JSON.exists())
    try:
        H = json.loads(config.HEALTH_JSON.read_text())
        check("doctor health is HEALTHY", H.get("status") == "HEALTHY",
              f"{H.get('status')} — {H.get('summary','')}")
    except Exception as e:
        check("doctor health present", False, str(e))
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        check("cron scan job installed", "campsage_app" in cron)
        check("cron concierge job installed", "ai_concierge.sh" in cron)
        check("cron doctor job installed", "campsage_doctor.sh" in cron)
    except Exception as e:
        check("cron jobs installed", False, str(e))

print("\nRESULT:", "ALL PASS ✅" if ok else "SOME CHECKS FAILED ❌")
raise SystemExit(0 if ok else 1)
