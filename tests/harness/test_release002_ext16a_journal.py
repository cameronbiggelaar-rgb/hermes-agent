"""EXT-16A D-10: the journal must record mutation truthfully, and keep it.

The isolated APPLY deployed the candidate and verified it, yet its authoritative journal ended with
``mutated: false``. The engine held mutation only in the shell variable ``MUTATED`` (set just
before the merge as a fail-closed *intent* guard) and never wrote it to the journal, so:

* the successful run's receipt claimed nothing had changed;
* a failure between merge and the next phase could only be classified post-mutation by the
  ``PHASES_AFTER_MUTATION`` phase-name heuristic;
* an attempt interrupted during ``unfreeze``/``report`` — after the deploy receipt — looked
  pre-mutation to ``decide_recovery`` and would have been "unfrozen" rather than completed or
  rolled back.

The correction keeps the two concepts apart: ``MUTATED`` stays the intent guard, while the journal
records mutation from the receipt that *proves* the change (deploy: HEAD, tree and clean worktree
verified) and every later record re-derives the flag from the journal itself.

    env -u PYTHONPATH /usr/bin/python3 -m pytest tests/harness/test_release002_ext16a_journal.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "harness" / "release002_release.sh"
CONTROL = ROOT / "harness" / "release002_control.py"

sys.path.insert(0, str(ROOT / "harness"))
import release002_control as ctl  # noqa: E402

CANDIDATE = "450a93872d5be7b6e5b14c0a523a3b0c26e08945"
BASELINE = "3652a3702dae8d2917b91b6949da580efd3ef496"


def engine() -> str:
    return ENGINE.read_text()


def engine_code() -> str:
    """The script with whole-line comments removed: assertions are about behaviour, not prose."""
    return "\n".join(line for line in ENGINE.read_text().splitlines()
                     if not line.lstrip().startswith("#"))


# --------------------------------------------------------------------------- journal contract
def test_successful_merge_records_mutation() -> None:
    journal = ctl.phase_record(None, phase="start", extra={"candidate": CANDIDATE}, run_id="r1")
    journal = ctl.phase_record(journal, phase="deploy", mutated=True, note=f"deployed {CANDIDATE}",
                               extra={"head": CANDIDATE, "candidate": CANDIDATE})
    assert journal["mutated"] is True
    deploy = journal["phases"][-1]
    assert deploy["phase"] == "deploy"
    assert deploy["mutated"] is True            # dated on the record that proves the change
    state = ctl.mutation_state(journal)
    assert state["mutated"] is True
    assert state["recorded"] is True
    assert state["since_phase"] == "deploy"
    assert state["since_at"] == deploy["at"]


def test_mutation_is_sticky_through_every_later_phase() -> None:
    journal = ctl.phase_record(None, phase="start", extra={"candidate": CANDIDATE}, run_id="r1")
    journal = ctl.phase_record(journal, phase="deploy", mutated=True, extra={"candidate": CANDIDATE})
    for phase in ("unit", "restart", "verify", "unfreeze", "report"):
        journal = ctl.phase_record(journal, phase=phase)
        assert journal["mutated"] is True, phase
    assert ctl.mutation_state(journal)["since_phase"] == "deploy"


def test_a_refusal_before_the_merge_stays_unmutated() -> None:
    journal = ctl.phase_record(None, phase="start", extra={"candidate": CANDIDATE}, run_id="r1")
    journal = ctl.phase_record(journal, phase="revalidate", note="in-freeze revalidation NO_GO")
    journal = ctl.phase_record(journal, phase="aborted", result="ABORTED_SAFE")
    assert journal["mutated"] is False
    assert ctl.mutation_state(journal)["mutated"] is False
    # ... and the failure contract then permits nothing but releasing the freeze.
    decision = ctl.decide_failure("revalidate", mutated=ctl.mutation_state(journal)["mutated"])
    assert decision["action"] == "release_freeze"
    assert decision["restart"] is False


def test_a_failure_after_the_merge_is_post_mutation() -> None:
    journal = ctl.phase_record(None, phase="start", extra={"candidate": CANDIDATE}, run_id="r1")
    journal = ctl.phase_record(journal, phase="deploy", mutated=True, extra={"candidate": CANDIDATE})
    journal = ctl.phase_record(journal, phase="unit")
    mutated = ctl.mutation_state(journal)["mutated"]
    decision = ctl.decide_failure("unit", mutated=mutated, restart_attempts=0)
    assert decision["action"] == "rollback"
    assert decision["restart"] is True
    # Without the truthful flag the same failure would have been classified as unknown state.
    assert ctl.decide_failure("unit", mutated=False)["action"] == "needs_human"


def test_rollback_preserves_the_mutation_history() -> None:
    journal = ctl.phase_record(None, phase="start", extra={"candidate": CANDIDATE}, run_id="r1")
    journal = ctl.phase_record(journal, phase="deploy", mutated=True, extra={"candidate": CANDIDATE})
    journal = ctl.phase_record(journal, phase="rollback", mutated=True, note="rollback started")
    journal = ctl.phase_record(journal, phase="report", mutated=True, result="ROLLED_BACK")
    state = ctl.mutation_state(journal)
    assert state["mutated"] is True
    assert state["since_phase"] == "deploy"
    assert state["result"] == "ROLLED_BACK"
    assert state["phase"] == "report"
    assert [r["phase"] for r in journal["phases"]] == ["start", "deploy", "rollback", "report"]


def test_interruption_immediately_after_the_first_mutation() -> None:
    """The deploy record is the last thing written: recovery must treat the attempt as mutated."""
    journal = ctl.phase_record(None, phase="start", extra={"candidate": CANDIDATE}, run_id="r1")
    journal = ctl.phase_record(journal, phase="freeze", extra={"candidate": CANDIDATE})
    journal = ctl.phase_record(journal, phase="deploy", mutated=True, extra={"head": CANDIDATE,
                                                                   "candidate": CANDIDATE})
    facts = {"install_head": CANDIDATE, "candidate": CANDIDATE, "deployed": BASELINE,
             "freeze_present": True, "estop_present": True}
    plan = ctl.decide_recovery(journal, {**facts, "service_active": False})
    assert plan["action"] == "rollback"           # candidate is checked out, nothing is serving
    plan = ctl.decide_recovery(journal, {**facts, "service_active": True})
    assert plan["action"] == "complete_verification"
    # An unreadable service state is never guessed into a rollback or a completion.
    assert ctl.decide_recovery(journal, {**facts, "service_active": None})["action"] == "needs_human"


def test_interruption_after_the_deploy_receipt_is_not_treated_as_unmutated() -> None:
    """``unfreeze``/``report`` are outside PHASES_AFTER_MUTATION: only the journal can prove it."""
    journal = ctl.phase_record(None, phase="deploy", mutated=True, extra={"candidate": CANDIDATE})
    journal = ctl.phase_record(journal, phase="unfreeze", note="admission restored: release verified")
    facts = {"install_head": CANDIDATE, "candidate": CANDIDATE, "deployed": BASELINE,
             "service_active": True, "freeze_present": False, "estop_present": False}
    assert ctl.decide_recovery(journal, facts)["action"] == "complete_verification"


def test_an_old_journal_with_a_deploy_receipt_still_counts_as_mutated() -> None:
    """Backwards compatibility: journals written before D-10 carry no flag but do carry deploy."""
    legacy = {"phase": "unit", "run_id": "legacy",
              "phases": [{"phase": "start", "at": "2026-11-06T00:00:00+00:00", "note": ""},
                         {"phase": "deploy", "at": "2026-11-06T00:01:00+00:00",
                          "note": f"deployed {CANDIDATE} tree x", "head": CANDIDATE}]}
    state = ctl.mutation_state(legacy)
    assert state["mutated"] is True
    assert state["recorded"] is False             # honest: the old engine never recorded it
    assert state["since_phase"] == "deploy"


def test_journal_state_cli_reports_the_authoritative_state(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = ctl.phase_record(None, phase="deploy", mutated=True, extra={"candidate": CANDIDATE},
                             run_id="r1")
    ctl.write_journal(journal, str(path))
    proc = subprocess.run([sys.executable, str(CONTROL), "journal-state", "--path", str(path)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    state = json.loads(proc.stdout)["state"]
    assert state["mutated"] is True
    assert state["since_phase"] == "deploy"
    assert state["candidate"] == CANDIDATE
    # No journal is "unevaluable", never "unmutated".
    missing = subprocess.run([sys.executable, str(CONTROL), "journal-state", "--path",
                              str(tmp_path / "absent.json")], capture_output=True, text=True,
                             timeout=60)
    assert missing.returncode == 3


# --------------------------------------------------------------------------- engine wiring
def test_engine_marks_the_deploy_receipt_as_the_mutation() -> None:
    src = engine()
    assert 'journal deploy "deployed $HEAD tree $TREE" --head "$HEAD" --mutated' in src
    assert "JOURNAL_MUTATED=1" in src
    deploy = src[src.index('journal deploy "deployed $HEAD tree $TREE"'):]
    # the flag is set on the receipt, and only after HEAD/tree/dirty were verified above it
    assert deploy.index("JOURNAL_MUTATED=1") < deploy.index("phase 6: regenerate unit")


def test_engine_derives_the_flag_from_the_journal_not_the_intent_guard() -> None:
    src = engine()
    code = engine_code()
    block = code[code.index("journal() { # journal <phase>"):]
    block = block[:block.index("\n}\n") + 3]
    assert "JOURNAL_MUTATED:-0" in block
    assert '\'"mutated": true\'' in block                 # re-derived from the journal on every write
    assert "MUTATED" not in block.replace("JOURNAL_MUTATED", "")   # never from the intent guard
    assert "--mutated" in block


def test_engine_never_writes_mutation_before_the_merge() -> None:
    code = engine_code()
    deploy_at = code.index('journal deploy "deployed $HEAD tree $TREE"')
    pre = code[:deploy_at]
    # No journal call before the merge may claim a mutation, and nothing may set the journal's
    # record before the change is proven. (The failure-decision call is a different question: it is
    # allowed to *ask* whether the attempt is post-mutation, and answers from the journal.)
    calls = [line for line in pre.splitlines() if line.strip().startswith("journal ")]
    assert calls, "expected phase records before the merge"
    assert all("--mutated" not in line for line in calls), calls
    assert "JOURNAL_MUTATED=1" not in pre


def test_engine_failure_decision_sees_the_journalled_mutation() -> None:
    """The contract is asked with --mutated when either record says a mutation may have happened."""
    src = engine()
    block = src[src.index("abort_before_mutation() {"):src.index("rollback() { # rollback <reason>")]
    assert "ct failure --phase" in block
    assert "grep -q '\"mutated\": true' \"$JOURNAL\"" in block
    assert 'mutated_flag="--mutated"' in block
    assert '$mutated_flag' in block


def test_receipt_states_the_journal_mutation_state() -> None:
    src = engine()
    receipt = src[src.index("receipt() { #"):src.index("keepalive_loop() {")]
    assert "journal-state" in receipt
    assert "journal state:" in receipt


def test_both_mutation_variables_are_initialised() -> None:
    """Two variables, two jobs: the intent guard starts clear, and so does the journal's record."""
    code = engine_code()
    head = code[:code.index("PHASE=init")]
    assert "MUTATED=0" in head
    assert "JOURNAL_MUTATED=0" in head


def test_engine_still_syntax_checks() -> None:
    bash = shutil.which("bash")
    assert bash
    proc = subprocess.run([bash, "-n", str(ENGINE)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr