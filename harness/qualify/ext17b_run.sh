#!/usr/bin/env bash
# EXT-17B — enter the disposable release environment and run a command inside it.
#
#   ext17b_run.sh <scenario-root> <command...>
#
# A private user+mount namespace is created with `unshare -r -m`. Inside it, every production path
# the release writes is replaced by a fresh tmpfs and seeded from the scenario fixture:
#
#   /etc/systemd/system              the unit + drop-ins the release regenerates and reads
#   /usr/local/lib/hermes-release    the launcher library, staged engine and manifest
#   /var/lib/hermes-release          the release journal and lock
#   /var/log                         the supervisor log
#
# Nothing outside those paths is mounted, and nothing on the host is modified: mounts exist only for
# the lifetime of this process tree. The guard (ext17b_guard.py) refuses to run unless all four are
# the private mount proven by a per-run random sentinel.
set -euo pipefail

ROOT=${1:?usage: ext17b_run.sh <scenario-root> <command...>}
shift
. "$ROOT/env.sh"

EXT17B_SENTINEL=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
export EXT17B_SENTINEL
export EXT17B_ROOT="$ROOT"

exec unshare -r -m --propagation private /bin/bash "$(dirname "$(readlink -f "$0")")/ext17b_ns.sh" "$@"
