#!/usr/bin/env bash
# Copy the stack's source (this repository, without .git) to a server, where
# docker compose builds from it. The alternative to a git clone there:
#
#   deploy/push.sh user@host /path/on/server
#
# Opens one SSH connection that later calls reuse for 10 minutes, so a
# password is asked at most once. Files the server owns are left alone: .env
# (secrets), data/ (container state) and anything else excluded below.
# PUSH_CHOWN=uid:gid sets the owner of the copied files (Unraid: 99:100).
set -euo pipefail

if [ $# -ne 2 ]; then
    echo "usage: $0 user@host /path/on/server" >&2
    exit 2
fi
host="$1"
dest="$2"
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
control="$HOME/.ssh/cm-%r@%h:%p"

if ! ssh -o ControlPath="$control" -O check "$host" 2>/dev/null; then
    ssh -o ControlMaster=yes -o ControlPath="$control" -o ControlPersist=600 \
        -o ConnectTimeout=15 -fN "$host"
fi

rsync -az --delete --itemize-changes ${PUSH_CHOWN:+--chown="$PUSH_CHOWN"} \
    -e "ssh -o ControlPath=$control" \
    --exclude=/.git/ --exclude=/.env --exclude=/client.env --exclude=/data/ \
    --exclude=/docker-compose.override.yml \
    --exclude=__pycache__/ --exclude='*.egg-info/' --exclude=.pytest_cache/ \
    --exclude=.ruff_cache/ --exclude=build/ --exclude=.venv/ \
    "$src/" "$host:$dest/"

echo "Pushed $src -> $host:$dest"
echo "Next, on the server: cd $dest && docker compose up -d --build"
