#!/usr/bin/env python3
"""
scan_pending.py — cron glue (every minute via supercronic) that scans locations
added from the web UI. The trigger is simply "saved in the store but absent from
locations.json": an add creates that condition, a crashed scan leaves it (so it
retries), and a successful scan clears it via update_index. Exits instantly when
there's nothing to do; skips (and retries next minute) if a scan already holds the
lock — pending scans must queue behind the 7/13/18 cron scans, never overlap them.
"""
import random
import sys
import time

import locations


def pending_slugs():
    try:
        store = locations.load_store()   # seeds from config on the very first tick,
    except Exception:                    # so the web edit UI appears without waiting
        return []                        # for the next full cron scan
    idx_slugs = {e.get("slug") for e in (locations.read_index() or {}).get("locations", [])}
    return [e["slug"] for e in store.get("locations", []) if e["slug"] not in idx_slugs]


def main():
    if not pending_slugs():
        return 0
    with locations.scan_lock(blocking=False) as got:
        if not got:
            return 0                     # a scan is running; cron retries in a minute
        todo = pending_slugs()           # re-check under the lock — the scan we
        if not todo:                     # waited out may have covered it
            return 0
        import camp_agent
        camp_agent.log(f"scan_pending: new location(s) {', '.join(todo)}")
        locs = locations.load_locations()
        first = True
        for loc in (l for l in locs if l.slug in todo):
            if not first:
                time.sleep(random.uniform(90, 240))
            first = False
            camp_agent.LOG_PREFIX = f"[{loc.slug}] "
            status = camp_agent.run(loc, locs)
            locations.update_index(loc, status)
        camp_agent.LOG_PREFIX = ""
        try:
            import camp_wiki_images
            camp_wiki_images.run()       # new location's parks need photos too
        except Exception as e:
            camp_agent.log(f"scan_pending: wiki images failed (non-fatal): {e}")
        locations.prune_orphan_dirs()
    return 0


if __name__ == "__main__":
    sys.exit(main())
