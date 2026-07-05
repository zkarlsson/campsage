#!/usr/bin/env python3
"""
campsage_web.py — standalone web UI for CampSage. Serves the phone status page + the interactive
map (Leaflet + OpenStreetMap, no API keys). Reads the scan output that camp_agent.py writes to
DATA_DIR. Run:  python campsage_web.py   (then open http://localhost:5001/camp)
"""
import json
from pathlib import Path
from flask import Flask, jsonify, Response

import config

DATA = config.DATA_DIR
app = Flask(__name__)


def load_json_safe(path, default=None):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default if default is not None else {}


@app.route("/")
@app.route("/camp")
def camp_page():
    f = DATA / "dashboard.html"
    if f.exists():
        return f.read_text()
    return ("<body style='font:16px sans-serif;padding:40px'>CampSage hasn't run yet \u2014 "
            "run camp_agent.py first.</body>", 200)


@app.route("/camp/data")
def camp_data():
    f = DATA / "status.json"
    if f.exists():
        return Response(f.read_text(), mimetype="application/json")
    return jsonify({"status": "pending"}), 200


@app.route("/camp/map")
def camp_map():
    """Self-contained Leaflet map of every available campsite + beach (OSM tiles, no API keys)."""
    status = load_json_safe(DATA / "status.json", {})
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
    return Response(_CAMP_MAP_HTML.replace("__DATA__", json.dumps(pts))
                    .replace("__HOME__", str(home)).replace("__N__", str(len(pts))),
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
<div class="bar"><a href="/camp">← List</a><b>🏕️ CampSage Map</b><span class="muted" id="count">__N__ shown</span></div>
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
const map = L.map('map',{zoomControl:true}).setView([34.05,-118.24], 8);
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
