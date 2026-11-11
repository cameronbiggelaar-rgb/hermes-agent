#!/usr/bin/env bash
# EXT-17B — namespace body: mount the throwaway release paths, seed the fixture, verify the guard,
# then run the requested command. Invoked by ext17b_run.sh; never run directly.
set -euo pipefail

: "${EXT17B_ROOT:?}" "${EXT17B_SENTINEL:?}"
ROOT=$EXT17B_ROOT
. "$ROOT/env.sh"
export PATH="$ROOT/bin:$PATH"

# /etc/systemd/system is a *fresh tmpfs* each run, so the unit the release regenerates never leaks
# into another scenario and the operator's real unit is unreachable. The other three paths are bind
# mounts onto the scenario directory, so the journal, archive, staged engine and supervisor log
# survive the namespace and can be inspected afterwards. Either way the write lands in the scenario,
# never in production.
mount -t tmpfs -o size=64M,mode=0755 none /etc/systemd/system
mount --bind "$ROOT/lib" /usr/local/lib/hermes-release
mount --bind "$ROOT/var" /var/lib/hermes-release
mount --bind "$ROOT/logs" /var/log

for path in /etc/systemd/system /usr/local/lib/hermes-release /var/lib/hermes-release /var/log; do
  printf '%s\n' "$EXT17B_SENTINEL" > "$path/.ext17b-sentinel"
done

install -d -m 0755 /etc/systemd/system/hermes-gateway.service.d
install -m 0644 "$ROOT/systemd/hermes-gateway.service" /etc/systemd/system/hermes-gateway.service
install -m 0644 "$ROOT/systemd/hermes-gateway.service.d/"*.conf \
  /etc/systemd/system/hermes-gateway.service.d/

/usr/bin/python3.13 "$(dirname "$(readlink -f "$0")")/ext17b_guard.py" --root "$ROOT"

exec "$@"
