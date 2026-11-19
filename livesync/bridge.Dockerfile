# syntax=docker/dockerfile:1.7
# livesync-bridge (https://github.com/vrtmrz/livesync-bridge), pinned to one
# commit: upstream has no releases and main changes often.
#
# Runs as PUID:PGID (build args, from .env) instead of upstream's uid 1993,
# so the files it writes into the vault stay editable by the save service and
# by the vault's owner.
FROM docker.io/denoland/deno:debian-2.9.7

ARG PUID=1000
ARG PGID=1000

ARG BRIDGE_REF=1c252305bbddcacd200b8560d3bb786afe0906e9
ADD https://github.com/vrtmrz/livesync-bridge.git#${BRIDGE_REF} /app
WORKDIR /app
RUN deno install --frozen \
 && mkdir -p /app/dat /deno-dir/location_data \
 && chown -R ${PUID}:${PGID} /app /deno-dir

COPY bridge-start.ts bridge-entrypoint.sh /opt/
USER ${PUID}:${PGID}
ENTRYPOINT ["sh", "/opt/bridge-entrypoint.sh"]
