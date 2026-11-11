#!/usr/bin/env bash
# release002_driver.sh — the supervised, detached RESTORE-002 release driver (EXT-15, ext15.2).
#
# Why this exists: the EXT-14 driver ran outside the gateway cgroup (the release restarts the
# gateway, so a driver inside it would be killed mid-release) and it *did* stop safely — but it
# exited STOPPED_SAFE with no receipt, because its receipt writer only existed after APPLY. The
# absence of a published receipt then had to be reconciled from host evidence after the fact.
#
# Contract enforced here:
#   * EVERY terminal result — guard failure, closed window, deadline, unevaluable plan, signal,
#     apply refusal, apply failure, applied — writes summary.json + RECEIPT.md EXACTLY ONCE, and
#     both are written atomically (temp file in the same directory, fsync, rename).
#   * every run carries an exact attempt identity: a content hash over candidate, expected and
#     observed install identities, mode, window and quiet parameters, plus the attempt sequence.
#     Each attempt is archived under attempts/<attempt_id>/ and never rewritten.
#   * phase accounting is explicit: every phase records begin/end timestamps, and a stop that never
#     reached PLAN records PLAN_NOT_ENTERED — it never implies that PLAN passed.
#   * the receipt carries the actual failed gates, per attempt and in union, with the runtime
#     snapshot observed beside each verdict.
#   * a resumed driver never launches a second APPLY: if any prior attempt in the evidence
#     directory invoked apply, the run refuses as NEEDS_HUMAN.
#
# This is an *observability and bookkeeping* correction. It never shortens the quiet window, never
# aborts a turn to manufacture quiescence, never retries a deployment, never stops or restarts the
# service, and never applies without an explicit, self-consistent PLAN GO.
#
#   release002_driver.sh --mode plan|apply \
#     --candidate SHA --expect-head SHA --expect-tree TREE --install DIR --launcher PATH \
#     --evidence-dir DIR --open-after HH:MM|+SECS --deadline HH:MM|+SECS \
#     [--quiet-window 130] [--poll 20] [--settle 130] \
#     [--receipt-repo OWNER/REPO --receipt-pr N --publish 0|1] \
#     [--service hermes-gateway.service] [--journal PATH] [--unit-dir DIR] [--proc-root /proc] \
#     [--gateway-state PATH] [--allow-unevaluable-retry]
#
# Exit codes: 0 GO_READY (plan) / APPLIED (apply rc 0) · 2 STOPPED_SAFE · 3 NEEDS_HUMAN
#             4 GUARD_FAILED · 64 usage.
#
# The driver's own receipt is *not* an operational verification. When it applies, the release
# runner writes the authoritative RECEIPT.md under the release archive; this driver records the
# apply exit code and points at it, and never claims RELEASED on its own.
set -uo pipefail

DRIVER_VERSION="ext15.2"
TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }
NOW() { date -u +%s; }
log() { printf '%s %s\n' "[$(TS)]" "$*" | tee -a "${LOG:-/dev/null}" >&2; }

usage() {
  sed -n '3,45p' "$0" | sed 's/^# \{0,1\}//'
  exit 64
}

MODE=""; CANDIDATE=""; EXPECT_HEAD=""; EXPECT_TREE=""; INSTALL=""; LAUNCHER=""
EVIDENCE_DIR=""; OPEN_AFTER=""; DEADLINE=""; QUIET_WINDOW=130; POLL=20; SETTLE=130
RECEIPT_REPO=""; RECEIPT_PR=""; PUBLISH=0
SERVICE="hermes-gateway.service"; JOURNAL="/var/lib/hermes-release/restore-002/journal.json"
GATEWAY_STATE=""
UNIT_DIR="/etc/systemd/system"; PROC_ROOT="/proc"
ALLOW_UNEVALUABLE_RETRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2;;
    --candidate) CANDIDATE="$2"; shift 2;;
    --expect-head) EXPECT_HEAD="$2"; shift 2;;
    --expect-tree) EXPECT_TREE="$2"; shift 2;;
    --install) INSTALL="$2"; shift 2;;
    --launcher) LAUNCHER="$2"; shift 2;;
    --evidence-dir) EVIDENCE_DIR="$2"; shift 2;;
    --open-after) OPEN_AFTER="$2"; shift 2;;
    --deadline) DEADLINE="$2"; shift 2;;
    --quiet-window) QUIET_WINDOW="$2"; shift 2;;
    --poll) POLL="$2"; shift 2;;
    --settle) SETTLE="$2"; shift 2;;
    --receipt-repo) RECEIPT_REPO="$2"; shift 2;;
    --receipt-pr) RECEIPT_PR="$2"; shift 2;;
    --publish) PUBLISH="$2"; shift 2;;
    --service) SERVICE="$2"; shift 2;;
    --journal) JOURNAL="$2"; shift 2;;
    --unit-dir) UNIT_DIR="$2"; shift 2;;
    --proc-root) PROC_ROOT="$2"; shift 2;;
    --gateway-state) GATEWAY_STATE="$2"; shift 2;;
    --allow-unevaluable-retry) ALLOW_UNEVALUABLE_RETRY=1; shift;;
    -h|--help) usage;;
    *) echo "unknown argument: $1" >&2; exit 64;;
  esac
done

for req in MODE CANDIDATE EXPECT_HEAD EXPECT_TREE INSTALL LAUNCHER EVIDENCE_DIR OPEN_AFTER DEADLINE; do
  [ -n "${!req}" ] || { echo "missing --$(echo "$req" | tr 'A-Z_' 'a-z-')" >&2; exit 64; }
done
case "$MODE" in plan|apply) ;; *) echo "--mode must be plan or apply" >&2; exit 64;; esac
mkdir -p "$EVIDENCE_DIR" || { echo "cannot create evidence dir" >&2; exit 3; }

LOG="$EVIDENCE_DIR/driver.log"
SAMPLES="$EVIDENCE_DIR/samples.log"
PHASES="$EVIDENCE_DIR/phases.log"
SUMMARY="$EVIDENCE_DIR/summary.json"
RECEIPT="$EVIDENCE_DIR/RECEIPT.md"
PUBLISH_MARK="$EVIDENCE_DIR/.published"

STARTED_AT="$(TS)"; STARTED_EPOCH="$(NOW)"

# ---------------------------------------------------------------- atomic writes
# A receipt that is half-written is worse than no receipt: it reads as evidence. Every durable
# artifact goes to a sibling temp file, is fsynced, then renamed over the target.
atomic_write() {  # target ; stdin is the content
  local tgt="$1" tmp="$1.tmp.$$"
  if ! cat >"$tmp"; then rm -f "$tmp"; return 1; fi
  sync "$tmp" 2>/dev/null || sync 2>/dev/null || true
  if ! mv -f "$tmp" "$tgt"; then rm -f "$tmp"; return 1; fi
  sync "$(dirname "$tgt")" 2>/dev/null || true
  return 0
}
atomic_copy() {  # src target
  local src="$1" tgt="$2" tmp="$2.tmp.$$"
  cp "$src" "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  sync "$tmp" 2>/dev/null || sync 2>/dev/null || true
  mv -f "$tmp" "$tgt" || { rm -f "$tmp"; return 1; }
  sync "$(dirname "$tgt")" 2>/dev/null || true
}

# ---------------------------------------------------------------- window parsing
epoch_of() {  # "HH:MM" (local, today; tomorrow if already past) or "+SECS"
  case "$1" in
    +*) NOW | awk -v d="${1#+}" '{print $1+d}';;
    *) date -d "today $1" +%s 2>/dev/null || { date -d "$1" +%s 2>/dev/null || echo 0; };;
  esac
}

DISPOSITION="NEEDS_HUMAN"
REASON="driver did not reach a terminal state"
EXIT_CODE=3
PLAN_STATE="NOT_ENTERED"          # NOT_ENTERED | ENTERED_NO_GO | ENTERED_GO | UNEVALUABLE
PLAN_ENTERED=0
PLAN_RC=""; PLAN_LAST_GATES=""; PLAN_AGENTS="" ; PLAN_LIVE=""
APPLY_STARTED=0; APPLY_RC=""
GUARDS=""
SAMPLES_N=0; AGENTS_MIN=""; AGENTS_MAX=""; LIVE_MIN=""; LIVE_MAX=""
QUIESCENCE_ONLY=0; OTHER_GATES=""; ALL_GATES=""
QUIET_RESTARTS=0
PRIOR_ATTEMPTS=""; PRIOR_APPLY_SEEN=0
INSTALL_HEAD=""; INSTALL_TREE=""; ATTEMPT_ID=""; ATTEMPT_SEQ=1; ATTEMPT_DIR=""

# ---------------------------------------------------------------- phases
phase_begin() { local n="$1"; printf '%s attempt=%s phase=%s state=begin %s\n' "$(TS)" "$ATTEMPT_ID" "$n" "${2:-}" >>"$PHASES"; }
phase_end()   { local n="$1"; printf '%s attempt=%s phase=%s state=end %s\n' "$(TS)" "$ATTEMPT_ID" "$n" "${2:-}" >>"$PHASES"; log "phase=$n end ${2:-}"; }
phase() { phase_begin "$1" "${2:-}"; phase_end "$1" "${2:-}"; }

# ---------------------------------------------------------------- guards
guard() {  # name ok detail
  GUARDS="${GUARDS}${GUARDS:+,}{\"name\":\"$1\",\"ok\":$2,\"detail\":\"$(printf '%s' "$3" | sed 's/"/\\"/g')\"}"
  [ "$2" = true ] || log "GUARD FAILED $1: $3"
}

collect_guards() {
  local units mainpid hold dropin
  INSTALL_HEAD=$(git -C "$INSTALL" rev-parse HEAD 2>/dev/null)
  INSTALL_TREE=$(git -C "$INSTALL" rev-parse 'HEAD^{tree}' 2>/dev/null)
  guard install_head "$([ "$INSTALL_HEAD" = "$EXPECT_HEAD" ] && echo true || echo false)" \
    "install HEAD=${INSTALL_HEAD:-unreadable} expected=$EXPECT_HEAD"
  guard install_tree "$([ "$INSTALL_TREE" = "$EXPECT_TREE" ] && echo true || echo false)" \
    "install tree=${INSTALL_TREE:-unreadable} expected=$EXPECT_TREE"
  guard journal_absent "$([ -e "$JOURNAL" ] && echo false || echo true)" \
    "journal=$JOURNAL present=$([ -e "$JOURNAL" ] && echo yes || echo no)"
  units=$(systemctl list-units --all --no-legend 'hermes-release-*' 2>/dev/null | wc -l | tr -d ' ')
  guard no_release_units "$([ "${units:-0}" = 0 ] && echo true || echo false)" "hermes-release-* units=${units:-0}"
  mainpid=$(systemctl show "$SERVICE" -p MainPID --value 2>/dev/null)
  hold=$(sudo -n tr '\0' '\n' <"$PROC_ROOT/${mainpid:-0}/environ" 2>/dev/null | grep -c '^HERMES_RECOVERY_HOLD=1$')
  guard recovery_hold "$([ "${hold:-0}" -ge 1 ] && echo true || echo false)" \
    "mainpid=${mainpid:-none} HERMES_RECOVERY_HOLD=${hold:-0}"
  dropin=0
  [ -e "$UNIT_DIR/$SERVICE.d/90-recovery-hold.conf" ] && dropin=$((dropin+1))
  [ -e "$UNIT_DIR/$SERVICE.d/95-timeout-stop.conf" ] && dropin=$((dropin+1))
  guard protective_dropins "$([ "$dropin" = 2 ] && echo true || echo false)" "drop-ins present=${dropin}/2"
}

guards_ok() { ! printf '%s' "$GUARDS" | grep -q '"ok":false'; }

# ---------------------------------------------------------------- attempt identity
compute_attempt() {
  local ident
  ident=$(printf 'driver=%s\nmode=%s\ncandidate=%s\nexpect_head=%s\nexpect_tree=%s\ninstall_head=%s\ninstall_tree=%s\nopen=%s\ndeadline=%s\nquiet=%s\npoll=%s\nsettle=%s\n' \
    "$DRIVER_VERSION" "$MODE" "$CANDIDATE" "$EXPECT_HEAD" "$EXPECT_TREE" "${INSTALL_HEAD:-unreadable}" \
    "${INSTALL_TREE:-unreadable}" "$OPEN_EPOCH" "$DEADLINE_EPOCH" "$QUIET_WINDOW" "$POLL" "$SETTLE" \
    | sha256sum | cut -c1-32)
  ATTEMPT_ID="$ident"
  ATTEMPT_SEQ=$(( $(ls -1d "$EVIDENCE_DIR"/attempts/*/ 2>/dev/null | wc -l | tr -d ' ') + 1 ))
  ATTEMPT_DIR="$EVIDENCE_DIR/attempts/$ATTEMPT_ID"
  if ! mkdir -p "$ATTEMPT_DIR"; then
    REASON="cannot create the attempt archive directory — evidence is not durable"
    DISPOSITION=NEEDS_HUMAN; FORCED_RC=3; exit 3
  fi
}

write_attempt_json() {  # state: begin|final
  local pj="false" aj="false"
  [ "$PLAN_ENTERED" = 1 ] && pj="true"
  [ "$APPLY_STARTED" = 1 ] && aj="true"
  atomic_write "$ATTEMPT_DIR/attempt.json" <<EOF
{"attempt_id":"$ATTEMPT_ID","attempt_seq":$ATTEMPT_SEQ,"driver_version":"$DRIVER_VERSION",
 "mode":"$MODE","state":"$1","candidate":"$CANDIDATE","expect_head":"$EXPECT_HEAD","expect_tree":"$EXPECT_TREE",
 "install_head":"${INSTALL_HEAD:-unreadable}","install_tree":"${INSTALL_TREE:-unreadable}",
 "service":"$SERVICE","quiet_window_s":$QUIET_WINDOW,"poll_s":$POLL,"settle_s":$SETTLE,
 "open_at":"$(date -u -d "@$OPEN_EPOCH" +%FT%TZ)","deadline_at":"$(date -u -d "@$DEADLINE_EPOCH" +%FT%TZ)",
 "started_at":"$STARTED_AT","updated_at":"$(TS)","finished_at":"$([ "$1" = final ] && TS || echo "")",
 "plan_entered":$pj,"plan_state":"$PLAN_STATE","apply_started":$aj,"disposition":"$DISPOSITION"}
EOF
}

# A resumed driver must not launch a second APPLY, and must say that an earlier attempt was cut
# short rather than presenting its own result as the whole story.
scan_prior_attempts() {
  local d aj fin
  for d in "$EVIDENCE_DIR"/attempts/*/; do
    [ -f "$d/attempt.json" ] || continue
    [ "$(cd "$d" && pwd)" = "$(cd "$ATTEMPT_DIR" && pwd)" ] && continue
    aj=false; fin=false
    grep -q '"apply_started": *true' "$d/attempt.json" 2>/dev/null && aj=true
    [ -e "$d/.finalised" ] && fin=true
    [ "$aj" = true ] && PRIOR_APPLY_SEEN=1
    PRIOR_ATTEMPTS="${PRIOR_ATTEMPTS}${PRIOR_ATTEMPTS:+,}{\"attempt_id\":\"$(basename "$d")\",\"apply_started\":$aj,\"finalised\":$fin}"
  done
}

# ---------------------------------------------------------------- plan evaluation
classify_gates() {  # raw-failed-list -> BLOCKER in the current shell
  local cleaned tok q=0 other=0
  cleaned=$(printf '%s' "${1:-}" | tr -d '"' | tr ',' '\n' | tr -d ' ')
  while [ -n "$cleaned" ]; do
    tok=${cleaned%%$'\n'*}
    case "$tok" in
      ''|none) ;;
      no_active_turns|recent_session_activity) q=$((q+1));;
      *) other=$((other+1)); OTHER_GATES="$OTHER_GATES${OTHER_GATES:+, }$tok";;
    esac
    case "$tok" in ''|none) ;; *) ALL_GATES="$ALL_GATES${ALL_GATES:+, }$tok";; esac
    case "$cleaned" in *$'\n'*) cleaned=${cleaned#*$'\n'};; *) cleaned="";; esac
  done
  if [ "$other" -gt 0 ]; then BLOCKER=other; elif [ "$q" -gt 0 ]; then BLOCKER=quiescence; else BLOCKER=none; fi
}

plan_once() {  # sets P_RC P_GATES P_AGENTS P_LIVE
  local out
  PLAN_ENTERED=1
  out=$("$LAUNCHER" plan --candidate "$CANDIDATE" 2>&1); P_RC=$?
  P_GATES=$(printf '%s' "$out" | sed -n 's/.*"failed": *\[\([^]]*\)\].*/\1/p' | head -1)
  P_AGENTS=$(printf '%s' "$out" | sed -n 's/.*"active_agents": *\([0-9][0-9]*\).*/\1/p' | head -1)
  P_LIVE=$(printf '%s' "$out" | sed -n 's/.*"live_admissions": *\([0-9][0-9]*\).*/\1/p' | head -1)
  # raw form distinguishes "key absent" from "key present but unreadable" — both are refused, but
  # the receipt should say which one happened.
  P_AGENTS_RAW=$(printf '%s' "$out" | sed -n 's/.*"active_agents": *\([^,}]*\).*/\1/p' | head -1 | tr -d ' ')
  P_LIVE_RAW=$(printf '%s' "$out" | sed -n 's/.*"live_admissions": *\([^,}]*\).*/\1/p' | head -1 | tr -d ' ')
  P_VERDICT=$(printf '%s' "$out" | sed -n 's/.*"verdict": *"\([A-Z_]*\)".*/\1/p' | head -1)
  # EXT-16: a phase-aware engine says which contract it evaluated. Absent (legacy engine) keeps the
  # pre-EXT-16 behaviour so an older launcher is never reinterpreted as something it is not.
  P_PHASE=$(printf '%s' "$out" | sed -n 's/.*"phase": *"\([a-z_]*\)".*/\1/p' | head -1)
  P_PENDING=$(printf '%s' "$out" | sed -n 's/.*"pending_gates": *\[\([^]]*\)\].*/\1/p' | head -1)
  P_DEPLOY_READY=$(printf '%s' "$out" | sed -n 's/.*"deployment_ready": *\([a-z]*\).*/\1/p' | head -1)
  PLAN_RC="$P_RC"; PLAN_LAST_GATES="${P_GATES:-}"; PLAN_AGENTS="$P_AGENTS"; PLAN_LIVE="$P_LIVE"
}

# A GO is accepted only when the evidence that justifies it is present and agrees. This cannot turn a
# NO_GO into a GO; it can only refuse a GO.
#
# EXT-16: on a phase-aware engine, a *pre-freeze* GO must never be validated with the admission
# counters — on a gateway carrying traffic those are non-zero by design, and requiring zero here is
# the very defect EXT-16 corrects. The pre-freeze GO is validated instead on: the engine says it ran
# the `pre` phase, it lists no failed gate, and the quiescence gates are *pending* rather than passed.
# A pre-freeze GO that claims quiescence is refused as manufactured.
validate_go() {  # -> 0 ok, 1 reason in VALIDATE_ERR
  VALIDATE_ERR=""
  # A GO that still lists failed gates contradicts itself.
  case "$(printf '%s' "${P_GATES:-}" | tr -d '" ' )" in
    ''|none) ;;
    *) VALIDATE_ERR="PLAN returned GO while listing failed gates (${P_GATES})"; return 1;;
  esac
  if [ -n "${P_PHASE:-}" ]; then
    if [ "$P_PHASE" != "pre" ]; then
      VALIDATE_ERR="PLAN GO came from phase '$P_PHASE'; the pre-freeze gate must be evaluated as 'pre'"
      return 1
    fi
    if [ "${P_DEPLOY_READY:-false}" = "true" ]; then
      VALIDATE_ERR="PLAN returned deployment_ready=true before the freeze"
      return 1
    fi
    # Quiescence gates must be pending before the freeze. If any of them is reported as evaluated
    # (i.e. absent from pending_gates) the engine is claiming quiescence it cannot have.
    local qg="no_active_agents no_live_admissions quiet_window_elapsed cron_inflight_zero"
    local q
    for q in $qg; do
      case "$(printf '%s' "${P_PENDING:-}" | tr -d '" ')" in
        *"$q"*) ;;
        *) VALIDATE_ERR="PLAN GO before the freeze without '$q' listed as pending — quiescence cannot be claimed pre-freeze"
           return 1;;
      esac
    done
    return 0
  fi
  if [ -z "${P_AGENTS:-}" ]; then
    case "${P_AGENTS_RAW:-}" in
      ''|null) VALIDATE_ERR="PLAN returned GO without active_agents/live_admissions evidence";;
      *) VALIDATE_ERR="PLAN returned GO with unreadable active_agents ($P_AGENTS_RAW)";;
    esac
    return 1
  fi
  if [ -z "${P_LIVE:-}" ]; then
    case "${P_LIVE_RAW:-}" in
      ''|null) VALIDATE_ERR="PLAN returned GO without active_agents/live_admissions evidence";;
      *) VALIDATE_ERR="PLAN returned GO with unreadable live_admissions ($P_LIVE_RAW)";;
    esac
    return 1
  fi
  # Non-numeric counters are not "zero by accident": they are missing evidence.
  case "$P_AGENTS" in ''|*[!0-9]*) VALIDATE_ERR="PLAN returned GO with unreadable active_agents ($P_AGENTS)"; return 1;; esac
  case "$P_LIVE" in ''|*[!0-9]*) VALIDATE_ERR="PLAN returned GO with unreadable live_admissions ($P_LIVE)"; return 1;; esac
  if [ "$P_AGENTS" -ne 0 ] || [ "$P_LIVE" -ne 0 ]; then
    VALIDATE_ERR="PLAN returned GO with contradictory counters (active_agents=$P_AGENTS live_admissions=$P_LIVE)"; return 1
  fi
  # A GO that still lists failed gates contradicts itself.
  case "$(printf '%s' "${P_GATES:-}" | tr -d '" ' )" in
    ''|none) ;;
    *) VALIDATE_ERR="PLAN returned GO while listing failed gates (${P_GATES})"; return 1;;
  esac
  return 0
}

sample() {  # label
  local obs="unknown" obs_at="unknown" a="${P_AGENTS:-}" l="${P_LIVE:-}"
  SAMPLES_N=$((SAMPLES_N+1))
  case "$a" in ''|*[!0-9]*) a="";; esac
  case "$l" in ''|*[!0-9]*) l="";; esac
  if [ -n "$a" ]; then
    { [ -z "$AGENTS_MIN" ] || [ "$a" -lt "$AGENTS_MIN" ]; } && AGENTS_MIN="$a"
    { [ -z "$AGENTS_MAX" ] || [ "$a" -gt "$AGENTS_MAX" ]; } && AGENTS_MAX="$a"
  fi
  if [ -n "$l" ]; then
    { [ -z "$LIVE_MIN" ] || [ "$l" -lt "$LIVE_MIN" ]; } && LIVE_MIN="$l"
    { [ -z "$LIVE_MAX" ] || [ "$l" -gt "$LIVE_MAX" ]; } && LIVE_MAX="$l"
  fi
  if [ -n "$GATEWAY_STATE" ] && [ -r "$GATEWAY_STATE" ]; then
    obs=$(sed -n 's/.*"active_agents": *\([0-9][0-9]*\).*/\1/p' "$GATEWAY_STATE" 2>/dev/null | head -1)
    obs_at=$(sed -n 's/.*"updated_at": *"\([^"]*\)".*/\1/p' "$GATEWAY_STATE" 2>/dev/null | head -1)
  fi
  classify_gates "${P_GATES:-}"
  [ "$BLOCKER" = quiescence ] && QUIESCENCE_ONLY=$((QUIESCENCE_ONLY+1))
  printf '%s attempt=%s label=%s plan_rc=%s verdict=%s blocker=%s active_agents=%s live_admissions=%s gate_active_agents=%s gate_live_admissions=%s observed_agents=%s observed_at=%s gates=%s\n' \
    "$(TS)" "$ATTEMPT_ID" "$1" "${P_RC:-}" "${P_VERDICT:-unknown}" "$BLOCKER" "${a:-unknown}" "${l:-unknown}" \
    "${PLAN_AGENTS:-unknown}" "${PLAN_LIVE:-unknown}" "${obs:-unknown}" "${obs_at:-unknown}" \
    "$(printf '%s' "${P_GATES:-none}" | tr -d '"' | tr ',' ';' | tr -d ' ')" >>"$SAMPLES"
}

# The quiet window is an *observation*, not a sleep: it must be SETTLE seconds of uninterrupted GO.
# Any NO_GO inside it resets the requirement to zero and restarts the full interval.
require_quiet_window() {  # start_epoch label
  local start="$1" label="$2" now
  phase_begin quiet-window "need ${SETTLE}s uninterrupted from $(date -u -d "@$start" +%FT%TZ)"
  while :; do
    now=$(NOW)
    if [ "$now" -ge "$DEADLINE_EPOCH" ]; then
      REASON="deadline reached before ${SETTLE}s of uninterrupted quiescence"; DISPOSITION=STOPPED_SAFE; return 2
    fi
    plan_once
    case "$P_RC" in
      0)
        if ! validate_go; then
          PLAN_STATE="ENTERED_GO"; REASON="$VALIDATE_ERR"; DISPOSITION=NEEDS_HUMAN; return 3
        fi
        # Measure from *after* the probe returns: a slow plan must not inflate the observed
        # window, or the settle interval would be credited with time it never held.
        now=$(NOW)
        if [ -z "$start" ]; then
          start="$now"; log "quiet window restarts at $(date -u -d "@$start" +%FT%TZ)"
        fi
        sample "$label"
        if [ $(( now - start )) -ge "$SETTLE" ]; then
          phase_end quiet-window "held $((now-start))s"
          return 0
        fi
        ;;
      2)
        sample "$label"
        if [ -n "$start" ]; then
          QUIET_RESTARTS=$((QUIET_RESTARTS+1))
          log "quiet window interrupted (gates=${P_GATES:-none}); requirement resets to ${SETTLE}s"
          start=""
        fi
        ;;
      3)
        sample "$label"
        PLAN_STATE="UNEVALUABLE"
        REASON="PLAN UNEVALUABLE (rc=3) — a required gate could not be evaluated"
        DISPOSITION=NEEDS_HUMAN; return 3
        ;;
      *)
        sample "$label"
        REASON="PLAN failed unexpectedly (rc=$P_RC)"; DISPOSITION=NEEDS_HUMAN; return 3
        ;;
    esac
    sleep "$POLL"
  done
}

# ---------------------------------------------------------------- receipt
failed_gates_json() {
  printf '%s' "${PLAN_LAST_GATES:-}" | tr -d '"' | tr ',' '\n' | tr -d ' ' \
    | awk 'NF && $0!="none"{printf "%s\"%s\"",(n++?",":""),$0}'
}

write_summary() {
  local dur=$(( $(NOW) - STARTED_EPOCH ))
  atomic_write "$ATTEMPT_DIR/summary.json" <<EOF
{"attempt_id":"$ATTEMPT_ID","attempt_seq":$ATTEMPT_SEQ,"driver_version":"$DRIVER_VERSION",
 "mode":"$MODE","disposition":"$DISPOSITION","exit_code":$EXIT_CODE,
 "reason":"$(printf '%s' "$REASON" | sed 's/"/\\"/g')",
 "candidate":"$CANDIDATE","expect_head":"$EXPECT_HEAD","expect_tree":"$EXPECT_TREE",
 "install_head":"${INSTALL_HEAD:-unreadable}","install_tree":"${INSTALL_TREE:-unreadable}",
 "quiet_window_s":$QUIET_WINDOW,"poll_s":$POLL,"settle_s":$SETTLE,
 "started_at":"$STARTED_AT","finished_at":"$(TS)","duration_s":$dur,
 "open_at":"$(date -u -d "@$OPEN_EPOCH" +%FT%TZ)","deadline_at":"$(date -u -d "@$DEADLINE_EPOCH" +%FT%TZ)",
 "plan_entered":$([ "$PLAN_ENTERED" = 1 ] && echo true || echo false),"plan_state":"$PLAN_STATE",
 "plan_phase":"${P_PHASE:-legacy}","plan_pending_gates":"$(printf '%s' "${P_PENDING:-}" | sed 's/"/\\"/g')",
 "plan_deployment_ready":"${P_DEPLOY_READY:-unknown}",
 "quiescence_asserted_by_plan":"$([ -n "${P_PHASE:-}" ] && echo false || echo unknown)",
 "plan_rc":"$PLAN_RC","plan_gates":"$(printf '%s' "${PLAN_LAST_GATES:-}" | sed 's/"/\\"/g')",
 "plan_failed_gates":[$(failed_gates_json)],"all_failed_gates_seen":"$(printf '%s' "$ALL_GATES" | sed 's/"/\\"/g')",
 "samples":$SAMPLES_N,"agents_min":"$AGENTS_MIN","agents_max":"$AGENTS_MAX",
 "live_admissions_min":"$LIVE_MIN","live_admissions_max":"$LIVE_MAX",
 "quiescence_only_samples":$QUIESCENCE_ONLY,"other_blocking_gates":"$(printf '%s' "$OTHER_GATES" | sed 's/"/\\"/g')",
 "quiet_window_restarts":$QUIET_RESTARTS,
 "apply_started":$([ "$APPLY_STARTED" = 1 ] && echo true || echo false),"apply_rc":"$APPLY_RC",
 "guards":[$GUARDS],"prior_attempts":[$PRIOR_ATTEMPTS],
 "published":$([ -e "$PUBLISH_MARK" ] && echo true || echo false)}
EOF
}

write_receipt() {
  local apply_line
  {
    printf '# RESTORE-002 release driver receipt\n\n'
    printf -- '- **disposition: `%s`** — %s\n' "$DISPOSITION" "$REASON"
    printf -- '- attempt: `%s` (seq %s, driver %s, mode %s)\n' "$ATTEMPT_ID" "$ATTEMPT_SEQ" "$DRIVER_VERSION" "$MODE"
    printf -- '- started: %s · finished: %s · duration: %ss\n' "$STARTED_AT" "$(TS)" "$(( $(NOW) - STARTED_EPOCH ))"
    printf -- '- candidate: `%s` (tree `%s`)\n- expected installed baseline: `%s` (tree `%s`)\n- observed installed baseline: `%s` (tree `%s`)\n' \
      "$CANDIDATE" "$EXPECT_TREE" "$EXPECT_HEAD" "$EXPECT_TREE" "${INSTALL_HEAD:-unreadable}" "${INSTALL_TREE:-unreadable}"
    printf -- '- window: open %s · deadline %s · quiessence %ss · poll %ss · settle %ss\n' \
      "$(date -u -d "@$OPEN_EPOCH" +%FT%TZ)" "$(date -u -d "@$DEADLINE_EPOCH" +%FT%TZ)" "$QUIET_WINDOW" "$POLL" "$SETTLE"
    printf -- '- PLAN: **%s**' "$PLAN_STATE"
    if [ "$PLAN_ENTERED" = 0 ]; then
      printf -- ' — `PLAN_NOT_ENTERED`: no PLAN evaluation occurred. This is not a PLAN failure and it is not a PLAN pass.\n'
    else
      printf -- ' (last rc=%s)\n' "${PLAN_RC:-none}"
      printf -- '- failed gates (last sample): %s\n' "${PLAN_LAST_GATES:-none}"
      printf -- '- failed gates (union, all samples): %s\n' "${ALL_GATES:-none}"
      if [ -n "${P_PHASE:-}" ]; then
        printf -- '- PLAN phase: `%s` — pre-freeze only. Quiescence gates are **pending**, not passed: %s\n' \
          "$P_PHASE" "${P_PENDING:-none}"
        printf -- '- PLAN deployment_ready: `%s` (a pre-freeze GO never authorises deployment)\n' "${P_DEPLOY_READY:-unknown}"
      fi
    fi
    printf -- '- plan samples: %s (active_agents %s..%s, live_admissions %s..%s); quiescence-only in %s/%s sample(s)%s\n' \
      "$SAMPLES_N" "${AGENTS_MIN:-unknown}" "${AGENTS_MAX:-unknown}" "${LIVE_MIN:-unknown}" "${LIVE_MAX:-unknown}" \
      "$QUIESCENCE_ONLY" "$SAMPLES_N" \
      "$([ -n "$OTHER_GATES" ] && printf '; other gates seen: %s' "$OTHER_GATES" || printf '; no non-quiescence gate failed')"
    printf -- '- quiet-window restarts caused by interruption: %s\n' "$QUIET_RESTARTS"
    apply_line=$([ "$APPLY_STARTED" = 1 ] && echo 'STARTED once' || echo 'never started')
    printf -- '- apply: %s (rc=%s)\n' "$apply_line" "${APPLY_RC:-n/a}"
    printf -- '- mutation performed by this driver: %s\n' \
      "$([ "$APPLY_STARTED" = 1 ] && echo 'apply was invoked; see the runner receipt' || echo 'none')"
    if [ -n "$PRIOR_ATTEMPTS" ]; then
      printf -- '- prior attempts in this evidence directory: %s\n' "$PRIOR_ATTEMPTS"
      printf -- '- **a prior attempt invoked apply: %s**\n' "$([ "$PRIOR_APPLY_SEEN" = 1 ] && echo 'yes — this run refuses to apply again' || echo 'no')"
    fi
    printf -- '- guards: %s\n' "$GUARDS"
    printf -- '- evidence: `driver.log`, `samples.log`, `phases.log`, `summary.json`, `attempts/%s/`\n' "$ATTEMPT_ID"
    if [ "$APPLY_STARTED" = 1 ]; then
      printf -- '\nOperational verification is **not** asserted here: the authoritative receipt is the\nrelease runner'"'"'s `RECEIPT.md` under the release archive. This driver records only that apply\nwas invoked and its exit code.\n'
    else
      printf -- '\nNo deployment mutation occurred: the launcher'"'"'s `apply` was never invoked, the gateway\nwas never stopped, and the installed baseline above is unchanged by this run.\n'
    fi
    printf -- '\n## Phase log\n\n```\n'; cat "$PHASES"; printf '```\n'
    printf -- '\n## Samples (last 10)\n\n```\n'; tail -10 "$SAMPLES"; printf '```\n'
  } | atomic_write "$ATTEMPT_DIR/RECEIPT.md" || return 1
  atomic_copy "$ATTEMPT_DIR/RECEIPT.md" "$RECEIPT" || return 1
  atomic_copy "$ATTEMPT_DIR/summary.json" "$SUMMARY" || return 1
  return 0
}

publish() {
  [ "$PUBLISH" = 1 ] || { log "publish disabled (evidence kept in $EVIDENCE_DIR)"; return 0; }
  [ -e "$PUBLISH_MARK" ] && { log "already published; not publishing twice"; return 0; }
  [ -n "$RECEIPT_REPO" ] && [ -n "$RECEIPT_PR" ] || { log "no receipt target; publication skipped"; return 1; }
  if gh api -X POST "repos/$RECEIPT_REPO/issues/$RECEIPT_PR/comments" -F body=@"$RECEIPT" >>"$LOG" 2>&1; then
    : >"$PUBLISH_MARK"; sync 2>/dev/null || true
    log "receipt published to $RECEIPT_REPO#$RECEIPT_PR"
  else
    log "PUBLICATION FAILED — receipt remains durable at $RECEIPT"; return 1
  fi
}

finalize() {
  local rc=$?
  trap - EXIT INT TERM
  case "$DISPOSITION" in
    GO_READY|APPLIED) EXIT_CODE=0;;
    STOPPED_SAFE) EXIT_CODE=2;;
    GUARD_FAILED) EXIT_CODE=4;;
    *) EXIT_CODE=3;;
  esac
  if [ -e "$ATTEMPT_DIR/.finalised" ]; then
    log "attempt $ATTEMPT_ID already finalised; not rewriting its receipt"
    exit "${FORCED_RC:-$rc}"
  fi
  # The attempt is marked finalised *only* when its receipt actually landed. An unwritable
  # evidence directory must leave the attempt visibly incomplete, so a later resume reports it
  # instead of reading a marker that says the record is whole.
  local wrote=0
  write_attempt_json final || wrote=1
  write_summary || wrote=1
  write_receipt || wrote=1
  if [ "$wrote" = 0 ]; then
    : >"$ATTEMPT_DIR/.finalised"; sync 2>/dev/null || true
    publish || true
  else
    log "RECEIPT WRITE FAILED — attempt $ATTEMPT_ID is NOT finalised; evidence dir must be writable"
    EXIT_CODE=3
  fi
  exit "${FORCED_RC:-$EXIT_CODE}"
}
trap finalize EXIT
trap 'if [ "$APPLY_STARTED" = 1 ]; then REASON="interrupted during apply — state must be read from the runner journal";
      DISPOSITION=NEEDS_HUMAN; FORCED_RC=3; exit 3; else REASON="interrupted by signal";
      DISPOSITION=STOPPED_SAFE; FORCED_RC=2; exit 2; fi' INT TERM

OPEN_EPOCH=$(epoch_of "$OPEN_AFTER")
DEADLINE_EPOCH=$(epoch_of "$DEADLINE")
: >"$SAMPLES"; : >"$PHASES"

# Even a window that is rejected before PLAN gets an attempt identity and a durable receipt: an
# operator must never have to guess whether a silent driver ran.
early_stop() {  # reason
  REASON="$1"; DISPOSITION=NEEDS_HUMAN
  INSTALL_HEAD=$(git -C "$INSTALL" rev-parse HEAD 2>/dev/null)
  INSTALL_TREE=$(git -C "$INSTALL" rev-parse 'HEAD^{tree}' 2>/dev/null)
  compute_attempt
  phase start "rejected-before-window"
  write_attempt_json begin
  phase window-rejected "$REASON"
  exit 3
}

if [ "$OPEN_EPOCH" -le 0 ] || [ "$DEADLINE_EPOCH" -le 0 ]; then
  early_stop "unparseable window"
fi
if [ "$DEADLINE_EPOCH" -le "$(NOW)" ]; then
  early_stop "the requested window has already closed"
fi
if [ "$DEADLINE_EPOCH" -le "$OPEN_EPOCH" ]; then
  early_stop "deadline precedes open-after"
fi

phase start "open=$(date -u -d "@$OPEN_EPOCH" +%FT%TZ) deadline=$(date -u -d "@$DEADLINE_EPOCH" +%FT%TZ)"
collect_guards
compute_attempt
write_attempt_json begin
scan_prior_attempts
phase attempt "id=$ATTEMPT_ID seq=$ATTEMPT_SEQ"
phase guards "$(guards_ok && echo pass || echo fail)"
if ! guards_ok; then REASON="pre-mutation guard failed"; DISPOSITION=GUARD_FAILED; exit 4; fi
if [ "$MODE" = apply ] && [ "$PRIOR_APPLY_SEEN" = 1 ]; then
  REASON="a prior attempt in this evidence directory invoked apply — refusing to apply twice"
  DISPOSITION=NEEDS_HUMAN; exit 3
fi

while [ "$(NOW)" -lt "$OPEN_EPOCH" ]; do
  remaining=$(( OPEN_EPOCH - $(NOW) ))
  if [ "$remaining" -gt 30 ]; then sleep 30; elif [ "$remaining" -gt 0 ]; then sleep "$remaining"; else sleep 1; fi
done
phase window-open

while :; do
  now=$(NOW)
  if [ "$now" -ge "$DEADLINE_EPOCH" ]; then
    REASON="deadline reached without PLAN GO"; DISPOSITION=STOPPED_SAFE; exit 2
  fi
  plan_once
  case "$P_RC" in
    0) if ! validate_go; then PLAN_STATE="ENTERED_GO"; REASON="$VALIDATE_ERR"; DISPOSITION=NEEDS_HUMAN; exit 3; fi
       PLAN_STATE="ENTERED_GO"; sample first-go; phase plan-go "rc=0"; GO_EPOCH=$now; break;;
    2) PLAN_STATE="ENTERED_NO_GO"; sample poll
       log "plan NO_GO gates=${P_GATES:-unknown}; continuing to poll";;
    3) PLAN_STATE="UNEVALUABLE"; sample poll
       if [ "$ALLOW_UNEVALUABLE_RETRY" = 1 ]; then log "plan UNEVALUABLE; retry permitted by flag"
       else REASON="PLAN UNEVALUABLE (rc=3) — a required gate could not be evaluated"; DISPOSITION=NEEDS_HUMAN; exit 3; fi;;
    *) sample poll; REASON="PLAN failed unexpectedly (rc=$P_RC)"; DISPOSITION=NEEDS_HUMAN; exit 3;;
  esac
  sleep "$POLL"
done

if [ "$MODE" = plan ]; then
  # EXT-16: a pre-freeze GO qualifies the *sequence* to attempt a freeze; it is not deployment-ready
  # and it does not assert quiescence, which can only be proven inside the freeze.
  if [ -n "${P_PHASE:-}" ]; then
    REASON="PLAN ${P_VERDICT:-GO} in plan mode (phase=${P_PHASE}); pre-freeze only — quiescence is not asserted and nothing was applied"
  else
    REASON="PLAN GO in plan mode; nothing applied"
  fi
  DISPOSITION=GO_READY; exit 0
fi

# EXT-16: for a phase-aware engine the pre-freeze settle wait is removed — it was the ordering defect
# (it required quiescence that only an engaged freeze can produce) and the runner now proves
# quiescence inside the freeze with a stricter gate set. A legacy launcher keeps the old wait.
if [ -n "${P_PHASE:-}" ]; then
  phase settle "skipped: quiescence is proven inside the release freeze by the runner (EXT-16)"
  journal_note="driver skipped pre-freeze settle for a phase-aware engine"
  log "$journal_note"
else
  require_quiet_window "$GO_EPOCH" quiet
  case "$?" in
    0) ;;
    2) exit 2;;
    *) exit 3;;
  esac
fi

phase apply "single application of $CANDIDATE"
APPLY_STARTED=1
write_attempt_json begin
"$LAUNCHER" apply --candidate "$CANDIDATE" >>"$LOG" 2>&1; APPLY_RC=$?
case "$APPLY_RC" in
  0) REASON="apply returned 0; operational verification is the runner receipt"; DISPOSITION=APPLIED;;
  2|65) REASON="apply refused before mutation (rc=$APPLY_RC)"; DISPOSITION=STOPPED_SAFE;;
  *) REASON="apply failed (rc=$APPLY_RC) — see runner receipt and journal"; DISPOSITION=NEEDS_HUMAN;;
esac
exit 0
