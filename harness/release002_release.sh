#!/usr/bin/env bash
# RESTORE-002 bounded release engine — one controlled gateway stop/start, no retries, reversible.
#
# THIS FILE IS NOT THE ENTRY POINT. The one authoritative command is
#
#     /usr/local/lib/hermes-release/hermes-release-002  plan|apply|verify|recover|status
#
# which pins this file, its helpers and the candidate commit/tree by checksum and runs the apply
# path from an independent transient unit. Invoking this script directly is refused unless it is
# the staged copy the launcher verified (HERMES_RELEASE_LAUNCH_TOKEN present), so there is exactly
# one documented launch path.
#
# Modes: --plan (default; mutates nothing), --apply, --verify-only, --recover.
#
# Boundaries honoured here: the recovery hold stays (90-recovery-hold.conf and the env var are never
# touched), 95-timeout-stop.conf is retained through the restart, the session database is opened
# read-only and never written, and there is exactly one restart on the success path (a second only
# inside rollback, after a failure).
set -euo pipefail

INSTALL=${HERMES_RELEASE_INSTALL:-/home/hermes/.hermes/hermes-agent}
# EXT-13B: three distinct identities. $INSTALL is the *installed* tree — the preserved RESTORE-001
# baseline before the deploy and the candidate afterwards. $SOURCE is the repository that holds the
# candidate commit; pre-release manifest checks read candidate objects/blobs from git there, because
# the baseline predates files this release introduces. $ENGINE_DIR is the staged execution engine.
SOURCE=${HERMES_RELEASE_SOURCE:-$INSTALL}
export HERMES_RELEASE_SOURCE="$SOURCE"
SERVICE=hermes-gateway.service
# The unit path is the production systemd path, always. The isolated qualification harness may point
# it at a temporary unit, but only with the explicit flag below, so no environment can silently
# redirect a real release's unit (EXT-11 qualification hook).
if [ -n "${HERMES_RELEASE_QUALIFY_UNIT:-}" ]; then
  [ "${HERMES_RELEASE_QUALIFY:-}" = "1" ] \
    || { echo "refusing HERMES_RELEASE_QUALIFY_UNIT without HERMES_RELEASE_QUALIFY=1" >&2; exit 64; }
  UNIT=$HERMES_RELEASE_QUALIFY_UNIT
else
  UNIT=/etc/systemd/system/$SERVICE
fi
DROPIN_DIR=$UNIT.d
HOLD_DROPIN=$DROPIN_DIR/90-recovery-hold.conf
TIMEOUT_DROPIN=$DROPIN_DIR/95-timeout-stop.conf
HHOME=${HERMES_HOME:-/home/hermes/.hermes}
STATE_JSON=$HHOME/gateway_state.json
ESTOP_FILE=$HHOME/ESTOP
LOCK=/var/lib/hermes-release/restore-002.lock
JOURNAL=${HERMES_RELEASE_JOURNAL:-/var/lib/hermes-release/restore-002/journal.json}
ENGINE_DIR=$(cd "$(dirname "$0")" && pwd)
# The manifest's engine keys are "harness/<file>" relative to the staged engine root; $ENGINE_DIR is
# this script's own directory, so --engine-dir must name the parent or every staged_file_bytes gate
# would look for harness/harness/<file> (EXT-13B).
ENGINE_ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=$INSTALL/venv/bin/python
PY3=/usr/bin/python3.13
EXPECT_HEAD=${EXPECT_HEAD:-e05128febbc559469a5fb835a9235a543dfdfa64}
EXPECT_TREE=${EXPECT_TREE:-dc05abd074c834569cf19ca89a6446d74be5c41d}
MAX_QUIESCE=${MAX_QUIESCE:-180}
QUIESCE_WAIT=${QUIESCE_WAIT:-180}
STARTUP_WAIT=${STARTUP_WAIT:-240}
DRAIN_ENGAGE_WAIT=${DRAIN_ENGAGE_WAIT:-45}
UNFREEZE_WAIT=${UNFREEZE_WAIT:-60}
MODE=plan
CANDIDATE=""
ARCHIVE=""
MUTATED=0
PHASE=init
RUN_ID=${HERMES_RELEASE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
FROZEN=0
ESTOP_OURS=0
KEEPALIVE_PID=""
RESTART_ATTEMPTS=0

log()  { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PHASE" "$*"; }
warn() { printf '%s [%s] WARN %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PHASE" "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --plan) MODE=plan; shift ;;
    --apply) MODE=apply; shift ;;
    --verify-only) MODE=verify; shift ;;
    --recover) MODE=recover; shift ;;
    --candidate) CANDIDATE=$2; shift 2 ;;
    --candidate=*) CANDIDATE=${1#*=}; shift ;;
    --archive) ARCHIVE=$2; shift 2 ;;
    --archive=*) ARCHIVE=${1#*=}; shift ;;
    --help|-h) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

if [ -z "$CANDIDATE" ]; then
  echo "--candidate <sha> is required (the exact commit this release deploys)" >&2
  exit 64
fi

ct() { env -u PYTHONPATH "$PY3" "$ENGINE_DIR/release002_control.py" "$@"; }
journal() { # journal <phase> [note] [extra ct args...]
  local phase=$1
  shift || true
  local note=""
  if [ $# -gt 0 ]; then note=$1; shift; fi
  PHASE=$phase
  if [ "$MODE" = plan ]; then return 0; fi
  mkdir -p "$(dirname "$JOURNAL")" 2>/dev/null || true
  # Every record pins the attempt's exact candidate, so a journal can never be replayed against a
  # different candidate and a stale attempt is detectable on its own (EXT-11).
  env -u PYTHONPATH "$PY3" "$ENGINE_DIR/release002_control.py" journal --path "$JOURNAL" \
    --phase "$phase" --note "$note" --candidate "$CANDIDATE" --run-id "$RUN_ID" "$@" >/dev/null 2>&1 \
    || warn "journal write failed for phase $phase"
}

case "$CANDIDATE" in
  *[!0-9a-f]*|"") echo "refusing to run: --candidate must be a full 40-character commit SHA" >&2; exit 64 ;;
esac
[ ${#CANDIDATE} -eq 40 ] || { echo "refusing to run: --candidate must be a full 40-character commit SHA" >&2; exit 64; }
# rev-parse echoes an unresolvable argument back on stdout, so the exit code alone is not a
# presence test: --verify is what makes an absent candidate fail instead of "resolving" to itself.
CAND_TREE=$(git -C "$INSTALL" rev-parse --verify --quiet "$CANDIDATE^{tree}" 2>/dev/null || true)
if [ -z "$CAND_TREE" ]; then
  echo "candidate $CANDIDATE is not present in $INSTALL (stage it first: hermes-release-002 stage)" >&2
  exit 64
fi
# ---------------------------------------------------------------- toolchain gate
# A broken interpreter must not be mistaken for a failed release gate later on.
if [ ! -x "$PY" ]; then
  for cand in "${HERMES_RELEASE_PY:-}" "$HOME/.hermes/venv/bin/python" /usr/bin/python3.13 /usr/bin/python3; do
    [ -n "$cand" ] && [ -x "$cand" ] && { PY=$cand; warn "install interpreter missing; falling back to $PY"; break; }
  done
fi
[ -x "$PY" ] || { echo "refusing to run: no usable python interpreter for $INSTALL" >&2; exit 65; }
"$PY3" -c 'import sys' >/dev/null 2>&1 || { echo "refusing to run: $PY3 is not usable" >&2; exit 65; }
"$PY3" -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$ENGINE_DIR/release002_control.py" >/dev/null 2>&1 \
  || { echo "refusing to run: control library unusable at $ENGINE_DIR/release002_control.py" >&2; exit 65; }

# ------------------------------------------------------------------ apply-only guards
PHASE=guard
if [ "$MODE" = apply ]; then
  APPROVAL=${HERMES_RELEASE_APPROVAL:-}
  if [ "$APPROVAL" != "$CANDIDATE" ]; then
    echo "refusing to mutate: HERMES_RELEASE_APPROVAL must equal the candidate SHA ($CANDIDATE)" >&2
    exit 65
  fi
  if [ -z "${HERMES_RELEASE_LAUNCH_TOKEN:-}" ] || [ -z "${HERMES_RELEASE_TOKEN_EXPECTED:-}" ] \
     || [ "${HERMES_RELEASE_LAUNCH_TOKEN}" != "${HERMES_RELEASE_TOKEN_EXPECTED}" ]; then
    echo "refusing to mutate: launch token absent or mismatched — launch via hermes-release-002 apply" >&2
    exit 65
  fi
  if [ "$(id -u)" -ne 0 ]; then
    echo "refusing to mutate: run as root from the transient supervisor unit" >&2
    exit 65
  fi
  SELF_CG=$(cat /proc/self/cgroup | head -1 | cut -d: -f3)
  SVC_CG=$(systemctl show "$SERVICE" -p ControlGroup --value)
  case "$SELF_CG" in
    "$SVC_CG"|"$SVC_CG"/*)
      echo "refusing to mutate: this process runs inside $SVC_CG; launch from the transient supervisor" >&2
      exit 65 ;;
  esac
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE=${ARCHIVE:-/home/hermes/work/HERMES-RESTORE-002/release-$STAMP}

LAUNCHER=${HERMES_RELEASE_LAUNCHER:-/usr/local/lib/hermes-release/hermes-release-002}
MANIFEST=${HERMES_RELEASE_MANIFEST:-/usr/local/lib/hermes-release/manifest.json}
POST_UNIT_SHA=""

preflight() { # $1=phase  [$2=head $3=tree] -> prints JSON, exit 0 only when every critical gate passes
  case "$1" in
    pre)  "$PY" "$ENGINE_DIR/release002_preflight.py" --phase pre \
            --expect-head "${2:-$EXPECT_HEAD}" --expect-tree "${3:-$EXPECT_TREE}" \
            --candidate "$CANDIDATE" --source-repo "$SOURCE" ;;
    post|released)
          "$PY" "$ENGINE_DIR/release002_preflight.py" --phase "$1" --candidate "$CANDIDATE" \
            --expect-head "$CANDIDATE" --expect-tree "$CAND_TREE" --source-repo "$SOURCE" ;;
    *)    echo "preflight: unknown phase '$1'" >&2; return 64 ;;
  esac
}

manifest_verify() { # $1=phase [$2=expected post unit sha256] -> prints JSON; 0 only when pinned ids hold
  local phase=$1 expected=${2:-}
  case "$phase" in
    post|released)
      # EXT-11: these phases prove the deployed unit byte-for-byte. Without a journaled checksum
      # there is nothing to compare against, so the call is refused rather than run unguarded.
      if [ -z "$expected" ]; then
        warn "manifest verification ($phase) requires the journaled unit checksum -- refusing to skip exact-byte proof"
        return 1
      fi ;;
  esac
  if [ ! -f "$MANIFEST" ]; then
    # Missing manifest is *unevaluable*, not a failed gate: 3 keeps it distinguishable from rc 2 in
    # the PLAN decision and preserves fail-closed behaviour (EXT-13C).
    warn "no release manifest at $MANIFEST — manifest verification is unevaluable"
    return 3
  fi
  if [ -n "$expected" ]; then
    ct manifest --verify --phase "$phase" --path "$MANIFEST" --repo "$SOURCE" --source "$SOURCE" \
      --engine-dir "$ENGINE_ROOT" --install "$INSTALL" --launcher "$LAUNCHER" \
      --expected-post-unit-sha256 "$expected"
  else
    ct manifest --verify --phase "$phase" --path "$MANIFEST" --repo "$SOURCE" --source "$SOURCE" \
      --engine-dir "$ENGINE_ROOT" --install "$INSTALL" --launcher "$LAUNCHER"
  fi
}

gateway_field() { # $1=field -> value from gateway_state.json, empty when unreadable
  env -u PYTHONPATH "$PY3" - "$STATE_JSON" "$1" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    print("")
PYEOF
}

receipt() { # $1=status  $2=detail-file
  local out=$ARCHIVE/RECEIPT.md
  mkdir -p "$ARCHIVE"
  {
    echo "# RESTORE-002 bounded release receipt"
    echo
    echo "- status: **$1**"
    echo "- run id: \`$RUN_ID\`"
    echo "- generated: $STAMP (UTC)"
    echo "- candidate: \`$CANDIDATE\` (tree \`$CAND_TREE\`)"
    echo "- install before: \`$EXPECT_HEAD\` (tree \`$EXPECT_TREE\`)"
    echo "- archive: \`$ARCHIVE\`"
    echo "- journal: \`$JOURNAL\`"
    echo "- unit fragment sha256: $(sha256sum "$UNIT" 2>/dev/null | cut -d' ' -f1)"
    echo "- hold drop-in sha256: $(sha256sum "$HOLD_DROPIN" 2>/dev/null | cut -d' ' -f1)"
    echo "- timeout drop-in sha256: $(sha256sum "$TIMEOUT_DROPIN" 2>/dev/null | cut -d' ' -f1)"
    echo "- effective TimeoutStopUSec: $(systemctl show "$SERVICE" -p TimeoutStopUSec --value)"
    echo "- MainPID: $(systemctl show "$SERVICE" -p MainPID --value)  NRestarts: $(systemctl show "$SERVICE" -p NRestarts --value)"
    echo "- restart attempts: $RESTART_ATTEMPTS"
    echo "- freeze engaged by this run: $FROZEN  (estop written by this run: $ESTOP_OURS)"
    echo
    echo '```'
    cat "$2" 2>/dev/null || true
    echo '```'
  } >"$out"
  log "receipt written: $out"
}

# ------------------------------------------------------------------ freeze / unfreeze
keepalive_loop() {
  while [ -e "$ARCHIVE/.freeze-alive" ]; do
    ct refresh-drain --home "$HHOME" >/dev/null 2>&1 || warn "drain keep-alive refresh failed"
    sleep 45
  done
}

estop_is_ours() {
  [ -e "$ESTOP_FILE" ] || return 1
  env -u PYTHONPATH "$PY3" - "$ESTOP_FILE" <<'PYEOF' >/dev/null 2>&1
import json, sys
try:
    reason = (json.load(open(sys.argv[1])) or {}).get("reason") or ""
except Exception:
    sys.exit(1)
sys.exit(0 if reason.startswith("release-freeze:") else 1)
PYEOF
}

freeze_engage() {
  local principal="release-freeze:$RUN_ID"
  PHASE=freeze
  if [ -e "$ESTOP_FILE" ]; then
    log "an ESTOP sentinel already exists; leaving it in place and not claiming it"
  fi
  ( cd "$INSTALL" && env -u PYTHONPATH HERMES_HOME="$HHOME" "$PY" - "$principal" <<'PYEOF'
import sys
from gateway.drain_control import write_drain_request
payload = write_drain_request(principal=sys.argv[1], suppress_notification=True)
print(payload["requested_at"])
PYEOF
  ) || { warn "could not write the drain marker"; return 1; }
  FROZEN=1
  if [ ! -e "$ESTOP_FILE" ]; then
    ( cd "$INSTALL" && env -u PYTHONPATH HERMES_HOME="$HHOME" "$PY" - "$principal" <<'PYEOF'
import sys
from agent.estop import engage
print(engage(reason=sys.argv[1]))
PYEOF
    ) && ESTOP_OURS=1
  fi
  mkdir -p "$ARCHIVE"; : >"$ARCHIVE/.freeze-alive"
  keepalive_loop & KEEPALIVE_PID=$!
  # The marker alone is not proof: wait until the LIVE process reports it honours the freeze.
  local deadline=$((SECONDS + DRAIN_ENGAGE_WAIT)) state=""
  while [ $SECONDS -lt $deadline ]; do
    state=$(gateway_field gateway_state)
    [ "$state" = "draining" ] && break
    sleep 2
  done
  if [ "$state" != "draining" ]; then
    warn "the gateway did not report 'draining' within ${DRAIN_ENGAGE_WAIT}s (state='${state:-unreadable}')"
    return 1
  fi
  log "freeze engaged: drain marker (principal $principal) + ESTOP; gateway_state=draining"
  journal freeze "drain marker + estop engaged; gateway_state=draining"
}

freeze_release() {
  local why=${1:-release complete}
  PHASE=unfreeze
  rm -f "$ARCHIVE/.freeze-alive"
  if [ -n "$KEEPALIVE_PID" ]; then kill "$KEEPALIVE_PID" 2>/dev/null || true; KEEPALIVE_PID=""; fi
  if [ "$FROZEN" -eq 1 ]; then
    ( cd "$INSTALL" && env -u PYTHONPATH HERMES_HOME="$HHOME" "$PY" - <<'PYEOF'
from gateway.drain_control import clear_drain_request
print("cleared" if clear_drain_request() else "absent")
PYEOF
    ) || warn "could not clear the drain marker"
    FROZEN=0
  fi
  if [ "$ESTOP_OURS" -eq 1 ]; then
    ( cd "$INSTALL" && env -u PYTHONPATH HERMES_HOME="$HHOME" "$PY" - <<'PYEOF'
from agent.estop import disengage
print("cleared" if disengage() else "absent")
PYEOF
    ) || warn "could not clear the ESTOP sentinel this run wrote"
    ESTOP_OURS=0
  fi
  local deadline=$((SECONDS + UNFREEZE_WAIT)) state=""
  while [ $SECONDS -lt $deadline ]; do
    state=$(gateway_field gateway_state)
    [ "$state" != "draining" ] && break
    sleep 2
  done
  log "admission restored ($why): gateway_state=${state:-unreadable} estop=$([ -e "$ESTOP_FILE" ] && echo present || echo absent)"
  journal unfreeze "admission restored: $why" 2>/dev/null || true
}

abort_before_mutation() { # $1=phase $2=reason -> NO_GO with zero changes and zero restarts (EXT-08)
  local phase=$1 reason=$2 decision action
  if [ "$MUTATED" -ne 0 ]; then
    # Defensive: a pre-mutation abort is only valid before the first mutation. If that invariant is
    # ever broken, undoing the mutation is the correct response, not pretending nothing happened.
    warn "abort_before_mutation reached with MUTATED=1; deferring to rollback"
    rollback "$reason"
  fi
  decision=$(ct failure --phase "$phase" --restart-attempts "$RESTART_ATTEMPTS" 2>/dev/null || echo '{}')
  action=$(printf '%s' "$decision" | env -u PYTHONPATH "$PY3" -c \
    'import json,sys;print(json.load(sys.stdin).get("action","needs_human"))' 2>/dev/null || echo needs_human)
  PHASE=aborted
  if [ -n "$ARCHIVE" ]; then
    mkdir -p "$ARCHIVE"; printf '%s\n' "$decision" >"$ARCHIVE/no-go-decision.json" 2>/dev/null || true
  fi
  log "NO_GO before any mutation at phase '$phase': $reason (restart=no, rollback=no)"
  if [ "$action" != "release_freeze" ]; then
    warn "failure contract for '$phase' says '$action', not release_freeze: leaving state for a human"
    journal aborted "NO_GO at $phase needs a human: $reason" 2>/dev/null || true
    exit 2
  fi
  # Release only the freeze this run created: freeze_release clears the drain marker only when
  # FROZEN=1 (ours) and the ESTOP sentinel only when ESTOP_OURS=1 (ours).
  if [ "$FROZEN" -eq 1 ] || [ "$ESTOP_OURS" -eq 1 ]; then
    freeze_release "NO_GO before mutation at $phase: $reason"
  fi
  journal aborted "NO_GO before mutation at $phase: $reason" --result ABORTED_SAFE 2>/dev/null || true
  if [ -n "$ARCHIVE" ]; then
    receipt "NO_GO_BEFORE_MUTATION" "$ARCHIVE/no-go-decision.json" 2>/dev/null || true
  fi
  exit 2
}

rollback() { # $1=reason
  local reason=$1
  PHASE=rollback
  warn "rolling back: $reason"
  journal rollback "rollback started: $reason"
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
  RESTART_ATTEMPTS=$((RESTART_ATTEMPTS + 1))
  timeout 340 systemctl restart "$SERVICE" || warn "rollback restart returned non-zero"
  sleep 20
  git -C "$INSTALL" rev-parse HEAD >"$ARCHIVE/rollback.txt" 2>&1 || true
  systemctl show "$SERVICE" -p ActiveState -p MainPID -p NRestarts >>"$ARCHIVE/rollback.txt" 2>&1 || true
  freeze_release "rollback: $reason"
  journal report "rollback complete" --result ROLLED_BACK
  receipt "ROLLED_BACK" "$ARCHIVE/rollback.txt"
  exit 1
}

on_exit() {
  local rc=$?
  if [ -d "${ARCHIVE:-}" ]; then rm -f "$ARCHIVE/.freeze-alive"; fi
  if [ -n "$KEEPALIVE_PID" ]; then kill "$KEEPALIVE_PID" 2>/dev/null || true; fi
  case "$PHASE" in
    rollback|done|recover|aborted|interrupted) return 0 ;;
  esac
  if [ "$rc" -ne 0 ]; then
    if [ "$MUTATED" -ne 0 ]; then
      rollback "unhandled failure in phase '$PHASE' (rc=$rc)"
    elif [ "$FROZEN" -eq 1 ] || [ "$ESTOP_OURS" -eq 1 ]; then
      freeze_release "aborted in phase '$PHASE' (rc=$rc) before any mutation"
      journal aborted "aborted before mutation: $PHASE" --result ABORTED_SAFE
    fi
  fi
}
trap on_exit EXIT
trap 'PHASE=interrupted; warn "signal received in phase $PHASE"; exit 70' INT TERM

# ------------------------------------------------------------------ mode: recover
if [ "$MODE" = recover ]; then
  PHASE=recover
  DEPLOYED_HEAD=$(git -C "$INSTALL" rev-parse HEAD)
  set +e
  PLAN=$(ct recover-plan --journal "$JOURNAL" --repo "$INSTALL" --candidate "$CANDIDATE" --deployed "$EXPECT_HEAD")
  RC=$?
  set -e
  ACTION=$(printf '%s' "$PLAN" | env -u PYTHONPATH "$PY3" -c 'import json,sys;print(json.load(sys.stdin).get("action","needs_human"))' 2>/dev/null || echo needs_human)
  REASON=$(printf '%s' "$PLAN" | env -u PYTHONPATH "$PY3" -c 'import json,sys;print(json.load(sys.stdin).get("reason",""))' 2>/dev/null || echo "")
  log "recovery decision: $ACTION ($REASON) rc=$RC install_head=$DEPLOYED_HEAD"
  journal recover "decision=$ACTION reason=$REASON"
  mkdir -p "$ARCHIVE"; printf '%s\n' "$PLAN" >"$ARCHIVE/recover-plan.json"
  case "$ACTION" in
    none) log "nothing to recover"; PHASE=recover; exit 0 ;;
    unfreeze) FROZEN=1; ESTOP_OURS=$(estop_is_ours && echo 1 || echo 0); freeze_release "recovery: $REASON"; exit 0 ;;
    rollback) MUTATED=1; rollback "recovery: $REASON" ;;
    complete_verification)
      "$PY" "$ENGINE_DIR/restore002_session_inventory.py" --candidate "$CANDIDATE" --source-repo "$SOURCE" \
        --json "$ARCHIVE/sessions-after.json" >/dev/null 2>&1 || true
      FROZEN=1; ESTOP_OURS=$(estop_is_ours && echo 1 || echo 0)
      freeze_release "recovery: verification completed by operator"
      journal report "recovered by completing verification" --result COMPLETE
      receipt "RELEASED_VERIFIED" "$ARCHIVE/recover-plan.json"
      exit 0 ;;
    *) warn "NEEDS_HUMAN: $REASON — the freeze is left in place deliberately"; PHASE=recover; exit 2 ;;
  esac
fi

# ---------------------------------------------------------------- mode: verify only
if [ "$MODE" = verify ]; then
  PHASE=verify-only
  phase=${VERIFY_PHASE:-released}
  log "post-release verification against $CANDIDATE / $CAND_TREE (phase=$phase, no mutation)"
  set +e
  preflight "$phase"
  RC=$?
  set -e
  log "preflight verdict rc=$RC"
  [ "$RC" -eq 0 ] || exit 2
  # EXT-11: the regenerated unit's exact checksum comes only from a validated journal receipt.
  # A missing journal, a missing unit receipt or a malformed digest is a NO_GO -- never an empty
  # string that would leave the manifest check with nothing to compare.
  mkdir -p "$ARCHIVE" 2>/dev/null || true
  # Bind the checksum to this exact attempt: when the operator names the attempt id it must match.
  RID_ARGS=()
  if [ -n "${HERMES_RELEASE_RUN_ID:-}" ]; then RID_ARGS=(--run-id "$HERMES_RELEASE_RUN_ID"); fi
  set +e
  POST_UNIT_SHA=$(ct unit-receipt --journal "$JOURNAL" --candidate "$CANDIDATE" \
                    --unit-path "$UNIT" "${RID_ARGS[@]}" --print sha256 2>"$ARCHIVE/unit-receipt.json")
  URC=$?
  set -e
  if [ "$URC" -ne 0 ] || [ -z "$POST_UNIT_SHA" ]; then
    warn "no valid regenerated-unit receipt in $JOURNAL (rc=$URC) -- refusing to verify"
    [ -f "$ARCHIVE/unit-receipt.json" ] && sed -n '1,40p' "$ARCHIVE/unit-receipt.json"
    exit 2
  fi
  log "journaled unit receipt: sha256=$POST_UNIT_SHA"
  set +e
  manifest_verify "$phase" "$POST_UNIT_SHA" >"${ARCHIVE:-/tmp}/verify-manifest.json" 2>&1
  MRC=$?
  set -e
  tail -40 "${ARCHIVE:-/tmp}/verify-manifest.json" 2>/dev/null || true
  log "manifest verification ($phase) rc=$MRC (post-unit sha256 $POST_UNIT_SHA)"
  [ "$MRC" -eq 0 ] || exit 2
  HOLD=$(systemctl show "$SERVICE" -p Environment --value | grep -c 'HERMES_RECOVERY_HOLD=1' || true)
  log "hold env entries: $HOLD"
  [ "$HOLD" -ge 1 ] || exit 2
  exit 0
fi

# ---------------------------------------------------------------- mode: plan / apply
mkdir -p "$ARCHIVE/unit"
journal start "mode=$MODE candidate=$CANDIDATE" 2>/dev/null || true

PHASE=identity
log "verifying identities: install $EXPECT_HEAD/$EXPECT_TREE, candidate $CANDIDATE/$CAND_TREE"
HEAD_NOW=$(git -C "$INSTALL" rev-parse HEAD)
TREE_NOW=$(git -C "$INSTALL" rev-parse HEAD^{tree})
if [ "$HEAD_NOW" != "$EXPECT_HEAD" ] || [ "$TREE_NOW" != "$EXPECT_TREE" ]; then
  log "NO_GO: install identity is $HEAD_NOW/$TREE_NOW, expected $EXPECT_HEAD/$EXPECT_TREE"
  journal aborted "identity drift before start" --result ABORTED_SAFE 2>/dev/null || true
  exit 2
fi
if [ -n "$(git -C "$INSTALL" status --porcelain)" ]; then
  log "NO_GO: install worktree is dirty"
  journal aborted "dirty worktree before start" --result ABORTED_SAFE 2>/dev/null || true
  exit 2
fi

PHASE=preflight
log "preflight against $EXPECT_HEAD / $EXPECT_TREE"
set +e
preflight pre "$EXPECT_HEAD" "$EXPECT_TREE" >"$ARCHIVE/preflight-before.json" 2>"$ARCHIVE/preflight-before.err"
PF_RC=$?
set -e
if [ "$MODE" = plan ]; then
  # A dry-run runs while the gateway is still serving Telegram and cron work, so the quiescence
  # gates legitimately fail here; they are re-evaluated after the freeze. Print the runbook either
  # way and keep the exit code honest (2 = the release would refuse in the current state).
  PHASE=dry-run
  if [ "$PF_RC" -ne 0 ]; then
    log "preflight verdict NO_GO (rc=$PF_RC) in the current state — nothing was changed"
  else
    log "preflight verdict GO"
  fi
  grep -E '"(gate|failed|verdict)"' "$ARCHIVE/preflight-before.json" 2>/dev/null \
    || cat "$ARCHIVE/preflight-before.err" 2>/dev/null || true
  log "DRY RUN — the following would execute, in this order:"
  cat <<EOF
  1. archive $UNIT + $DROPIN_DIR/*.conf -> $ARCHIVE/unit/ (+ sha256 manifest)
  2. engage the admission freeze: drain marker (principal release-freeze:$RUN_ID) + ESTOP,
     then wait until the live gateway reports gateway_state=draining
  3. quiesce: three consecutive samples, 20 s apart, of
     (active_agents == 0) AND (live admissions for the current runtime epoch == 0)
  4. revalidate install/candidate/unit/drop-in identities immediately before the first mutation
  5. git -C $INSTALL merge --ff-only $CANDIDATE   (expects tree $CAND_TREE)
  6. $PY -m hermes_cli.main gateway install --system   (regenerates the unit: TimeoutStopSec=300s)
  7. verify /etc unit TimeoutStopSec == 300s and systemctl show TimeoutStopUSec == 5min
  8. one timed: systemctl restart $SERVICE   (must stop without SIGKILL, restart active)
  9. post-verify: preflight vs $CANDIDATE, hold still active, session fingerprint unchanged,
     no SIGKILL in the window
 10. release the freeze, confirm gateway_state is no longer draining and ESTOP is absent
 11. receipt -> $ARCHIVE/RECEIPT.md   (status RELEASED_VERIFIED, or ROLLED_BACK)
  Rollback on any failure after step 5: restore archived unit + drop-ins, daemon-reload,
  git reset --hard $EXPECT_HEAD, one restart, then release the freeze.
EOF
  set +e
  manifest_verify pre >"$ARCHIVE/preflight-manifest.json" 2>"$ARCHIVE/preflight-manifest.err"
  MF_RC=$?
  set -e
  log "manifest verification (pre) rc=$MF_RC (plan only; apply refuses on any failure)"
  ct admission --home "$HHOME" || true
  # EXT-13C: GO requires *both* the operational preflight and the structural manifest verification.
  # A passing preflight must never launder a failed (rc=2) or unevaluable (rc=3) manifest into GO.
  if [ "$PF_RC" -eq 3 ] || [ "$MF_RC" -eq 3 ]; then
    log "DRY RUN RESULT: UNEVALUABLE — a required gate could not be evaluated (preflight rc=$PF_RC, manifest rc=$MF_RC)"
    exit 3
  fi
  if [ "$PF_RC" -ne 0 ] || [ "$MF_RC" -ne 0 ]; then
    log "DRY RUN RESULT: NO_GO — preflight rc=$PF_RC, manifest rc=$MF_RC (both must pass; nothing was changed)"
    exit 2
  fi
  log "DRY RUN RESULT: GO — every gate passes and no mutation was performed"
  exit 0
fi

if [ "$PF_RC" -ne 0 ]; then
  log "preflight verdict NO_GO (rc=$PF_RC) — nothing was changed"
  grep -E '"(gate|failed|verdict)"' "$ARCHIVE/preflight-before.json" || cat "$ARCHIVE/preflight-before.err"
  journal aborted "preflight NO_GO rc=$PF_RC" --result ABORTED_SAFE 2>/dev/null || true
  exit 2
fi
log "preflight verdict GO"

PHASE=manifest-pre
set +e
manifest_verify pre >"$ARCHIVE/manifest-pre.json" 2>"$ARCHIVE/manifest-pre.err"
MF_RC=$?
set -e
if [ "$MF_RC" -ne 0 ]; then
  grep -E '"(gate|failed|ok|phase)"' "$ARCHIVE/manifest-pre.json" 2>/dev/null \
    || cat "$ARCHIVE/manifest-pre.err" 2>/dev/null || true
  abort_before_mutation manifest "the staged manifest does not describe this installation (pre-release identities)"
fi
log "manifest verified against the pre-release identities"

OTHER_ENGINES=$(pgrep -f 'release002_release\.sh --apply' | grep -v -e "^$$" -e "^$PPID$" || true)
if [ -n "$OTHER_ENGINES" ]; then
  echo "refusing to mutate: another release engine is already running (pids: $OTHER_ENGINES)" >&2
  exit 65
fi
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "refusing to mutate: $LOCK is held by another release" >&2
  exit 65
fi

# ------------------------------------------------------------ phase 1: archive (reversible state)
PHASE=archive
install -m 0644 -o root -g root "$UNIT" "$ARCHIVE/unit/hermes-gateway.service"
for f in "$DROPIN_DIR"/*.conf; do [ -e "$f" ] && install -m 0644 -o root -g root "$f" "$ARCHIVE/unit/$(basename "$f")"; done
sha256sum "$UNIT" "$DROPIN_DIR"/*.conf >"$ARCHIVE/unit/SHA256SUMS" 2>/dev/null || true
git -C "$INSTALL" rev-parse HEAD >"$ARCHIVE/install-before.txt"
"$PY" "$ENGINE_DIR/restore002_session_inventory.py" --candidate "$CANDIDATE" --source-repo "$SOURCE" \
  --json "$ARCHIVE/sessions-before.json" >/dev/null
journal archive "unit, drop-ins, identity and session inventory archived"
log "archived unit, drop-ins, install identity and session inventory"

# ------------------------------------------------------------ phase 2: freeze admission
if ! freeze_engage; then
  freeze_release "freeze could not be established"
  journal aborted "freeze could not be established" --result ABORTED_SAFE 2>/dev/null || true
  exit 2
fi

# ------------------------------------------------------- phase 3: quiesce (freeze honoured)
PHASE=quiesce
if [ "$QUIESCE_WAIT" -gt "$MAX_QUIESCE" ]; then
  log "refusing: QUIESCE_WAIT=$QUIESCE_WAIT exceeds the envelope's ${MAX_QUIESCE}s quiesce budget"
  freeze_release "configuration refused before quiescence"
  exit 65
fi
deadline=$((SECONDS + QUIESCE_WAIT))
quiet=0
while [ $SECONDS -lt $deadline ]; do
  if ct admission --home "$HHOME" >"$ARCHIVE/admission-$quiet.json" 2>/dev/null; then
    quiet=$((quiet + 1))
  else
    quiet=0
  fi
  log "admission sample $quiet/3: $(ct admission --home "$HHOME" 2>/dev/null | tr -d '\n' | cut -c1-160 || true)"
  if [ "$quiet" -ge 3 ]; then break; fi
  sleep 20
done
journal quiesce "quiet samples=$quiet"
if [ "$quiet" -lt 3 ]; then
  log "no quiescent window within ${QUIESCE_WAIT}s — aborting before any mutation"
  freeze_release "not quiet within ${QUIESCE_WAIT}s"
  journal aborted "not quiet within ${QUIESCE_WAIT}s" --result ABORTED_SAFE 2>/dev/null || true
  receipt "ABORTED_NOT_QUIET" "$ARCHIVE/preflight-before.json"
  exit 3
fi

# ------------------------------------- phase 4: revalidate identities, then first mutation
PHASE=revalidate
HEAD_NOW=$(git -C "$INSTALL" rev-parse HEAD)
TREE_NOW=$(git -C "$INSTALL" rev-parse HEAD^{tree})
DIRTY=$(git -C "$INSTALL" status --porcelain)
UNIT_SHA_NOW=$(sha256sum "$UNIT" | cut -d' ' -f1)
# EXT-08: every failure in this block happens with zero deployed mutations, so it must leave the
# installation, the unit and the service exactly as they were and release only this run's freeze.
[ "$HEAD_NOW" = "$EXPECT_HEAD" ] || abort_before_mutation revalidate "install HEAD drifted to $HEAD_NOW"
[ "$TREE_NOW" = "$EXPECT_TREE" ] || abort_before_mutation revalidate "install tree drifted to $TREE_NOW"
[ -z "$DIRTY" ] || abort_before_mutation revalidate "install worktree became dirty"
[ "$UNIT_SHA_NOW" = "$(cut -d' ' -f1 "$ARCHIVE/unit/SHA256SUMS" | head -1)" ] \
  || abort_before_mutation revalidate "unit file changed between archive and mutation"
set +e
manifest_verify pre >"$ARCHIVE/manifest-revalidate.json" 2>&1
MF2=$?
set -e
[ "$MF2" -eq 0 ] || abort_before_mutation revalidate "pre-release manifest identities no longer hold"
ct admission --home "$HHOME" >"$ARCHIVE/admission-pre-mutation.json" 2>/dev/null \
  || abort_before_mutation revalidate "admission is not frozen/quiescent"
journal revalidate "identities and admission revalidated immediately before mutation"

# ------------------------------------------------------------------- phase 5: deploy
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
journal deploy "deployed $HEAD tree $TREE" --head "$HEAD"
log "deployed HEAD=$HEAD tree=$TREE"

# --------------------------------------------------------------- phase 6: regenerate unit
PHASE=unit
log "regenerating the unit from the deployed code (no restart)"
( cd "$INSTALL" && HERMES_HOME="$HHOME" "$PY" -m hermes_cli.main gateway install --system ) \
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
POST_UNIT_SHA=$(sha256sum "$UNIT" | cut -d' ' -f1)
[ -n "$POST_UNIT_SHA" ] || rollback "cannot checksum the regenerated unit"
journal unit "regenerated unit sha256=$POST_UNIT_SHA effective=$EFFECTIVE" \
  --sha256 "$POST_UNIT_SHA" --unit-path "$UNIT"

# ------------------------------------------------- phase 7: one controlled stop/start
PHASE=restart
OLD_PID=$(systemctl show "$SERVICE" -p MainPID --value)
RESTART_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START_MS=$(date +%s%3N)
RESTART_ATTEMPTS=$((RESTART_ATTEMPTS + 1))
set +e
timeout 340 systemctl restart "$SERVICE" >"$ARCHIVE/restart.txt" 2>&1
RESTART_RC=$?
set -e
STOP_MS=$(( $(date +%s%3N) - START_MS ))
log "systemctl restart rc=$RESTART_RC after ${STOP_MS}ms"
journal restart "restart rc=$RESTART_RC elapsed_ms=$STOP_MS" --restart-attempts "$RESTART_ATTEMPTS"
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

# ------------------------------------------------------------------- phase 8: forensics
PHASE=verify-shutdown
journalctl -u "$SERVICE" --since "$RESTART_AT" --no-pager -o short-iso >"$ARCHIVE/journal-after-restart.txt" 2>&1 || true
KILLS=$(grep -c 'with signal KILL' "$ARCHIVE/journal-after-restart.txt" || true)
DRAIN=$(grep -E 'drain|Drain' "$ARCHIVE/journal-after-restart.txt" | tail -5 || true)
STOPLINE=$(grep -E 'Stopped |Stopping ' "$ARCHIVE/journal-after-restart.txt" | tail -4 || true)
log "SIGKILL lines after restart: ${KILLS:-0}"
[ "${KILLS:-0}" -eq 0 ] || rollback "systemd SIGKILLed the gateway during the controlled stop"

# --------------------------------------------------------------------- phase 9: verification
PHASE=verify-post
set +e
preflight post >"$ARCHIVE/preflight-after.json" 2>"$ARCHIVE/preflight-after.err"
PF2=$?
set -e
if [ "$PF2" -ne 0 ]; then
  grep -E '"(gate|failed|verdict)"' "$ARCHIVE/preflight-after.json" 2>/dev/null \
    || cat "$ARCHIVE/preflight-after.err" 2>/dev/null || true
  rollback "post-release preflight NO_GO (phase=post)"
fi
set +e
manifest_verify post "$POST_UNIT_SHA" >"$ARCHIVE/manifest-post.json" 2>"$ARCHIVE/manifest-post.err"
MF3=$?
set -e
if [ "$MF3" -ne 0 ]; then
  grep -E '"(gate|failed|ok|phase)"' "$ARCHIVE/manifest-post.json" 2>/dev/null \
    || cat "$ARCHIVE/manifest-post.err" 2>/dev/null || true
  rollback "post-release manifest verification NO_GO (post_release identities)"
fi
log "post-release verification: preflight GO (phase=post, inside the freeze) and manifest post_release identities hold"
HOLD_ENV=$(tr '\0' '\n' <"/proc/$NEW_PID/environ" | grep -c '^HERMES_RECOVERY_HOLD=1$' || true)
[ "$HOLD_ENV" -eq 1 ] || rollback "recovery hold is not set in the new process environment"

"$PY" "$ENGINE_DIR/restore002_session_inventory.py" --candidate "$CANDIDATE" --source-repo "$SOURCE" \
  --json "$ARCHIVE/sessions-after.json" >/dev/null
python_compare=$("$PY" - "$ARCHIVE/sessions-before.json" "$ARCHIVE/sessions-after.json" <<'PYEOF'
import json,sys
a,b=(json.load(open(p)) for p in sys.argv[1:3])
keys=("open_sessions_fingerprint_sha256","empty_open")
same=all(a.get(k)==b.get(k) for k in keys)
print("same" if same else f"CHANGED {[(k,a.get(k),b.get(k)) for k in keys if a.get(k)!=b.get(k)]}")
PYEOF
)
log "session database: $python_compare"
[ "$python_compare" = "same" ] || rollback "session inventory changed across the release ($python_compare)"
journal verify "preflight GO (phase=post), manifest pre+post identities verified, hold set, sessions unchanged, sigkill=0"

# ---------------------------------------------------------- phase 10: restore admission
freeze_release "release verified"

# ------------------------------------------------------------------------------ receipt
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
journal report "release verified" --result COMPLETE
receipt "RELEASED_VERIFIED" "$ARCHIVE/verification.txt"
log "release complete: HEAD=$HEAD TimeoutStopUSec=$EFFECTIVE SIGKILL=${KILLS:-0}"
