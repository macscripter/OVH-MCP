#!/bin/sh
# Start as root only long enough to make the mounted volume usable, then drop privileges.
#
# Platforms mount persistent volumes owned by root, and this image runs as uid 10001. The
# result is a server that starts, answers /healthz, and fails the first tool call that
# writes with "[Errno 13] Permission denied: /data/profiles" — a failure that looks like a
# bug in the application and is really a mount option.
#
# If the container is already non-root (someone set `user:` themselves), this does nothing
# and runs the command as-is.
set -e

STATE_DIR="${SIMPL_MCP_STATE_DIR:-/data}"
APP_UID=10001
APP_GID=0

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$STATE_DIR"
    # Ownership only; a chown of a large existing tree is cheap here because this directory
    # holds a handful of small JSON files and kubeconfigs.
    chown -R "${APP_UID}:${APP_GID}" "$STATE_DIR" 2>/dev/null || \
        echo "warning: could not take ownership of $STATE_DIR; writes may fail" >&2
    chmod 0770 "$STATE_DIR" 2>/dev/null || true
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups -- "$@"
fi

exec "$@"
