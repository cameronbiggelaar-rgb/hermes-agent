"""EXT-15: every terminal result of the release driver must produce a durable receipt.

RESTORE-002 stopped safely (STOPPED_SAFE) with zero mutation, but the driver's receipt writer
only existed on the post-APPLY path, so the safe stop left no receipt at all and the outcome had
to be reconstructed from host evidence afterwards. That is an observability defect, not a safety
defect — and it is the one this file pins.

These tests run the real ``harness/release002_driver.sh`` against stub ``git``/``systemctl``/
``sudo``/``gh`` and a stub launcher, so the paths that matter are exercised without a host, a
gateway, or any release authority:

* STOPPED_SAFE at the deadline — receipt written, apply never invoked;
* PLAN GO in plan mode — never applies;
* apply mode — quiescence re-checked across the settle window, then exactly one apply;
* UNEVALUABLE plan — fail closed as NEEDS_HUMAN;
* guard failure — no polling, no apply, receipt still written;
* SIGTERM — interrupted run still writes a receipt;
* publication — at most once, and a failed publication never loses the durable receipt.

    env -u PYTHONPATH /usr/bin/python3 -m pytest tests/harness/test_release002_ext15.py -q
"""
from __future__ import annotations

import json
import os
import shutil
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
if [ "$1 $3" = "-C rev-parse" ] || [ "$2" = "rev-parse" ]; then echo "${STUB_GIT_HEAD}"; exit 0; fi
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
  GO)     printf '{"verdict":"GO","failed":[],"evidence":{"runtime":{"active_agents":%s}}}\\n' "${STUB_AGENTS:-0}"; exit 0;;
  NO_GO)  printf '{"verdict":"NO_GO","failed":["no_active_turns","recent_session_activity"],"evidence":{"runtime":{"active_agents":%s}}}\\n' "${STUB_AGENTS:-2}"; exit 2;;
  NO_GO_OTHER) printf '{"verdict":"NO_GO","failed":["install_head_mismatch"],"evidence":{"runtime":{"active_agents":0}}}\\n'; exit 2;;
  UNEVAL) printf '{"verdict":"UNEVALUABLE","failed":["gate_unevaluable"],"evidence":{"runtime":{"active_agents":0}}}\\n'; exit 3;;
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
            timeout=120,
        )

    # --- receipt readers -------------------------------------------------
    @property
    def receipt(self) -> Path:
        return self.evidence / "RECEIPT.md"

    def summary(self) -> dict:
        return json.loads((self.evidence / "summary.json").read_text())

    def applied(self) -> int:
        return len(self.apply_log.read_text().splitlines()) if self.apply_log.exists() else 0

    def plans(self) -> int:
        return len(self.plan_log.read_text().splitlines()) if self.plan_log.exists() else 0

    def published(self) -> int:
        return len(self.gh_log.read_text().splitlines()) if self.gh_log.exists() else 0


@pytest.fixture()
def host(tmp_path):
    return Host(tmp_path)


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
    assert s["apply_started"] is False
    assert host.applied() == 0


def test_apply_mode_rechecks_quiescence_across_settle(host):
    proc = host.run(mode="apply", seq=["GO", "NO_GO"], deadline="+30", settle=1)
    assert proc.returncode == 2, proc.stderr
    assert host.applied() == 0, "a settle re-check that loses quiescence must not apply"
    assert host.plans() >= 2
    assert "STOPPED_SAFE" in host.receipt.read_text()


def test_receipt_classifies_a_quiescence_only_blocker(host):
    """The approved sequence evaluates the quiescence gates *before* the freeze that produces
    quiescence. A NO_GO caused only by those gates is a sequencing fact, not a gate fault, and the
    receipt must say so — otherwise the next reader has to re-derive it from the logs, which is
    exactly what EXT-15 had to do for the EXT-14 window."""
    proc = host.run(mode="plan", seq=["NO_GO", "NO_GO", "GO"], deadline="+30", poll=1)
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["samples"] == 3
    assert s["quiescence_only_samples"] == 2
    assert s["other_blocking_gates"] == ""
    text = host.receipt.read_text()
    assert "quiescence-only in 2/3 sample(s)" in text
    assert "no non-quiescence gate failed" in text


def test_receipt_names_a_non_quiescence_blocker(host):
    proc = host.run(mode="plan", seq=["NO_GO_OTHER", "GO"], deadline="+30", poll=1)
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["quiescence_only_samples"] == 0
    assert "install_head_mismatch" in s["other_blocking_gates"]
    assert "other gates seen: install_head_mismatch" in host.receipt.read_text()


def test_apply_mode_applies_exactly_once_after_go(host):
    proc = host.run(mode="apply", seq=["GO", "GO", "GO"], deadline="+30", settle=1)
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
    assert host.applied() == 0
    assert "NEEDS_HUMAN" in host.receipt.read_text()


@pytest.mark.parametrize("mutate,needle", [
    ({"journal": True}, "journal_absent"),
    ({"git_head": "deadbeef"}, "install_head"),
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
    host.seq.write_text("NO_GO\n" * 50)
    proc = subprocess.Popen(
        host.argv(mode="apply", deadline="+60", poll=1, settle=1),
        cwd=str(host.install), env=host.env(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    time.sleep(2.0)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 2, (out, err)
    assert host.receipt.exists(), "an interrupted run must still leave a durable receipt"
    text = host.receipt.read_text()
    assert "interrupted by signal" in text
    assert host.applied() == 0


def test_publication_happens_at_most_once(host):
    # no receipt target: publication is skipped, but the receipt is still durable
    proc = host.run(mode="plan", seq=["NO_GO"] * 20, deadline="+2", publish=1,
                    env={"STUB_GH_RC": 0})
    assert proc.returncode == 2, proc.stderr
    assert host.receipt.exists()
    assert host.published() == 0

    shutil.rmtree(host.evidence)

    target = ["--receipt-repo", "cameronbiggelaar-rgb/hermes-agent", "--receipt-pr", "9"]
    proc = subprocess.run(
        host.argv(mode="plan", deadline="+2", publish=1) + target,
        cwd=str(host.install), env=host.env(STUB_GH_RC=0), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2, proc.stderr
    assert host.published() == 1, "a terminal result publishes its receipt exactly once"
    assert (host.evidence / ".published").exists()


def test_failed_publication_keeps_the_receipt(host):
    host.seq.write_text("NO_GO\n" * 20)
    proc = subprocess.run(
        host.argv(mode="plan", deadline="+2", publish=1)
        + ["--receipt-repo", "cameronbiggelaar-rgb/hermes-agent", "--receipt-pr", "9"],
        cwd=str(host.install), env=host.env(STUB_GH_RC=1), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2, proc.stderr
    assert host.receipt.exists()
    text = host.receipt.read_text()
    assert "STOPPED_SAFE" in text
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
        assert "active_agents=2" in line, line


def test_closed_window_is_a_usage_error_not_a_safe_stop(host):
    proc = host.run(mode="apply", seq=["GO", "GO"], open_after="+0", deadline="+0")
    assert proc.returncode == 3, proc.stderr
    assert host.summary()["disposition"] == "NEEDS_HUMAN"
    assert host.plans() == 0 and host.applied() == 0
    assert "already closed" in host.receipt.read_text()


@pytest.mark.live_system_guard_bypass
def test_interrupt_during_apply_is_needshuman_not_stopped_safe(host):
    host.seq.write_text("GO\nGO\nGO\n")
    env = host.env(STUB_APPLY_SLEEP=30)
    proc = subprocess.Popen(
        host.argv(mode="apply", deadline="+60", poll=1, settle=1),
        cwd=str(host.install), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
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


def test_driver_cannot_manufacture_quiescence():
    """Static invariant: the driver must not be able to terminate work or stop the service."""
    src = DRIVER.read_text()
    for forbidden in ("pkill", "killall", "kill -", "systemctl stop", "systemctl restart",
                      "systemctl kill", "esstop", "admission freeze"):
        assert forbidden not in src.lower(), f"driver must not reference {forbidden!r}"
    assert "--quiet-window" in src and "QUIET_WINDOW=130" in src, "the quiet window must stay a parameter, default 130"
