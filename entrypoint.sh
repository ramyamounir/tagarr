#!/bin/sh

PUID=${PUID:-1000}
PGID=${PGID:-1000}

groupadd -o -g "$PGID" appgroup 2>/dev/null
useradd -o -u "$PUID" -g "$PGID" -M -s /bin/sh appuser 2>/dev/null

chown "$PUID":"$PGID" /app /data /data/sonarr /data/radarr

exec gosu "$PUID":"$PGID" "$@"
