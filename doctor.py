#!/usr/bin/env python3
"""
CampSage doctor — a self-check that runs after each scan. It detects breakage with
DETERMINISTIC checks (no AI), and ONLY when something is actually broken does it ask
Claude — on the SUBSCRIPTION, never the billable API — to diagnose the likely cause and
suggest a fix. Writes ~/campsage/health.json (a banner shows on /camp if not HEALTHY) and
re-renders the page. Run via campsage_doctor.sh (which strips the API key from the env).

Why this exists: recreation.gov / ReserveCalifornia can change their APIs with no notice
(the old ReserveCalifornia host silently became 0.0.0.0). Deterministic checks catch the
breakage; the subscription review explains it so it gets fixed fast.
"""
import json
import subprocess
import time
import urllib.request
from datetime import datetime

import config
import camp_agent
import locations

PT = camp_agent.PT


def _checks(s, page_url="http://127.0.0.1:5001/camp"):
    """Return list of (name, ok, severity, detail). severity: CRIT | WARN."""
    out = []
    now = time.time()
    age_h = (now - s.get("generated_epoch", 0)) / 3600
    c = s.get("counts", {})
    out.append(("scan_fresh", age_h < 15, "CRIT", f"{age_h:.1f}h old (a scan may have been missed)"))
    out.append(("recgov_search_ok", c.get("scanned", 0) > 0, "CRIT",
                f"{c.get('scanned')} campgrounds from recreation.gov (0 ⇒ search API broke)"))
    out.append(("recgov_filter_ok", c.get("qualified", 0) > 0, "CRIT",
                f"{c.get('qualified')} passed the review/distance filter (0 ⇒ API shape changed)"))
    out.append(("reservecalifornia_ok", s.get("beach_count", 0) > 0, "CRIT",
                f"{s.get('beach_count')} state beaches (0 ⇒ ReserveCalifornia API broke)"))
    out.append(("regions_ok", len(s.get("regions", [])) > 0, "WARN", "place tabs computed"))
    out.append(("errors_low", c.get("errors", 0) <= 5, "WARN", f"{c.get('errors')} fetch errors this run"))
    # social cache populated?
    try:
        sc = json.loads((config.DATA_DIR / "social_cache.json").read_text())
        out.append(("social_ok", len(sc) > 0, "WARN", f"{len(sc)} social scores cached"))
    except Exception:
        out.append(("social_ok", False, "WARN", "social cache missing/unreadable"))
    # dashboard serving?
    try:
        body = urllib.request.urlopen(page_url, timeout=10).read().decode()
        out.append(("dashboard_ok", "CampSage" in body, "CRIT", f"{page_url} serves"))
    except Exception as e:
        out.append(("dashboard_ok", False, "CRIT", f"{page_url} not serving: {e}"))
    return out


def _verdict(checks):
    crit = [c for c in checks if not c[1] and c[2] == "CRIT"]
    warn = [c for c in checks if not c[1] and c[2] == "WARN"]
    if crit:
        return "BROKEN", crit + warn
    if warn:
        return "DEGRADED", warn
    return "HEALTHY", []


def _ask_claude(status, failed, s):
    """Subscription diagnosis (campsage_doctor.sh has unset ANTHROPIC_API_KEY)."""
    log_tail = ""
    try:
        log_tail = "\n".join(config.LOG_FILE.read_text().splitlines()[-25:])
    except Exception:
        pass
    snapshot = (
        f"CampSage status: {status}\n"
        f"Failed checks:\n" + "\n".join(f"  - {n}: {d}" for n, ok, sev, d in failed) + "\n\n"
        f"counts: {json.dumps(s.get('counts', {}))}\n"
        f"beach_count: {s.get('beach_count')}\n"
        f"regions: {[r['label'] for r in s.get('regions', [])]}\n"
        f"errors (sample): {json.dumps(s.get('errors', [])[:5])}\n\n"
        f"recent log:\n{log_tail}\n"
    )
    prompt = (
        "You are the CampSage doctor. CampSage is a Python app on a small Linux box that finds "
        "California campsites from the recreation.gov public JSON API and the ReserveCalifornia "
        "RDR API (base URL read from reservecalifornia.com/config.json -> rdrApiUrl), plus a "
        "YouTube-derived 'social buzz' score. A deterministic self-check just FAILED. Using the "
        "snapshot below, give the SINGLE most likely root cause and a concrete, specific fix "
        "(e.g. an API host/field that changed, a cron that didn't run, the dashboard being down). "
        "Be concise and actionable. Do NOT invent facts. Respond exactly as:\n"
        "DIAGNOSIS: <one or two sentences>\n"
        "FIX: <one or two concrete steps>\n\n"
        "--- SNAPSHOT ---\n" + snapshot
    )
    try:
        r = subprocess.run(["claude", "-p", "--output-format", "text"],
                           input=prompt, capture_output=True, text=True, timeout=180)
        txt = (r.stdout or "").strip()
        return txt or "(no diagnosis returned)"
    except Exception as e:
        return f"(subscription doctor unavailable: {e})"


def main():
    ts = datetime.now(PT).strftime("%Y-%m-%d %H:%M") if PT else str(datetime.now())
    locs = locations.load_locations()
    per, worst, worst_failed, worst_status = {}, "HEALTHY", [], None
    RANK = {"HEALTHY": 0, "DEGRADED": 1, "BROKEN": 2}
    for loc in locs:
        try:
            s = json.loads(loc.status_json.read_text())
        except Exception as e:
            per[loc.slug] = {"status": "BROKEN", "summary": f"status.json unreadable: {e}"}
            worst = "BROKEN"
            continue
        checks = _checks(s, page_url=f"http://127.0.0.1:5001/camp/{loc.slug}")
        status, failed = _verdict(checks)
        per[loc.slug] = {
            "status": status,
            "summary": ("all systems nominal" if status == "HEALTHY"
                        else ", ".join(f"{n} failed" for n, ok, sev, d in failed)),
            "checks": [{"name": n, "ok": ok, "severity": sev, "detail": d}
                       for n, ok, sev, d in checks],
        }
        if RANK[status] > RANK[worst]:
            worst, worst_failed, worst_status = status, failed, s

    summary = ("all systems nominal" if worst == "HEALTHY" else
               "; ".join(f"[{slug}] {v['summary']}" for slug, v in per.items()
                         if v["status"] != "HEALTHY"))
    diagnosis = ""
    if worst != "HEALTHY" and worst_status is not None:
        diagnosis = _ask_claude(worst, worst_failed, worst_status)

    health = {
        "status": worst, "summary": summary, "diagnosis": diagnosis,
        "locations": per,
        "generated": ts, "ts": int(time.time()),
    }
    config.HEALTH_JSON.write_text(json.dumps(health, indent=2))

    # Re-render every location so the banner reflects the latest verdict.
    for loc in locs:
        try:
            s = json.loads(loc.status_json.read_text())
            camp_agent.render_html(s, loc, locs)
        except Exception as e:
            print(f"doctor: re-render {loc.slug} failed: {e}")

    line = f"{ts} [INFO] CampSage doctor: {worst} — {summary}"
    try:
        with open(config.LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


if __name__ == "__main__":
    main()
