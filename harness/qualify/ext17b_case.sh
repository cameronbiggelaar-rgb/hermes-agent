#!/usr/bin/env bash
# EXT-17B — one adversarial scenario, driven end to end inside the disposable namespace.
#
#   ext17b_case.sh <case>
#
#   dirty            a dirty installation must be refused before any mutation
#   foreign-freeze   a drain marker owned by another attempt must be refused
#   busy             a host that never presents a quiet window must abort pre-mutation
#   rollback         a restart that cannot come up must roll the deployment back
#   interrupt        a TERM during the restart must settle, not abandon, the run
#   timeout          a restart that never stops must settle at the 300 s budget
#   trigger          trigger v2 must hand off exactly once into the qualified supervisor
#   wal-live         a live admission committed into an uncheckpointed WAL must block the window
#
# Every case prints `CASE=<name> STEP=<step> RC=<n>` lines for each step and nothing else on stdout;
# the evidence (journal, archive, install identity, gateway state) is read from the case root by the
# host afterwards. Never run this outside ext17b_run.sh: it addresses /etc/systemd/system and
# /var/lib/hermes-release by name and relies on the private mounts to redirect them.
set -uo pipefail

REPO=${HERMES_RELEASE_REPO:-/home/hermes/work/restore002-fix}
LIB=${HERMES_RELEASE_LIB:-/usr/local/lib/hermes-release}
CAND=${EXT17B_CANDIDATE:?EXT17B_CANDIDATE required}
Q=$REPO/harness/qualify/ext17b_scenario.sh
TRIGGER=$REPO/harness/trigger/qw-launch-apply-v2.sh
STATE=${EXT17B_STATE:?EXT17B_STATE required}
ROOT=${EXT17B_ROOT:?EXT17B_ROOT required}
INSTALLED=$LIB/hermes-release-002
BASE=3652a3702dae8d2917b91b6949da580efd3ef496

CASE=${1:?case required}

say() { printf 'CASE=%s STEP=%s RC=%s\n' "$CASE" "$1" "$2"; }

# A scenario leaves nothing running: the fixture gateway is stopped on every exit path, including the
# ones that end in a refusal, so a later "no orphaned processes" check is not polluted by the fixture.
cleanup() { "$Q" stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

run() { # run <step> [args...] — records the step's exit status without aborting the case
  local step=$1; shift
  # Every step keeps its own transcript: a refusal is evidence, and stdout is the only place the
  # release states which gate it refused on.
  "$@" >"$ARCHIVE/step-$step.log" 2>&1
  local rc=$?
  say "$step" "$rc"
  return $rc
}

# Wait for a line in the supervisor log, bounded. wait_log <pattern> <timeout_s>
wait_log() {
  local pattern=$1 deadline=$(( $(date +%s) + ${2:-240} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    # Two logs matter: the supervisor's own transcript and the systemd substitute's journal, which is
    # where a controlled stop is announced before the supervisor gets its result back.
    if grep -qE "$pattern" "$STATE"/../logs/hermes-release-002-*.log "$STATE"/gateway.log 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

run start bash -c 'systemctl daemon-reload && systemctl start hermes-gateway.service'
run stage "$Q" stage

case "$CASE" in
  dirty)
    run plan "$Q" plan
    run apply "$Q" apply
    ;;

  foreign-freeze)
    run plan "$Q" plan
    run apply "$Q" apply
    ;;

  busy)
    run plan "$Q" plan
    run apply "$Q" apply
    ;;

  rollback)
    run plan "$Q" plan
    run apply "$Q" apply
    run status "$Q" status
    ;;

  needs-human)
    run plan "$Q" plan
    run apply "$Q" apply
    # The journal must be left open, not forced to a passing state: phase aborted, result unset,
    # NEEDS_HUMAN evidence beside it. Then an operator clears the fault and recovery completes the
    # run from the durable journal alone.
    run status "$Q" status
    /usr/bin/python3.13 - "$ROOT" <<'PY'
import json, pathlib, sys
cfg = pathlib.Path(sys.argv[1]) / "state" / "config.json"
body = json.loads(cfg.read_text())
body["fail_start_after"] = 0
cfg.write_text(json.dumps(body, indent=1) + "\n")
PY
    run recover "$Q" recover
    run status-after-recover "$Q" status
    ;;

  interrupt)
    run plan "$Q" plan
    "$Q" apply --detach >/dev/null 2>&1 &
    say apply-detach 0
    # The restart is where the run becomes unrecoverable if it is simply abandoned, so interrupt
    # there: the settlement path must record the outcome even though the supervisor is being torn down.
    # The engine logs the restart only once it has returned, so the signal is timed from the unit's
    # own stop rather than from a line the engine writes afterwards.
    if wait_log 'Stopping hermes-gateway' 420; then
      pid=$(pgrep -f 'release002_release\.sh' | head -1)
      if [ -n "${pid:-}" ]; then
        kill -TERM "$pid" 2>/dev/null
        say signal-term "$?"
      else
        say signal-term 1
      fi
    else
      say signal-term 2
    fi
    wait || true
    # Recovery while that supervisor is still running must be refused: acting on a half-finished
    # attempt races the settlement that is about to happen, and can claim a completion the
    # supervisor then rolls back.
    run recover-while-running "$Q" recover
    for _ in $(seq 1 600); do
      grep -q '"result"' /var/lib/hermes-release/restore-002/journal.json 2>/dev/null && break
      sleep 1
    done
    # The terminal record is written before the supervisor finishes tearing down; wait for the
    # engine itself to be gone so recovery does not refuse a still-exiting supervisor.
    for _ in $(seq 1 120); do
      pgrep -f 'release002_release\.sh --apply' >/dev/null 2>&1 || break
      sleep 1
    done
    # The supervisor has settled; recovery now binds to the durable journal alone.
    run recover "$Q" recover
    ;;

  timeout)
    # restart cannot stop: the unit's TimeoutStopUSec (300 s) must be the effective bound and the
    # run must settle as a failure rather than hang the supervisor for its whole runtime cap.
    run plan "$Q" plan
    run apply "$Q" apply
    run status "$Q" status
    ;;

  trigger)
    run plan "$Q" plan
    # Two independent arming records: one carries the refused attempt, the other the single
    # legitimate fire, so single-use can be shown without the first record masking the second.
    fire() { # fire <state-dir> <candidate> <deadline>
      TRIGGER_QUALIFY=1 TRIGGER_STATE="$1" \
      TRIGGER_PW="$HOME/gateway_state.json" TRIGGER_DB="$HOME/state.db" \
      TRIGGER_LAUNCHER="$INSTALLED" TRIGGER_NEED="${TRIGGER_NEED:-3}" \
      TRIGGER_INTERVAL="${TRIGGER_INTERVAL:-1}" TRIGGER_DEADLINE_S="$3" \
        "$TRIGGER" --candidate "$2" --expect-head "$BASE"
    }
    # A candidate the installation is not staged for: the launcher refuses, the trigger records the
    # attempt as failed and does not retry, and the refusal propagates out as a non-zero exit.
    run trigger-wrong-candidate fire "$STATE/armed-refused" 9601cc8a8ec1fc7fb9d534c653cf66e916b50c20 20
    run trigger-wrong-again fire "$STATE/armed-refused" 9601cc8a8ec1fc7fb9d534c653cf66e916b50c20 5
    run trigger fire "$STATE/armed" "$CAND" 120
    run trigger-again fire "$STATE/armed" "$CAND" 5
    # The supervisor the trigger started detached must finish on its own: wait for its journal.
    for _ in $(seq 1 420); do
      grep -q '"result"' /var/lib/hermes-release/restore-002/journal.json 2>/dev/null && break
      sleep 1
    done
    run status "$Q" status
    ;;

  wal-live)
    # EXT-17C §1, end to end. The fixture store is opened in WAL mode with autocheckpoint off and a
    # queued admission for the current epoch is committed and left in the -wal, with the writer still
    # open: the shape of a gateway that has just admitted a session. `immutable=1` reads only the main
    # file, so it would report zero and the window would qualify over live work; the trigger must
    # refuse instead, and must still fire once that same row is terminal.
    run plan "$Q" plan
    /usr/bin/python3.13 - "$HOME/state.db" "$STATE/wal-writer.pid" >"$ARCHIVE/step-wal-writer.log" 2>&1 <<'PY' &
import pathlib, sqlite3, sys, time
db, pidfile = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("pragma wal_autocheckpoint=0")
con.execute("insert into session_admissions (admission_id, owner_epoch, status, target_session_id)"
            " values ('wal-live-1', (select epoch from runtime_epoch where singleton=1),"
            " 'queued', 'fixture-open-1')")
con.commit()
pathlib.Path(pidfile).write_text(str(__import__("os").getpid()))
time.sleep(900)
PY
    wallet_pid=$!
    for _ in $(seq 1 30); do [ -s "$STATE/wal-writer.pid" ] && break; sleep 0.2; done
    # The WAL must actually hold frames, or the case would prove nothing.
    say wal-bytes "$(stat -c %s "$HOME/state.db-wal" 2>/dev/null || echo 0)"
    fire() { # fire <state-dir> <candidate> <deadline>
      TRIGGER_QUALIFY=1 TRIGGER_STATE="$1" \
      TRIGGER_PW="$HOME/gateway_state.json" TRIGGER_DB="$HOME/state.db" \
      TRIGGER_LAUNCHER="$INSTALLED" TRIGGER_NEED=2 TRIGGER_INTERVAL=1 \
      TRIGGER_DEADLINE_S="$3" \
        "$TRIGGER" --candidate "$2" --expect-head "$BASE"
    }
    # Live admission in a hot, uncheckpointed WAL: no quiet window, no attempt armed.
    run trigger-wal-live fire "$STATE/armed-wal" "$CAND" 8
    say fired-record "$([ -e "$STATE/armed-wal/attempt-fired.json" ] && echo present || echo absent)"
    kill "$wallet_pid" 2>/dev/null || true
    wait "$wallet_pid" 2>/dev/null || true
    # Same row, now terminal: the window must open and the single handoff must happen for real.
    sqlite3 "$HOME/state.db" "update session_admissions set status='terminal' where admission_id='wal-live-1';"
    run trigger-wal-settled fire "$STATE/armed-wal" "$CAND" 120
    for _ in $(seq 1 420); do
      grep -q '"result"' /var/lib/hermes-release/restore-002/journal.json 2>/dev/null && break
      sleep 1
    done
    run status "$Q" status
    ;;

  *) echo "unknown case $CASE" >&2; exit 64 ;;
esac
exit 0
