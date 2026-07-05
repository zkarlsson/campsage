"""
CampSage configuration.

A small, dependency-free agent that hunts recreation.gov for great California
campsites with 2-3 consecutive nights open at the SAME site, ranked closest-first
among well-reviewed spots, and publishes a phone-friendly status page.

Everything tunable lives here. Edit + re-run camp_agent.py (or wait for cron).
"""
from pathlib import Path

# ── Where "closest" is measured from (home base for drive distance) ───────────
HOME_NAME = "Los Angeles"
HOME_LAT  = 34.0522
HOME_LNG  = -118.2437

# ── How far you'll drive + how good a spot has to be ──────────────────────────
SEARCH_RADIUS_MI = 150      # recreation.gov search radius (miles)
MAX_DISTANCE_MI  = 150      # hard cap: drop anything farther than this
MIN_RATING       = 4.0      # only "good reviews" — average stars >= this
MIN_REVIEWS      = 4        # ...backed by at least this many ratings (signal, not noise)

# A second, lower bar so genuinely great but lightly-reviewed gems still surface,
# clearly flagged as "few reviews". Set EQUAL to the above to disable the tier.
SOFT_MIN_RATING  = 4.5      # a 4.5★+ spot with only a couple reviews can still show
SOFT_MIN_REVIEWS = 1

# ── The trip you want ─────────────────────────────────────────────────────────
WINDOW_DAYS   = 60          # search this many days out from today
NIGHTS        = [3, 2]      # acceptable consecutive-night blocks (prefer 3, accept 2)
WEEKENDS_ONLY = False       # True => only blocks that include a Fri or Sat night

# ── Beach section (MAINLAND drive-up state beaches via ReserveCalifornia) ─────
# These are the iconic CA ocean beach campgrounds (Leo Carrillo, San Onofre, Carpinteria,
# El Capitán, Refugio, Pismo, …). They live on ReserveCalifornia, NOT recreation.gov, and
# the system has no review/star scores — so the beach section is ranked by distance + soonest.
# Channel Islands (boat-in) are deliberately NOT here — the user doesn't want island camping.
BEACH_ENABLED      = True
BEACH_MAX_DISTANCE = 175     # miles from home; covers Pismo/El Capitán up the coast
# A ReserveCalifornia place is "beach camping" if its name ends in " SB" (State Beach) or
# contains the word Beach, or is one of these coastal State Parks that have camping…
BEACH_ALLOW = ("Leo Carrillo SP", "Point Mugu SP", "Crystal Cove SP Moro Campground",
               "Gaviota SP", "Montana De Oro SP", "Morro Bay SP")
# …but never these (inland, lakes, cottages/trailers, off-highway dune areas).
BEACH_VETO  = ("Cottages", "Trailers", "Lake", "SRA", "SVRA", "SHP", "Reservoir", "Desert")

# ── Output / ranking ──────────────────────────────────────────────────────────
TOP_N_DISPLAY = 30          # max campgrounds to render on the page
DEFAULT_SORT  = "distance"  # "distance" (closest first) | "rating" | "soonest"
SHOW_ONLY_OPENINGS = True   # only show places with an actual 2-3 night block; hide full ones entirely
                            # (beaches + the "great but full" alert list). Set False to show full places.

# ── Destination regions (tabs) ────────────────────────────────────────────────
# Each campground is tagged with the NEAREST of these anchors (by lat/lng), so the page
# can show a tab per place (Big Bear, Lake Arrowhead, …). Only regions that actually have
# campgrounds in range appear as tabs. Add an anchor here to split/merge a region.
REGION_ANCHORS = [
    ("Big Bear",              34.244, -116.911),
    ("Lake Arrowhead",        34.249, -117.189),
    ("Idyllwild / San Jacinto", 33.745, -116.716),
    ("Wrightwood / Big Pines", 34.360, -117.700),
    ("San Gabriels / Angeles", 34.300, -118.020),
    ("Ojai / Los Padres",     34.490, -119.250),
    ("Mt Pinos / Frazier Park", 34.780, -119.000),
    ("Pyramid Lake",          34.670, -118.790),
    ("Malibu / Ventura Coast", 34.050, -118.950),
    ("Orange County Coast",   33.460, -117.660),
    ("Santa Barbara Coast",   34.460, -120.050),
    ("San Diego Coast",       33.050, -117.290),
    ("Central Coast / Pismo", 35.130, -120.630),
    ("Big Sur",               36.270, -121.800),
    # Desert (added 2026-07-01 from the sought-after study — Joshua Tree Jumbo Rocks/Indian Cove &
    # Anza-Borrego are top bucket-list demand and in LA range, but had no anchor so were never searched).
    ("Joshua Tree",           34.000, -116.160),
    ("Anza-Borrego Desert",   33.270, -116.410),
]

# Far destinations (e.g. Big Sur ~250mi) sit beyond the everyday LA radius and the
# recreation.gov search's 150-result score cap, so a single LA-centered search never
# returns them. CampSage therefore ALSO runs a small search centered on each anchor and
# merges the results. The "All" tab stays closest-to-LA (≤ MAX_DISTANCE_MI); farther
# destination finds appear only under their own region tab.
ANCHOR_SEARCH_ENABLED   = True
REGION_SEARCH_RADIUS_MI = 60    # radius for each per-anchor destination search (covers the
                                # full ~50mi span of a region, e.g. Plaskett Creek south of Big Sur)
REGION_MAX_DISTANCE_MI  = 300   # absolute outer cap for any destination find (sanity)
REGION_MAX_PER_TAB      = 12    # cap destination campgrounds kept per far region tab

# ── State parks (ReserveCalifornia, beyond the beach section) ─────────────────
# Pull general CA STATE PARK campgrounds (Pfeiffer Big Sur, Andrew Molera, Limekiln, …) —
# NOT on recreation.gov — and merge them into the region tabs + a 🏕️ State Parks tab.
# Searched per region anchor (same pattern as the federal anchor search). No review scores
# exist in ReserveCalifornia, so these rank by distance/soonest like the beaches do.
STATE_PARKS_ENABLED    = True
STATE_PARK_PER_ANCHOR  = 6      # nearest campable state parks kept per region (bounds API load)
STATE_PARK_MAX_ANALYZE = 45     # global cap on parks we fetch availability for (API budget)

# ── Plumbing ──────────────────────────────────────────────────────────────────
DATA_DIR      = Path.home() / "campsage"
STATUS_JSON   = DATA_DIR / "status.json"
DASHBOARD_HTML= DATA_DIR / "dashboard.html"
TIPS_JSON     = DATA_DIR / "booking_tips.json"   # written by ai_concierge.sh (subscription)
HEALTH_JSON   = DATA_DIR / "health.json"          # written by campsage_doctor.sh (subscription)
LOG_FILE      = DATA_DIR / "campsage.log"

# Browser-like UA — recreation.gov's public JSON endpoints answer these; a bare
# python UA can get an HTML interstitial or a block.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HTTP_TIMEOUT  = 30
MAX_WORKERS   = 8           # parallel availability fetches (be polite)
RETRIES       = 3
