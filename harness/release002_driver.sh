#!/usr/bin/env bash
# release002_driver.sh — the supervised, detached RESTORE-002 release driver.
#
# Why this exists: the EXT-14 driver ran outside the gateway cgroup (the release restarts the
# gateway, so a driver inside it would be killed mid-release) and it *did* stop safely — but it
# exited STOPPED_SAFE with no receipt, because its receipt writer only existed after APPLY. The
# absence of a published receipt then had to be reconciled from host evidence after the fact.
#
# Contract enforced here: EVERY terminal result — guard failure, deadline, unevaluable plan,
# interrupted, applied — writes a durable receipt (summary.json + RECEIPT.md) into the evidence
# directory, exactly once, and publishes it at most once. This is a *observability* correction:
# it never shortens the quiet window, never aborts a turn to manufacture quiescence, never retries
# a deployment, and never applies without an explicit PLAN GO.
#
#   release002_driver.sh --mode plan|apply \
#     --candidate SHA --expect-head SHA --expect-tree TREE --install DIR --launcher PATH \
#     --evidence-dir DIR --open-after HH:MM|+SECS --deadline HH:MM|+SECS \
#     [--quiet-window 130] [--poll 20] [--settle 130] \
#     [--receipt-repo OWNER/REPO --receipt-pr N --publish 0|1] \
#     [--service hermes-gateway.service] [--journal PATH] [--unit-dir DIR] [--proc-root /proc]
#
# Exit codes: 0 GO_READY (plan) / APPLIED (apply rc 0) · 2 STOPPED_SAFE · 3 NEEDS_HUMAN
#             4 GUARD_FAILED · 64 usage.
#
# The driver's own receipt is *not* an operational verification. When it applies, the release
# runner writes the authoritative RECEIPT.md under the release archive; this driver records the
# apply exit code and points at it, and never claims RELEASED on its own.
set -uo pipefail

DRIVER_VERSION="ext15.1"
TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "[$(TS)]" "$*" | tee -a "${LOG:-/dev/null}" >&2; }

usage() {
  sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
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

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
LOG="$EVIDENCE_DIR/driver.log"
SAMPLES="$EVIDENCE_DIR/samples.log"
PHASES="$EVIDENCE_DIR/phases.log"
SUMMARY="$EVIDENCE_DIR/summary.json"
RECEIPT="$EVIDENCE_DIR/RECEIPT.md"
FINAL_MARK="$EVIDENCE_DIR/.finalised"
PUBLISH_MARK="$EVIDENCE_DIR/.published"
: >"$SAMPLES"; : >"$PHASES"; : >"$LOG"

DISPOSITION="NEEDS_HUMAN"
REASON="driver did not reach a terminal state"
PLAN_RC=""; PLAN_LAST_GATES=""; PLAN_LAST_AGENTS=""
APPLY_STARTED=0; APPLY_RC=""
GUARDS=""
SAMPLES_N=0; AGENTS_MIN=""; AGENTS_MAX=""; QUIESCENCE_ONLY=0; OTHER_GATES=""

phase() { printf '%s phase=%s %s\n' "$(TS)" "$1" "${2:-}" >>"$PHASES"; log "phase=$1 ${2:-}"; }

epoch_of() {  # "HH:MM" (local, today; tomorrow if already past) or "+SECS"
  case "$1" in
    +*) date -u +%s | awk -v d="${1#+}" '{print $1+d}';;
    *) date -d "today $1" +%s 2>/dev/null || { date -d "$1" +%s 2>/dev/null || echo 0; };;
  esac
}

guard() {  # name ok detail
  GUARDS="${GUARDS}${GUARDS:+,}{\"name\":\"$1\",\"ok\":$2,\"detail\":\"$(printf '%s' "$3" | sed 's/"/\\"/g')\"}"
  [ "$2" = true ] || log "GUARD FAILED $1: $3"
}

collect_guards() {
  local head units mainpid hold dropin
  head=$(git -C "$INSTALL" rev-parse HEAD 2>/dev/null)
  guard install_head "$([ "$head" = "$EXPECT_HEAD" ] && echo true || echo false)" "install HEAD=${head:-unreadable} expected=$EXPECT_HEAD"
  guard journal_absent "$([ -e "$JOURNAL" ] && echo false || echo true)" "journal=$JOURNAL present=$([ -e "$JOURNAL" ] && echo yes || echo no)"
  units=$(systemctl list-units --all --no-legend 'hermes-release-*' 2>/dev/null | wc -l | tr -d ' ')
  guard no_release_units "$([ "${units:-0}" = 0 ] && echo true || echo false)" "hermes-release-* units=${units:-0}"
  mainpid=$(systemctl show "$SERVICE" -p MainPID --value 2>/dev/null)
  hold=$(sudo -n tr '\0' '\n' <"$PROC_ROOT/${mainpid:-0}/environ" 2>/dev/null | grep -c '^HERMES_RECOVERY_HOLD=1$')
  guard recovery_hold "$([ "${hold:-0}" -ge 1 ] && echo true || echo false)" "mainpid=${mainpid:-none} HERMES_RECOVERY_HOLD=${hold:-0}"
  dropin=0
  [ -e "$UNIT_DIR/$SERVICE.d/90-recovery-hold.conf" ] && dropin=$((dropin+1))
  [ -e "$UNIT_DIR/$SERVICE.d/95-timeout-stop.conf" ] && dropin=$((dropin+1))
  guard protective_dropins "$([ "$dropin" = 2 ] && echo true || echo false)" "drop-ins present=${dropin}/2"
}

guards_ok() { ! printf '%s' "$GUARDS" | grep -q '"ok":false'; }

# Classify a PLAN "failed" list. The approved sequence evaluates the quiescence gates *before* the
# freeze that produces quiescence, so a pre-freeze NO_GO caused only by those gates is expected and
# must be reported as such rather than as an ordinary gate failure. Classification is evidence only:
# it never turns a NO_GO into a GO and never changes the sequence.
classify_gates() {  # raw-failed-list -> quiescence | other | none
  local cleaned tok q=0 other=0
  cleaned=$(printf '%s' "${1:-}" | tr -d '"' | tr ',' '\n' | tr -d ' ')
  while [ -n "$cleaned" ]; do
    tok=${cleaned%%$'\n'*}
    case "$tok" in
      ''|none) ;;
      no_active_turns|recent_session_activity) q=$((q+1));;
      *) other=$((other+1)); OTHER_GATES="$OTHER_GATES${OTHER_GATES:+, }$tok";;
    esac
    case "$cleaned" in *$'\n'*) cleaned=${cleaned#*$'\n'};; *) cleaned="";; esac
  done
  # Sets BLOCKER in the *current* shell: a $(...) call would run in a subshell and lose OTHER_GATES.
  if [ "$other" -gt 0 ]; then BLOCKER=other; elif [ "$q" -gt 0 ]; then BLOCKER=quiescence; else BLOCKER=none; fi
}

sample() {  # plan_rc active_agents gates blocker
  SAMPLES_N=$((SAMPLES_N+1))
  if [ "${4:-none}" = quiescence ]; then QUIESCENCE_ONLY=$((QUIESCENCE_ONLY+1)); fi
  local obs="unknown" obs_at="unknown"
  local a="$2"
  case "$a" in ''|*[!0-9]*) a="";; esac
  if [ -n "$a" ]; then
    if [ -z "$AGENTS_MIN" ] || [ "$a" -lt "$AGENTS_MIN" ]; then AGENTS_MIN="$a"; fi
    if [ -z "$AGENTS_MAX" ] || [ "$a" -gt "$AGENTS_MAX" ]; then AGENTS_MAX="$a"; fi
  fi
  # The plan verdict alone truncates the per-sample facts, which is why the EXT-14 window could
  # not be attributed after the fact. Record the published runtime snapshot too: active_agents and
  # its updated_at, so a receipt shows what the gateway itself thought was running when PLAN said
  # NO_GO. This is evidence capture only — the verdict stays PLAN's.
  if [ -n "$GATEWAY_STATE" ] && [ -r "$GATEWAY_STATE" ]; then
    obs=$(sed -n 's/.*"active_agents": *\([0-9][0-9]*\).*/\1/p' "$GATEWAY_STATE" 2>/dev/null | head -1)
    obs_at=$(sed -n 's/.*"updated_at": *"\([^"]*\)".*/\1/p' "$GATEWAY_STATE" 2>/dev/null | head -1)
  fi
  printf '%s plan_rc=%s blocker=%s active_agents=%s observed_agents=%s observed_at=%s gates=%s\n' \
    "$(TS)" "$1" "${4:-none}" "${a:-unknown}" "${obs:-unknown}" "${obs_at:-unknown}" "$3" >>"$SAMPLES"
}

write_receipt() {
  {
    printf '# RESTORE-002 release driver receipt\n\n'
    printf -- '- run: `%s` (driver %s, mode %s)\n' "$RUN_ID" "$DRIVER_VERSION" "$MODE"
    printf -- '- **disposition: `%s`** — %s\n' "$DISPOSITION" "$REASON"
    printf -- '- candidate: `%s`\n- expected installed baseline: `%s`\n- candidate tree: `%s`\n' \
      "$CANDIDATE" "$EXPECT_HEAD" "$EXPECT_TREE"
    printf -- '- quiet window required: %ss (poll %ss, settle %ss)\n' "$QUIET_WINDOW" "$POLL" "$SETTLE"
    printf -- '- plan samples: %s (active_agents min/max %s/%s); last plan rc=%s gates=%s\n' \
      "$SAMPLES_N" "${AGENTS_MIN:-unknown}" "${AGENTS_MAX:-unknown}" "${PLAN_RC:-none}" "${PLAN_LAST_GATES:-none}"
    printf -- '- plan blockers: quiescence-only in %s/%s sample(s)%s\n' \
      "$QUIESCENCE_ONLY" "$SAMPLES_N" "$([ -n "$OTHER_GATES" ] && printf '; other gates seen: %s' "$OTHER_GATES" || printf '; no non-quiescence gate failed')"
    printf -- '- apply: %s (rc=%s)\n' \
      "$([ "$APPLY_STARTED" = 1 ] && echo 'STARTED once' || echo 'never started')" "${APPLY_RC:-n/a}"
    printf -- '- mutation performed by this driver: %s\n' \
      "$([ "$APPLY_STARTED" = 1 ] && echo 'apply was invoked; see the runner receipt' || echo 'none')"
    printf -- '- guards: %s\n' "$GUARDS"
    printf -- '- evidence: `driver.log`, `samples.log`, `phases.log`, `summary.json`\n'
    if [ "$APPLY_STARTED" = 1 ]; then
      printf -- '\nOperational verification is **not** asserted here: the authoritative receipt is the\nrelease runner'"'"'s `RECEIPT.md` under the release archive. This driver records only that apply\nwas invoked and its exit code.\n'
    else
      printf -- '\nNo deployment mutation occurred: the launcher'"'"'s `apply` was never invoked, the gateway\nwas never stopped, and the installed baseline above is unchanged by this run.\n'
    fi
    printf -- '\n## Phase log\n\n```\n'; cat "$PHASES"; printf '```\n'
    printf -- '\n## Samples (last 10)\n\n```\n'; tail -10 "$SAMPLES"; printf '```\n'
  } >"$RECEIPT"
}

write_summary() {
  cat >"$SUMMARY" <<EOF
{"run_id":"$RUN_ID","driver_version":"$DRIVER_VERSION","mode":"$MODE","disposition":"$DISPOSITION",
 "reason":"$(printf '%s' "$REASON" | sed 's/"/\\"/g')","candidate":"$CANDIDATE","expect_head":"$EXPECT_HEAD",
 "expect_tree":"$EXPECT_TREE","quiet_window_s":$QUIET_WINDOW,"poll_s":$POLL,"settle_s":$SETTLE,
 "samples":$SAMPLES_N,"agents_min":"$AGENTS_MIN","agents_max":"$AGENTS_MAX",
 "quiescence_only_samples":$QUIESCENCE_ONLY,"other_blocking_gates":"$OTHER_GATES",
 "plan_rc":"$PLAN_RC","plan_gates":"$(printf '%s' "$PLAN_LAST_GATES" | sed 's/"/\\"/g')",
 "apply_started":$([ "$APPLY_STARTED" = 1 ] && echo true || echo false),"apply_rc":"$APPLY_RC","guards":[$GUARDS],
 "published":$([ -e "$PUBLISH_MARK" ] && echo true || echo false),"finished_at":"$(TS)"}
EOF
}

publish() {
  [ "$PUBLISH" = 1 ] || { log "publish disabled (evidence kept in $EVIDENCE_DIR)"; return 0; }
  [ -e "$PUBLISH_MARK" ] && { log "already published; not publishing twice"; return 0; }
  [ -n "$RECEIPT_REPO" ] && [ -n "$RECEIPT_PR" ] || { log "no receipt target; publication skipped"; return 1; }
  if gh api -X POST "repos/$RECEIPT_REPO/issues/$RECEIPT_PR/comments" -F body=@"$RECEIPT" >>"$LOG" 2>&1; then
    : >"$PUBLISH_MARK"; log "receipt published to $RECEIPT_REPO#$RECEIPT_PR"
  else
    log "PUBLICATION FAILED — receipt remains durable at $RECEIPT"; return 1
  fi
}

finalize() {
  local rc=$?
  trap - EXIT INT TERM
  [ -e "$FINAL_MARK" ] && exit "$rc"
  : >"$FINAL_MARK"
  phase finalize "disposition=$DISPOSITION"
  write_summary
  write_receipt
  publish || true
  case "$DISPOSITION" in
    GO_READY|APPLIED) exit 0;;
    STOPPED_SAFE) exit 2;;
    GUARD_FAILED) exit 4;;
    *) exit 3;;
  esac
}
trap finalize EXIT
trap 'if [ "$APPLY_STARTED" = 1 ]; then REASON="interrupted during apply — state must be read from the runner journal";
      DISPOSITION="NEEDS_HUMAN"; exit 3; else REASON="interrupted by signal"; DISPOSITION="STOPPED_SAFE"; exit 2; fi' INT TERM

OPEN_EPOCH=$(epoch_of "$OPEN_AFTER")
DEADLINE_EPOCH=$(epoch_of "$DEADLINE")
[ "$OPEN_EPOCH" -gt 0 ] && [ "$DEADLINE_EPOCH" -gt 0 ] || { REASON="unparseable window"; DISPOSITION="NEEDS_HUMAN"; exit 3; }
if [ "$DEADLINE_EPOCH" -le "$(date -u +%s)" ]; then
  REASON="the requested window has already closed"; DISPOSITION="NEEDS_HUMAN"; exit 3
fi
if [ "$DEADLINE_EPOCH" -le "$OPEN_EPOCH" ]; then
  REASON="deadline precedes open-after"; DISPOSITION="NEEDS_HUMAN"; exit 3
fi

phase start "open=$(date -u -d "@$OPEN_EPOCH" +%FT%TZ) deadline=$(date -u -d "@$DEADLINE_EPOCH" +%FT%TZ)"
collect_guards
phase guards "$(guards_ok && echo pass || echo fail)"
if ! guards_ok; then REASON="pre-mutation guard failed"; DISPOSITION="GUARD_FAILED"; exit 4; fi

while [ "$(date -u +%s)" -lt "$OPEN_EPOCH" ]; do
  remaining=$(( OPEN_EPOCH - $(date -u +%s) ))
  if [ "$remaining" -gt 30 ]; then sleep 30; elif [ "$remaining" -gt 0 ]; then sleep "$remaining"; else sleep 1; fi
done
phase window-open

while :; do
  now=$(date -u +%s)
  if [ "$now" -ge "$DEADLINE_EPOCH" ]; then
    REASON="deadline reached without PLAN GO"; DISPOSITION="STOPPED_SAFE"; exit 2
  fi
  out=$("$LAUNCHER" plan --candidate "$CANDIDATE" 2>&1); PLAN_RC=$?
  PLAN_LAST_GATES=$(printf '%s' "$out" | sed -n 's/.*"failed": *\[\([^]]*\)\].*/\1/p' | head -1)
  agents=$(printf '%s' "$out" | sed -n 's/.*"active_agents": *\([0-9][0-9]*\).*/\1/p' | head -1)
  BLOCKER=none; classify_gates "${PLAN_LAST_GATES:-}"; blocker=$BLOCKER
  sample "$PLAN_RC" "$agents" "${PLAN_LAST_GATES:-none}" "$blocker"
  case "$PLAN_RC" in
    0) phase plan-go "rc=0"; break;;
    2) log "plan NO_GO blocker=${blocker} gates=${PLAN_LAST_GATES:-unknown}; continuing to poll";;
    3) if [ "$ALLOW_UNEVALUABLE_RETRY" = 1 ]; then log "plan UNEVALUABLE; retry permitted by flag"
       else REASON="PLAN UNEVALUABLE (rc=3) — a required gate could not be evaluated"; DISPOSITION="NEEDS_HUMAN"; exit 3; fi;;
    *) REASON="PLAN failed unexpectedly (rc=$PLAN_RC)"; DISPOSITION="NEEDS_HUMAN"; exit 3;;
  esac
  sleep "$POLL"
done

if [ "$MODE" = plan ]; then REASON="PLAN GO in plan mode; nothing applied"; DISPOSITION="GO_READY"; exit 0; fi

# Settle: one more PLAN over a quiet interval before the single mutation.
phase settle "waiting ${SETTLE}s after GO"
sleep "$SETTLE"
out=$("$LAUNCHER" plan --candidate "$CANDIDATE" 2>&1); PLAN_RC=$?
agents=$(printf '%s' "$out" | sed -n 's/.*"active_agents": *\([0-9][0-9]*\).*/\1/p' | head -1)
sample "$PLAN_RC" "$agents" "settle-recheck"
if [ "$PLAN_RC" -ne 0 ]; then
  REASON="quiescence lost during settle (PLAN rc=$PLAN_RC) — no mutation attempted"; DISPOSITION="STOPPED_SAFE"; exit 2
fi

phase apply "single application of $CANDIDATE"
APPLY_STARTED=1
"$LAUNCHER" apply --candidate "$CANDIDATE" >>"$LOG" 2>&1; APPLY_RC=$?
case "$APPLY_RC" in
  0) REASON="apply returned 0; operational verification is the runner receipt"; DISPOSITION="APPLIED";;
  2|65) REASON="apply refused before mutation (rc=$APPLY_RC)"; DISPOSITION="STOPPED_SAFE";;
  *) REASON="apply failed (rc=$APPLY_RC) — see runner receipt and journal"; DISPOSITION="NEEDS_HUMAN";;
esac
exit 0
