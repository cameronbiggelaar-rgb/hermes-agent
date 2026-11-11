#!/usr/bin/env bash
# EXT-17B — drive the assembled release path inside the disposable environment.
#
#   ext17b_scenario.sh <step> [step...]
#
# steps: start | bootstrap | stage | plan | apply | apply-detach | recover | verify | status | stop
# env:   EXT17B_CANDIDATE (defaults to the EXT-17B candidate)
#
# Always invoked through ext17b_run.sh so the release paths are the private mounts. `start` launches
# the gateway substitute through the qualification `systemctl`, inside the namespace, so the release
# can read its /proc entries and signal it exactly as it would signal the real unit. The launcher is
# the repository copy for bootstrap/stage (it must be a committed blob) and the installed copy for
# every later step, exactly as in production.
set -euo pipefail

REPO=${HERMES_RELEASE_REPO:-/home/hermes/work/restore002-fix}
LIB=${HERMES_RELEASE_LIB:-/usr/local/lib/hermes-release}
CAND=${EXT17B_CANDIDATE:-9601cc8a8ec1fc7fb9d534c653cf66e916b50c20}
SRC=$REPO/harness/hermes-release-002
INSTALLED=$LIB/hermes-release-002

for step in "$@"; do
  case "$step" in
    start)     systemctl daemon-reload && systemctl start hermes-gateway.service ;;
    stop)      systemctl stop hermes-gateway.service ;;
    restart)   systemctl restart hermes-gateway.service ;;
    bootstrap) "$SRC" bootstrap ;;
    stage)     "$SRC" stage --candidate "$CAND" ;;
    plan)      "$INSTALLED" plan --candidate "$CAND" ;;
    apply)     HERMES_RELEASE_APPROVAL="$CAND" "$INSTALLED" apply --candidate "$CAND" ;;
    apply-detach) HERMES_RELEASE_APPROVAL="$CAND" "$INSTALLED" apply --candidate "$CAND" --detach ;;
    recover)   HERMES_RELEASE_APPROVAL="$CAND" "$INSTALLED" recover --candidate "$CAND" ;;
    verify)    "$INSTALLED" verify --candidate "$CAND" ;;
    status)    "$INSTALLED" status ;;
    *) echo "unknown step $step" >&2; exit 64 ;;
  esac
done
