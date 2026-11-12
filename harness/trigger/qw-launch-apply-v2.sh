#!/bin/bash
# RESTORE-002 quiet-window launcher — v2 (EXT-17A correction; NOT installed)
#
# v1 lives at /usr/local/lib/hermes-release/qw-launch-apply.sh (root-owned, 0700) and stays exactly as
# it is: EXT-17A has no authority to change it. This file is the versioned correction an owner can
# review and install later; see harness/trigger/INSTALL-v2.md for the procedure and the diffs.
#
# What v1 got wrong, and what v2 does instead:
#
#  1. v1 passed `GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'` to the launcher, i.e. it
#     re-opened the wildcard-trust hole that R-12 had closed at /etc/gitconfig with two explicit
#     paths. v2 passes exactly those two paths and nothing else.
#  2. v1 exited 0 whatever the launcher's exit status was, and wrote attempt-fired.json even when
#     the launcher had refused to start anything — a false record of a spent attempt. v2 records the
#     attempt as `pending` *before* the call (so a crash mid-call cannot lose the fact that it fired),
#     rewrites it to `fired` or `failed` afterwards, and exits non-zero when nothing started.
#  3. v1's only retry guard was the engine journal, which the engine creates *after* the launcher
#     returns; a crash in that window left a re-armable host. v2 refuses to arm on either artefact.
#  4. v1 could overshoot its deadline by a full sample interval and logged nothing when it was
#     terminated mid-wait. v2 clamps the sleep to the deadline and disarms on TERM/INT with a record.
#  5. v1 hardcoded the candidate, so a re-arm after a correction would have replayed the old
#     identity. v2 requires the candidate (and its expected install head) on the command line.
#
# It adds no gate and bypasses none: it only decides WHEN to make the single invocation, and the
# qualified launcher still enforces every existing budget and gate unchanged.
set -u

QUALIFY=${TRIGGER_QUALIFY:-0}

if [ "$QUALIFY" = 1 ]; then
  # Qualification-only overrides. A real arming cannot move the state dir, the observation
  # instruments or the budgets through the environment.
  STATE=${TRIGGER_STATE:?TRIGGER_STATE required with TRIGGER_QUALIFY=1}
  PW=${TRIGGER_PW:?TRIGGER_PW required with TRIGGER_QUALIFY=1}
  DB=${TRIGGER_DB:?TRIGGER_DB required with TRIGGER_QUALIFY=1}
  LAUNCHER=${TRIGGER_LAUNCHER:?TRIGGER_LAUNCHER required with TRIGGER_QUALIFY=1}
  NEED=${TRIGGER_NEED:-6}
  INTERVAL=${TRIGGER_INTERVAL:-20}
  DEADLINE_S=${TRIGGER_DEADLINE_S:-14400}
else
  STATE=/var/lib/hermes-release/restore-002
  PW=/home/hermes/.hermes/gateway_state.json
  DB=/home/hermes/.hermes/state.db
  LAUNCHER=/usr/local/lib/hermes-release/hermes-release-002
  NEED=6              # consecutive zero-activity samples before firing (120 s at 20 s intervals)
  INTERVAL=20         # seconds between samples
  DEADLINE_S=14400    # 4 h; disarm without applying if no window appears
fi

LOG=$STATE/qw-launcher.log
FIRED=$STATE/attempt-fired.json
JOURNAL=$STATE/journal.json
LOCK=$STATE/qw-launcher.lock

CAND=""
EXPECT_HEAD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --candidate) CAND=${2:-}; shift 2 ;;
    --expect-head) EXPECT_HEAD=${2:-}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG"; }

mkdir -p "$STATE" || { echo "cannot create $STATE" >&2; exit 64; }
[ -n "$CAND" ] || { log "REFUSE: --candidate is required"; exit 64; }
case "$CAND" in
  *[!0-9a-f]*) log "REFUSE: candidate '$CAND' is not a lowercase hex object name"; exit 64 ;;
esac
[ "${#CAND}" -eq 40 ] || { log "REFUSE: candidate '$CAND' is not 40 hex characters"; exit 64; }
[ -n "$EXPECT_HEAD" ] || { log "REFUSE: --expect-head is required (the install this attempt is for)"; exit 64; }

if [ ! -f "$PW" ]; then log "REFUSE: no gateway state file at $PW"; exit 64; fi
if [ ! -f "$DB" ]; then log "REFUSE: no session database at $DB"; exit 64; fi

exec 9>"$LOCK"
flock -n 9 || { log "REFUSE: another quiet-window launcher holds the lock"; exit 75; }

# A second arming is not a retry: the attempt is fired once or not at all.
if [ -e "$JOURNAL" ]; then
  log "REFUSE: attempt journal already present at $JOURNAL"
  exit 76
fi
if [ -e "$FIRED" ]; then
  log "REFUSE: attempt-fired record already present at $FIRED"
  exit 76
fi
if systemctl list-units --all --no-legend 'hermes-release-*' 2>/dev/null | grep -q '\.service'; then
  log "REFUSE: a hermes-release-* unit already exists"
  exit 77
fi

write_fired() { # write_fired <status> <apply_rc> <run_id>  — atomic, so a crash cannot truncate it
  local tmp="$STATE/.attempt-fired.$$.json"
  cat >"$tmp" <<EOF
{
  "mission": "RESTORE-002",
  "attempt_label": "PRODEXEC01-ATTEMPT-1",
  "candidate_commit": "$CAND",
  "expect_install_head": "$EXPECT_HEAD",
  "status": "$1",
  "apply_rc": $2,
  "engine_run_id": "$3",
  "fired_at": "$(date -u +%FT%TZ)",
  "quiet_samples": $NEED,
  "sample_interval_s": $INTERVAL,
  "launcher": "$LAUNCHER",
  "admission_read_mode": "mode=ro",
  "trigger_version": "v2.1"
}
EOF
  mv -f "$tmp" "$FIRED" && chmod 0644 "$FIRED"
}

DISARMED=0
on_signal() { # a term/INT before the fire is a disarm, never a silent exit
  local sig=$1
  if [ "$DISARMED" -eq 0 ]; then
    DISARMED=1
    log "DISARMED by SIG$sig before firing — attempt unspent, no retry"
  fi
  exit 143
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

start=$(date +%s); end=$((start + DEADLINE_S))
log "ARM v2.1 label=PRODEXEC01-ATTEMPT-1 candidate=$CAND expect_head=$EXPECT_HEAD need=$NEED interval=${INTERVAL}s deadline_s=$DEADLINE_S"

# Admission observation. `immutable=1` reads the main database file and ignores the -wal, so an
# admission committed since the last checkpoint is invisible and a multi-sample window can qualify
# over live work. `mode=ro` participates in WAL indexing, so it is the only read allowed to prove
# quiet. Every failure — unopenable, unparseable, absent epoch, an undated row, a stale-epoch row too
# young to be provably dead — is a refusal, never a zero.
ADMISSION_SQL="
select (select epoch from runtime_epoch where singleton=1),
       (select count(*) from session_admissions
         where status in ('queued','started','unknown')
           and owner_epoch=(select epoch from runtime_epoch where singleton=1)),
       (select count(*) from session_admissions
         where status in ('queued','started','unknown')
           and owner_epoch>(select epoch from runtime_epoch where singleton=1)),
       (select count(*) from session_admissions a
          left join sessions s on s.id=a.target_session_id
         where a.status in ('queued','started','unknown')
           and a.owner_epoch<(select epoch from runtime_epoch where singleton=1)
           and (s.last_activity_at is null
                or (cast(strftime('%s','now') as real)-s.last_activity_at) <= 21600)),
       (select count(*) from session_admissions a
          left join sessions s on s.id=a.target_session_id
         where a.status in ('queued','started','unknown')
           and a.owner_epoch<(select epoch from runtime_epoch where singleton=1)
           and s.last_activity_at is not null
           and (cast(strftime('%s','now') as real)-s.last_activity_at) > 21600),
       (select count(*) from session_admissions
         where status in ('queued','started','unknown') and owner_epoch is null);"

# A stale-epoch row is only provably dead once its session activity is older than the engine's own
# stale-admission horizon (release002_control STALE_ADMISSION_HOURS = 6). Younger ones might belong
# to a runtime that has not finished draining, so they block quiet instead of being written off.
ADM_STALE_HORIZON_S=21600

observe_admissions() { # sets ADM_STATE + six counts; returns 1 only when nothing could be read
  local raw f
  ADM_STATE=unreadable
  raw=$(sqlite3 "file:$DB?mode=ro" "$ADMISSION_SQL" 2>/dev/null) || return 1
  case "$raw" in '' | *[!0-9'|']*) return 1 ;; esac
  IFS='|' read -r ADM_EPOCH ADM_LIVE ADM_AHEAD ADM_STALE_RECENT ADM_STALE_EXPIRED ADM_UNDATED <<<"$raw"
  for f in "$ADM_EPOCH" "$ADM_LIVE" "$ADM_AHEAD" "$ADM_STALE_RECENT" "$ADM_STALE_EXPIRED" "$ADM_UNDATED"; do
    case "$f" in '' | *[!0-9]*) return 1 ;; esac
  done
  [ "$ADM_EPOCH" -ge 1 ] || return 1
  if [ "$ADM_LIVE" -ne 0 ] || [ "$ADM_AHEAD" -ne 0 ] || \
     [ "$ADM_STALE_RECENT" -ne 0 ] || [ "$ADM_UNDATED" -ne 0 ]; then
    ADM_STATE=busy
    return 0
  fi
  ADM_STATE=quiet
  return 0
}

wal_bytes() { stat -c %s "$DB-wal" 2>/dev/null || echo 0; }

# The instrument must work before an attempt is armed: a store that cannot be read cannot be
# qualified quiet, and arming blind would spend the single attempt on a guess.
if ! observe_admissions; then
  log "REFUSE at arm: admission state is $ADM_STATE (db=$DB) — no attempt is armed"
  exit 64
fi
log "admission observation mode=ro epoch=$ADM_EPOCH live=$ADM_LIVE ahead=$ADM_AHEAD stale_recent=$ADM_STALE_RECENT stale_expired=$ADM_STALE_EXPIRED undated=$ADM_UNDATED wal_bytes=$(wal_bytes)"

quiet=0
while :; do
  now=$(date +%s)
  [ "$now" -ge "$end" ] && break

  agents=$(/usr/bin/python3.13 -c "import json;print(json.load(open('$PW')).get('active_agents'))" 2>/dev/null || echo ERR)
  observe_admissions

  # Only an exact all-zero observation advances the window. An unreadable store, an unknown epoch,
  # a stale-but-recent admission or an undated row all reset it.
  if [ "$agents" = "0" ] && [ "$ADM_STATE" = "quiet" ]; then quiet=$((quiet + 1)); else quiet=0; fi
  log "sample agents=$agents admissions=$ADM_STATE epoch=${ADM_EPOCH:-?} live=${ADM_LIVE:-?} ahead=${ADM_AHEAD:-?} stale_recent=${ADM_STALE_RECENT:-?} stale_expired=${ADM_STALE_EXPIRED:-?} undated=${ADM_UNDATED:-?} wal_bytes=$(wal_bytes) quiet=$quiet/$NEED"
  [ "$quiet" -ge "$NEED" ] && break

  now=$(date +%s)
  remaining=$((end - now))
  [ "$remaining" -le 0 ] && break
  [ "$remaining" -lt "$INTERVAL" ] && sleep "$remaining" || sleep "$INTERVAL"
done

if [ "$quiet" -lt "$NEED" ]; then
  log "DEADLINE: no quiet window within ${DEADLINE_S}s — disarmed without applying (attempt unspent)"
  exit 3
fi

# Final refusal checks immediately before the single invocation.
if [ -e "$JOURNAL" ]; then log "REFUSE at fire: journal appeared"; exit 76; fi
if [ -e "$FIRED" ]; then log "REFUSE at fire: attempt-fired record appeared"; exit 76; fi
if systemctl list-units --all --no-legend 'hermes-release-*' 2>/dev/null | grep -q '\.service'; then
  log "REFUSE at fire: a hermes-release-* unit already exists"; exit 77
fi

log "quiet window confirmed ($NEED x ${INTERVAL}s) — recording the attempt and starting the single supervisor"
write_fired pending 0 ""

# Trust is granted for exactly two repositories, as /etc/gitconfig does; never for '*'.
# The attempt deadline bounds the call, not just the wait for a quiet window: a launcher that never
# returns would otherwise leave this trigger blocked forever and the record stuck at `pending`, which
# is the state nobody can act on. `--kill-after` makes sure the child is gone even if it ignores TERM.
remaining=$((end - $(date +%s)))
[ "$remaining" -ge 10 ] || remaining=10
out=$(timeout --kill-after=5 "$remaining" \
      env HERMES_RELEASE_APPROVAL=$CAND \
      HERMES_RELEASE_EXPECT_HEAD=$EXPECT_HEAD \
      GIT_CONFIG_COUNT=2 \
      GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/home/hermes/.hermes/hermes-agent \
      GIT_CONFIG_KEY_1=safe.directory GIT_CONFIG_VALUE_1=/home/hermes/work/restore002-fix \
      "$LAUNCHER" apply --candidate "$CAND" --detach 2>&1)
rc=$?
printf '%s\n' "$out" >>"$LOG"
run_id=""
if [ "$rc" -eq 0 ]; then
  run_id=$(printf '%s\n' "$out" | sed -n 's/.*supervisor started detached: hermes-release-\(.*\)\.service.*/\1/p' | head -1)
fi
log "apply rc=$rc engine_run_id=${run_id:-UNKNOWN}"

if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  # The launcher was still running at the deadline. Whether it started a supervisor is now unknowable
  # from here, so the attempt is recorded as spent and the operator must read the journal: this
  # trigger will not fire again either way.
  write_fired failed "$rc" ""
  log "DEADLINE: the launcher did not return within ${remaining}s — attempt recorded as spent, verify the journal before retrying"
  exit 5
fi

if [ "$rc" -ne 0 ] || [ -z "$run_id" ]; then
  # Nothing is running: the attempt is not spent, and the record says so instead of claiming a fire.
  write_fired failed "$rc" "${run_id:-}"
  log "REFUSE: the launcher did not start a supervisor (rc=$rc) — no retry from this trigger"
  exit 4
fi

write_fired fired "$rc" "$run_id"
log "attempt-fired.json written (status=fired) — launcher exiting, no retry, no second APPLY"
exit 0
