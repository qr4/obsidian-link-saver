#!/usr/bin/env bash
# Update the stack from git and rebuild it. Run it on the server, in the clone
# of this repository the stack builds from:
#
#   deploy/update.sh [--project-dir DIR] [--rebuild] [-y]
#
#   --project-dir DIR  the folder Compose runs from: it holds .env and, for a
#                      stack manager such as Unraid's Compose Manager, its own
#                      copy of docker-compose.yml (plus an override file).
#                      Default: the folder the obsidian-saver container was
#                      started from, or the clone if that container doesn't
#                      exist.
#   --rebuild          skip git; only sync the compose file, build and restart
#                      (e.g. after rolling back to an earlier commit)
#   -y, --yes          don't ask before each step
#
# Steps: fetch and show the incoming commits, fast-forward (refusing local
# changes), copy docker-compose.yml into the project folder when it differs,
# build the images, and restart only if the build succeeded. Data folders
# (DATA_DIR, VAULT_DIR, BLOBS_DIR) are never touched. When the project folder
# isn't the clone, its .env must set STACK_DIR to the clone.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project=""
rebuild=0
yes=0
while [ $# -gt 0 ]; do
    case "$1" in
        --project-dir) project="$(cd "${2:?--project-dir needs a folder}" && pwd)"; shift 2 ;;
        --rebuild) rebuild=1; shift ;;
        -y|--yes) yes=1; shift ;;
        -h|--help) sed -n '2,/^set -e/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
done

confirm() {
    [ "$yes" = 1 ] && return 0
    read -r -p "$1 [y/N] " answer
    case "$answer" in [yY]|[yY][eE][sS]) return 0 ;; *) echo "Stopped."; exit 1 ;; esac
}

if [ -z "$project" ]; then
    started_from="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
        obsidian-saver 2>/dev/null || true)"
    if [ -d "$started_from" ]; then
        project="$(cd "$started_from" && pwd)"
    else
        project="$here"
    fi
    [ "$project" = "$here" ] || echo "Compose project folder: $project (from the obsidian-saver container)"
fi

cd "$here"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || { echo "$here is not a git clone (deploy/push.sh is the alternative)" >&2; exit 1; }
before="$(git rev-parse HEAD)"

# --- 1. git ------------------------------------------------------------------
if [ "$rebuild" = 0 ]; then
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "Local changes in the checkout; commit, stash or discard them first:" >&2
        git status --short --untracked-files=no >&2
        exit 1
    fi
    branch="$(git symbolic-ref --short -q HEAD)" \
        || { echo "Detached HEAD; check out a branch first (git checkout main)" >&2; exit 1; }
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)" \
        || { echo "Branch $branch tracks no remote branch" >&2; exit 1; }

    echo "Fetching $upstream ..."
    git fetch --quiet
    if git merge-base --is-ancestor "$upstream" HEAD; then
        echo "Already up to date: $(git log -1 --format='%h %s')"
        echo "(--rebuild rebuilds anyway)"
        exit 0
    fi
    git merge-base --is-ancestor HEAD "$upstream" \
        || { echo "$branch has commits that $upstream doesn't; can't fast-forward" >&2; exit 1; }

    echo
    echo "Incoming commits:"
    git log --oneline --no-decorate "HEAD..$upstream"
    echo
    echo "Changed files:"
    git diff --stat "HEAD..$upstream" | tail -n 25
    echo
    confirm "Fast-forward $branch to $(git rev-parse --short "$upstream")?"
    git merge --ff-only --quiet "$upstream"
fi

# --- 2. compose file -----------------------------------------------------------
if [ "$project" != "$here" ]; then
    if ! cmp -s "$here/docker-compose.yml" "$project/docker-compose.yml"; then
        echo
        echo "docker-compose.yml differs from the copy in $project:"
        diff -u "$project/docker-compose.yml" "$here/docker-compose.yml" || true
        echo
        confirm "Copy the new docker-compose.yml into $project? (the old one stays in git history)"
        cp "$here/docker-compose.yml" "$project/docker-compose.yml"
    fi
fi

# --- 3. build and restart ----------------------------------------------------
# Without -f, Compose picks up docker-compose.yml, docker-compose.override.yml
# and .env from the project folder by itself.
cd "$project"
docker compose config -q
# Services come out sorted by name, so livesync-bridge's context (<clone>/livesync)
# is listed first: look for the clone among all of them.
if ! docker compose config | sed -n 's/^ *context: //p' | grep -qxF "$here"; then
    echo "The images wouldn't build from this clone ($here)." >&2
    echo "Set STACK_DIR=$here in $project/.env." >&2
    exit 1
fi

echo
confirm "Build the images (the running stack keeps running meanwhile)?"
docker compose build
echo
confirm "Restart the stack with the new images?"
docker compose up -d
docker compose ps

echo
echo "Updated: $(git -C "$here" log -1 --format='%h %s')"
if [ "$(git -C "$here" rev-parse HEAD)" != "$before" ]; then
    extra=""
    [ "$project" != "$here" ] && extra=" --project-dir $project"
    echo "Roll back with: git -C $here reset --hard ${before:0:12} && $here/deploy/update.sh --rebuild$extra"
fi
