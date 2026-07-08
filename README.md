# CampSage 🏕️

Finds great **California** campsites with **2–3 consecutive nights open at the same site**,
ranked **closest-to-home first** among **well-reviewed** spots, and publishes a phone-friendly
status page + an interactive map — for **every saved location** (zip code or address) in
`config.LOCATIONS`. Runs on any always-on box via cron or the included docker-compose stack.
**No API keys** (geocoding included: Zippopotam for zips, Nominatim for addresses; results are
cached forever in `geocode_cache.json`).

## Quick start
```bash
pip install flask                 # the only dependency (scanner itself is stdlib-only)
# 1. set your locations in config.py:
#    LOCATIONS = [{"name": "Oakland", "query": "94607"}, ...]   # zip or address;
#    explicit "lat"/"lng" skip geocoding (escape hatch). DEFAULT_LOCATION picks the /camp tab.
python3 camp_agent.py             # scan each location -> locations/<slug>/status.json + index
python3 camp_wiki_images.py       # (optional) fetch beach/park photos from Wikimedia Commons
python3 campsage_web.py           # serve it -> open http://localhost:5001/camp
```
URLs: `/camp` redirects to the default location; each location lives at `/camp/<slug>`
(`/camp/<slug>/map`, `/camp/<slug>/data`). The page header has a location-switcher pill row
with a ✎ button to **add or remove locations at runtime** (shared deployment-wide; capped at
`MAX_LOCATIONS`). Adds geocode immediately, show a ⏳ pill, and get scanned within minutes by
`scan_pending.py` (which queues behind any running scan — scans are strictly serialized via a
scan lock because recreation.gov enforces an adaptive per-IP quota). The runtime location list
lives in `DATA_DIR/locations_config.json`; `config.LOCATIONS` only seeds it on first run.
Region tabs are statewide: only anchors within ~300 mi of a location are searched, so an
Oakland scan gets Point Reyes/Tahoe/Big Sur tabs while an LA scan keeps Big Bear/Ojai/Joshua
Tree — API load stays flat per location.

Docker: `docker compose up -d --build` (web on :5055 host-side; scanner runs 7:00/13:00/18:00 PT
via supercronic and scans all locations sequentially).
Run `camp_agent.py` on a cron (e.g. a few times a day) to keep results fresh. Add the page to your
phone's Home Screen. Every card has a green **Book on Recreation.gov →** button, a **See calendar**
link, and **Directions**.

## The map (`/camp/map`)
Interactive Leaflet map (OpenStreetMap tiles, **no key**) of every open site — filter by
type / nights / weekend / sought-after / region; tap a pin for photos, the exact **open dates + site
numbers**, and a booking link. Beach/state-park photos come from Wikimedia Commons (keyless).

## Place tabs
A scrollable tab strip groups every campground by **destination region** (Big Bear, Lake Arrowhead,
Ojai, Orange County Coast, …). Each campground is tagged with its nearest anchor in
`config.REGION_ANCHORS` by lat/lng; only regions that have campgrounds in range become tabs,
ordered closest-first. Tabs: **All** · **🏖️ Beaches** · one per region. A 📍 chip on each card
shows its region. All cards live in one `#grid`; tabs filter, the sort bar sorts.

## Social score (number + stars)
Every card shows a **social buzz** score — a 0–5 number + stars derived from YouTube (how many
videos + total views for "<name> camping"), via `socialscore.py`. It's POPULARITY, not
satisfaction, and labelled that way. Live Reddit/IG/TikTok scraping is impossible (they 403
datacenter IPs), so this is the honest no-key signal; scores are cached ~2 weeks so the scan stays
light. Sortable via the "Social buzz" button. Plus the 💬 Reviews link row (Reddit/YouTube/TikTok/
Google searches) per card.

## Sections on the page
1. **⛰️ All campgrounds** — 2–3 night openings, closest-to-LA first. Sort: Closest /
   Best reviewed / Most reviewed / Highest rated / Soonest.
2. **🏖️ Beach camping — state beaches** — MAINLAND drive-up California state beaches
   (Leo Carrillo, San Onofre, Carpinteria, El Capitán, Refugio, Pismo, …) via **ReserveCalifornia**.
   Island camping is excluded. The state-park system has no review scores, so this ranks by
   **closest** + **soonest** + **most sites open**. Own sort bar; Book button → ReserveCalifornia.
3. **🧭 Booking concierge** + **Also great nearby — currently full**.

The beach source is `reservecalifornia.py` — it reverse-maps the same RDR API the
reservecalifornia.com site uses (`search/place` for nearby parks + distance, `search/grid` for
per-night `IsFree` availability). No API key. Base URL is read from the site's own config.json.

"Best reviewed" = `rating × log10(reviews+1)` — credibility-weighted, so a 4.7★/986-review spot
beats a 4.6★/5-review one. "Most reviewed" = raw count.

**Social reviews:** every card has a 💬 Reviews row (Reddit / YouTube / TikTok / Google) that
opens a search pre-filled with the campground name. These are deep links, not scraped — Reddit,
Instagram, and TikTok all 403 datacenter IPs, so live fetching is impossible/unreliable; the
links open on the phone where the user is logged in and get real results every time.

## How it works
- **Data:** recreation.gov public JSON — `search` (ratings, review counts, drive distance,
  lat/lng) + `availability` (per-night status per site, so consecutive nights are detectable).
- `camp_agent.py` discovers campgrounds within `MAX_DISTANCE_MI` of `HOME_*`, keeps the
  well-reviewed ones (`MIN_RATING`/`MIN_REVIEWS`, plus a high-rating "gem" tier), fetches each
  campground's availability across the window in parallel, finds each site's earliest run of
  2–3 available nights, ranks closest-first, and writes:
  - `~/campsage/status.json` — machine-readable results
  - `~/campsage/dashboard.html` — the self-contained page served at `/camp`
- `ai_concierge.sh` asks Claude (on the **subscription**, no API cost) for booking tips specific
  to the current top spots → `~/campsage/booking_tips.json` (shown under "Booking concierge").

## Tuning
Everything is in `config.py`: home base, max distance, rating bars, window length, nights
(`NIGHTS=[3,2]`), `WEEKENDS_ONLY`. Edit and re-run `python3 camp_agent.py`.

## Cron (Pacific, in `crontab -l`)
- `camp_agent.py` at **7:00 / 13:00 / 18:00** (7am catches recreation.gov's rolling 6-month
  release; midday + evening catch cancellations).
- `ai_concierge.sh` at **7:10** (subscription tips refresh).

## Validate
`python3 selftest.py` — checks the live API, freshness, sort/distance/rating/nights invariants,
that a claimed opening is *independently* still available, `/camp` serves, and cron is installed.
Exit 0 = all pass.

## Notes
- Single-site **group** campgrounds can show "1 site open" and get booked in minutes — book fast.
- Coverage is recreation.gov (national forests, NPS, etc.). California **state parks**
  (ReserveCalifornia) are a separate system not yet included — a future add.
