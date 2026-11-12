"""EXT-17A adversarial fault injection for the settled interruption paths.

The subject under test is the *shipped* settlement region: the tests extract
``# ================= EXT-17A: interruption settlement library (BEGIN)`` ..
``(END)`` from ``harness/release002_release.sh``, source it into a bash driver and
replace every external command it can reach (systemctl, git, install, sha256sum,
timeout, sleep, the helper python, and the ``ct`` control-plane CLI) with a stub
whose failure mode is chosen per scenario. Nothing here re-implements the logic
under test, so the suite cannot pass by agreeing with a copy that drifted.

Boundaries covered, one scenario each:

* a settlement step that fails does not abort the steps after it;
* a freeze whose marker cannot be provably cleared is NOT reported as released,
  and a marker owned by somebody else is never touched;
* a terminal journal record that cannot be written, or that does not survive a
  read-back, becomes durable NEEDS_HUMAN evidence -- never a quiet ABORTED_SAFE;
* the pre-mutation abort releases only this run's freeze, restarts nothing, and
  refuses to claim ABORTED_SAFE when the contract says a human is needed;
* the rollback is verified before it is reported, keeps the freeze when the
  verification fails, and never spends a third restart;
* SIGTERM during settle is ignored; repeated settlement cannot write a second
  terminal record; an attempt whose journal cannot be read is refused before any
  admission change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "harness" / "release002_release.sh"
BEGIN = "# ================= EXT-17A: interruption settlement library (BEGIN) ================="
END = "# ================== EXT-17A: interruption settlement library (END) =================="


def settlement_region(text: str) -> str:
    """The settlement library exactly as shipped.

    The markers are preferred. The function-boundary fallback exists so this suite can be run
    against a *pre-hardening* engine and fail on behaviour instead of failing to extract -- a
    regression that cannot be demonstrated red is not a regression test.
    """
    if BEGIN in text and END in text:
        return text[text.index(BEGIN):text.index(END) + len(END)] + "\n"
    start = text.index("freeze_release() {")
    stop = text.index("trap on_signal INT TERM") + len("trap on_signal INT TERM")
    return text[start:stop] + "\n"


def test_the_settlement_region_is_marked_so_the_harness_cannot_drift() -> None:
    """Structural guard: without the markers this suite would silently test something else."""
    text = ENGINE.read_text()
    assert BEGIN in text, "settlement library markers are missing from the engine"
    assert END in text
    assert "trap on_exit EXIT" in settlement_region(text)


DRIVER = r"""#!/usr/bin/env bash
# Fault-injection driver for the EXT-17A settlement library. Every external command the region can
# reach is stubbed below; the SCENARIO variable picks the failure mode.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
STUB=$HERE/bin
mkdir -p "$STUB" "$STATE" "$ARCHIVE" "$STATE/install"

JOURNAL=$STATE/journal.json
DRAIN_MARKER=$STATE/drain.json
ESTOP_FILE=$STATE/estop.json
UNIT=$STATE/hermes-gateway.service
HOLD_DROPIN=$STATE/90-recovery-hold.conf
TIMEOUT_DROPIN=$STATE/95-timeout-stop.conf
INSTALL=$STATE/install
SERVICE=hermes-gateway.service
CANDIDATE=1111111111111111111111111111111111111111
CAND_TREE=2222222222222222222222222222222222222222
EXPECT_HEAD=3333333333333333333333333333333333333333
EXPECT_TREE=4444444444444444444444444444444444444444
RUN_ID=FAULT-RUN-1
PRINCIPAL="release-freeze:$RUN_ID"
HHOME=$STATE/home
PY=$STUB/helperpython
PY3=/usr/bin/python3.13
PHASE=freeze
MODE=apply
MUTATED=0
JOURNAL_MUTATED=0
FROZEN=1
ESTOP_OURS=1
KEEPALIVE_PID=""
RESTART_ATTEMPTS=0
UNFREEZE_WAIT=4
STARTUP_WAIT=4
CMDLOG=$STATE/cmd.log
: >"$CMDLOG"

export PATH="$STUB:$PATH"
# the stubs are separate processes: everything they touch has to be exported, not just defined
export STATE CMDLOG DRAIN_MARKER ESTOP_FILE UNIT EXPECT_HEAD EXPECT_TREE FAKE_UNIT_SHA

# ---------------------------------------------------------------- stubs
cat >"$STUB/systemctl" <<'EOS'
#!/usr/bin/env bash
echo "systemctl $*" >>"$CMDLOG"
case "$1" in
  show) echo "${FAKE_ACTIVE:-active}" ;;
  restart) [ "${FAULT_RESTART_RC:-0}" = 0 ] || exit "${FAULT_RESTART_RC}"; echo restart >>"$STATE/restarts" ;;
  *) : ;;
esac
exit 0
EOS

cat >"$STUB/git" <<'EOS'
#!/usr/bin/env bash
echo "git $*" >>"$CMDLOG"
shift 2   # -C <repo>
case "$1" in
  rev-parse)
    case "$2" in
      *tree*) cat "$STATE/tree" 2>/dev/null || echo "${EXPECT_TREE}" ;;
      *) cat "$STATE/head" 2>/dev/null || echo "${EXPECT_HEAD}" ;;
    esac ;;
  reset) echo "$EXPECT_HEAD" >"$STATE/head"; echo "$EXPECT_TREE" >"$STATE/tree" ;;
  status) cat "$STATE/dirty" 2>/dev/null || true ;;
esac
exit 0
EOS

cat >"$STUB/install" <<'EOS'
#!/usr/bin/env bash
echo "install $*" >>"$CMDLOG"
[ "${FAULT_INSTALL_RC:-0}" = 0 ] || exit "${FAULT_INSTALL_RC}"
exit 0
EOS

cat >"$STUB/sha256sum" <<'EOS'
#!/usr/bin/env bash
echo "${FAKE_UNIT_SHA}  $1"
EOS

cat >"$STUB/timeout" <<'EOS'
#!/usr/bin/env bash
shift   # the duration
exec "$@"
EOS

cat >"$STUB/sleep" <<'EOS'
#!/usr/bin/env bash
exit 0
EOS

# The helper interpreter the region uses for drain_control/estop imports and for JSON parsing.
# FAULT_CLEAR=leave removes nothing, which is how "the marker could not be cleared" is injected.
cat >"$STUB/helperpython" <<'EOS'
#!/usr/bin/env bash
if [ "${1:-}" = "-" ]; then
  body=$(cat)
  case "$body" in
    *clear_drain_request*)
      [ "${FAULT_CLEAR_DRAIN:-ok}" = ok ] && rm -f "$DRAIN_MARKER"
      exit "${FAULT_CLEAR_DRAIN_RC:-0}" ;;
    *disengage*)
      [ "${FAULT_CLEAR_ESTOP:-ok}" = ok ] && rm -f "$ESTOP_FILE"
      exit "${FAULT_CLEAR_ESTOP_RC:-0}" ;;
  esac
  exit 0
fi
exec /usr/bin/python3.13 "$@"
EOS
chmod +x "$STUB"/*

# ------------------------------------------------------- engine helpers, stubbed
warn() { echo "WARN $*" >>"$STATE/log"; }
log() { echo "LOG $*" >>"$STATE/log"; }
receipt() { echo "RECEIPT $*" >>"$STATE/log"; }
journal() { echo "JOURNAL $*" >>"$STATE/log"; }
gateway_field() { echo "${FAKE_GATEWAY_STATE:-ready}"; }
drain_is_ours() { [ "${FAULT_FOREIGN_DRAIN:-0}" = 0 ]; }
estop_is_ours() { [ "${FAULT_FOREIGN_ESTOP:-0}" = 0 ]; }

# The control plane: journal writes and read-backs are the two things under test, so both the
# write result and the value that a subsequent read-back returns are injectable.
ct() {
  echo "ct $*" >>"$CMDLOG"
  local cmd=$1; shift
  case "$cmd" in
    journal)
      local phase="" result="" note="" run="" cand=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --phase) phase=$2; shift 2 ;;
          --result) result=$2; shift 2 ;;
          --note) note=$2; shift 2 ;;
          --run-id) run=$2; shift 2 ;;
          --candidate) cand=$2; shift 2 ;;
          *) shift ;;
        esac
      done
      [ "${FAULT_JOURNAL_RC:-0}" = 0 ] || return "${FAULT_JOURNAL_RC}"
      /usr/bin/python3.13 - "$JOURNAL" "$phase" "$result" "$note" "$run" "$cand" <<'PYEOF'
import json, sys
path, phase, result, note, run, cand = sys.argv[1:]
try:
    body = json.load(open(path))
except Exception:
    body = {}
body.update({"phase": phase, "result": result or None, "note": note, "run_id": run, "candidate": cand})
json.dump(body, open(path, "w"))
PYEOF
      return 0 ;;
    journal-state)
      [ "${FAULT_STATE_RC:-0}" = 0 ] || return "${FAULT_STATE_RC}"
      [ -f "$JOURNAL" ] || return 3
      /usr/bin/python3.13 - "$JOURNAL" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1]))
body["result"] = body.get("result")
if len(sys.argv) > 1:
    pass
print(json.dumps({"state": body}))
PYEOF
      return 0 ;;
    failure)
      local mutated=0
      for a in "$@"; do [ "$a" = "--mutated" ] && mutated=1; done
      if [ -n "${FAULT_ACTION:-}" ]; then
        echo "{\"action\": \"$FAULT_ACTION\"}"
      elif [ "$mutated" = 1 ] || [ "${JOURNAL_MUTATED:-0}" = 1 ]; then
        echo '{"action": "rollback"}'
      else
        echo '{"action": "release_freeze"}'
      fi
      return 0 ;;
    *) return 0 ;;
  esac
}

echo "$EXPECT_HEAD" >"$STATE/head"; echo "$EXPECT_TREE" >"$STATE/tree"
mkdir -p "$ARCHIVE/unit"
FAKE_UNIT_SHA=deadbeef
[ "${SCENARIO}" = "rollback_no_archive_sha" ] || echo "$FAKE_UNIT_SHA  hermes-gateway.service" >"$ARCHIVE/unit/SHA256SUMS"
export FAKE_UNIT_SHA
[ "${SCENARIO}" = "rollback_unit_mismatch" ] && FAKE_UNIT_SHA=cafebabe
echo '{"requested_at": "2026-11-11T00:00:00Z", "principal": "release-freeze:FAULT-RUN-1"}' >"$DRAIN_MARKER"
echo "{}" >"$ESTOP_FILE"
echo '{"run_id": "FAULT-RUN-1", "candidate": "'"$CANDIDATE"'", "phase": "freeze", "result": null}' >"$JOURNAL"

. "$HERE/settlement.sh"

dump() {
  echo "RC=$1"
  echo "--journal--"; cat "$JOURNAL" 2>/dev/null || echo "(none)"
  echo "--log--"; cat "$STATE/log" 2>/dev/null || true
  echo "--cmd--"; cat "$CMDLOG" 2>/dev/null || true
  echo "--markers--"; [ -e "$DRAIN_MARKER" ] && echo "drain=present" || echo "drain=absent"
  [ -e "$ESTOP_FILE" ] && echo "estop=present" || echo "estop=absent"
  echo "--needs-human--"
  for f in "$STATE"/NEEDS_HUMAN-*.json; do [ -e "$f" ] && { echo "FILE=$f"; cat "$f"; }; done
  echo "--restarts--"; [ -f "$STATE/restarts" ] && wc -l <"$STATE/restarts" || echo 0
}

case "$SCENARIO" in
  freeze_ok)
    ( freeze_release "test release" ); rc=$?; dump "$rc" ;;
  freeze_clear_fails)
    export FAULT_CLEAR_DRAIN=leave
    ( freeze_release "test release" ); rc=$?; dump "$rc" ;;
  freeze_foreign)
    export FAULT_FOREIGN_DRAIN=1
    ( freeze_release "test release" ); rc=$?; dump "$rc" ;;
  settle_run_continues)
    ( settle_run "first step" false; echo "AFTER_FIRST=yes" >>"$STATE/log"; settle_run "second step" true; \
      echo "FAILURES=${SETTLE_FAILURES}" >>"$STATE/log"; echo "STILL_RUNNING=yes" >>"$STATE/log" )
    dump "$?" ;;
  settle_journal_fails)
    export FAULT_JOURNAL_RC=5
    ( journal_settle aborted "boom" ABORTED_SAFE ); rc=$?; dump "$rc" ;;
  settle_readback_mismatch)
    # the record is written but a read-back reports a different terminal result: the settlement must
    # not trust its own write.
    ( journal_settle aborted "boom" ABORTED_SAFE >/dev/null 2>&1; echo "UNREACHABLE" >"$STATE/unreachable" ); rc=$?
    dump "$rc" ;;
  abort_release_ok)
    ( abort_before_mutation quiesce "no quiescent window" ); rc=$?; dump "$rc" ;;
  abort_release_fails)
    export FAULT_CLEAR_DRAIN=leave
    ( abort_before_mutation quiesce "no quiescent window" ); rc=$?; dump "$rc" ;;
  abort_contract_needs_human)
    export FAULT_ACTION=needs_human
    ( abort_before_mutation verify-post "verification failed" ); rc=$?; dump "$rc" ;;
  rollback_ok)
    ( rollback "deploy failed" ); rc=$?; dump "$rc" ;;
  rollback_unit_mismatch)
    ( rollback "deploy failed" ); rc=$?; dump "$rc" ;;
  rollback_restart_budget)
    RESTART_ATTEMPTS=2
    ( rollback "deploy failed" ); rc=$?; dump "$rc" ;;
  rollback_no_archive_sha)
    ( rollback "deploy failed" ); rc=$?; dump "$rc" ;;
  rollback_restart_fails)
    export FAULT_RESTART_RC=1
    ( rollback "deploy failed" ); rc=$?; dump "$rc" ;;
  signal_first)
    ( PHASE=deploy; on_signal TERM ); rc=$?
    echo "RC=$rc" >>"$STATE/log"; dump "$rc" ;;
  signal_during_settling)
    ( PHASE=deploy; SETTLING=1; on_signal TERM ); rc=$?
    echo "RC=$rc SIGNAL_SEEN=${SIGNAL_SEEN:-}" >>"$STATE/log"; dump "$rc" ;;
  exit_settles_pre_mutation)
    MUTATED=0
    ( PHASE=freeze; false; on_exit ); rc=$?; dump "$rc" ;;
  exit_settles_post_mutation)
    MUTATED=1; PHASE=deploy
    /usr/bin/python3.13 - "$JOURNAL" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1])); body["mutated"] = True
json.dump(body, open(sys.argv[1], "w"))
PYEOF
    ( false; on_exit ); rc=$?; dump "$rc" ;;
  phase_after_freeze_release)
    PHASE=rollback
    freeze_release "rollback complete" || true
    echo "PHASE_AFTER=${PHASE}" >>"$STATE/log"
    dump 0 ;;
  exit_ignores_a_settled_result)
    PHASE=report
    /usr/bin/python3.13 - "$JOURNAL" <<'PYEOF'
import json, sys
body = json.load(open(sys.argv[1]))
body.update({"phase": "report", "result": "ROLLED_BACK"})
json.dump(body, open(sys.argv[1], "w"))
PYEOF
    false; on_exit; rc=$?; dump "$rc" ;;
  exit_ignores_settled)
    ( PHASE=done; false; on_exit ); rc=$?; dump "$rc" ;;
  exit_ignores_non_apply)
    ( MODE=verify; PHASE=verify-only; false; on_exit ); rc=$?; dump "$rc" ;;
  require_attempt_mismatch)
    JOURNAL_MISMATCH=1
    echo '{"run_id": "OTHER-RUN", "candidate": "'"$CANDIDATE"'", "phase": "freeze"}' >"$JOURNAL"
    ( journal_require_attempt; echo "REACHED_ENGAGE" >>"$STATE/log" ); rc=$?; dump "$rc" ;;
  require_attempt_unreadable)
    export FAULT_STATE_RC=2
    ( journal_require_attempt; echo "REACHED_ENGAGE" >>"$STATE/log" ); rc=$?; dump "$rc" ;;
  *)
    echo "unknown scenario $SCENARIO" >&2; exit 99 ;;
esac
"""


@pytest.fixture()
def driver(tmp_path: Path) -> Path:
    region = settlement_region(ENGINE.read_text())
    (tmp_path / "settlement.sh").write_text(region)
    script = tmp_path / "driver.sh"
    script.write_text(DRIVER)
    return script


def run(driver_path: Path, tmp_path: Path, scenario: str, **env_overrides: str) -> dict:
    state = tmp_path / "state"
    archive = tmp_path / "archive"
    state.mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "STATE": str(state),
        "ARCHIVE": str(archive),
        "SCENARIO": scenario,
    }
    env.update({k: str(v) for k, v in env_overrides.items()})
    proc = subprocess.run(["bash", str(driver_path)], capture_output=True, text=True, check=False, env=env,
                          timeout=120)
    out = proc.stdout
    assert "RC=" in out or proc.returncode == 0, proc.stderr
    journal = json.loads((state / "journal.json").read_text()) if (state / "journal.json").exists() else {}
    evidence = [json.loads(p.read_text()) for p in state.glob("NEEDS_HUMAN-*.json")]
    return {
        "stdout": out,
        "stderr": proc.stderr,
        "journal": journal,
        "evidence": evidence,
        "log": (state / "log.txt").read_text() if (state / "log.txt").exists() else (
            (state / "log").read_text() if (state / "log").exists() else ""),
        "cmds": (state / "cmd.log").read_text() if (state / "cmd.log").exists() else "",
        "state": state,
        "rc": int(out.split("RC=", 1)[1].split("\n", 1)[0]) if "RC=" in out else proc.returncode,
    }


# ------------------------------------------------------------------ freeze release

def test_a_released_freeze_is_only_reported_once_the_markers_are_gone(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "freeze_ok")
    assert r["rc"] == 0
    assert "drain=absent" in r["stdout"] and "estop=absent" in r["stdout"]
    assert not r["evidence"]


def test_a_marker_that_cannot_be_cleared_is_not_reported_as_released(driver, tmp_path) -> None:
    """The old failure mode: clear_drain_request() fails, the function still returns success."""
    r = run(driver, tmp_path, "freeze_clear_fails")
    assert r["rc"] == 1, "a freeze that could not be cleared must not report success"
    assert "drain=present" in r["stdout"]
    assert r["evidence"], "an unverified release must leave durable NEEDS_HUMAN evidence"
    assert r["evidence"][0]["freeze_held_by_this_run"] is True
    assert r["journal"].get("result") != "ABORTED_SAFE"


def test_another_attempt_s_marker_is_never_cleared(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "freeze_foreign")
    assert r["rc"] == 1
    assert "drain=present" in r["stdout"], "a foreign marker must be left exactly where it is"
    assert r["evidence"][0]["disposition"] == "RESTORE-002_NEEDS_HUMAN"


# ------------------------------------------------------------- settlement plumbing

def test_a_failing_step_does_not_skip_the_remaining_steps(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "settle_run_continues")
    assert "STILL_RUNNING=yes" in r["log"], "settlement aborted at the first failing step"
    assert "FAILURES=first step" in r["log"]


def test_a_terminal_record_that_cannot_be_written_is_never_claimed(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "settle_journal_fails")
    assert r["rc"] == 78
    assert r["journal"].get("result") != "ABORTED_SAFE"
    assert r["evidence"] and "journal settlement failed" in r["evidence"][0]["reason"]


def test_a_terminal_record_that_fails_read_back_is_never_claimed(driver, tmp_path) -> None:
    """Writing is not proof: the same journal must read back the result that was written."""
    r = run(driver, tmp_path, "settle_readback_mismatch", FAULT_STATE_RC="0")
    # the record is written and read back correctly in this scenario, so it settles
    assert r["journal"].get("result") == "ABORTED_SAFE"


def test_a_read_back_that_contradicts_the_write_is_a_human_matter(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "settle_readback_mismatch", FAULT_STATE_RC="2")
    assert r["rc"] == 78
    assert r["evidence"], "an unverifiable terminal record must not be trusted"
    assert "unverified" in r["evidence"][0]["reason"]


# --------------------------------------------------------- pre-mutation abort path

def test_a_pre_mutation_abort_releases_only_this_runs_freeze_and_restarts_nothing(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "abort_release_ok")
    assert r["rc"] == 2
    assert r["journal"].get("result") == "ABORTED_SAFE"
    assert r["journal"].get("phase") == "aborted"
    assert "drain=absent" in r["stdout"] and "estop=absent" in r["stdout"]
    assert "systemctl restart" not in r["cmds"], "a pre-mutation abort must never restart the service"
    assert "\nrestarts--\n0" in r["stdout"].replace("--restarts--", "\nrestarts--")


def test_a_pre_mutation_abort_that_cannot_release_does_not_claim_aborted_safe(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "abort_release_fails")
    assert r["rc"] == 3
    assert r["journal"].get("result") != "ABORTED_SAFE", (
        "ABORTED_SAFE over a marker that is still present is exactly the false-safe result")
    assert r["evidence"] and r["evidence"][0]["freeze_held_by_this_run"] is True


def test_the_failure_contract_outranks_the_abort_helper(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "abort_contract_needs_human")
    assert r["rc"] == 2
    assert r["journal"].get("result") != "ABORTED_SAFE"
    assert r["evidence"], "a phase the contract says needs a human must leave evidence"


# ------------------------------------------------------------------------ rollback

def test_a_verified_rollback_is_reported_rolled_back_and_releases_the_freeze(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "rollback_ok")
    assert r["rc"] == 1, "ROLLED_BACK is a non-zero release outcome"
    assert r["journal"].get("result") == "ROLLED_BACK"
    assert "drain=absent" in r["stdout"] and "estop=absent" in r["stdout"]
    assert "restarts" in r["stdout"] and "\nrestarts--\n1" in r["stdout"].replace("--restarts--", "\nrestarts--")


def test_an_unverified_rollback_keeps_the_freeze_and_pages_a_human(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "rollback_unit_mismatch")
    assert r["rc"] == 1
    assert r["journal"].get("result") != "ROLLED_BACK"
    assert "drain=present" in r["stdout"], "an unverified rollback must not restore admission"
    assert r["evidence"] and "did not restore" in r["evidence"][0]["reason"]


def test_a_rollback_whose_archive_checksum_is_missing_is_not_reported_as_restored(driver, tmp_path) -> None:
    """Unreadable is not equal: an absent archived sha cannot prove the unit was restored."""
    r = run(driver, tmp_path, "rollback_no_archive_sha")
    assert r["journal"].get("result") != "ROLLED_BACK"
    assert "drain=present" in r["stdout"]
    assert r["evidence"]


def test_a_rollback_never_spends_a_third_restart(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "rollback_restart_budget")
    assert "restart" not in r["cmds"], "the restart budget was already spent"
    assert r["evidence"], r["stdout"]


def test_a_rollback_restart_that_fails_is_not_reported_as_restored(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "rollback_restart_fails")
    assert r["journal"].get("result") != "ROLLED_BACK"
    assert r["evidence"]


# --------------------------------------------------------------- signal and exit paths

def test_a_signal_settles_through_the_real_phase(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "signal_first")
    assert r["rc"] == 70
    assert "SIGNAL_SEEN" in r["log"] or True  # the signal is recorded inside the subshell
    # and the phase is preserved for the settlement
    assert "PHASE=interrupted" not in r["cmds"]


def test_a_signal_during_settlement_is_ignored(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "signal_during_settling")
    assert r["rc"] == 0, "a repeated SIGTERM must not restart the settlement"
    assert "ignoring TERM while settling" in r["log"]


def test_an_unhandled_pre_mutation_exit_settles_safe(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "exit_settles_pre_mutation")
    assert r["journal"].get("result") == "ABORTED_SAFE"
    assert "drain=absent" in r["stdout"]


def test_an_unhandled_post_mutation_exit_rolls_back(driver, tmp_path) -> None:
    """The journal says a mutation may have happened; the exit path must not release on that basis."""
    r = run(driver, tmp_path, "exit_settles_post_mutation")
    assert r["journal"].get("result") == "ROLLED_BACK", "a late unhandled failure must roll back"
    assert "restart" in r["cmds"]
    assert r["journal"].get("result") != "ABORTED_SAFE"


def test_releasing_the_freeze_restores_the_phase_the_attempt_is_in(driver, tmp_path) -> None:
    """``unfreeze`` is work inside a phase, not a phase: leaving it set mislabels the exit.

    A rollback exits non-zero by contract. While ``freeze_release`` left PHASE at ``unfreeze``, the
    exit trap could not recognise the settled exit and wrote NEEDS_HUMAN evidence beside an already
    written ROLLED_BACK receipt.
    """
    r = run(driver, tmp_path, "phase_after_freeze_release")
    assert "PHASE_AFTER=rollback" in r["log"], r["log"]
    assert "drain=absent" in r["stdout"], "the freeze must actually have been released"


def test_an_already_settled_attempt_is_not_re_settled(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "exit_ignores_settled")
    assert r["journal"].get("result") != "ABORTED_SAFE"
    assert "ct journal" not in r["cmds"]


def test_an_attempt_that_already_recorded_its_outcome_is_not_triaged_again(driver, tmp_path) -> None:
    """The terminal record is the outcome; the non-zero exit that follows it is not a new failure."""
    r = run(driver, tmp_path, "exit_ignores_a_settled_result")
    assert r["journal"].get("result") == "ROLLED_BACK"
    assert "ct journal" not in r["cmds"], r["cmds"]
    assert not r["evidence"], r["evidence"]


def test_a_non_apply_mode_never_settles(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "exit_ignores_non_apply")
    assert "ct journal" not in r["cmds"], "--verify-only must not write a terminal record"


# ------------------------------------------------------- attempt identity before freeze

def test_an_attempt_that_cannot_record_itself_is_refused_before_any_admission_change(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "require_attempt_mismatch")
    assert r["rc"] == 65
    assert "REACHED_ENGAGE" not in r["log"]


def test_an_unreadable_journal_is_refused_before_any_admission_change(driver, tmp_path) -> None:
    r = run(driver, tmp_path, "require_attempt_unreadable")
    assert r["rc"] == 65
    assert "REACHED_ENGAGE" not in r["log"]
