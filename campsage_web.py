#!/usr/bin/env python3
"""
campsage_web.py — standalone web UI for CampSage. Serves the phone status page + the interactive
map (Leaflet + OpenStreetMap, no API keys) for every saved location camp_agent.py has scanned
(DATA_DIR/locations/<slug>/). Legacy pre-multi-location volumes (flat status.json/dashboard.html,
no locations.json) keep working until the first new-style scan lands.
Run:  python campsage_web.py   (then open http://localhost:5001/camp)
"""
import html
import json
import re
import urllib.parse
from pathlib import Path
from flask import Flask, jsonify, Response, redirect, request

import config
import locations

DATA = config.DATA_DIR
app = Flask(__name__)

_BACK_RE = re.compile(r"^/camp(/[a-z0-9-]+)?$")   # referrer allowlist (no open redirect)


def load_json_safe(path, default=None):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default if default is not None else {}


def _index():
    """locations.json, or None on a legacy (single flat scan) volume."""
    return load_json_safe(DATA / "locations.json", None) or None


def _known_slugs(idx):
    return [e.get("slug") for e in (idx or {}).get("locations", []) if e.get("slug")]


def _unknown(idx):
    slugs = ", ".join(_known_slugs(idx)) or "(none scanned yet)"
    return (f"<body style='font:16px sans-serif;padding:40px'>Unknown location. "
            f"Known: {slugs}</body>", 404)


def _store():
    """The runtime location store, or None if unreadable/unseeded (web never seeds —
    that's the scanner's job; without a store we fall back to index-only pills)."""
    try:
        return locations.read_store()
    except Exception:
        return None


# ── live location bar (replaces the scan-time-baked pills at serve time) ──────
_LOCBAR_STYLE = """<style>
  .locpill.pending{opacity:.55;cursor:default}
  .locpill.editbtn{background:none;border-style:dashed;cursor:pointer;font:inherit}
  .locedit{display:none;margin-top:8px;padding:10px;background:#13201a;
           border:1px solid #25382e;border-radius:10px}
  .locedit.open{display:block}
  .locedit form{display:inline-flex;gap:6px;margin:3px 8px 3px 0;vertical-align:middle}
  .locedit input{background:#0f1411;border:1px solid #2d4a3b;color:#e7efe9;
                 border-radius:8px;padding:7px 10px;font-size:14px;max-width:150px}
  .locedit button{background:#1f8a4c;color:#fff;border:none;border-radius:8px;
                  padding:7px 12px;font-size:13px;font-weight:600;cursor:pointer}
  .locedit .rm button{background:#5a2626}
  .locerr{color:#e08a8a;font-size:13px;margin-top:6px}
  .lochint{color:#6f9583;font-size:12px;margin-top:6px}
</style>"""


def _locbar(active_slug, err=None):
    """Pill row + edit panel built from the CURRENT store/index (not the scan-time
    bake): scanned locations link, store-only ones show as ⏳ pending."""
    store, idx = _store(), _index()
    scanned = set(_known_slugs(idx))
    entries = (store or {}).get("locations") or \
              [{"slug": e.get("slug"), "name": e.get("name", e.get("slug"))}
               for e in (idx or {}).get("locations", [])]
    if not entries:
        return ""
    e_ = lambda s: html.escape(str(s), quote=True)
    pills, removes = [], []
    for e in entries:
        slug, name = e.get("slug"), e.get("name") or e.get("slug")
        if slug in scanned:
            pills.append(f"<a class='locpill{' on' if slug == active_slug else ''}' "
                         f"href='/camp/{e_(slug)}'>📍 {e_(name)}</a>")
        else:
            pills.append(f"<span class='locpill pending' title='first scan queued — "
                         f"usually ready within ~30 min'>⏳ {e_(name)}</span>")
        removes.append(
            f"<form class='rm' method='post' action='/camp/locations/remove' "
            f"onsubmit=\"return confirm('Remove {e_(name)} for everyone?')\">"
            f"<input type='hidden' name='slug' value='{e_(slug)}'>"
            f"<button>✕ {e_(name)}</button></form>")
    edit_ui = ""
    if store:                                      # no store → view-only pills
        removes_html = "".join(removes) if len(entries) > 1 else ""
        err_html = f"<div class='locerr'>{e_(err)}</div>" if err else ""
        edit_ui = (
            "<button class='locpill editbtn' title='add / remove locations' "
            "onclick=\"document.getElementById('locedit').classList.toggle('open')\">✎</button>"
            f"</div><div class='locedit{' open' if err else ''}' id='locedit'>{removes_html}"
            "<form method='post' action='/camp/locations/add'>"
            "<input name='query' placeholder='zip or city, ST' required maxlength='120'>"
            "<input name='name' placeholder='label (optional)' maxlength='40'>"
            "<button>+ Add</button></form>"
            f"{err_html}"
            "<div class='lochint'>Shared with everyone on this deployment. A new "
            "location's first scan takes ~15–30 min (waits for any scan in progress)."
            "</div>")
    if not edit_ui:
        pills.append("")                           # keep the closing div balanced
    return (_LOCBAR_STYLE + "<div class='locs'>" + "".join(pills) + edit_ui + "</div>")


def _inject_locbar(body, active_slug, err=None):
    """Swap the scan-time-baked pill block for the live bar. No markers (pre-upgrade
    bake) → serve unchanged; the next cron re-bake adds them."""
    start, end = body.find("<!--LOCS-->"), body.find("<!--/LOCS-->")
    if start == -1 or end == -1 or end < start:
        return body
    return body[:start] + _locbar(active_slug, err) + body[end + len("<!--/LOCS-->"):]


def _pending_page(slug, store):
    name = next((e.get("name", slug) for e in store.get("locations", [])
                 if e.get("slug") == slug), slug)
    e_ = lambda s: html.escape(str(s), quote=True)
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<meta http-equiv='refresh' content='60'>"
            f"<title>🏕️ CampSage — {e_(name)}</title>"
            f"<style>:root{{color-scheme:dark}}body{{margin:0;font:16px/1.5 -apple-system,"
            f"system-ui,sans-serif;background:#0f1411;color:#e7efe9;padding:20px}}"
            f".locs{{display:flex;gap:8px;overflow-x:auto;margin-bottom:18px}}"
            f".locpill{{flex:0 0 auto;padding:6px 12px;border:1px solid #2d4a3b;"
            f"background:#16271f;color:#cfe9da;border-radius:20px;font-size:13px;"
            f"font-weight:600;text-decoration:none}}"
            f".locpill.on{{background:#1f8a4c;color:#fff;border-color:#1f8a4c}}</style>"
            f"</head><body>{_locbar(slug)}"
            f"<h2>⏳ Scanning {e_(name)}…</h2>"
            f"<p style='color:#9bbfac'>First results usually appear within ~30 minutes "
            f"(a new location waits for any in-progress scan to finish, then scans at a "
            f"deliberately polite pace). This page refreshes itself.</p></body></html>")


def _back(default="/camp"):
    ref = request.referrer or ""
    try:
        path = urllib.parse.urlparse(ref).path
    except Exception:
        return default
    return path if _BACK_RE.match(path or "") else default


@app.route("/camp/locations/add", methods=["POST"])
def locations_add():
    try:
        entry = locations.add_location(request.form.get("query", ""),
                                       request.form.get("name", ""))
    except ValueError as e:
        return redirect(_back() + "?err=" + urllib.parse.quote(str(e)))
    except Exception:
        return redirect(_back() + "?err=" + urllib.parse.quote(
            "Something went wrong saving that location."))
    return redirect(f"/camp/{entry['slug']}")


@app.route("/camp/locations/remove", methods=["POST"])
def locations_remove():
    slug = request.form.get("slug", "")
    try:
        locations.remove_location(slug)
    except ValueError as e:
        return redirect(_back() + "?err=" + urllib.parse.quote(str(e)))
    except Exception:
        return redirect(_back() + "?err=" + urllib.parse.quote(
            "Something went wrong removing that location."))
    back = _back()
    if back.rstrip("/").endswith(f"/{slug}"):      # was viewing the removed city
        back = "/camp"
    return redirect(back)


@app.route("/")
@app.route("/camp")
def camp_page():
    idx = _index()
    if idx:
        return redirect(f"/camp/{idx.get('default') or _known_slugs(idx)[0]}")
    f = DATA / "dashboard.html"                     # legacy flat volume
    if f.exists():
        return f.read_text()
    return ("<body style='font:16px sans-serif;padding:40px'>CampSage hasn't run yet \u2014 "
            "run camp_agent.py first.</body>", 200)


@app.route("/camp/data")
def camp_data():
    idx = _index()
    if idx:
        return redirect(f"/camp/{idx.get('default') or _known_slugs(idx)[0]}/data")
    f = DATA / "status.json"                        # legacy flat volume
    if f.exists():
        return Response(f.read_text(), mimetype="application/json")
    return jsonify({"status": "pending"}), 200


@app.route("/camp/<slug>")
def camp_page_loc(slug):
    idx = _index()
    store = _store()
    store_slugs = {e.get("slug") for e in (store or {}).get("locations", [])}
    err = request.args.get("err")
    f = DATA / "locations" / slug / "dashboard.html"
    if slug in _known_slugs(idx) and f.exists():
        return _inject_locbar(f.read_text(), slug, err)
    if slug in store_slugs:                        # added, first scan still queued
        return _pending_page(slug, store)
    return _unknown(idx)


@app.route("/camp/<slug>/data")
def camp_data_loc(slug):
    idx = _index()
    if not idx or slug not in _known_slugs(idx):
        return _unknown(idx)
    f = DATA / "locations" / slug / "status.json"
    if f.exists():
        return Response(f.read_text(), mimetype="application/json")
    return jsonify({"status": "pending"}), 200


@app.route("/camp/map")
def camp_map():
    idx = _index()
    if idx:
        return redirect(f"/camp/{idx.get('default') or _known_slugs(idx)[0]}/map")
    return _render_map(load_json_safe(DATA / "status.json", {}))    # legacy flat volume


@app.route("/camp/<slug>/map")
def camp_map_loc(slug):
    idx = _index()
    if not idx or slug not in _known_slugs(idx):
        return _unknown(idx)
    return _render_map(load_json_safe(DATA / "locations" / slug / "status.json", {}),
                       back=f"/camp/{slug}")


def _render_map(status, back="/camp"):
    """Self-contained Leaflet map of every available campsite + beach (OSM tiles, no API keys)."""
    wiki = load_json_safe(DATA / "wiki_images.json", {})
    pts = []
    for kind, arr in (("camp", status.get("results") or []), ("beach", status.get("beach") or [])):
        for s in arr:
            try:
                lat, lng = float(s.get("lat")), float(s.get("lng"))
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                continue
            pts.append({
                "k": kind, "name": s.get("name", ""), "parent": s.get("parent", ""),
                "lat": lat, "lng": lng, "rating": s.get("rating"), "reviews": s.get("reviews"),
                "dist": s.get("distance"), "region": s.get("region", ""),
                "n3": str(s.get("has_3night")).lower() == "true",
                "n2": str(s.get("has_2night")).lower() == "true",
                "wknd": str(s.get("has_weekend")).lower() == "true",
                "star": str(s.get("marquee")).lower() == "true",
                "dates": [{"s": o.get("start"), "e": o.get("end"), "n": o.get("nights"),
                           "c": o.get("count"),
                           "sites": [(x.split("\u00b7")[-1].strip() if "\u00b7" in x else x.strip())[:14]
                                     for x in (o.get("sites") or [])[:4]]}
                          for o in (s.get("openings") or [])[:8] if isinstance(o, dict)],
                "imgs": ([s.get("image")] if "recreation.gov" in (s.get("image") or "")
                         else (wiki.get(s.get("name", "")) or [])),
                "url": s.get("book_url") or s.get("avail_url") or "",
            })
    home = status.get("home") or "home"
    try:
        center = [float(status["lat"]), float(status["lng"])]
    except (KeyError, TypeError, ValueError):
        center = [36.78, -119.42]                   # CA center (legacy status w/o lat/lng)
    return Response(_CAMP_MAP_HTML.replace("__DATA__", json.dumps(pts))
                    .replace("__HOME__", str(home)).replace("__N__", str(len(pts)))
                    .replace("__CENTER__", json.dumps(center)).replace("__BACK__", back),
                    mimetype="text/html")


_CAMP_MAP_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>CampSage — Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;background:#0f1411;font-family:-apple-system,system-ui,sans-serif}
  #map{position:absolute;inset:0;top:88px}
  .bar{position:absolute;top:0;left:0;right:0;height:46px;background:#0f1411;color:#e7efe9;
    display:flex;align-items:center;gap:12px;padding:0 14px;z-index:1000;box-shadow:0 1px 6px rgba(0,0,0,.5)}
  .bar a{color:#7fd1a8;text-decoration:none;font-weight:600}
  .bar b{font-size:15px}.bar .muted{color:#8ba398;font-size:12px;margin-left:auto}
  .filters{position:absolute;top:46px;left:0;right:0;height:42px;background:#13201a;z-index:999;
    display:flex;align-items:center;gap:6px;padding:0 10px;overflow-x:auto;white-space:nowrap;
    box-shadow:0 1px 4px rgba(0,0,0,.4)}
  .chip{flex:0 0 auto;background:#0f1411;color:#8ba398;border:1px solid #24382c;border-radius:14px;
    padding:4px 11px;font-size:12px;font-weight:600;cursor:pointer;user-select:none}
  .chip.active{background:#1c3a2b;color:#7fd1a8;border-color:#2e7d5b}
  .sep{flex:0 0 auto;width:1px;height:20px;background:#24382c;margin:0 2px}
  select.chip{-webkit-appearance:none;appearance:none;padding-right:22px}
  .leaflet-popup-content{margin:10px 12px;font-size:13px;line-height:1.5}
  .leaflet-popup-content b{font-size:14px}
  .book{display:inline-block;margin-top:6px;background:#2e7d5b;color:#fff;padding:4px 10px;
    border-radius:6px;text-decoration:none;font-weight:600}
  .badge{display:inline-block;background:#1c3a2b;color:#7fd1a8;border-radius:4px;padding:1px 6px;font-size:11px;margin-right:4px}
  .legend{position:absolute;bottom:14px;left:10px;background:rgba(15,20,17,.92);color:#e7efe9;
    padding:8px 10px;border-radius:8px;font-size:12px;z-index:1000;line-height:1.7}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
</style></head><body>
<div class="bar"><a href="__BACK__">← List</a><b>🏕️ CampSage Map — __HOME__</b><span class="muted" id="count">__N__ shown</span></div>
<div class="filters" id="filters">
  <span class="chip active" data-f="type" data-v="all">All</span>
  <span class="chip" data-f="type" data-v="camp">🏕️ Camp</span>
  <span class="chip" data-f="type" data-v="beach">🏖️ Beach</span>
  <span class="sep"></span>
  <span class="chip active" data-f="nights" data-v="any">Any nights</span>
  <span class="chip" data-f="nights" data-v="2">2+ night</span>
  <span class="chip" data-f="nights" data-v="3">3-night</span>
  <span class="sep"></span>
  <span class="chip" data-t="wknd">📅 Weekend</span>
  <span class="chip" data-t="star">⭐ Sought-after</span>
  <span class="sep"></span>
  <select class="chip" id="region"><option value="all">All regions</option></select>
</div>
<div id="map"></div>
<div class="legend">
  <div><span class="dot" style="background:#2ec27e"></span>3-night open</div>
  <div><span class="dot" style="background:#4aa3ff"></span>2-night open</div>
  <div><span class="dot" style="background:#8ba398"></span>shorter/other</div>
  <div><span class="dot" style="background:#f0b429"></span>🏖️ beach</div>
</div>
<script>
const PTS = __DATA__;
const map = L.map('map',{zoomControl:true}).setView(__CENTER__, 8);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:18, attribution:'© OpenStreetMap'}).addTo(map);
const color = p => p.k==='beach' ? '#f0b429' : p.n3 ? '#2ec27e' : p.n2 ? '#4aa3ff' : '#8ba398';
// tap-arrow photo carousel (swipe fights Leaflet's touch handling inside popups, so we tap)
window.galNav = function(btn, dir){
  const g = btn.closest('.gal'); if(!g) return;
  const ims = g.querySelectorAll('img');
  let i = (parseInt(g.dataset.i||'0') + dir + ims.length) % ims.length;
  g.dataset.i = i;
  ims.forEach((im,idx)=>{ im.style.display = idx===i ? 'block' : 'none'; });
  const c = g.querySelector('.gcount'); if(c) c.textContent = (i+1)+'/'+ims.length;
};
const MK = PTS.map(p => {
  const m = L.circleMarker([p.lat,p.lng], {radius:7, color:'#0f1411', weight:1.5,
     fillColor:color(p), fillOpacity:.95});
  const stars = (p.rating && p.rating!=='None') ? `⭐ ${p.rating} (${p.reviews})` : '';
  const badges = (p.n3?'<span class="badge">3-night</span>':'') + (p.n2&&!p.n3?'<span class="badge">2-night</span>':'') +
     (p.k==='beach'?'<span class="badge">🏖️ beach</span>':'');
  const fmt = ds => { try { return new Date(ds+'T00:00').toLocaleDateString('en-US',{month:'short',day:'numeric'}); } catch(e){ return ds; } };
  let dhtml = '';
  if (p.dates && p.dates.length) {
    dhtml = '<div style="margin-top:6px;font-size:12px;line-height:1.6"><b>📅 Open dates &amp; sites:</b>' +
      p.dates.slice(0,5).map(o=>{
        const codes = (o.sites||[]).join(', ');
        const more = (o.c && o.c>(o.sites||[]).length) ? ` +${o.c-(o.sites||[]).length}` : '';
        const siteTxt = codes ? ` — <span style="color:#7fd1a8">site ${codes}${more}</span>` : (o.c?` — ${o.c} sites`:'');
        return `<div>${fmt(o.s)}–${fmt(o.e)} · ${o.n}nt${siteTxt}</div>`;
      }).join('') +
      (p.dates.length>5 ? `<div style="color:#8ba398">+${p.dates.length-5} more windows</div>` : '') + '</div>';
  }
  let imgHtml = '';
  if (p.imgs && p.imgs.length === 1) {
    imgHtml = `<img src="${p.imgs[0]}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'" `+
      `style="width:100%;height:130px;object-fit:cover;border-radius:6px;margin-bottom:6px;display:block">`;
  } else if (p.imgs && p.imgs.length > 1) {
    const A = 'position:absolute;top:50%;transform:translateY(-50%);width:28px;height:28px;border:none;'+
      'border-radius:50%;background:rgba(15,20,17,.7);color:#fff;font-size:18px;line-height:1;cursor:pointer;z-index:2;padding:0';
    // preload ALL photos stacked; arrows just toggle which is visible (no src-swap → no reload/onerror flicker)
    imgHtml = `<div class="gal" data-i="0" style="position:relative;height:130px;margin-bottom:6px;border-radius:6px;overflow:hidden">`+
      p.imgs.map((u,idx)=>`<img src="${u}" loading="lazy" referrerpolicy="no-referrer" `+
        `style="position:absolute;inset:0;width:100%;height:130px;object-fit:cover;display:${idx===0?'block':'none'}">`).join('')+
      `<button onclick="galNav(this,-1);return false;" style="${A};left:5px">‹</button>`+
      `<button onclick="galNav(this,1);return false;" style="${A};right:5px">›</button>`+
      `<span class="gcount" style="position:absolute;bottom:5px;right:7px;background:rgba(15,20,17,.75);color:#fff;font-size:11px;padding:1px 7px;border-radius:10px;z-index:2">1/${p.imgs.length}</span>`+
      `</div>`;
  }
  m.bindPopup(imgHtml + `<b>${p.name}</b><br><span style="color:#6b7d73">${p.parent||''}</span><br>`+
    `${stars} ${p.dist?'· '+p.dist+' mi':''}<br>${p.region||''}<br>${badges}`+ dhtml +
    (p.url?`<br><a class="book" href="${p.url}" target="_blank">Book / check →</a>`:''), {maxWidth:280});
  return {p, m};
});
// populate region dropdown from the data
const regions = [...new Set(PTS.map(p=>p.region).filter(Boolean))].sort();
const rsel = document.getElementById('region');
regions.forEach(r => { const o=document.createElement('option'); o.value=r; o.textContent=r; rsel.appendChild(o); });

const state = {type:'all', nights:'any', region:'all', wknd:false, star:false};
function match(p){
  if(state.type!=='all' && p.k!==state.type) return false;
  if(state.nights==='3' && !p.n3) return false;
  if(state.nights==='2' && !(p.n2||p.n3)) return false;
  if(state.region!=='all' && p.region!==state.region) return false;
  if(state.wknd && !p.wknd) return false;
  if(state.star && !p.star) return false;
  return true;
}
function apply(){
  const vis=[];
  MK.forEach(({p,m})=>{ if(match(p)){ m.addTo(map); vis.push(m); } else { map.removeLayer(m); } });
  document.getElementById('count').textContent = vis.length+' shown';
  if(vis.length) map.fitBounds(L.featureGroup(vis).getBounds().pad(0.15));
}
// exclusive groups (type, nights)
document.querySelectorAll('.chip[data-f]').forEach(c => c.addEventListener('click', () => {
  const f=c.dataset.f;
  document.querySelectorAll(`.chip[data-f="${f}"]`).forEach(x=>x.classList.remove('active'));
  c.classList.add('active'); state[f]=c.dataset.v; apply();
}));
// independent toggles (weekend, sought-after)
document.querySelectorAll('.chip[data-t]').forEach(c => c.addEventListener('click', () => {
  const t=c.dataset.t; state[t]=!state[t]; c.classList.toggle('active', state[t]); apply();
}));
rsel.addEventListener('change', () => { state.region=rsel.value; apply(); });
apply();
</script></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
