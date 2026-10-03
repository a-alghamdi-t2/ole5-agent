#!/bin/sh
# Runs as root at container start, only to fix ownership on the bind-mounted
# ./data volume -- its permissions come from the host, not the image, so the
# build-time chown in the Dockerfile does not survive the mount. Then drops
# to the unprivileged user for the actual process.
set -e
mkdir -p /app/data/uploads
chown -R ole5:ole5 /app/data
exec gosu ole5 "$@"