#!/usr/bin/env python3
"""CampSage self-test — validates the whole pipeline end to end. Exit 0 = all pass."""
import json, os, subprocess, time, urllib.request, urllib.parse, urllib.error
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

print("CampSage self-test")

# 1. live endpoints reachable
try:
    q = urllib.parse.urlencode({"fq": "entity_type:campground", "lat": config.HOME_LAT,
                                "lng": config.HOME_LNG, "radius": 50, "size": 1})
    d = fetch_json(f"{PT}/api/search?{q}")
    check("recreation.gov search API", int(d.get("total", 0)) > 0, f"{d.get('total')} results")
except Exception as e:
    check("recreation.gov search API", False, str(e))

# 2. status.json exists, fresh, well-formed
try:
    s = json.loads(config.STATUS_JSON.read_text())
    age_h = (time.time() - s.get("generated_epoch", 0)) / 3600
    check("status.json present + parses", True)
    check("status.json fresh (<24h)", age_h < 24, f"{age_h:.1f}h old")
    check("has campgrounds with openings", s["counts"]["with_openings"] > 0,
          f"{s['counts']['with_openings']} found")
    # results = everyday spots (closest-first, within the everyday radius) followed by
    # far-destination finds (anchor search, region tabs only, within the outer cap).
    everyday = [h for h in s["results"] if h.get("everyday", True)]
    dest = [h for h in s["results"] if not h.get("everyday", True)]
    check("everyday results sorted closest-first",
          all(everyday[i]["distance"] <= everyday[i+1]["distance"]
              for i in range(len(everyday)-1)))
    check("everyday results within max distance",
          all(h["distance"] <= config.MAX_DISTANCE_MI for h in everyday))
    check("destination finds within outer cap",
          all(h["distance"] <= config.REGION_MAX_DISTANCE_MI for h in dest))
    # State parks (ReserveCalifornia) carry no review scores — rating is None by design.
    check("all rated spots meet min rating",
          all(h["rating"] >= config.MIN_RATING
              for h in s["results"] if h.get("rating") is not None))
    check("openings respect night bounds",
          all(min(config.NIGHTS) <= o["nights"] <= max(config.NIGHTS)
              for h in s["results"] for o in h["openings"]))
    check("no fetch errors", s["counts"]["errors"] == 0, f"{s['counts']['errors']} errors")
except Exception as e:
    check("status.json present + parses", False, str(e))
    s = None

# 3. a claimed opening is independently real (merge months — a block can cross a boundary)
try:
    # Re-verify against recreation.gov, so pick a federal campground (state parks/beaches
    # merged into results live on ReserveCalifornia and have non-rec.gov ids).
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
    check("top opening is genuinely available", real > 0,
          f"{real} sites open for {h['name']} {o['start']}")
except Exception as e:
    if "429" in str(e):
        skip("top opening is genuinely available", "API rate-limited (try again in a minute)")
    else:
        check("top opening is genuinely available", False, str(e))

# 3b. beach section = MAINLAND state beaches (ReserveCalifornia), no islands
try:
    b = s.get("beach", [])
    check("beach section populated", len(b) > 0, f"{s.get('beach_count')} state beaches")
    check("beach is all state beaches", all(h.get("state_beach") for h in b))
    check("beach has NO island camping",
          not any("island" in (h["name"] or "").lower() for h in b))
    check("beach within max distance",
          all(h["distance"] <= config.BEACH_MAX_DISTANCE for h in b))
    check("beach sorted closest-first",
          all(b[i]["distance"] <= b[i+1]["distance"] for i in range(len(b)-1)))
    check("beach has 2-3 night openings",
          sum(1 for h in b if h.get("openings")) > 0,
          f"{sum(1 for h in b if h.get('openings'))} with openings")
    check("beach openings respect night bounds",
          all(min(config.NIGHTS) <= o["nights"] <= max(config.NIGHTS)
              for h in b for o in h.get("openings", [])))
    # independently re-confirm one beach opening via the live ReserveCalifornia API
    import reservecalifornia as rc
    bo = next((h for h in b if h.get("openings")), None)
    if bo:
        o = bo["openings"][0]
        ws = date.fromisoformat(o["start"]); we = date.fromisoformat(o["end"])
        need = {(ws + timedelta(days=i)).isoformat() for i in range(o["nights"])}
        ok_unit = False
        for fid, _ in rc.facilities_for(bo["id"], ws.isoformat()):
            for dates in rc.free_dates_by_unit(fid, ws, we - timedelta(days=1)).values():
                if need <= dates:
                    ok_unit = True; break
            if ok_unit:
                break
        check("a beach opening is genuinely available (ReserveCalifornia)", ok_unit,
              f"{bo['name']} {o['nights']}N from {o['start']}")
except Exception as e:
    check("beach section populated", False, str(e))

# 3c. destination regions (place tabs)
try:
    regs = s.get("regions", [])
    check("regions computed", len(regs) > 0,
          ", ".join(f"{r['label']}({r['count']})" for r in regs[:6]) + ("…" if len(regs) > 6 else ""))
    check("every displayed campground has a region",
          all(h.get("region_slug") for h in (s["results"] + s.get("beach", []))))
    check("region counts add up",
          sum(r["count"] for r in regs) == len(s["results"]) + len(s.get("beach", [])))
except Exception as e:
    check("regions computed", False, str(e))

# 4. dashboard /camp serves the page (local + the route exists)
try:
    body = urllib.request.urlopen("http://127.0.0.1:5001/camp", timeout=15).read().decode()
    check("/camp serves on :5001", "CampSage" in body and "card" in body)
    check("/camp shows Beaches tab", "data-filter='beach'" in body or "🏖️ Beaches" in body)
    check("/camp shows place tabs + region chips",
          "function tab(" in body and body.count("data-region=") >= 10
          and "regionchip" in body)
    check("/camp shows social score (number + stars)",
          "social buzz" in body and "class='sscore'" in body)
    check("/camp shows social review links",
          "reddit.com/search" in body and "youtube.com/results" in body
          and body.count("💬 Reviews") >= 10)
except Exception as e:
    check("/camp serves on :5001", False, str(e))

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
