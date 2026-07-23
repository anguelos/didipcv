#!/bin/sh
# Entrypoint for every didipcv container: optionally start the debug sshd, then hand over to the
# real command (the microservice).
#
# The service is the point of the container and the sshd is a convenience, so a failure to start
# sshd NEVER stops the service -- it warns and carries on. `exec` at the end keeps the service as
# PID 1, so it still receives SIGTERM from `docker stop` and shuts down cleanly.
#
#   DDP_SSHD=1  (default) start sshd on port 2222 when possible
#   DDP_SSHD=0            do not start it at all
set -e

if [ "${DDP_SSHD:-1}" = "1" ] && [ -x /usr/sbin/sshd ]; then
    if [ "$(id -u)" = "0" ]; then
        mkdir -p /run/sshd
        # -e keeps sshd's log on stderr, so `docker compose logs` shows auth failures
        if /usr/sbin/sshd -e; then
            echo "[ddp-entrypoint] sshd listening on 2222 (key-only)" >&2
        else
            echo "[ddp-entrypoint] WARNING: sshd failed to start; continuing without it" >&2
        fi
    else
        # sshd needs root: privilege separation, the host keys in /etc/ssh, and /run/sshd are all
        # root-owned. Running as another uid it cannot authenticate anyone, so do not pretend.
        echo "[ddp-entrypoint] sshd NOT started: container runs as uid $(id -u), sshd needs root." >&2
        echo "[ddp-entrypoint] Use \`docker compose exec <service> bash\`, or set DDP_USER=0:0 to" >&2
        echo "[ddp-entrypoint] run this container as root and get ssh on port 2222." >&2
    fi
fi

exec "$@"
