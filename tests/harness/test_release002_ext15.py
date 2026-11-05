"""EXT-15: the release driver's receipts, attempt identity and fail-closed admission evidence.

RESTORE-002 stopped safely (STOPPED_SAFE) with zero mutation, but the driver's receipt writer
only existed on the post-APPLY path, so the safe stop left no receipt at all and the outcome had
to be reconstructed from host evidence afterwards. EXT-15 phase 5 pins the correction:

* every terminal result writes summary.json + RECEIPT.md exactly once, atomically;
* every run carries an exact attempt identity and a phase log with begin/end timestamps;
* a stop that never reached PLAN records PLAN_NOT_ENTERED — never an implied PLAN pass;
* the actual failed gates are recorded per sample and in union;
* a GO is accepted only when its counters are present and self-consistent;
* an interrupted quiet window resets the whole observation;
* a killed driver leaves no success receipt, and a resumed driver never applies a second time.

These tests run the real ``harness/release002_driver.sh`` against stub ``git``/``systemctl``/
``sudo``/``gh`` and a stub launcher — no host, no gateway, no release authority.

    env -u PYTHONPATH /usr/bin/python3 -m pytest tests/harness/test_release002_ext15.py -q
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "harness" / "release002_driver.sh"

CAND = "16a4d39a418f16ebac8eeb27ce7585158ce9e8ef"
HEAD = "e05128febbc559469a5fb835a9235a543dfdfa64"
TREE = "3ff43dc4aed41d3cc8bbe8df527aedbd6b77466f"
PID = "4242"

GIT_STUB = """#!/usr/bin/env bash
if [ "$1 $3" = "-C rev-parse" ] || [ "$2" = "rev-parse" ]; then
  case "$*" in
    *"^{tree}"*) echo "${STUB_GIT_TREE:-3ff43dc4aed41d3cc8bbe8df527aedbd6b77466f}"; exit 0;;
  esac
  echo "${STUB_GIT_HEAD}"; exit 0
fi
exit 1
"""

SYSTEMCTL_STUB = """#!/usr/bin/env bash
case "$1" in
  list-units) [ "${STUB_RELEASE_UNITS:-0}" = 1 ] && echo "hermes-release-002.service loaded failed failed" || true; exit 0;;
  show) echo "${STUB_MAINPID:-4242}"; exit 0;;
esac
exit 0
"""

SUDO_STUB = """#!/usr/bin/env bash
shift          # -n
exec "$@"
"""

GH_STUB = """#!/usr/bin/env bash
echo "$*" >> "${STUB_GH_LOG}"
exit "${STUB_GH_RC:-0}"
"""

# Each token is a complete PLAN payload. The driver must accept a GO only when the counters that
# justify it are present and agree with it.
LAUNCHER_STUB = """#!/usr/bin/env bash
verb="$1"; shift
if [ "$verb" = "apply" ]; then
  echo "apply $*" >> "${STUB_APPLY_LOG}"
  [ -n "${STUB_APPLY_SLEEP:-}" ] && sleep "${STUB_APPLY_SLEEP}"
  exit "${STUB_APPLY_RC:-0}"
fi
echo "plan $*" >> "${STUB_PLAN_LOG}"
seqf="${STUB_PLAN_SEQ}"
tok=""
if [ -s "$seqf" ]; then tok="$(head -1 "$seqf")"; tail -n +2 "$seqf" > "$seqf.tmp"; mv "$seqf.tmp" "$seqf"; fi
case "${tok:-NO_GO}" in
  GO)
    printf '{"verdict":"GO","failed":[],"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n'; exit 0;;
  GO_TERMINAL)
    printf '{"verdict":"GO","failed":[],"evidence":{"runtime":{"active_agents":0,"live_admissions":0,"terminal_admissions":5}}}\\n'; exit 0;;
  CONTRADICT)
    printf '{"verdict":"GO","failed":[],"evidence":{"runtime":{"active_agents":3,"live_admissions":3}}}\\n'; exit 0;;
  GO_NO_EVIDENCE)
    printf '{"verdict":"GO","failed":[],"evidence":{}}\n'; exit 0;;
  GO_GARBAGE)
    printf '{"verdict":"GO","failed":[],"evidence":{"runtime":{"active_agents":"unknown","live_admissions":0}}}\n'; exit 0;;
  GO_WITH_GATES)
    printf '{"verdict":"GO","failed":["recent_session_activity"],"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\n'; exit 0;;
  NO_GO)
    printf '{"verdict":"NO_GO","failed":["no_active_turns","recent_session_activity"],"evidence":{"runtime":{"active_agents":%s,"live_admissions":%s}}}\\n' "${STUB_AGENTS:-2}" "${STUB_LIVE:-2}"; exit 2;;
  NO_GO_LIVE)
    printf '{"verdict":"NO_GO","failed":["live_admissions"],"evidence":{"runtime":{"active_agents":0,"live_admissions":1,"live_admission_age_s":28800}}}\\n'; exit 2;;
  NO_GO_CRON)
    printf '{"verdict":"NO_GO","failed":["no_active_turns"],"evidence":{"runtime":{"active_agents":1,"live_admissions":1,"cron_running":true}}}\\n'; exit 2;;
  NO_GO_OTHER)
    printf '{"verdict":"NO_GO","failed":["install_head_mismatch"],"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n'; exit 2;;
  UNEVAL)
    printf '{"verdict":"UNEVALUABLE","failed":["gate_unevaluable"],"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n'; exit 3;;
esac
exit 2
"""


class Host:
    """A disposable host shape: stub PATH, fake install, fake unit dir, fake /proc entry."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (
            ("git", GIT_STUB),
            ("systemctl", SYSTEMCTL_STUB),
            ("sudo", SUDO_STUB),
            ("gh", GH_STUB),
            ("hermes-release-002", LAUNCHER_STUB),
        ):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)

        self.install = tmp_path / "install"
        self.install.mkdir()
        self.evidence = tmp_path / "evidence"
        self.unit_dir = tmp_path / "etc" / "systemd" / "system"
        dropins = self.unit_dir / "hermes-gateway.service.d"
        dropins.mkdir(parents=True)
        (dropins / "90-recovery-hold.conf").write_text("[Service]\n")
        (dropins / "95-timeout-stop.conf").write_text("[Service]\nTimeoutStopSec=300s\n")

        self.proc_root = tmp_path / "proc"
        envdir = self.proc_root / PID
        envdir.mkdir(parents=True)
        (envdir / "environ").write_bytes(b"PATH=/usr/bin\0HERMES_RECOVERY_HOLD=1\0")

        self.journal = tmp_path / "state" / "journal.json"
        self.seq = tmp_path / "plan-seq"
        self.plan_log = tmp_path / "plan.log"
        self.apply_log = tmp_path / "apply.log"
        self.gh_log = tmp_path / "gh.log"

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        e.update(
            PATH=f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            STUB_GIT_HEAD=HEAD,
            STUB_GIT_TREE=TREE,
            STUB_PLAN_SEQ=str(self.seq),
            STUB_PLAN_LOG=str(self.plan_log),
            STUB_APPLY_LOG=str(self.apply_log),
            STUB_GH_LOG=str(self.gh_log),
            STUB_MAINPID=PID,
            TMPDIR=str(self.root),
        )
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def argv(self, *, mode="plan", open_after="+0", deadline="+4", publish=0, quiet_window=130,
             poll=1, settle=1, gateway_state=None):
        return [
            "bash", str(DRIVER),
            "--mode", mode,
            "--candidate", CAND,
            "--expect-head", HEAD,
            "--expect-tree", TREE,
            "--install", str(self.install),
            "--launcher", str(self.bin / "hermes-release-002"),
            "--evidence-dir", str(self.evidence),
            "--open-after", open_after,
            "--deadline", deadline,
            "--quiet-window", str(quiet_window),
            "--poll", str(poll),
            "--settle", str(settle),
            "--publish", str(publish),
            "--journal", str(self.journal),
            "--unit-dir", str(self.unit_dir),
            "--proc-root", str(self.proc_root),
        ] + (["--gateway-state", str(gateway_state)] if gateway_state else [])

    def run(self, **kw):
        publish = kw.pop("publish", 0)
        extra = kw.pop("env", {})
        seq = kw.pop("seq", None)
        if seq is not None:
            self.seq.write_text("\n".join(seq) + "\n")
        return subprocess.run(
            self.argv(publish=publish, **kw),
            cwd=str(self.install),
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=180,
        )

    def popen(self, **kw):
        extra = kw.pop("env", {})
        seq = kw.pop("seq", None)
        if seq is not None:
            self.seq.write_text("\n".join(seq) + "\n")
        return subprocess.Popen(
            self.argv(**kw), cwd=str(self.install), env=self.env(**extra),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    # --- receipt readers -------------------------------------------------
    @property
    def receipt(self) -> Path:
        return self.evidence / "RECEIPT.md"

    def summary(self) -> dict:
        return json.loads((self.evidence / "summary.json").read_text())

    def attempts(self) -> list[Path]:
        d = self.evidence / "attempts"
        return sorted(p for p in d.iterdir() if p.is_dir()) if d.exists() else []

    def applied(self) -> int:
        return len(self.apply_log.read_text().splitlines()) if self.apply_log.exists() else 0

    def plans(self) -> int:
        return len(self.plan_log.read_text().splitlines()) if self.plan_log.exists() else 0

    def published(self) -> int:
        return len(self.gh_log.read_text().splitlines()) if self.gh_log.exists() else 0

    def seed_prior_attempt(self, attempt_id="deadbeefdeadbeef", apply_started=True):
        d = self.evidence / "attempts" / attempt_id
        d.mkdir(parents=True)
        (d / "attempt.json").write_text(json.dumps(
            {"attempt_id": attempt_id, "mode": "apply", "apply_started": apply_started,
             "plan_state": "ENTERED_GO"}) + "\n")
        return d


@pytest.fixture()
def host(tmp_path):
    return Host(tmp_path)


# ---------------------------------------------------------------- receipt durability
def test_deadline_without_go_writes_stopped_safe_receipt(host):
    proc = host.run(mode="plan", seq=["NO_GO"] * 20, deadline="+3")
    assert proc.returncode == 2, proc.stderr
    assert host.receipt.exists()
    text = host.receipt.read_text()
    assert "STOPPED_SAFE" in text
    assert "never started" in text
    s = host.summary()
    assert s["disposition"] == "STOPPED_SAFE"
    assert s["apply_started"] is False
    assert s["samples"] >= 1
    assert s["agents_min"] == "2" and s["agents_max"] == "2"
    assert host.applied() == 0


def test_plan_mode_go_records_go_ready_and_never_applies(host):
    proc = host.run(mode="plan", seq=["GO"], deadline="+30", poll=1, settle=1)
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["disposition"] == "GO_READY"
    assert s["plan_rc"] == "0"
    assert s["plan_state"] == "ENTERED_GO"
    assert s["apply_started"] is False
    assert host.applied() == 0


def test_apply_mode_rechecks_quiescence_across_settle(host):
    proc = host.run(mode="apply", seq=["GO", "NO_GO"], deadline="+6", settle=1)
    assert proc.returncode == 2, proc.stderr
    assert host.applied() == 0, "a settle re-check that loses quiescence must not apply"
    assert host.plans() >= 2
    assert "STOPPED_SAFE" in host.receipt.read_text()
    assert host.summary()["quiet_window_restarts"] >= 1


def test_apply_mode_applies_exactly_once_after_go(host):
    proc = host.run(mode="apply", seq=["GO", "GO", "GO", "GO"], deadline="+30", settle=1)
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["disposition"] == "APPLIED"
    assert s["apply_started"] is True
    assert s["apply_rc"] == "0"
    assert host.applied() == 1, "the driver must apply exactly once — never retry"
    text = host.receipt.read_text()
    assert "runner" in text.lower() and "authoritative" in text.lower()
    assert "RELEASED" not in text, "the driver must not claim operational verification itself"


def test_unevaluable_plan_fails_closed_as_needs_human(host):
    proc = host.run(mode="apply", seq=["UNEVAL"], deadline="+30")
    assert proc.returncode == 3, proc.stderr
    assert host.summary()["disposition"] == "NEEDS_HUMAN"
    assert host.summary()["plan_state"] == "UNEVALUABLE"
    assert host.applied() == 0
    assert "NEEDS_HUMAN" in host.receipt.read_text()


@pytest.mark.parametrize("mutate,needle", [
    ({"journal": True}, "journal_absent"),
    ({"git_head": "deadbeef"}, "install_head"),
    ({"git_tree": "deadbeef"}, "install_tree"),
    ({"units": 1}, "no_release_units"),
    ({"dropins": 1}, "protective_dropins"),
])
def test_guard_failure_writes_receipt_and_never_polls(host, mutate, needle):
    if mutate.get("journal"):
        host.journal.parent.mkdir(parents=True, exist_ok=True)
        host.journal.write_text("{}\n")
    extra = {}
    if mutate.get("git_head"):
        extra["STUB_GIT_HEAD"] = mutate["git_head"]
    if mutate.get("git_tree"):
        extra["STUB_GIT_TREE"] = mutate["git_tree"]
    if mutate.get("units"):
        extra["STUB_RELEASE_UNITS"] = 1
    if mutate.get("dropins"):
        (host.unit_dir / "hermes-gateway.service.d" / "95-timeout-stop.conf").unlink()
    proc = host.run(mode="apply", seq=["GO", "GO"], env=extra, deadline="+30")
    assert proc.returncode == 4, proc.stderr
    s = host.summary()
    assert s["disposition"] == "GUARD_FAILED"
    assert any(g["name"] == needle and g["ok"] is False for g in s["guards"]), s["guards"]
    assert host.plans() == 0, "guards are evaluated before any launcher call"
    assert host.applied() == 0
    assert host.receipt.exists()


@pytest.mark.live_system_guard_bypass
def test_sigterm_still_writes_a_receipt(host):
    proc = host.popen(mode="apply", seq=["NO_GO"] * 50, deadline="+60", poll=1, settle=1)
    time.sleep(2.0)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 2, (out, err)
    assert host.receipt.exists(), "an interrupted run must still leave a durable receipt"
    assert "interrupted by signal" in host.receipt.read_text()
    assert host.summary()["plan_state"] in ("ENTERED_NO_GO", "NOT_ENTERED")
    assert host.applied() == 0


def test_publication_happens_at_most_once(host):
    proc = host.run(mode="plan", seq=["NO_GO"] * 20, deadline="+2", publish=1)
    assert proc.returncode == 2, proc.stderr
    assert host.receipt.exists()
    assert host.published() == 0, "no receipt target means no publication, but the receipt stays"

    target = ["--receipt-repo", "cameronbiggelaar-rgb/hermes-agent", "--receipt-pr", "9"]
    proc = subprocess.run(
        host.argv(mode="plan", deadline="+2", publish=1) + target,
        cwd=str(host.install), env=host.env(STUB_GH_RC=0), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 2, proc.stderr
    assert host.published() == 1, "a terminal result publishes its receipt exactly once"
    assert (host.evidence / ".published").exists()


def test_failed_publication_keeps_the_receipt(host):
    host.seq.write_text("NO_GO\n" * 20)
    proc = subprocess.run(
        host.argv(mode="plan", deadline="+2", publish=1)
        + ["--receipt-repo", "cameronbiggelaar-rgb/hermes-agent", "--receipt-pr", "9"],
        cwd=str(host.install), env=host.env(STUB_GH_RC=1), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 2, proc.stderr
    assert host.receipt.exists()
    assert "STOPPED_SAFE" in host.receipt.read_text()
    assert not (host.evidence / ".published").exists()


def test_samples_record_the_runtime_snapshot_beside_each_verdict(host):
    state = host.root / "gateway_state.json"
    state.write_text(json.dumps({"active_agents": 3, "updated_at": "2026-11-05T05:34:25Z"}) + "\n")
    proc = host.run(mode="plan", seq=["NO_GO"] * 20, deadline="+2", gateway_state=state)
    assert proc.returncode == 2, proc.stderr
    samples = (host.evidence / "samples.log").read_text().strip().splitlines()
    assert samples, "at least one sample must be recorded"
    for line in samples:
        assert "observed_agents=3" in line, line
        assert "observed_at=2026-11-05T05:34:25Z" in line, line
        assert "gate_active_agents=2" in line, line
        assert "gate_live_admissions=2" in line, line
        assert "gates=no_active_turns;recent_session_activity" in line, line


def test_closed_window_is_a_usage_error_not_a_safe_stop(host):
    proc = host.run(mode="apply", seq=["GO", "GO"], open_after="+0", deadline="+0")
    assert proc.returncode == 3, proc.stderr
    s = host.summary()
    assert s["disposition"] == "NEEDS_HUMAN"
    assert s["plan_state"] == "NOT_ENTERED" and s["plan_entered"] is False
    assert host.plans() == 0 and host.applied() == 0
    assert "already closed" in host.receipt.read_text()
    assert "PLAN_NOT_ENTERED" in host.receipt.read_text()


@pytest.mark.live_system_guard_bypass
def test_interrupt_during_apply_is_needshuman_not_stopped_safe(host):
    env = host.env(STUB_APPLY_SLEEP=30)
    proc = host.popen(mode="apply", seq=["GO", "GO", "GO", "GO"], deadline="+60", settle=1, env=env)
    deadline = time.time() + 30
    while time.time() < deadline and not host.apply_log.exists():
        time.sleep(0.2)
    assert host.apply_log.exists(), "apply should have started"
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=60)
    assert proc.returncode == 3, proc.returncode
    s = host.summary()
    assert s["disposition"] == "NEEDS_HUMAN"
    assert s["apply_started"] is True
    text = host.receipt.read_text()
    assert "interrupted during apply" in text
    assert "No deployment mutation occurred" not in text


# ---------------------------------------------------------------- attempt identity / phases
def test_attempt_identity_is_exact_and_archived(host):
    proc = host.run(mode="plan", seq=["NO_GO"] * 20, deadline="+2")
    assert proc.returncode == 2, proc.stderr
    s = host.summary()
    assert len(s["attempt_id"]) == 32 and int(s["attempt_id"], 16) >= 0
    assert s["attempt_seq"] == 1
    assert s["candidate"] == CAND and s["expect_head"] == HEAD and s["expect_tree"] == TREE
    assert s["install_head"] == HEAD and s["install_tree"] == TREE
    assert s["started_at"] and s["finished_at"] and s["started_at"] <= s["finished_at"]
    assert s["duration_s"] >= 0
    arch = host.attempts()
    assert len(arch) == 1
    assert (arch[0] / "attempt.json").exists() and (arch[0] / "summary.json").exists()
    assert (arch[0] / "RECEIPT.md").exists() and (arch[0] / ".finalised").exists()
    aj = json.loads((arch[0] / "attempt.json").read_text())
    assert aj["state"] == "final" and aj["disposition"] == "STOPPED_SAFE"
    assert aj["finished_at"]


def test_attempt_identity_changes_with_the_identity_tuple(host):
    host.run(mode="plan", seq=["NO_GO"] * 20, deadline="+2")
    first = host.summary()["attempt_id"]
    host.run(mode="apply", seq=["NO_GO"] * 20, deadline="+2")
    second = host.summary()["attempt_id"]
    assert first != second, "mode is part of the attempt identity"
    assert host.summary()["attempt_seq"] == 2
    assert len(host.attempts()) == 2, "each attempt keeps its own immutable archive"


def test_stop_before_plan_records_plan_not_entered(host):
    """A safe stop that never evaluated PLAN must say so — it must not imply PLAN passed."""
    proc = host.run(mode="apply", seq=["GO"], deadline="+30", env={"STUB_RELEASE_UNITS": 1})
    assert proc.returncode == 4, proc.stderr
    s = host.summary()
    assert s["plan_entered"] is False and s["plan_state"] == "NOT_ENTERED"
    assert s["samples"] == 0
    assert "PLAN_NOT_ENTERED" in host.receipt.read_text()
    assert "not a PLAN pass" in host.receipt.read_text()


def test_phase_log_records_begin_and_end_timestamps(host):
    host.run(mode="plan", seq=["NO_GO", "NO_GO", "GO"], deadline="+10", poll=1)
    lines = (host.evidence / "phases.log").read_text().strip().splitlines()
    assert lines
    begins = [ln for ln in lines if "state=begin" in ln]
    ends = [ln for ln in lines if "state=end" in ln]
    assert begins and ends
    for ln in lines:
        assert ln.startswith("20") and ln[4] == "-" and "attempt=" in ln and "phase=" in ln
    names_begin = {ln.split("phase=")[1].split()[0] for ln in begins}
    names_end = {ln.split("phase=")[1].split()[0] for ln in ends}
    assert names_begin <= names_end, (names_begin - names_end)


# ---------------------------------------------------------------- phase 5 adversarial matrix
def test_genuine_active_turn_blocks_quiescence(host):
    proc = host.run(mode="apply", seq=["NO_GO"] * 30, deadline="+3", settle=1,
                    env={"STUB_AGENTS": 2, "STUB_LIVE": 2})
    assert proc.returncode == 2, proc.stderr
    s = host.summary()
    assert s["disposition"] == "STOPPED_SAFE"
    assert s["apply_started"] is False and host.applied() == 0
    assert s["agents_min"] == "2" and s["live_admissions_min"] == "2"
    assert "no_active_turns" in s["all_failed_gates_seen"]


def test_terminal_admission_does_not_count_as_active(host):
    """Completed rows are history: an all-terminal admission table must not block the release."""
    proc = host.run(mode="apply", seq=["GO_TERMINAL", "GO_TERMINAL", "GO_TERMINAL"], deadline="+30", settle=1)
    assert proc.returncode == 0, proc.stderr
    assert host.summary()["disposition"] == "APPLIED"
    assert host.applied() == 1


def test_live_long_running_admission_is_never_treated_as_stale(host):
    """Age alone must never reclassify a live admission — 8 h old here, and still blocking."""
    proc = host.run(mode="apply", seq=["NO_GO_LIVE"] * 30, deadline="+3", settle=1)
    assert proc.returncode == 2, proc.stderr
    s = host.summary()
    assert s["disposition"] == "STOPPED_SAFE" and host.applied() == 0
    assert "live_admissions" in s["all_failed_gates_seen"]
    assert s["live_admissions_min"] == "1"


def test_scheduler_activity_prevents_an_unsafe_restart(host):
    proc = host.run(mode="apply", seq=["NO_GO_CRON"] * 30, deadline="+3", settle=1)
    assert proc.returncode == 2, proc.stderr
    assert host.applied() == 0
    assert "no_active_turns" in host.summary()["all_failed_gates_seen"]


def test_missing_admission_evidence_fails_closed(host):
    proc = host.run(mode="apply", seq=["GO_NO_EVIDENCE"], deadline="+30", settle=1)
    assert proc.returncode == 3, proc.stderr
    s = host.summary()
    assert s["disposition"] == "NEEDS_HUMAN"
    assert "without active_agents/live_admissions evidence" in s["reason"]
    assert host.applied() == 0


def test_contradictory_counters_fail_closed(host):
    proc = host.run(mode="apply", seq=["CONTRADICT"], deadline="+30", settle=1)
    assert proc.returncode == 3, proc.stderr
    s = host.summary()
    assert s["disposition"] == "NEEDS_HUMAN"
    assert "contradictory counters" in s["reason"]
    assert s["apply_started"] is False and host.applied() == 0


def test_unreadable_counters_in_a_go_fail_closed(host):
    """A GO whose counters are not numbers is missing evidence, not an accidental zero."""
    proc = host.run(mode="apply", seq=["GO_GARBAGE"], deadline="+30", settle=1)
    assert proc.returncode == 3, proc.stderr
    s = host.summary()
    assert s["disposition"] == "NEEDS_HUMAN"
    assert "unreadable active_agents" in s["reason"]
    assert host.applied() == 0


def test_a_go_that_lists_failed_gates_fails_closed(host):
    proc = host.run(mode="apply", seq=["GO_WITH_GATES"], deadline="+30", settle=1)
    assert proc.returncode == 3, proc.stderr
    assert "while listing failed gates" in host.summary()["reason"]
    assert host.applied() == 0


def test_quiet_window_interruption_resets_the_required_observation(host):
    seq = ["GO", "NO_GO", "GO", "GO", "GO", "GO", "GO", "GO"]
    proc = host.run(mode="apply", seq=seq, deadline="+25", settle=3, poll=1)
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["disposition"] == "APPLIED" and host.applied() == 1
    assert s["quiet_window_restarts"] >= 1, "the interruption must reset the whole window"
    # the apply is only reached after a fresh, uninterrupted 3 s window
    lines = (host.evidence / "samples.log").read_text().splitlines()
    assert any("verdict=NO_GO" in ln for ln in lines)


def test_unrecovered_interruption_never_applies(host):
    seq = ["GO"] + ["NO_GO"] * 60
    proc = host.run(mode="apply", seq=seq, deadline="+5", settle=3, poll=1)
    assert proc.returncode == 2, proc.stderr
    s = host.summary()
    assert s["disposition"] == "STOPPED_SAFE"
    assert s["quiet_window_restarts"] >= 1
    assert host.applied() == 0


@pytest.mark.live_system_guard_bypass
def test_supervisor_exit_before_plan_stops_safe_with_plan_not_entered(host):
    proc = host.popen(mode="apply", seq=["GO"], open_after="+60", deadline="+120")
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 2, (out, err)
    s = host.summary()
    assert s["disposition"] == "STOPPED_SAFE"
    assert s["plan_entered"] is False and s["plan_state"] == "NOT_ENTERED"
    assert "PLAN_NOT_ENTERED" in host.receipt.read_text()
    assert host.plans() == 0 and host.applied() == 0


@pytest.mark.live_system_guard_bypass
def test_killed_driver_leaves_no_false_success_and_a_resume_records_it(host):
    proc = host.popen(mode="apply", seq=["NO_GO"] * 50, deadline="+60", poll=1, settle=1)
    deadline = time.time() + 20
    while time.time() < deadline and host.plans() < 2:
        time.sleep(0.2)
    assert host.plans() >= 2
    proc.kill()                      # SIGKILL: no trap, no receipt
    proc.communicate(timeout=30)
    assert not (host.evidence / "summary.json").exists(), "a killed driver must not leave a success receipt"
    assert not (host.evidence / "RECEIPT.md").exists()
    killed = host.attempts()
    assert len(killed) == 1
    assert (killed[0] / "attempt.json").exists(), "the attempt identity survives the crash"
    assert not (killed[0] / ".finalised").exists()

    # resume: no apply may be inherited, and the interrupted attempt must be reported
    proc = host.run(mode="apply", seq=["GO", "GO", "GO", "GO"], deadline="+30", settle=1)
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["disposition"] == "APPLIED" and host.applied() == 1
    assert len(s["prior_attempts"]) == 1
    assert s["prior_attempts"][0]["finalised"] is False
    assert s["prior_attempts"][0]["apply_started"] is False
    assert "prior attempts in this evidence directory" in host.receipt.read_text()


def test_resumed_driver_refuses_a_second_apply(host):
    host.seed_prior_attempt()
    proc = host.run(mode="apply", seq=["GO", "GO", "GO"], deadline="+30", settle=1)
    assert proc.returncode == 3, proc.stderr
    s = host.summary()
    assert s["disposition"] == "NEEDS_HUMAN"
    assert "refusing to apply twice" in s["reason"]
    assert s["apply_started"] is False
    assert host.applied() == 0
    assert "a prior attempt invoked apply: yes" in host.receipt.read_text()
    assert host.plans() == 0, "the refusal happens before any polling"


def test_receipts_are_written_atomically(host):
    proc = host.run(mode="plan", seq=["NO_GO", "NO_GO", "GO"], deadline="+10", poll=1)
    assert proc.returncode == 0, proc.stderr
    for d in (host.evidence, *host.attempts()):
        assert not list(d.glob("*.tmp.*")), f"atomic write left a temp file in {d}"
        assert not list(d.glob(".tmp*"))
    json.loads((host.evidence / "summary.json").read_text())
    json.loads((host.attempts()[0] / "summary.json").read_text())
    assert host.receipt.read_text() == (host.attempts()[0] / "RECEIPT.md").read_text()


def test_an_unwritable_evidence_dir_never_marks_the_attempt_finalised(host):
    """A receipt that could not be written must not be recorded as a complete attempt."""
    attempts = host.evidence / "attempts"
    attempts.mkdir(parents=True)
    attempts.chmod(0o500)
    try:
        proc = host.run(mode="plan", seq=["NO_GO"] * 20, deadline="+2")
    finally:
        attempts.chmod(0o700)
    assert proc.returncode == 3, proc.stderr
    assert not (host.evidence / "RECEIPT.md").exists()
    assert not (host.evidence / "summary.json").exists()
    assert not list(attempts.glob("*/.finalised")), "nothing may be marked final without a receipt"


def test_driver_cannot_manufacture_quiescence():
    """Static invariant: the driver must not be able to terminate work or stop the service."""
    src = DRIVER.read_text()
    for forbidden in ("pkill", "killall", "kill -", "systemctl stop", "systemctl restart",
                      "systemctl kill", "esstop", "admission freeze"):
        assert forbidden not in src.lower(), f"driver must not reference {forbidden!r}"
    assert "--quiet-window" in src and "QUIET_WINDOW=130" in src, "the quiet window must stay a parameter, default 130"
