#!/usr/bin/env bash
# RESTORE-002 bounded release — one controlled gateway stop/start, no retries, reversible.
#
#   dry run (default, mutates nothing):
#     bash harness/release002_release.sh --candidate <sha>
#
#   authorised run (must be launched from a transient systemd unit, never from inside the gateway):
#     HERMES_RELEASE_APPROVAL=<sha> systemd-run --unit=hermes-restore-002-release --collect \
#        --property=Type=oneshot --property=WorkingDirectory=/home/hermes/.hermes/hermes-agent \
#        --property=StandardOutput=journal --property=StandardError=journal \
#        /bin/bash /home/hermes/.hermes/hermes-agent/harness/release002_release.sh \
#        --apply --candidate <sha>
#
# Boundaries honoured here: the recovery hold stays (90-recovery-hold.conf and the env var are never
# touched), 95-timeout-stop.conf is retained through the restart, the session database is opened
# read-only and never written, and there is exactly one restart on the success path (a second only
# inside rollback, after a failure).
set -euo pipefail

INSTALL=/home/hermes/.hermes/hermes-agent
SERVICE=hermes-gateway.service
UNIT=/etc/systemd/system/$SERVICE
DROPIN_DIR=$UNIT.d
HOLD_DROPIN=$DROPIN_DIR/90-recovery-hold.conf
TIMEOUT_DROPIN=$DROPIN_DIR/95-timeout-stop.conf
STATE_JSON=/home/hermes/.hermes/gateway_state.json
HERE=$(cd "$(dirname "$0")" && pwd)
PY=$INSTALL/venv/bin/python
EXPECT_HEAD=${EXPECT_HEAD:-e05128febbc559469a5fb835a9235a543dfdfa64}
EXPECT_TREE=${EXPECT_TREE:-dc05abd074c834569cf19ca89a6446d74be5c41d}
QUIESCE_WAIT=${QUIESCE_WAIT:-900}
STARTUP_WAIT=${STARTUP_WAIT:-240}
APPLY=0
CANDIDATE=""
ARCHIVE=""
MUTATED=0
PHASE="init"

log()  { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PHASE" "$*"; }
warn() { printf '%s [%s] WARN %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PHASE" "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --candidate) CANDIDATE=$2; shift 2 ;;
    --candidate=*) CANDIDATE=${1#*=}; shift ;;
    --archive) ARCHIVE=$2; shift 2 ;;
    --archive=*) ARCHIVE=${1#*=}; shift ;;
    --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

if [ -z "$CANDIDATE" ]; then
  echo "--candidate <sha> is required (the exact commit this release deploys)" >&2
  exit 64
fi

CAND_TREE=$(git -C "$INSTALL" rev-parse "$CANDIDATE^{tree}" 2>/dev/null || true)
if [ -z "$CAND_TREE" ]; then
  echo "candidate $CANDIDATE is not present in $INSTALL" >&2
  exit 64
fi
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE=${ARCHIVE:-/home/hermes/work/HERMES-RESTORE-002/release-$STAMP}

preflight() { # $1=head  $2=tree  -> prints JSON, exit 0 only when every critical gate passes
  "$PY" "$HERE/release002_preflight.py" --expect-head "$1" --expect-tree "$2"
}

receipt() { # $1=status  $2=detail-file
  local out=$ARCHIVE/RECEIPT.md
  {
    echo "# RESTORE-002 bounded release receipt"
    echo
    echo "- status: **$1**"
    echo "- generated: $STAMP (UTC)"
    echo "- candidate: \`$CANDIDATE\` (tree \`$CAND_TREE\`)"
    echo "- install before: \`$EXPECT_HEAD\` (tree \`$EXPECT_TREE\`)"
    echo "- archive: \`$ARCHIVE\`"
    echo "- unit fragment sha256: $(sha256sum "$UNIT" | cut -d' ' -f1)"
    echo "- hold drop-in sha256: $(sha256sum "$HOLD_DROPIN" 2>/dev/null | cut -d' ' -f1)"
    echo "- timeout drop-in sha256: $(sha256sum "$TIMEOUT_DROPIN" 2>/dev/null | cut -d' ' -f1)"
    echo "- effective TimeoutStopUSec: $(systemctl show "$SERVICE" -p TimeoutStopUSec --value)"
    echo "- MainPID: $(systemctl show "$SERVICE" -p MainPID --value)  NRestarts: $(systemctl show "$SERVICE" -p NRestarts --value)"
    echo
    echo '```'
    cat "$2" 2>/dev/null || true
    echo '```'
  } >"$out"
  log "receipt written: $out"
}

rollback() { # $1=reason
  local reason=$1
  PHASE=rollback
  warn "rolling back: $reason"
  if [ -f "$ARCHIVE/unit/hermes-gateway.service" ]; then
    install -m 0644 -o root -g root "$ARCHIVE/unit/hermes-gateway.service" "$UNIT"
  fi
  if [ -f "$ARCHIVE/unit/90-recovery-hold.conf" ]; then
    install -m 0644 -o root -g root "$ARCHIVE/unit/90-recovery-hold.conf" "$HOLD_DROPIN"
  fi
  if [ -f "$ARCHIVE/unit/95-timeout-stop.conf" ]; then
    install -m 0644 -o root -g root "$ARCHIVE/unit/95-timeout-stop.conf" "$TIMEOUT_DROPIN"
  fi
  systemctl daemon-reload
  git -C "$INSTALL" reset --hard "$EXPECT_HEAD" >/dev/null 2>&1 || warn "git reset --hard failed"
  timeout 340 systemctl restart "$SERVICE" || warn "rollback restart returned non-zero"
  sleep 20
  git -C "$INSTALL" rev-parse HEAD >"$ARCHIVE/rollback.txt" 2>&1 || true
  systemctl show "$SERVICE" -p ActiveState -p MainPID -p NRestarts >>"$ARCHIVE/rollback.txt" 2>&1 || true
  receipt "ROLLED_BACK" "$ARCHIVE/rollback.txt"
  exit 1
}

on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$MUTATED" -ne 0 ] && [ "$PHASE" != "rollback" ] && [ "$PHASE" != "done" ]; then
    rollback "unhandled failure in phase '$PHASE' (rc=$rc)"
  fi
}
trap on_exit EXIT

# ---------------------------------------------------------------- phase 0: preflight (read-only)
PHASE=preflight
log "preflight against $EXPECT_HEAD / $EXPECT_TREE"
mkdir -p "$ARCHIVE/unit"
set +e
preflight "$EXPECT_HEAD" "$EXPECT_TREE" >"$ARCHIVE/preflight-before.json" 2>"$ARCHIVE/preflight-before.err"
PF_RC=$?
set -e
if [ "$PF_RC" -ne 0 ]; then
  log "preflight verdict NO_GO (rc=$PF_RC) — nothing was changed"
  grep -E '"(gate|failed|verdict)"' "$ARCHIVE/preflight-before.json" || cat "$ARCHIVE/preflight-before.err"
  exit 2
fi
log "preflight verdict GO"

if [ "$APPLY" -ne 1 ]; then
  PHASE=dry-run
  log "DRY RUN — the following would execute, in this order, with exactly one restart:"
  cat <<EOF
  1. archive $UNIT + $DROPIN_DIR/*.conf -> $ARCHIVE/unit/ (+ sha256 manifest)
  2. wait for gateway_state.json active_agents == 0 three times, 5 s apart (deadline ${QUIESCE_WAIT}s)
  3. git -C $INSTALL merge --ff-only $CANDIDATE   (expects tree $CAND_TREE)
  4. $PY -m hermes_cli.main gateway install --system   (regenerates the unit: TimeoutStopSec=300s; no restart)
  5. verify /etc unit TimeoutStopSec == 300s and systemctl show TimeoutStopUSec == 5min
  6. one timed: systemctl restart $SERVICE   (must stop without SIGKILL, restart active)
  7. post-verify: preflight vs $CANDIDATE, hold still active, session fingerprint unchanged
  8. receipt -> $ARCHIVE/RECEIPT.md   (status RELEASED_VERIFIED, or ROLLED_BACK)
  Rollback on any failure after step 3: restore archived unit + drop-ins, daemon-reload,
  git reset --hard $EXPECT_HEAD, one restart.
EOF
  exit 0
fi

APPROVAL=${HERMES_RELEASE_APPROVAL:-}
if [ "$APPROVAL" != "$CANDIDATE" ]; then
  echo "refusing to mutate: HERMES_RELEASE_APPROVAL must equal the candidate SHA ($CANDIDATE)" >&2
  exit 65
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "refusing to mutate: run as root from a transient systemd unit (see header)" >&2
  exit 65
fi
SELF_CG=$(cat /proc/self/cgroup | head -1 | cut -d: -f3)
SVC_CG=$(systemctl show "$SERVICE" -p ControlGroup --value)
case "$SELF_CG" in
  "$SVC_CG"|"$SVC_CG"/*)
    echo "refusing to mutate: this process runs inside $SVC_CG; launch from a transient unit" >&2
    exit 65 ;;
esac

# ------------------------------------------------------------ phase 1: archive (reversible state)
PHASE=archive
install -m 0644 -o root -g root "$UNIT" "$ARCHIVE/unit/hermes-gateway.service"
for f in "$DROPIN_DIR"/*.conf; do [ -e "$f" ] && install -m 0644 -o root -g root "$f" "$ARCHIVE/unit/$(basename "$f")"; done
sha256sum "$UNIT" "$DROPIN_DIR"/*.conf >"$ARCHIVE/unit/SHA256SUMS" 2>/dev/null || true
git -C "$INSTALL" rev-parse HEAD >"$ARCHIVE/install-before.txt"
"$PY" "$HERE/restore002_session_inventory.py" --json "$ARCHIVE/sessions-before.json" >/dev/null
log "archived unit, drop-ins, install identity and session inventory"

# ------------------------------------------------------------------- phase 2: quiesce (no live turn)
PHASE=quiesce
deadline=$((SECONDS + QUIESCE_WAIT))
quiet=0
while [ $SECONDS -lt $deadline ]; do
  agents=$("$PY" - "$STATE_JSON" <<'EOF' 2>/dev/null || echo -1
import json,sys
try:
    print(int(json.load(open(sys.argv[1])).get("active_agents") or 0))
except Exception:
    print(-1)
EOF
)
  if [ "$agents" = "0" ]; then quiet=$((quiet + 1)); else quiet=0; fi
  log "active_agents=$agents quiet_samples=$quiet"
  if [ "$quiet" -ge 3 ]; then break; fi
  sleep 5
done
if [ "$quiet" -lt 3 ]; then
  log "no quiescent window within ${QUIESCE_WAIT}s — aborting before any mutation"
  receipt "ABORTED_NOT_QUIET" "$ARCHIVE/preflight-before.json"
  exit 3
fi

# ------------------------------------------------------------------- phase 3: deploy the candidate
PHASE=deploy
MUTATED=1
log "fast-forwarding $INSTALL to $CANDIDATE"
git -C "$INSTALL" merge --ff-only "$CANDIDATE" >"$ARCHIVE/merge.txt" 2>&1 || rollback "fast-forward merge refused"
HEAD=$(git -C "$INSTALL" rev-parse HEAD)
TREE=$(git -C "$INSTALL" rev-parse HEAD^{tree})
if [ "$HEAD" != "$CANDIDATE" ] || [ "$TREE" != "$CAND_TREE" ]; then
  rollback "post-merge identity mismatch: HEAD=$HEAD tree=$TREE"
fi
if [ -n "$(git -C "$INSTALL" status --porcelain)" ]; then
  rollback "working tree dirty after merge"
fi
log "deployed HEAD=$HEAD tree=$TREE"

# --------------------------------------------------------------- phase 4: regenerate the unit file
PHASE=unit
log "regenerating the unit from the deployed code (no restart)"
( cd "$INSTALL" && HERMES_HOME=/home/hermes/.hermes "$PY" -m hermes_cli.main gateway install --system ) \
  >"$ARCHIVE/unit-install.txt" 2>&1 || rollback "gateway install --system failed"
systemctl daemon-reload
UNIT_TIMEOUT=$(awk -F= '/^TimeoutStopSec=/{print $2}' "$UNIT" | tail -1)
EFFECTIVE=$(systemctl show "$SERVICE" -p TimeoutStopUSec --value)
log "unit TimeoutStopSec=${UNIT_TIMEOUT:-<absent>}  effective TimeoutStopUSec=$EFFECTIVE"
case "$UNIT_TIMEOUT" in
  300|300s|5min) : ;;
  *) rollback "generated unit does not carry TimeoutStopSec=300s (got '${UNIT_TIMEOUT:-absent}')" ;;
esac
[ "$EFFECTIVE" = "5min" ] || rollback "effective TimeoutStopUSec is '$EFFECTIVE', expected 5min"

# ------------------------------------------------- phase 5: one controlled stop/start, timed
PHASE=restart
OLD_PID=$(systemctl show "$SERVICE" -p MainPID --value)
RESTART_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START_MS=$(date +%s%3N)
set +e
timeout 340 systemctl restart "$SERVICE" >"$ARCHIVE/restart.txt" 2>&1
RESTART_RC=$?
set -e
STOP_MS=$(( $(date +%s%3N) - START_MS ))
log "systemctl restart rc=$RESTART_RC after ${STOP_MS}ms"
[ "$RESTART_RC" -eq 0 ] || rollback "systemctl restart returned $RESTART_RC"

NEW_PID=$(systemctl show "$SERVICE" -p MainPID --value)
deadline=$((SECONDS + STARTUP_WAIT))
while [ $SECONDS -lt $deadline ]; do
  state=$(systemctl show "$SERVICE" -p ActiveState --value)
  [ "$state" = "active" ] && [ "$NEW_PID" != "$OLD_PID" ] && [ "$NEW_PID" != "0" ] && break
  sleep 3
  NEW_PID=$(systemctl show "$SERVICE" -p MainPID --value)
done
[ "$state" = "active" ] || rollback "service did not return to active (state=$state)"

# ------------------------------------------------------------------- phase 6: shutdown forensics
PHASE=verify-shutdown
journalctl -u "$SERVICE" --since "$RESTART_AT" --no-pager -o short-iso >"$ARCHIVE/journal-after-restart.txt" 2>&1 || true
KILLS=$(grep -c 'with signal KILL' "$ARCHIVE/journal-after-restart.txt" || true)
DRAIN=$(grep -E 'drain|Drain' "$ARCHIVE/journal-after-restart.txt" | tail -5 || true)
STOPLINE=$(grep -E 'Stopped |Stopping ' "$ARCHIVE/journal-after-restart.txt" | tail -4 || true)
log "SIGKILL lines after restart: ${KILLS:-0}"
[ "${KILLS:-0}" -eq 0 ] || rollback "systemd SIGKILLed the gateway during the controlled stop"

# --------------------------------------------------------------------- phase 7: post-verification
PHASE=verify-post
set +e
preflight "$CANDIDATE" "$CAND_TREE" >"$ARCHIVE/preflight-after.json" 2>"$ARCHIVE/preflight-after.err"
PF2=$?
set -e
[ "$PF2" -eq 0 ] || rollback "post-release preflight NO_GO"
HOLD_ENV=$(tr '\0' '\n' <"/proc/$NEW_PID/environ" | grep -c '^HERMES_RECOVERY_HOLD=1$' || true)
[ "$HOLD_ENV" -eq 1 ] || rollback "recovery hold is not set in the new process environment"

"$PY" "$HERE/restore002_session_inventory.py" --json "$ARCHIVE/sessions-after.json" >/dev/null
python_compare=$("$PY" - "$ARCHIVE/sessions-before.json" "$ARCHIVE/sessions-after.json" <<'EOF'
import json,sys
a,b=(json.load(open(p)) for p in sys.argv[1:3])
keys=("open_sessions_fingerprint_sha256","empty_open")
same=all(a.get(k)==b.get(k) for k in keys)
print("same" if same else f"CHANGED {[(k,a.get(k),b.get(k)) for k in keys if a.get(k)!=b.get(k)]}")
EOF
)
log "session database: $python_compare"
[ "$python_compare" = "same" ] || rollback "session inventory changed across the release ($python_compare)"

# ------------------------------------------------------------------------------ phase 8: receipt
PHASE=done
{
  echo "restart_at: $RESTART_AT"
  echo "restart_rc: $RESTART_RC"
  echo "restart_elapsed_ms: $STOP_MS"
  echo "old_pid: $OLD_PID"
  echo "new_pid: $NEW_PID"
  echo "sigkill_lines: ${KILLS:-0}"
  echo "unit_timeout: ${UNIT_TIMEOUT}"
  echo "effective_timeout_usec: $EFFECTIVE"
  echo "head: $HEAD"
  echo "tree: $TREE"
  echo "hold_env_in_new_process: $HOLD_ENV"
  echo "session_inventory: $python_compare"
  echo "--- stop/drain lines ---"
  echo "$STOPLINE"
  echo "$DRAIN"
} >"$ARCHIVE/verification.txt"
receipt "RELEASED_VERIFIED" "$ARCHIVE/verification.txt"
log "release complete: HEAD=$HEAD TimeoutStopUSec=$EFFECTIVE SIGKILL=${KILLS:-0}"
