#!/bin/sh
# Scanner-container entrypoint: seed the page on first deploy, then hand off to supercronic.
set -u
DATA_DIR="${CAMPSAGE_DATA_DIR:-$HOME/campsage}"

if [ ! -f "$DATA_DIR/locations.json" ]; then
    echo "[scan-entrypoint] no locations.json yet - running initial scan"
    if python3 camp_agent.py; then
        python3 camp_wiki_images.py || echo "[scan-entrypoint] wiki image fetch failed (non-fatal)"
    else
        echo "[scan-entrypoint] initial scan failed; supercronic will retry on schedule"
    fi
fi

exec supercronic -passthrough-logs /app/crontab.scan
