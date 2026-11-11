"""EXT-17: the quiet interval measures work, and the supervisor envelope is not inert.

Two defects were found by RESTORE-002's own production outcome (RECON-01), not by a test:

* **R-13** — ``quiet_window_elapsed`` was computed as ``now - gateway_state.updated_at`` >= 120 s.
  ``updated_at`` is the running gateway's liveness heartbeat: it is rewritten every few seconds
  whether or not any work is running, and it was rewritten *inside the freeze* during attempt 3
  (``since_last_activity=2s``). The gate was therefore unsatisfiable on a live gateway. Attempt 3
  took nine in-freeze samples across 180 s, none qualified, and the engine aborted with
  ``ABORTED_SAFE`` without mutating anything: ``active_agents`` was 0, ``live_admissions`` was 0, the
  freeze was owned by the attempt, and no cron work was in flight. The measurement was wrong, not
  the host.
* **R-11** — the supervisor unit was created as ``Type=oneshot`` with ``RuntimeMaxSec=1500``. systemd
  ignores ``RuntimeMaxSec`` for oneshot units (``RuntimeMaxSec= has no effect in combination with
  Type=oneshot. Ignoring.``), so the advertised envelope did not bound a run at all; only the
  engine's own per-phase budgets did. Making the cap effective is only half the fix: the cap arrives
  as SIGTERM, and the engine used to answer a signal by leaving its drain marker and ESTOP engaged
  with no journal settlement.

These tests are behavioural where the code is behavioural (the two gate helpers and the session
work clock), and pinned-source where the artefact *is* source (a systemd property, a trap): a stub
cannot prove what systemd does with a unit, and the empirical check for that lives in the PR
evidence. They run without a host, a gateway, a session database or release authority.

    env -u PYTHONPATH /usr/bin/python3.13 -m pytest tests/harness/test_release002_ext17.py -q
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

import release002_control as ctl  # noqa: E402
import release002_preflight as pre  # noqa: E402

ENGINE = ROOT / "harness" / "release002_release.sh"
LAUNCHER = ROOT / "harness" / "hermes-release-002"

WINDOW = 120.0
HEARTBEAT_MAX = 180.0


def iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


# --------------------------------------------------------------------------- R-13: the work clock

class _Fact:
    """Duck-typed stand-in for ``gateway.session_resume_plan.SessionFact``.

    ``session_state`` only reads attributes; using the real dataclass would drag the candidate's
    module (and its own imports) into a harness test that is meant to prove the harness's arithmetic.
    """

    def __init__(self, session_id: str, age_s: float | None, *, ended: bool = False,
                 message_count: int = 1) -> None:
        self.session_id = session_id
        self.started_at = time.time() - 3600
        self.message_count = message_count
        self.ended_at = (time.time() - 60) if ended else None
        self.last_ts = None if age_s is None else time.time() - age_s


class _Planner:
    def __init__(self, facts: list[_Fact]) -> None:
        self._facts = facts

    def load_facts(self, db_path: str, index_path: str | None = None):  # noqa: ANN001 - stub
        return self._facts

    def classify(self, facts, *, now):  # noqa: ANN001 - stub
        return {"open": [f for f in facts if f.ended_at is None]}


@pytest.fixture()
def pinned_planner(monkeypatch):
    """Serve ``session_state`` a fake pinned planner instead of a git blob."""
    def install(facts: list[_Fact]) -> None:
        monkeypatch.setattr(
            ctl, "load_pinned_module",
            lambda *a, **k: {"ok": True, "module": _Planner(facts), "path": "gateway/session_resume_plan.py",
                             "sha256": "0" * 64, "commit": "c" * 40, "repo": "/tmp/stub"},
        )
    return install


def test_session_state_reports_the_newest_work_stamp_across_every_session(pinned_planner) -> None:
    """The work clock is the newest ``last_activity_at``, open rows and ended rows alike."""
    pinned_planner([
        _Fact("s-quiet", 900),
        _Fact("s-open", 40),
        _Fact("s-ended-but-latest", 5, ended=True),
    ])
    state = pre.session_state(WINDOW, candidate="c" * 40, source_repo="/tmp/stub")

    assert state["open"] == 2
    assert state["newest_activity_session"] == "s-ended-but-latest"
    assert state["newest_activity_age_s"] == pytest.approx(5, abs=2)


def test_quiet_interval_is_dated_from_work_when_the_heartbeat_is_two_seconds_old() -> None:
    """The exact production failure: a live gateway must not make the interval unmeasurable.

    Attempt 3 read ``since_last_activity=2s`` from ``updated_at`` on every sample and aborted. Here
    the same 2 s heartbeat is present — but the newest recorded work is 900 s old, so the interval
    has elapsed and is allowed to say so.
    """
    now = time.time()
    gw_state = {"gateway_state": "draining", "active_agents": 0, "updated_at": iso(2)}
    state = {"open": 3, "newest_activity_epoch": now - 900, "newest_activity_age_s": 900,
             "newest_activity_session": "s-quiet"}

    age = pre.heartbeat_age_s(gw_state, now=now)
    assert age == pytest.approx(2, abs=1)
    assert age <= HEARTBEAT_MAX, "a 2 s heartbeat is fresh: liveness is fine, only the clock was wrong"

    anchor, detail = pre.quiet_anchor(state=state, freeze_since=now - 950, now=now)
    assert now - anchor >= WINDOW
    assert "since_last_work" not in detail  # detail is the clock's account, not the gate's
    assert "work_age=900s" in detail


def test_recent_work_restarts_the_interval_even_inside_the_freeze() -> None:
    """A turn that wrote 10 s ago blocks the stop, however old the freeze is."""
    now = time.time()
    anchor, detail = pre.quiet_anchor(
        state={"newest_activity_epoch": now - 10, "newest_activity_age_s": 10,
               "newest_activity_session": "s-live"},
        freeze_since=now - 600, now=now)
    assert anchor == pytest.approx(now - 10, abs=1)
    assert now - anchor < WINDOW, "10 s of work is not 120 s of quiet"
    assert "s-live" in detail


def test_the_raw_epoch_wins_over_a_stale_derived_age() -> None:
    """Idle is measured from the recorded instant, not from an age computed seconds earlier."""
    now = time.time()
    anchor, detail = pre.quiet_anchor(
        state={"newest_activity_epoch": now - 30, "newest_activity_age_s": 600,
               "newest_activity_session": "s-epoch"},
        freeze_since=None, now=now)
    assert anchor == pytest.approx(now - 30, abs=1), "the stale 600s age must not be used"
    assert now - anchor < WINDOW
    assert "work_age=30s" in detail


def test_the_freeze_instant_is_an_anchor_but_never_a_shortcut() -> None:
    """With no work on record the interval counts from the freeze — admission is closed from then."""
    now = time.time()
    fresh_freeze, _ = pre.quiet_anchor(state={"newest_activity_age_s": None}, freeze_since=now - 30,
                                       now=now)
    assert fresh_freeze is not None and now - fresh_freeze < WINDOW

    old_freeze, _ = pre.quiet_anchor(state={"newest_activity_age_s": None}, freeze_since=now - 200,
                                     now=now)
    assert old_freeze is not None and now - old_freeze >= WINDOW


def test_an_unreadable_store_is_unevaluable_and_never_assumed_idle() -> None:
    now = time.time()
    anchor, why = pre.quiet_anchor(state={"error": "session state unreadable: OSError: no such file"},
                                   freeze_since=now - 600, now=now)
    assert anchor is None, "an old freeze must not rescue a store that cannot be read"
    assert "work clock unavailable" in why


def test_no_clock_at_all_is_unevaluable() -> None:
    anchor, why = pre.quiet_anchor(state={"newest_activity_age_s": None}, freeze_since=None,
                                   now=time.time())
    assert anchor is None
    assert "no work clock" in why


@pytest.mark.parametrize("gw_state", [
    {"error": "gateway_state.json unreadable"},
    {},
    {"gateway_state": "running"},
])
def test_heartbeat_age_is_none_when_it_cannot_be_dated(gw_state) -> None:
    assert pre.heartbeat_age_s(gw_state, now=time.time()) is None


def test_a_stale_heartbeat_fails_even_when_the_work_clock_says_quiet() -> None:
    """Freshness stayed, as the liveness cross-check: stale counters are not evidence of idleness."""
    now = time.time()
    age = pre.heartbeat_age_s({"updated_at": iso(600)}, now=now)
    assert age is not None and age > HEARTBEAT_MAX
    anchor, _ = pre.quiet_anchor(state={"newest_activity_age_s": 900}, freeze_since=None, now=now)
    assert anchor is not None and now - anchor >= WINDOW
    # The gate the engine reads is the conjunction of the two, and this sample is not quiet enough.
    assert not (age <= HEARTBEAT_MAX and (now - anchor) >= WINDOW)


def test_both_gates_are_in_the_in_freeze_gate_set() -> None:
    """A gate that exists but is not evaluated is exactly the defect this closes."""
    src = Path(str(pre.__file__)).read_text()
    block = src[src.index("QUIESCENCE_GATES = ("):src.index("if in_freeze:")]
    assert '"quiet_window_elapsed"' in block
    assert '"gateway_heartbeat_fresh"' in block
    assert '"cron_no_claims_after_freeze", "gateway_heartbeat_fresh"' in block


def test_the_quiet_gate_no_longer_ages_the_heartbeat() -> None:
    """Regression guard for the original expression, which cannot be allowed back."""
    src = Path(str(pre.__file__)).read_text()
    assert "since_last_activity" not in src
    assert "now - _stamp(gw_state.get(\"updated_at\")" not in src


# ------------------------------------------------------------------------ R-11: the real envelope

def supervisor_args() -> str:
    text = LAUNCHER.read_text()
    return text[text.index("  local args=("):text.index("/bin/bash \"$ENGINE_DIR")]


def test_supervisor_unit_can_be_bounded_by_systemd() -> None:
    """``RuntimeMaxSec`` is ignored on ``Type=oneshot``; the unit must therefore not be oneshot."""
    args = supervisor_args()
    assert "--property=Type=exec" in args
    assert "Type=oneshot" not in args, (
        "RuntimeMaxSec has no effect in combination with Type=oneshot — the envelope would be inert")
    assert '--property=RuntimeMaxSec="$runtime_max"' in args
    assert "--property=SendSIGKILL=yes" in args, "a settlement that hangs must still be killed"


def test_a_signal_settles_the_attempt_instead_of_abandoning_the_freeze() -> None:
    """SIGTERM is how the envelope now arrives, so it has to reach the settlement path."""
    text = ENGINE.read_text()
    on_signal = text[text.index("on_signal() {"):text.index("trap on_signal INT TERM")]
    assert 'case "$PHASE" in' in on_signal and "rollback|done|recover|aborted) exit 70" in on_signal
    assert "PHASE=interrupted" in on_signal
    assert "trap on_signal INT TERM" in text

    on_exit = text[text.index("on_exit() {"):text.index("trap on_exit EXIT")]
    assert "rollback|done|recover|aborted) return 0" in on_exit
    assert "interrupted) return 0" not in on_exit, (
        "an interrupted attempt must settle: rollback if mutated, else release this run's freeze "
        "and journal ABORTED_SAFE")
    assert 'freeze_release "aborted in phase' in on_exit
    assert "--result ABORTED_SAFE" in on_exit


def test_the_supervisor_envelope_still_covers_the_phase_budgets() -> None:
    """The correction must not shrink the envelope: it only makes the same number real."""
    for budget in ("MAX_QUIESCE=${MAX_QUIESCE:-180}", "QUIESCE_WAIT=${QUIESCE_WAIT:-180}",
                   "STARTUP_WAIT=${STARTUP_WAIT:-240}", "DRAIN_ENGAGE_WAIT=${DRAIN_ENGAGE_WAIT:-45}",
                   "UNFREEZE_WAIT=${UNFREEZE_WAIT:-60}"):
        assert budget in ENGINE.read_text()
