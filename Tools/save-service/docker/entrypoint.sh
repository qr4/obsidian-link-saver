#!/bin/sh
# Runs as root only long enough to (optionally) update yt-dlp and fix
# /config ownership, then drops to PUID:PGID, so files written into the
# vault belong to the vault's owner.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
umask "${UMASK:-002}"

if [ "${YTDLP_AUTO_UPDATE:-false}" = "true" ]; then
    echo "Updating yt-dlp ..."
    pip install -q -U "yt-dlp[default]" || echo "yt-dlp update failed, keeping the bundled version"
fi

if [ ! -d /vault ] || [ -z "$(ls -A /vault 2>/dev/null)" ]; then
    echo "WARNING: /vault is empty - mount your Obsidian vault there"
fi

if [ "$(id -u)" = "0" ] && [ "$PUID" != "0" ]; then
    mkdir -p /config
    chown -R "$PUID:$PGID" /config
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups env HOME=/config "$@"
fi
exec "$@"
