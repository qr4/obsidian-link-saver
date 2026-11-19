#!/bin/sh
# Entrypoint of the livesync-bridge container.
set -e

# Wait for the database to be initialised, then write config.json to match.
deno run --allow-env --allow-net --allow-write=/app/dat /opt/bridge-start.ts

# The bridge rescans the vault whenever it starts. Its file watcher can miss a
# file written right after its folder was created, and the save service does
# exactly that for every new video and repo. So the bridge stops every hour
# (BRIDGE_RESCAN_HOURS) and Docker's restart policy starts it again; the rescan picks up
# anything that was missed. A stop mid-transfer is safe: it resumes from the
# last completed change.
exec timeout "${BRIDGE_RESCAN_HOURS:-1}h" deno task run
