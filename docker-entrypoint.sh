#!/bin/sh
# Make the mounted volume writable, then drop privileges — but never at the cost of
# starting at all.
#
# A platform mounts its persistent volume after the image is built, owned by root, while
# this image's process runs as uid 10001. So the container starts as root, fixes the
# ownership, and steps down. Every step here is best-effort: a runtime that forbids chown
# or setuid should give you a server running as root with a warning, not a container that
# exits and a 502 with nothing to read.
STATE_DIR="${SIMPL_MCP_STATE_DIR:-/data}"
APP_UID=10001
APP_GID=0

if [ "$(id -u)" != "0" ]; then
    # Already unprivileged — nothing to do, and nothing we could do.
    exec "$@"
fi

mkdir -p "$STATE_DIR" 2>/dev/null || true
if ! chown -R "${APP_UID}:${APP_GID}" "$STATE_DIR" 2>/dev/null; then
    echo "entrypoint: could not take ownership of $STATE_DIR; writes may fail" >&2
fi
chmod 0770 "$STATE_DIR" 2>/dev/null || true

# Probe before committing: if this runtime cannot change uid, running as root beats not
# running.
if command -v setpriv >/dev/null 2>&1 && \
   setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups true 2>/dev/null; then
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups -- "$@"
fi

echo "entrypoint: could not drop privileges; continuing as root" >&2
exec "$@"
