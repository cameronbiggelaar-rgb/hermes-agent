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
DRAIN_MARKER=$HHOME/.drain_request.json
LOCK=/var/lib/hermes-release/restore-002.lock
# EXT-17A: the isolated qualification harness runs the real engine more than once and must not
# contend on (or even open) the production lock. Same guarded shape as the unit hook: the override
# exists only with HERMES_RELEASE_QUALIFY=1, so no environment can quietly move a real release's lock.
if [ -n "${HERMES_RELEASE_LOCK:-}" ]; then
  [ "${HERMES_RELEASE_QUALIFY:-}" = "1" ] \
    || { echo "refusing HERMES_RELEASE_LOCK without HERMES_RELEASE_QUALIFY=1" >&2; exit 64; }
  LOCK=$HERMES_RELEASE_LOCK
fi
JOURNAL=${HERMES_RELEASE_JOURNAL:-/var/lib/hermes-release/restore-002/journal.json}
ENGINE_DIR=$(cd "$(dirname "$0")" && pwd)
# The manifest's engine keys are "harness/<file>" relative to the staged engine root; $ENGINE_DIR is
# this script's own directory, so --engine-dir must name the parent or every staged_file_bytes gate
# would look for harness/harness/<file> (EXT-13B).
ENGINE_ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=$INSTALL/venv/bin/python
PY3=/usr/bin/python3.13
# EXT-17: the installation this candidate is reconciled against. Production is 3652a370 since the
# 2026-11-09 upgrade and is not reset by this release; the manifest pins both values, and these
# defaults only have to agree with it.
EXPECT_HEAD=${EXPECT_HEAD:-3652a3702dae8d2917b91b6949da580efd3ef496}
EXPECT_TREE=${EXPECT_TREE:-3bfc1c842bf76e8a95356f4f0f3a91e75767992c}
MAX_QUIESCE=${MAX_QUIESCE:-180}
QUIESCE_WAIT=${QUIESCE_WAIT:-180}
STARTUP_WAIT=${STARTUP_WAIT:-240}
DRAIN_ENGAGE_WAIT=${DRAIN_ENGAGE_WAIT:-45}
UNFREEZE_WAIT=${UNFREEZE_WAIT:-60}
MODE=plan
CANDIDATE=""
ARCHIVE=""
MUTATED=0
# MUTATED is the fail-closed intent guard (set immediately before the merge, so every failure from
# that point is treated as post-mutation). JOURNAL_MUTATED is the journal's own record of a change
# that was proven to have happened, and must never be set speculatively (D-10).
JOURNAL_MUTATED=0
PHASE=init
RUN_ID=${HERMES_RELEASE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
# EXT-16: the freeze principal is this attempt's identity, and it is checked exactly — a marker or
# ESTOP belonging to any other attempt (including an earlier one that leaked) is never "ours".
PRINCIPAL="release-freeze:$RUN_ID"
# Epoch second the freeze was engaged; set by freeze_engage and used to attribute scheduled work.
FREEZE_SINCE=""
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
  #
  # D-10: mutation is journal state, not shell state. Records write `--mutated` once the install is
  # proven changed, and the flag is re-derived from the journal on every write so a single lost
  # record cannot leave a post-mutation attempt looking untouched. It is deliberately NOT taken
  # from $MUTATED: that variable is the fail-closed *intent* guard set just before the merge, and
  # a refused merge must not be journalled as a mutation.
  local mutated_flag=""
  if [ "${JOURNAL_MUTATED:-0}" -ne 0 ] || grep -q '"mutated": true' "$JOURNAL" 2>/dev/null; then
    mutated_flag="--mutated"
  fi
  env -u PYTHONPATH "$PY3" "$ENGINE_DIR/release002_control.py" journal --path "$JOURNAL" \
    --phase "$phase" --note "$note" --candidate "$CANDIDATE" --run-id "$RUN_ID" $mutated_flag "$@" >/dev/null 2>&1 \
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
# The archive is where every preflight sample, manifest and receipt lands; a fresh run id must not
# make the first redirect fail, which would silently blank the quiescence samples.
mkdir -p "$ARCHIVE"

LAUNCHER=${HERMES_RELEASE_LAUNCHER:-/usr/local/lib/hermes-release/hermes-release-002}
MANIFEST=${HERMES_RELEASE_MANIFEST:-/usr/local/lib/hermes-release/manifest.json}
POST_UNIT_SHA=""

preflight() { # $1=phase  [$2=head $3=tree] -> prints JSON, exit 0 only when every critical gate passes
  case "$1" in
    pre)  "$PY" "$ENGINE_DIR/release002_preflight.py" --phase pre \
            --expect-head "${2:-$EXPECT_HEAD}" --expect-tree "${3:-$EXPECT_TREE}" \
            --candidate "$CANDIDATE" --source-repo "$SOURCE" ;;
    infreeze|revalidate)
          # EXT-16: the only phases allowed to assert quiescence. Exact principal, so a marker or
          # sentinel owned by another attempt can never be read as this run's freeze; the freeze
          # epoch, so scheduled work claimed after the freeze is attributable.
          local since_arg=()
          if [ -n "${FREEZE_SINCE:-}" ]; then since_arg=(--freeze-since "$FREEZE_SINCE"); else
            warn "in-freeze preflight without a freeze epoch: scheduled-claim attribution cannot be evaluated"
          fi
          "$PY" "$ENGINE_DIR/release002_preflight.py" --phase "$1" --candidate "$CANDIDATE" \
            --expect-head "$EXPECT_HEAD" --expect-tree "$EXPECT_TREE" --source-repo "$SOURCE" \
            --principal "$PRINCIPAL" "${since_arg[@]}" ;;
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
    # D-10: the receipt must state the journal's own mutation verdict, so the final receipt, the
    # phase records and the journal identity can be compared without re-deriving anything.
    echo "- journal state: $(ct journal-state --path "$JOURNAL" 2>/dev/null | env -u PYTHONPATH "$PY3" -c 'import json,sys
try:
    s = json.load(sys.stdin)["state"]
except Exception:
    print("unreadable")
else:
    print("mutated=%s since=%s phase=%s result=%s run_id=%s" % (
        str(s["mutated"]).lower(), s["since_phase"], s["phase"], s["result"], s["run_id"]))' 2>/dev/null || echo unreadable)"
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
  env -u PYTHONPATH "$PY3" - "$ESTOP_FILE" "$PRINCIPAL" <<'PYEOF' >/dev/null 2>&1
import json, sys
try:
    reason = (json.load(open(sys.argv[1])) or {}).get("reason") or ""
except Exception:
    sys.exit(1)
# Exact identity: a foreign release attempt's ESTOP is not ours to clear, even though it carries the
# release-freeze: prefix.
sys.exit(0 if reason == sys.argv[2] else 1)
PYEOF
}

drain_is_ours() {
  [ -e "$DRAIN_MARKER" ] || return 1
  env -u PYTHONPATH "$PY3" - "$DRAIN_MARKER" "$PRINCIPAL" <<'PYEOF' >/dev/null 2>&1
import json, sys
try:
    body = json.load(open(sys.argv[1])) or {}
except Exception:
    sys.exit(1)
principal = str(body.get("principal") or body.get("requested_by") or "")
sys.exit(0 if principal == sys.argv[2] else 1)
PYEOF
}

freeze_engage() {
  local principal="$PRINCIPAL"
  PHASE=freeze
  # EXT-16: a freeze we did not write is never adopted and never overwritten. Adopting a foreign or
  # stale marker would attribute another attempt's quiescence to this one; clearing it automatically
  # could release a freeze someone else is relying on. Both are NEEDS_HUMAN.
  if [ -e "$DRAIN_MARKER" ] && ! drain_is_ours; then
    warn "NEEDS_HUMAN: a freeze marker already exists and is not owned by $principal; refusing to overwrite it"
    journal aborted "foreign or stale drain marker; needs a human" 2>/dev/null || true
    exit 2
  fi
  if [ -e "$ESTOP_FILE" ] && ! estop_is_ours; then
    warn "NEEDS_HUMAN: an ESTOP sentinel exists and is not owned by $principal; refusing to adopt or clear it"
    journal aborted "foreign ESTOP sentinel; needs a human" 2>/dev/null || true
    exit 2
  fi
  set +e
  FREEZE_SINCE=$(
    cd "$INSTALL" && env -u PYTHONPATH HERMES_HOME="$HHOME" "$PY" - "$principal" <<'PYEOF'
import json, os, sys
from datetime import datetime
from gateway.drain_control import write_drain_request, drain_request_path
payload = write_drain_request(principal=sys.argv[1], suppress_notification=True)
# The keep-alive re-stamps ``requested_at`` every 45s, so that field cannot identify when the freeze
# began. Record the engagement epoch once, beside it, and never move it: a restart or a resumed
# attempt that finds its own marker keeps the original epoch instead of silently re-dating the
# freeze and making "no claims after the freeze" meaningless.
path = drain_request_path()
with open(path, encoding="utf-8") as fh:
    body = json.load(fh)
if not body.get("freeze_since"):
    body["freeze_since"] = body.get("requested_at") or payload["requested_at"]
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
print(int(datetime.fromisoformat(str(body["freeze_since"])).timestamp()))
PYEOF
  ) || { warn "could not write the drain marker"; set -e; return 1; }
  set -e
  case "$FREEZE_SINCE" in
    ''|*[!0-9]*) warn "drain marker written but its timestamp is unusable ('$FREEZE_SINCE'); refusing to guess when the freeze began"
                 FREEZE_SINCE=""; return 1 ;;
  esac
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

# ================= EXT-17A: interruption settlement library (BEGIN) =================
# Everything between these two markers is the settlement surface: freeze release, the pre-mutation
# abort, the bounded rollback, durable NEEDS_HUMAN evidence, verified terminal journal settlement
# and the EXIT/INT/TERM traps. It is deliberately self-contained and side-effect free apart from the
# trap registrations at the end, because tests/harness/test_release002_ext17a_settlement.py extracts
# exactly this region and drives it with fault-injecting stubs. That harness therefore executes
# *these* functions, not a copy of them: move or restructure the region and the extraction fails
# loudly instead of silently testing nothing.
#
# Settlement rules (each is covered by a fault-injection test):
#   * no settlement step is allowed to abort the remaining steps -- every step runs through
#     ``settle_run``, which records the failure and returns 0;
#   * nothing is reported as released, restored or settled until it has been verified;
#   * every unverifiable outcome becomes durable NEEDS_HUMAN evidence beside the journal, keyed to
#     this attempt's run id;
#   * a signal during settlement is ignored -- settlement, not the signal, decides the outcome.

SETTLE_FAILURES=""
SETTLING=0
SIGNAL_SEEN=""

settle_fail() { SETTLE_FAILURES="${SETTLE_FAILURES}${SETTLE_FAILURES:+; }$1"; warn "settlement step failed: $1"; }

settle_run() { # settle_run <description> <command...> -- records a failure, never aborts the settlement
  local what=$1
  shift
  if "$@" >>"${ARCHIVE:-/dev/null}/settlement.log" 2>&1; then
    return 0
  fi
  settle_fail "$what"
  return 0
}

needs_human() { # needs_human <reason> -- durable, attempt-scoped evidence; never fails the shell
  local reason=$1 dir file head tree active held
  dir=$(dirname "$JOURNAL")
  head=$(git -C "$INSTALL" rev-parse HEAD 2>/dev/null || echo unknown)
  tree=$(git -C "$INSTALL" rev-parse HEAD^{tree} 2>/dev/null || echo unknown)
  active=$(systemctl show "$SERVICE" -p ActiveState --value 2>/dev/null || echo unknown)
  held=false
  if [ "$FROZEN" -eq 1 ] || [ "$ESTOP_OURS" -eq 1 ]; then held=true; fi
  mkdir -p "$dir" 2>/dev/null || true
  file=$dir/NEEDS_HUMAN-$RUN_ID.json
  {
    printf '{\n'
    printf '  "release": "RESTORE-002",\n'
    printf '  "disposition": "RESTORE-002_NEEDS_HUMAN",\n'
    printf '  "run_id": "%s",\n' "$RUN_ID"
    printf '  "principal": "%s",\n' "$PRINCIPAL"
    printf '  "candidate": "%s",\n' "$CANDIDATE"
    printf '  "phase": "%s",\n' "$PHASE"
    printf '  "reason": "%s",\n' "${reason//\"/\'}"
    printf '  "mutated_intent": %s,\n' "$([ "$MUTATED" -ne 0 ] && echo true || echo false)"
    printf '  "journal_mutated": %s,\n' "$([ "${JOURNAL_MUTATED:-0}" -ne 0 ] && echo true || echo false)"
    printf '  "freeze_held_by_this_run": %s,\n' "$held"
    printf '  "drain_marker_present": %s,\n' "$([ -e "$DRAIN_MARKER" ] && echo true || echo false)"
    printf '  "estop_present": %s,\n' "$([ -e "$ESTOP_FILE" ] && echo true || echo false)"
    printf '  "install_head": "%s",\n' "$head"
    printf '  "install_tree": "%s",\n' "$tree"
    printf '  "service_active_state": "%s",\n' "$active"
    printf '  "restart_attempts": %s,\n' "$RESTART_ATTEMPTS"
    printf '  "settlement_failures": "%s",\n' "${SETTLE_FAILURES//\"/\'}"
    printf '  "signal_seen": "%s",\n' "$SIGNAL_SEEN"
    printf '  "at": "%s"\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '}\n'
  } >"$file.tmp" 2>/dev/null && mv -f "$file.tmp" "$file" 2>/dev/null \
    || warn "could not write NEEDS_HUMAN evidence at $file"
  if [ -n "${ARCHIVE:-}" ]; then
    mkdir -p "$ARCHIVE" 2>/dev/null || true
    cp "$file" "$ARCHIVE/NEEDS_HUMAN.json" 2>/dev/null || true
    printf '%s\n' "$reason" >"$ARCHIVE/NEEDS_HUMAN.txt" 2>/dev/null || true
  fi
  warn "NEEDS_HUMAN: $reason (evidence: $file)"
}

journal_settle() { # journal_settle <phase> <note> <result> -- terminal record, verified by read-back
  local phase=$1 note=$2 result=$3 out rc got mutated_flag=""
  if [ "${JOURNAL_MUTATED:-0}" -ne 0 ] || grep -q '"mutated": true' "$JOURNAL" 2>/dev/null; then
    mutated_flag="--mutated"
  fi
  mkdir -p "$(dirname "$JOURNAL")" 2>/dev/null || true
  set +e
  out=$(ct journal --path "$JOURNAL" --phase "$phase" --note "$note" --candidate "$CANDIDATE" \
        --run-id "$RUN_ID" --result "$result" $mutated_flag 2>&1)
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    warn "terminal journal write failed (rc=$rc) for $phase/$result: ${out:-no output}"
    needs_human "journal settlement failed at $phase ($result)"
    exit 78
  fi
  got=$(ct journal-state --path "$JOURNAL" 2>/dev/null | env -u PYTHONPATH "$PY3" -c \
    'import json,sys
try:
    print(json.load(sys.stdin)["state"]["result"] or "")
except Exception:
    print("")' 2>/dev/null || echo "")
  if [ "$got" != "$result" ]; then
    warn "terminal journal record did not survive read-back (wanted $result, read '${got:-unreadable}')"
    needs_human "journal settlement unverified at $phase ($result)"
    exit 78
  fi
  PHASE=$phase
  return 0
}

journal_require_attempt() { # refuse to change admission state without a durable record of this attempt
  local state rid cand rc
  set +e
  state=$(ct journal-state --path "$JOURNAL" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -ne 0 ] || [ -z "$state" ]; then
    warn "refusing: no readable journal at $JOURNAL; this attempt could not record itself"
    exit 65
  fi
  rid=$(printf '%s' "$state" | env -u PYTHONPATH "$PY3" -c \
    'import json,sys;print(json.load(sys.stdin)["state"]["run_id"] or "")' 2>/dev/null || echo "")
  cand=$(printf '%s' "$state" | env -u PYTHONPATH "$PY3" -c \
    'import json,sys;print(json.load(sys.stdin)["state"]["candidate"] or "")' 2>/dev/null || echo "")
  if [ "$rid" != "$RUN_ID" ] || [ "$cand" != "$CANDIDATE" ]; then
    warn "refusing: journal records run_id='${rid:-none}' candidate='${cand:-none}', this attempt is '$RUN_ID'/'$CANDIDATE'"
    exit 65
  fi
  return 0
}

freeze_release() { # freeze_release <why> -- 0 only when admission is provably restored by this attempt
  local why=${1:-release complete} deadline state="" cleared=1
  # The phase this attempt is actually in, restored before every return: releasing the freeze is work
  # done *inside* a phase, not a phase of its own. Leaving PHASE=unfreeze made the exit trap treat a
  # verified rollback's contractual non-zero exit as an unhandled failure and page a human over a run
  # that had already settled as ROLLED_BACK.
  local phase_before=$PHASE
  PHASE=unfreeze
  rm -f "${ARCHIVE:-/dev/null}/.freeze-alive" 2>/dev/null || true
  if [ -n "$KEEPALIVE_PID" ]; then kill "$KEEPALIVE_PID" 2>/dev/null || true; KEEPALIVE_PID=""; fi
  if [ "$FROZEN" -eq 1 ]; then
    # EXT-16: exact ownership before the marker is changed. A marker that is present but not ours is
    # left alone and reported, never cleared -- releasing another attempt's freeze is worse than
    # leaving a freeze in place for a human. EXT-17A: the ownership flag is NOT cleared on that path,
    # so the caller still knows which mechanism this attempt is holding.
    if [ -e "$DRAIN_MARKER" ] && ! drain_is_ours; then
      needs_human "the drain marker is no longer owned by $PRINCIPAL; leaving it in place"
      PHASE=$phase_before
      return 1
    fi
    if [ -e "$DRAIN_MARKER" ]; then
      ( cd "$INSTALL" && env -u PYTHONPATH HERMES_HOME="$HHOME" "$PY" - <<'PYEOF'
from gateway.drain_control import clear_drain_request
clear_drain_request()
PYEOF
      ) >>"${ARCHIVE:-/dev/null}/settlement.log" 2>&1 || settle_fail "could not clear the drain marker"
      # The return value of the helper is not the proof; the marker's absence is.
      if [ -e "$DRAIN_MARKER" ]; then
        cleared=0
        settle_fail "the drain marker is still present after clear_drain_request()"
      fi
    fi
  fi
  if [ "$ESTOP_OURS" -eq 1 ]; then
    if [ -e "$ESTOP_FILE" ] && ! estop_is_ours; then
      needs_human "the ESTOP sentinel is no longer owned by $PRINCIPAL; leaving it in place"
      PHASE=$phase_before
      return 1
    fi
    if [ -e "$ESTOP_FILE" ]; then
      ( cd "$INSTALL" && env -u PYTHONPATH HERMES_HOME="$HHOME" "$PY" - <<'PYEOF'
from agent.estop import disengage
disengage()
PYEOF
      ) >>"${ARCHIVE:-/dev/null}/settlement.log" 2>&1 || settle_fail "could not clear the ESTOP sentinel this run wrote"
      if [ -e "$ESTOP_FILE" ]; then
        cleared=0
        settle_fail "the ESTOP sentinel is still present after disengage()"
      fi
    fi
  fi
  deadline=$((SECONDS + UNFREEZE_WAIT))
  while [ $SECONDS -lt $deadline ]; do
    state=$(gateway_field gateway_state)
    [ "$state" != "draining" ] && break
    sleep 2
  done
  # EXT-17A: "admission restored" is a claim, so it is only made when all three proofs hold: this
  # run's markers are gone, no marker at all remains, and the live gateway is no longer draining.
  if [ "$cleared" -eq 1 ] && [ ! -e "$DRAIN_MARKER" ] && [ ! -e "$ESTOP_FILE" ] && [ "$state" != "draining" ]; then
    FROZEN=0
    ESTOP_OURS=0
    log "admission restored ($why): gateway_state=${state:-unreadable} estop=absent"
    journal unfreeze "admission restored: $why" 2>/dev/null || true
    PHASE=$phase_before
    return 0
  fi
  log "admission NOT restored ($why): gateway_state=${state:-unreadable} drain_marker=$([ -e "$DRAIN_MARKER" ] && echo present || echo absent) estop=$([ -e "$ESTOP_FILE" ] && echo present || echo absent)"
  needs_human "admission could not be restored ($why): ${SETTLE_FAILURES:-state unverifiable}; freeze left engaged"
  journal aborted "freeze release unverified: $why" 2>/dev/null || true
  PHASE=$phase_before
  return 1
}

abort_before_mutation() { # $1=phase $2=reason -> NO_GO with zero changes and zero restarts (EXT-08)
  local phase=$1 reason=$2 decision action mutated_flag=""
  SETTLING=1
  if [ "$MUTATED" -ne 0 ]; then
    # Defensive: a pre-mutation abort is only valid before the first mutation. If that invariant is
    # ever broken, undoing the mutation is the correct response, not pretending nothing happened.
    warn "abort_before_mutation reached with MUTATED=1; deferring to rollback"
    rollback "$reason"
  fi
  if [ "${JOURNAL_MUTATED:-0}" -ne 0 ] || grep -q '"mutated": true' "$JOURNAL" 2>/dev/null; then
    mutated_flag="--mutated"
  fi
  decision=$(ct failure --phase "$phase" --restart-attempts "$RESTART_ATTEMPTS" $mutated_flag 2>/dev/null || echo '{}')
  action=$(printf '%s' "$decision" | env -u PYTHONPATH "$PY3" -c \
    'import json,sys;print(json.load(sys.stdin).get("action","needs_human"))' 2>/dev/null || echo needs_human)
  PHASE=aborted
  if [ -n "$ARCHIVE" ]; then
    mkdir -p "$ARCHIVE" 2>/dev/null || true
    printf '%s\n' "$decision" >"$ARCHIVE/no-go-decision.json" 2>/dev/null || true
  fi
  log "NO_GO before any mutation at phase '$phase': $reason (restart=no, rollback=no)"
  if [ "$action" != "release_freeze" ]; then
    warn "failure contract for '$phase' says '$action', not release_freeze: leaving state for a human"
    needs_human "NO_GO at $phase needs a human: $reason"
    journal aborted "NO_GO at $phase needs a human: $reason" 2>/dev/null || true
    exit 2
  fi
  # Release only the freeze this run created: freeze_release clears the drain marker only when
  # FROZEN=1 (ours) and the ESTOP sentinel only when ESTOP_OURS=1 (ours), and only reports success
  # once both are verifiably gone.
  if [ "$FROZEN" -eq 1 ] || [ "$ESTOP_OURS" -eq 1 ]; then
    if ! freeze_release "NO_GO before mutation at $phase: $reason"; then
      needs_human "NO_GO at $phase could not release this attempt's freeze: $reason"
      journal aborted "NO_GO at $phase: freeze release failed: $reason" 2>/dev/null || true
      exit 3
    fi
  fi
  journal_settle aborted "NO_GO before mutation at $phase: $reason" ABORTED_SAFE
  if [ -n "$ARCHIVE" ]; then
    receipt "NO_GO_BEFORE_MUTATION" "$ARCHIVE/no-go-decision.json" 2>/dev/null || true
  fi
  exit 2
}

rollback() { # rollback <reason> -- bounded rollback whose result is verified before it is reported
  local reason=$1 head tree active unit_sha want_sha deadline state="" rc
  PHASE=rollback
  SETTLING=1
  warn "rolling back: $reason"
  journal rollback "rollback started: $reason"
  if [ -f "$ARCHIVE/unit/hermes-gateway.service" ]; then
    settle_run "restore the archived unit" install -m 0644 -o root -g root \
      "$ARCHIVE/unit/hermes-gateway.service" "$UNIT"
  fi
  if [ -f "$ARCHIVE/unit/90-recovery-hold.conf" ]; then
    settle_run "restore the recovery-hold drop-in" install -m 0644 -o root -g root \
      "$ARCHIVE/unit/90-recovery-hold.conf" "$HOLD_DROPIN"
  fi
  if [ -f "$ARCHIVE/unit/95-timeout-stop.conf" ]; then
    settle_run "restore the timeout-stop drop-in" install -m 0644 -o root -g root \
      "$ARCHIVE/unit/95-timeout-stop.conf" "$TIMEOUT_DROPIN"
  fi
  settle_run "daemon-reload after restoring the unit" systemctl daemon-reload
  if git -C "$INSTALL" reset --hard "$EXPECT_HEAD" >>"${ARCHIVE:-/dev/null}/settlement.log" 2>&1; then
    :
  else
    settle_fail "git reset --hard $EXPECT_HEAD failed"
  fi
  # EXT-17A: at most one rollback restart (the second restart overall). A third restart, or a
  # rollback re-entered after both budgets are spent, is a human decision, never automatic.
  if [ "$RESTART_ATTEMPTS" -ge 2 ]; then
    needs_human "rollback needed after $RESTART_ATTEMPTS restart attempt(s): refusing a further restart ($reason)"
  else
    RESTART_ATTEMPTS=$((RESTART_ATTEMPTS + 1))
    set +e
    timeout 340 systemctl restart "$SERVICE" >>"${ARCHIVE:-/dev/null}/settlement.log" 2>&1
    rc=$?
    set -e
    [ "$rc" -eq 0 ] || settle_fail "rollback restart returned $rc"
  fi
  deadline=$((SECONDS + STARTUP_WAIT))
  while [ $SECONDS -lt $deadline ]; do
    state=$(systemctl show "$SERVICE" -p ActiveState --value 2>/dev/null || echo unknown)
    [ "$state" = "active" ] && break
    sleep 3
  done
  head=$(git -C "$INSTALL" rev-parse HEAD 2>/dev/null || echo unknown)
  tree=$(git -C "$INSTALL" rev-parse HEAD^{tree} 2>/dev/null || echo unknown)
  active=$(systemctl show "$SERVICE" -p ActiveState --value 2>/dev/null || echo unknown)
  unit_sha=$(sha256sum "$UNIT" 2>/dev/null | cut -d' ' -f1 || echo unknown)
  want_sha=$(cut -d' ' -f1 "$ARCHIVE/unit/SHA256SUMS" 2>/dev/null | head -1 || echo unknown)
  {
    echo "reason: $reason"
    echo "head: $head (expected $EXPECT_HEAD)"
    echo "tree: $tree (expected $EXPECT_TREE)"
    echo "unit_sha256: $unit_sha (archived ${want_sha:-unknown})"
    echo "active_state: $active"
    echo "restart_attempts: $RESTART_ATTEMPTS"
    echo "settlement_failures: ${SETTLE_FAILURES:-none}"
  } >"$ARCHIVE/rollback.txt" 2>/dev/null || true
  # Verification first, settlement second: a rollback is only reported when the installation is
  # provably back at the deployed identity, the archived unit is back in place and the service is
  # active. Anything else is an unproven outcome and keeps the freeze for a human.
  if [ "$head" != "$EXPECT_HEAD" ] || [ "$tree" != "$EXPECT_TREE" ] \
     || [ "$active" != "active" ] \
     || { [ -n "$want_sha" ] && [ "$unit_sha" != "$want_sha" ]; } \
     || [ -n "$SETTLE_FAILURES" ]; then
    needs_human "rollback did not restore the deployed identity (head=$head tree=$tree active=$active unit=$unit_sha/${want_sha:-?}; ${SETTLE_FAILURES:-no step reported a failure})"
    journal aborted "rollback unverified: $reason" 2>/dev/null || true
    exit 1
  fi
  journal_settle report "rollback complete: $reason" ROLLED_BACK
  if ! freeze_release "rollback: $reason"; then
    needs_human "rollback restored the deployment but the freeze could not be released: $reason"
    exit 1
  fi
  receipt "ROLLED_BACK" "$ARCHIVE/rollback.txt"
  exit 1
}

verify_post_release() { # <phase> <unit_sha> -- the checks a COMPLETE claim depends on
  local phase=${1:-released} unit_sha=${2:-} rc hold
  if [ -z "$unit_sha" ]; then
    warn "refusing to verify without the regenerated unit's journaled sha256"
    return 2
  fi
  set +e
  preflight "$phase" >"${ARCHIVE:-/tmp}/verify-preflight-$phase.json" 2>&1
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    warn "post-release preflight NO_GO (phase=$phase, rc=$rc)"
    return 2
  fi
  set +e
  manifest_verify "$phase" "$unit_sha" >"${ARCHIVE:-/tmp}/verify-manifest-$phase.json" 2>&1
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    warn "post-release manifest verification NO_GO (phase=$phase, rc=$rc)"
    return 2
  fi
  hold=$(systemctl show "$SERVICE" -p Environment --value 2>/dev/null | grep -c 'HERMES_RECOVERY_HOLD=1' || true)
  if [ "${hold:-0}" -lt 1 ]; then
    warn "the recovery hold is not set in the service environment"
    return 2
  fi
  return 0
}

on_exit() {
  local rc=$? decision action mutated_flag=""
  trap - EXIT INT TERM
  SETTLING=1
  if [ -d "${ARCHIVE:-}" ]; then rm -f "$ARCHIVE/.freeze-alive" 2>/dev/null || true; fi
  if [ -n "$KEEPALIVE_PID" ]; then kill "$KEEPALIVE_PID" 2>/dev/null || true; fi
  # Only an APPLY settles on exit. --plan/--verify-only/--status mutate nothing and must never write
  # a terminal record over an attempt they did not run. Settled phases below are left alone.
  [ "${MODE:-}" = "apply" ] || return 0
  case "${PHASE:-}" in
    rollback|done|recover|aborted) return 0 ;;
  esac
  [ "$rc" -ne 0 ] || return 0
  # A terminal record for *this* run means the attempt is settled. Its non-zero exit is the outcome
  # it recorded — ROLLED_BACK exits 1 by contract — not a failure to triage, and re-settling it here
  # overwrites the result the release just proved (or, worse, files a contradictory NEEDS_HUMAN).
  if [ -f "$JOURNAL" ] && env -u PYTHONPATH "$PY3" -c \
      'import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d.get("result") and d.get("run_id")==sys.argv[2] else 1)' \
      "$JOURNAL" "$RUN_ID" 2>/dev/null; then
    return 0
  fi
  # EXT-17A: the exit path obeys the same failure contract as abort_before_mutation. It never
  # restarts blind, never treats a late phase as "nothing happened", and never claims ABORTED_SAFE
  # for a phase the contract says needs a human.
  if [ "${JOURNAL_MUTATED:-0}" -ne 0 ] || grep -q '"mutated": true' "$JOURNAL" 2>/dev/null; then
    mutated_flag="--mutated"
  fi
  decision=$(ct failure --phase "${PHASE:-unknown}" --restart-attempts "$RESTART_ATTEMPTS" $mutated_flag 2>/dev/null || echo '{}')
  action=$(printf '%s' "$decision" | env -u PYTHONPATH "$PY3" -c \
    'import json,sys;print(json.load(sys.stdin).get("action","needs_human"))' 2>/dev/null || echo needs_human)
  case "$action" in
    release_freeze)
      warn "settling an unhandled failure in phase '$PHASE' (rc=$rc): release the freeze, no restart"
      if [ "$FROZEN" -eq 1 ] || [ "$ESTOP_OURS" -eq 1 ]; then
        if ! freeze_release "aborted in phase '$PHASE' (rc=$rc) before any mutation"; then
          needs_human "aborted in phase '$PHASE' (rc=$rc) but the freeze could not be released"
          journal aborted "aborted in phase '$PHASE': freeze release failed" 2>/dev/null || true
          exit 1
        fi
      fi
      journal_settle aborted "aborted in phase '$PHASE' (rc=$rc) before any mutation" ABORTED_SAFE
      ;;
    rollback)
      rollback "unhandled failure in phase '$PHASE' (rc=$rc)"
      ;;
    *)
      warn "failure contract for '$PHASE' says '$action': keeping the freeze and leaving state for a human"
      needs_human "unhandled failure in phase '$PHASE' (rc=$rc): contract says $action"
      journal aborted "unhandled failure in phase '$PHASE': $action" 2>/dev/null || true
      ;;
  esac
}

on_signal() { # a signal is not a settlement: it marks the phase and lets on_exit settle it
  local sig=${1:-TERM}
  SIGNAL_SEEN=$sig
  if [ "$SETTLING" -eq 1 ]; then
    warn "ignoring $sig while settling phase '$PHASE': the settlement finishes first"
    return 0
  fi
  warn "signal $sig received in phase '$PHASE'"
  exit 70
}
trap 'on_signal TERM' INT TERM
trap on_exit EXIT
# ================== EXT-17A: interruption settlement library (END) ==================

# ------------------------------------------------------------------ mode: recover
if [ "$MODE" = recover ]; then
  PHASE=recover
  SETTLING=1
  # EXT-17A: recovery is bound to the attempt the journal records, never to whatever run id the
  # operator's shell happens to carry. The freeze principal is derived from that id, so a recovery
  # run using its own id could not release the attempt's freeze -- and could clear a different
  # attempt's marker. A journal that cannot be read is not "nothing to recover": it is a human.
  J_STATE=$(ct journal-state --path "$JOURNAL" 2>/dev/null || echo "")
  J_RUN=$(printf '%s' "$J_STATE" | env -u PYTHONPATH "$PY3" -c 'import json,sys;print(json.load(sys.stdin)["state"]["run_id"] or "")' 2>/dev/null || echo "")
  J_CAND=$(printf '%s' "$J_STATE" | env -u PYTHONPATH "$PY3" -c 'import json,sys;print(json.load(sys.stdin)["state"]["candidate"] or "")' 2>/dev/null || echo "")
  if [ -z "$J_RUN" ]; then
    warn "NEEDS_HUMAN: no readable journal at $JOURNAL; recovery has nothing to bind to"
    needs_human "recovery without a readable journal"
    exit 2
  fi
  if [ -n "${HERMES_RELEASE_RUN_ID:-}" ] && [ "$HERMES_RELEASE_RUN_ID" != "$J_RUN" ]; then
    warn "NEEDS_HUMAN: HERMES_RELEASE_RUN_ID=$HERMES_RELEASE_RUN_ID contradicts the journal attempt $J_RUN"
    needs_human "recovery run id contradicts the journal"
    exit 2
  fi
  if [ -n "$J_CAND" ] && [ "$J_CAND" != "$CANDIDATE" ]; then
    warn "NEEDS_HUMAN: journal candidate $J_CAND is not the requested $CANDIDATE"
    needs_human "recovery candidate does not match the journal"
    exit 2
  fi
  if [ "$RUN_ID" != "$J_RUN" ]; then
    log "recovery bound to the journal's attempt: run_id=$J_RUN (local default was $RUN_ID)"
    RUN_ID=$J_RUN
    PRINCIPAL="release-freeze:$RUN_ID"
  fi
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
    unfreeze)
      FROZEN=1; ESTOP_OURS=$(estop_is_ours && echo 1 || echo 0)
      if ! freeze_release "recovery: $REASON"; then
        needs_human "recovery could not release the freeze: $REASON"
        exit 2
      fi
      exit 0 ;;
    rollback) MUTATED=1; rollback "recovery: $REASON" ;;
    complete_verification)
      # EXT-17A: "complete verification" is a claim about the deployment, so recovery re-derives it
      # instead of trusting the earlier attempt's phase name, and only then settles COMPLETE.
      RID_ARGS=()
      if [ -n "${HERMES_RELEASE_RUN_ID:-}" ]; then RID_ARGS=(--run-id "$HERMES_RELEASE_RUN_ID"); fi
      set +e
      POST_UNIT_SHA=$(ct unit-receipt --journal "$JOURNAL" --candidate "$CANDIDATE" \
                        --unit-path "$UNIT" "${RID_ARGS[@]}" --print sha256 2>"$ARCHIVE/unit-receipt.json")
      URC=$?
      set -e
      if [ "$URC" -ne 0 ] || [ -z "$POST_UNIT_SHA" ]; then
        warn "NEEDS_HUMAN: no valid regenerated-unit receipt in $JOURNAL (rc=$URC)"
        [ -f "$ARCHIVE/unit-receipt.json" ] && sed -n '1,40p' "$ARCHIVE/unit-receipt.json" || true
        needs_human "recovery cannot verify the regenerated unit ($REASON)"
        exit 2
      fi
      FROZEN=1; ESTOP_OURS=$(estop_is_ours && echo 1 || echo 0)
      # The phase must be one the preflight can evaluate. In recovery the deployment has already
      # been restarted, so the state to prove is exactly the post-release one (freeze engaged, hold
      # active, candidate deployed) — "recover" is not a preflight phase, and passing it made every
      # recovery report an unevaluable NO_GO that no operator could ever clear.
      if ! verify_post_release post "$POST_UNIT_SHA"; then
        needs_human "recovery verification NO_GO: $REASON (freeze left engaged)"
        journal aborted "recovery verification NO_GO: $REASON" 2>/dev/null || true
        exit 2
      fi
      if ! freeze_release "recovery: verification completed by operator"; then
        needs_human "recovery verified the release but could not restore admission"
        exit 2
      fi
      journal_settle report "recovered by completing verification" COMPLETE
      receipt "RELEASED_VERIFIED" "$ARCHIVE/recover-plan.json"
      exit 0 ;;
    *) warn "NEEDS_HUMAN: $REASON — the freeze is left in place deliberately"
       needs_human "recovery requires a human: $REASON"
       PHASE=recover; exit 2 ;;
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
  journal_settle aborted "identity drift before start" ABORTED_SAFE
  exit 2
fi
if [ -n "$(git -C "$INSTALL" status --porcelain)" ]; then
  log "NO_GO: install worktree is dirty"
  journal_settle aborted "dirty worktree before start" ABORTED_SAFE
  exit 2
fi

PHASE=preflight
log "preflight against $EXPECT_HEAD / $EXPECT_TREE"
set +e
preflight pre "$EXPECT_HEAD" "$EXPECT_TREE" >"$ARCHIVE/preflight-before.json" 2>"$ARCHIVE/preflight-before.err"
PF_RC=$?
set -e
if [ "$MODE" = plan ]; then
  # EXT-16: the pre-freeze phase is expected to pass on a *busy* gateway — activity gates are
  # pending, not failed. A dry run still reports the honest exit code, but no longer treats ordinary
  # traffic as a reason the release could never proceed.
  PHASE=dry-run
  if [ "$PF_RC" -ne 0 ]; then
    log "preflight verdict NO_GO (rc=$PF_RC) in the current state — nothing was changed"
  else
    log "preflight verdict GO (pre-freeze only: quiescence is proven inside the freeze, not here)"
  fi
  grep -E '"(gate|failed|pending_gates|verdict)"' "$ARCHIVE/preflight-before.json" 2>/dev/null \
    || cat "$ARCHIVE/preflight-before.err" 2>/dev/null || true
  log "DRY RUN — the following would execute, in this order:"
  cat <<EOF
  1. archive $UNIT + $DROPIN_DIR/*.conf -> $ARCHIVE/unit/ (+ sha256 manifest)
  2. engage the admission freeze: drain marker (principal $PRINCIPAL) + ESTOP,
     then wait until the live gateway reports gateway_state=draining
  2b. refuse to adopt or overwrite a drain marker / ESTOP that this attempt does not own
     (a foreign or stale freeze is NEEDS_HUMAN, never automatic cleanup)
  3. quiesce inside the freeze: three consecutive samples, 20 s apart, each running the full
     in-freeze gate set -- (active_agents == 0) AND (live admissions for the current epoch == 0)
     AND this attempt still owns the freeze AND no cron execution in flight
     AND no scheduled claim at/after freeze time AND the two counters agree
  4. revalidate immediately before the first mutation: install/candidate/unit/drop-in identities,
     the pre-release manifest, and the same in-freeze gate set (drift fails closed)
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
  journal_settle aborted "preflight NO_GO rc=$PF_RC" ABORTED_SAFE
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
# pgrep forks a subshell that inherits this engine's argv, so it matches its own pattern; a naive
# self-exclusion reports that transient child as a foreign engine and refuses every APPLY. Keep a
# match only when the process is older than this engine, which no forked helper can be.
if [ -n "$OTHER_ENGINES" ]; then
  _SELF_START=$(sed -e 's/.*) //' "/proc/$$/stat" | awk '{print $20}')
  OTHER_ENGINES=$(printf '%s\n' $OTHER_ENGINES | while read -r _p; do
    case "$_p" in "$$"|"$PPID") continue ;; esac
    _st=$(sed -e 's/.*) //' "/proc/$_p/stat" 2>/dev/null | awk '{print $20}')
    [ -n "$_st" ] || continue
    [ "$_st" -lt "$_SELF_START" ] && printf '%s\n' "$_p"
  done || true)
fi
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
# EXT-17A: admission is not changed until this attempt is durably recorded. A journal that cannot be
# written is a release that cannot be recovered afterwards, so it is refused here (no marker, no
# merge) instead of being discovered later as an unattributable freeze.
journal_require_attempt
if ! freeze_engage; then
  # EXT-17A: the marker may already be on disk (freeze_engage writes it before it waits for the live
  # gateway to honour it), so this path owes the attempt a verified release before it may claim
  # ABORTED_SAFE -- and a durable NEEDS_HUMAN record when that release cannot be verified.
  if ! freeze_release "freeze could not be established"; then
    needs_human "freeze could not be established and could not be released"
    journal aborted "freeze could not be established: freeze release failed" 2>/dev/null || true
    exit 3
  fi
  journal_settle aborted "freeze could not be established" ABORTED_SAFE
  exit 2
fi

# ------------------------------------------------------- phase 3: quiesce (freeze honoured)
PHASE=quiesce
if [ "$QUIESCE_WAIT" -gt "$MAX_QUIESCE" ]; then
  log "refusing: QUIESCE_WAIT=$QUIESCE_WAIT exceeds the envelope's ${MAX_QUIESCE}s quiesce budget"
  freeze_release "configuration refused before quiescence"
  exit 65
fi
if [ -z "$FREEZE_SINCE" ]; then
  # Without the freeze epoch, scheduled work claimed during the freeze cannot be attributed and the
  # in-freeze gates are unevaluable. Abort rather than run a release that cannot prove its own safety.
  log "refusing: the freeze epoch is unknown; in-freeze quiescence cannot be attributed"
  if ! freeze_release "freeze epoch unknown"; then
    needs_human "the freeze epoch is unknown and the freeze could not be released"
    journal aborted "freeze epoch unknown: freeze release failed" 2>/dev/null || true
    receipt "ABORTED_NO_FREEZE_EPOCH" "$ARCHIVE/preflight-before.json" 2>/dev/null || true
    exit 3
  fi
  journal_settle aborted "freeze epoch unknown" ABORTED_SAFE
  receipt "ABORTED_NO_FREEZE_EPOCH" "$ARCHIVE/preflight-before.json"
  exit 3
fi
deadline=$((SECONDS + QUIESCE_WAIT))
quiet=0
while [ $SECONDS -lt $deadline ]; do
  # EXT-16: each sample is the full in-freeze gate set (zero agents, zero live admissions for the
  # current epoch, this attempt's freeze ownership, no cron work in flight, no scheduled claim after
  # the freeze, counters consistent). The qualified interval is three consecutive samples, unchanged.
  idx=$quiet
  if preflight infreeze >"$ARCHIVE/admission-$idx.json" 2>"$ARCHIVE/admission-$idx.err"; then
    quiet=$((quiet + 1))
  else
    if [ "$quiet" -gt 0 ]; then
      log "in-freeze sample failed after $quiet passing sample(s) — the quiet interval restarts"
    fi
    quiet=0
  fi
  # Read back the sample this iteration actually wrote. ``quiet`` has already been incremented on
  # success (and reset on failure), so ``admission-$quiet.json`` names a file that does not exist
  # yet — the log line then reported ENOENT instead of the gate detail it was meant to show.
  log "in-freeze quiescence sample $quiet/3: $(tr -d '\n' <"$ARCHIVE/admission-$idx.json" 2>/dev/null | grep -o '"failed": \[[^]]*\]' | cut -c1-160 || true)"
  if [ "$quiet" -ge 3 ]; then break; fi
  sleep 20
done
journal quiesce "quiet samples=$quiet freeze_since=$FREEZE_SINCE principal=$PRINCIPAL"
if [ "$quiet" -lt 3 ]; then
  log "no quiescent window within ${QUIESCE_WAIT}s — aborting before any mutation"
  tail -40 "$ARCHIVE/admission-0.err" 2>/dev/null || true
  if ! freeze_release "not quiet within ${QUIESCE_WAIT}s"; then
    needs_human "no quiescent window and the freeze could not be released"
    journal aborted "not quiet within ${QUIESCE_WAIT}s: freeze release failed" 2>/dev/null || true
    receipt "ABORTED_NOT_QUIET" "$ARCHIVE/preflight-before.json" 2>/dev/null || true
    exit 3
  fi
  journal_settle aborted "not quiet within ${QUIESCE_WAIT}s" ABORTED_SAFE
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
# EXT-16: freeze ownership is re-proven here by the shell itself, not only by the preflight payload,
# so a marker that changed hands between the last quiesce sample and the merge fails closed.
drain_is_ours || abort_before_mutation revalidate "admission is not frozen by this attempt ($PRINCIPAL)"
estop_is_ours || abort_before_mutation revalidate "admission is not frozen: ESTOP is not held by this attempt ($PRINCIPAL)"
set +e
manifest_verify pre >"$ARCHIVE/manifest-revalidate.json" 2>&1
MF2=$?
set -e
[ "$MF2" -eq 0 ] || abort_before_mutation revalidate "pre-release manifest identities no longer hold"
# EXT-16: the same in-freeze gate set as the quiesce samples, re-evaluated immediately before the
# first mutation, so drift in admission state, freeze ownership, scheduled work or identities between
# the last quiesce sample and the merge fails closed.
set +e
preflight revalidate >"$ARCHIVE/preflight-revalidate.json" 2>"$ARCHIVE/preflight-revalidate.err"
PF3=$?
set -e
if [ "$PF3" -ne 0 ]; then
  tail -40 "$ARCHIVE/preflight-revalidate.err" 2>/dev/null || true
  grep -E '"(gate|failed|verdict)"' "$ARCHIVE/preflight-revalidate.json" 2>/dev/null || true
  abort_before_mutation revalidate "in-freeze revalidation NO_GO (rc=$PF3)"
fi
journal revalidate "identities and admission revalidated immediately before mutation"

# ------------------------------------------------------------------- phase 5: deploy
PHASE=deploy
MUTATED=1
log "fast-forwarding $INSTALL to $CANDIDATE"
# EXT-17A: the first durable proof that a mutation was *attempted*. Without it a SIGKILL between the
# merge and the deploy receipt leaves a journal that still reads "revalidate, not mutated", and
# recovery would release the freeze over an installation that is already at the candidate.
journal deploy-intent "about to fast-forward $INSTALL from $EXPECT_HEAD to $CANDIDATE" --head "$EXPECT_HEAD"
if ! git -C "$INSTALL" merge --ff-only "$CANDIDATE" >"$ARCHIVE/merge.txt" 2>&1; then
  # A refused ff-only merge cannot change the install, but "cannot" is not "proven": release the
  # freeze without a restart only when the install still matches the deployed identity exactly, and
  # roll back (bounded, verified) otherwise.
  HEAD=$(git -C "$INSTALL" rev-parse HEAD 2>/dev/null || echo unknown)
  TREE=$(git -C "$INSTALL" rev-parse HEAD^{tree} 2>/dev/null || echo unknown)
  DIRTY=$(git -C "$INSTALL" status --porcelain 2>/dev/null || echo dirty-unknown)
  if [ "$HEAD" = "$EXPECT_HEAD" ] && [ "$TREE" = "$EXPECT_TREE" ] && [ -z "$DIRTY" ]; then
    MUTATED=0
    abort_before_mutation deploy-intent "fast-forward merge refused; install verified untouched ($HEAD/$TREE, clean)"
  fi
  rollback "fast-forward merge refused"
fi
HEAD=$(git -C "$INSTALL" rev-parse HEAD)
TREE=$(git -C "$INSTALL" rev-parse HEAD^{tree})
if [ "$HEAD" != "$CANDIDATE" ] || [ "$TREE" != "$CAND_TREE" ]; then
  rollback "post-merge identity mismatch: HEAD=$HEAD tree=$TREE"
fi
if [ -n "$(git -C "$INSTALL" status --porcelain)" ]; then
  rollback "working tree dirty after merge"
fi
journal deploy "deployed $HEAD tree $TREE" --head "$HEAD" --mutated
# The install is now provably at the candidate: from here on every record carries the mutation, and
# recovery can date the change from this receipt instead of guessing from the phase name (D-10).
JOURNAL_MUTATED=1
log "deployed HEAD=$HEAD tree=$TREE"

# --------------------------------------------------------------- phase 6: regenerate unit
PHASE=unit
log "regenerating the unit from the deployed code (no restart)"
( cd "$INSTALL" && HERMES_HOME="$HHOME" "$PY" -m hermes_cli.main gateway install --system ) \
  >"$ARCHIVE/unit-install.txt" 2>&1 || rollback "gateway install --system failed"
systemctl daemon-reload
UNIT_TIMEOUT=$(awk -F= '/^TimeoutStopSec=/{print $2}' "$UNIT" | tail -1)
EFFECTIVE=$(systemctl show "$SERVICE" -p TimeoutStopUSec --value)
# ``systemctl show --value`` prints the bare value on a real host; some versions and shims keep the
# property prefix. Strip it so the comparison is about the number, not the formatting.
EFFECTIVE=${EFFECTIVE#TimeoutStopUSec=}
EFFECTIVE=${EFFECTIVE#TimeoutStopSec=}
log "unit TimeoutStopSec=${UNIT_TIMEOUT:-<absent>}  effective TimeoutStopUSec=$EFFECTIVE"
# The contract is the timeout systemd actually enforces, not the unit file's own assignment: the
# stock unit ships TimeoutStopSec=210 and the operator drop-in 95-timeout-stop.conf raises it to
# 300s, so requiring 300 in the regenerated unit would roll back every correctly regenerated unit.
case "$EFFECTIVE" in
  300|300s|5min|300000000|"5min 0s") : ;;
  *) rollback "effective TimeoutStopUSec is '${EFFECTIVE:-absent}', expected 300s (5min)" ;;
esac
case "$UNIT_TIMEOUT" in
  300|300s|5min) : ;;
  *) log "note: regenerated unit carries TimeoutStopSec=${UNIT_TIMEOUT:-<absent>}; the 300s floor is supplied by the drop-in and verified effective above" ;;
esac
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
# EXT-17A: a verified release that could not release the freeze is not a COMPLETE release. The
# deployment stays verified, the freeze stays engaged and a human is told exactly that -- the
# alternative (journaling COMPLETE over a live ESTOP) is the failure mode this section exists for.
if ! freeze_release "release verified"; then
  needs_human "release verified and deployed, but admission could not be restored"
  journal aborted "release verified but the freeze could not be released" 2>/dev/null || true
  receipt "RELEASED_FREEZE_STUCK" "$ARCHIVE/verification.txt" 2>/dev/null || true
  exit 4
fi

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
journal_settle report "release verified" COMPLETE
receipt "RELEASED_VERIFIED" "$ARCHIVE/verification.txt"
log "release complete: HEAD=$HEAD TimeoutStopUSec=$EFFECTIVE SIGKILL=${KILLS:-0}"
