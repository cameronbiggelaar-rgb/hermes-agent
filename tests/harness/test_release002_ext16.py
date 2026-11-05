"""EXT-16: quiesce-first release sequencing.

The defect: the approved sequence ran the full preflight *before* ``freeze_engage``, and that
preflight required ``no_active_turns`` (active_agents == 0) plus ``admission_report()`` — which
itself reports ``freeze_not_engaged`` unless a drain marker exists. Quiescence before the freeze is
therefore unsatisfiable by construction, so a gateway carrying traffic could never reach PLAN,
let alone the deployment boundary.

EXT-16 splits the contract by phase:

* ``pre``        identity / installation / recovery gates only. Quiescence gates are **pending**.
* ``infreeze``   quiescence, freeze ownership (exact principal), freeze epoch attribution.
* ``revalidate`` the same in-freeze gates again, immediately before mutation.

and fixes the release script and the driver to honour it: engage the freeze first, prove quiescence
inside it, revalidate, then mutate. Nothing here mutates production — these tests run the real
harness against stub ``git``/``systemctl``/launcher and disposable homes.

    env -u PYTHONPATH /usr/bin/python3 -m pytest tests/harness/test_release002_ext16.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
DRIVER = HARNESS / "release002_driver.sh"
ENGINE = HARNESS / "release002_release.sh"
sys.path.insert(0, str(HARNESS))

import release002_control as ctl  # noqa: E402
import release002_preflight as pf  # noqa: E402

CAND = "ffe95da10804ff29571239c3ea33aca7b606b185"
HEAD = "e05128febbc559469a5fb835a9235a543dfdfa64"
TREE = "dc7555b7d09e0f20ae04679dadc84f63731f23a0"
PID = "4242"
PRINCIPAL = "release-freeze:test"

GIT_STUB = """#!/usr/bin/env bash
if [ "$1 $3" = "-C rev-parse" ] || [ "$2" = "rev-parse" ]; then
  case "$*" in
    *"^{tree}"*) echo "${STUB_GIT_TREE}"; exit 0;;
  esac
  echo "${STUB_GIT_HEAD}"; exit 0
fi
exit 1
"""

SYSTEMCTL_STUB = """#!/usr/bin/env bash
case "$1" in
  list-units) exit 0;;
  show) echo "${STUB_MAINPID:-4242}"; exit 0;;
esac
exit 0
"""

SUDO_STUB = """#!/usr/bin/env bash
shift
exec "$@"
"""

GH_STUB = """#!/usr/bin/env bash
echo "$*" >> "${STUB_GH_LOG}"
exit 0
"""

# Phase-aware launcher: EXT-16 engines report the phase they evaluated, the gates they left pending
# and whether anything is deployment-ready. Each token is one complete payload.
LAUNCHER_STUB = """#!/usr/bin/env bash
verb="$1"; shift
if [ "$verb" = "apply" ]; then
  echo "apply $*" >> "${STUB_APPLY_LOG}"
  exit "${STUB_APPLY_RC:-0}"
fi
echo "plan $*" >> "${STUB_PLAN_LOG}"
seqf="${STUB_PLAN_SEQ}"
tok=""
if [ -s "$seqf" ]; then tok="$(head -1 "$seqf")"; tail -n +2 "$seqf" > "$seqf.tmp"; mv "$seqf.tmp" "$seqf"; fi
PEND='["no_active_agents","no_live_admissions","admission_counters_consistent","freeze_owned_by_attempt","freeze_since_attributable","freeze_guards_engaged","cron_inflight_zero","cron_no_claims_after_freeze","quiet_window_elapsed"]'
case "${tok:-PRE_NO_GO}" in
  PRE_GO)
    # A busy gateway: three agents and three live admissions, and still a legitimate pre-freeze GO.
    printf '{"verdict":"GO","failed":[],"phase":"pre","deployment_ready":false,"pending_gates":%s,"evidence":{"runtime":{"active_agents":3,"live_admissions":3}}}\\n' "$PEND"; exit 0;;
  PRE_GO_CLAIMS_QUIET)
    printf '{"verdict":"GO","failed":[],"phase":"pre","deployment_ready":false,"pending_gates":[],"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n'; exit 0;;
  PRE_GO_DEPLOY_READY)
    printf '{"verdict":"GO","failed":[],"phase":"pre","deployment_ready":true,"pending_gates":%s,"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n' "$PEND"; exit 0;;
  IN_FREEZE_GO)
    printf '{"verdict":"GO","failed":[],"phase":"infreeze","deployment_ready":false,"pending_gates":[],"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n'; exit 0;;
  PRE_NO_GO)
    printf '{"verdict":"NO_GO","failed":["candidate_present"],"phase":"pre","deployment_ready":false,"pending_gates":%s,"evidence":{"runtime":{"active_agents":3,"live_admissions":3}}}\\n' "$PEND"; exit 2;;
  LEGACY_GO)
    printf '{"verdict":"GO","failed":[],"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n'; exit 0;;
  PRE_GO_WITH_GATES)
    printf '{"verdict":"GO","failed":["no_active_agents"],"phase":"pre","deployment_ready":false,"pending_gates":%s,"evidence":{"runtime":{"active_agents":0,"live_admissions":0}}}\\n' "$PEND"; exit 0;;
esac
exit 2
"""


class Host:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (("git", GIT_STUB), ("systemctl", SYSTEMCTL_STUB),
                           ("sudo", SUDO_STUB), ("gh", GH_STUB),
                           ("hermes-release-002", LAUNCHER_STUB)):
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
        e.update(PATH=f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", STUB_GIT_HEAD=HEAD,
                 STUB_GIT_TREE=TREE, STUB_PLAN_SEQ=str(self.seq), STUB_PLAN_LOG=str(self.plan_log),
                 STUB_APPLY_LOG=str(self.apply_log), STUB_GH_LOG=str(self.gh_log),
                 STUB_MAINPID=PID, TMPDIR=str(self.root))
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run(self, *, mode="plan", seq=None, **kw):
        if seq is not None:
            self.seq.write_text("\n".join(seq) + "\n")
        argv = ["bash", str(DRIVER), "--mode", mode, "--candidate", CAND,
                "--expect-head", HEAD, "--expect-tree", TREE, "--install", str(self.install),
                "--launcher", str(self.bin / "hermes-release-002"),
                "--evidence-dir", str(self.evidence), "--open-after", "+0", "--deadline", "+6",
                "--quiet-window", "130", "--poll", "1", "--settle", "1", "--publish", "0",
                "--journal", str(self.journal), "--unit-dir", str(self.unit_dir),
                "--proc-root", str(self.proc_root)]
        return subprocess.run(argv, cwd=str(self.install), env=self.env(**kw),
                              capture_output=True, text=True, timeout=180)

    def summary(self) -> dict:
        return json.loads((self.evidence / "summary.json").read_text())

    def receipt(self) -> str:
        return (self.evidence / "RECEIPT.md").read_text()

    def applied(self) -> int:
        return len(self.apply_log.read_text().splitlines()) if self.apply_log.exists() else 0


@pytest.fixture()
def host(tmp_path):
    return Host(tmp_path)


# --------------------------------------------------------------- driver: pre-freeze semantics
def test_busy_gateway_earns_a_pre_freeze_go(host):
    """THE defect: three active agents must not block the pre-freeze gate."""
    proc = host.run(mode="plan", seq=["PRE_GO"])
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["disposition"] == "GO_READY"
    assert s["plan_phase"] == "pre"
    assert s["plan_deployment_ready"] == "false"
    assert s["quiescence_asserted_by_plan"] == "false"
    assert "no_active_agents" in s["plan_pending_gates"]
    assert host.applied() == 0


def test_pre_freeze_go_is_reported_as_pre_freeze_only(host):
    proc = host.run(mode="plan", seq=["PRE_GO"])
    assert proc.returncode == 0, proc.stderr
    text = host.receipt()
    assert "pre-freeze only" in text
    assert "pending" in text
    assert "never authorises deployment" in text


def test_a_pre_freeze_go_that_claims_quiescence_is_refused(host):
    proc = host.run(mode="plan", seq=["PRE_GO_CLAIMS_QUIET"])
    assert proc.returncode != 0
    assert "pending" in proc.stderr or "pending" in host.receipt()
    s = host.summary()
    assert s["disposition"] in {"STOPPED_SAFE", "NEEDS_HUMAN"}  # never GO_READY, never APPLIED
    assert s["apply_started"] is False


def test_a_pre_freeze_go_that_claims_deployment_ready_is_refused(host):
    proc = host.run(mode="plan", seq=["PRE_GO_DEPLOY_READY"])
    assert proc.returncode != 0
    assert "deployment_ready" in (proc.stderr + host.receipt())


def test_a_go_from_the_in_freeze_phase_is_not_a_pre_freeze_go(host):
    proc = host.run(mode="plan", seq=["IN_FREEZE_GO"])
    assert proc.returncode != 0
    assert "infreeze" in (proc.stderr + host.receipt())


def test_a_go_listing_failed_gates_is_refused(host):
    proc = host.run(mode="plan", seq=["PRE_GO_WITH_GATES"])
    assert proc.returncode != 0
    assert "failed gates" in (proc.stderr + host.receipt())


def test_legacy_launcher_keeps_the_old_contract(host):
    """An engine that does not declare a phase is never reinterpreted as phase-aware."""
    proc = host.run(mode="plan", seq=["LEGACY_GO"])
    assert proc.returncode == 0, proc.stderr
    s = host.summary()
    assert s["plan_phase"] == "legacy"
    assert s["quiescence_asserted_by_plan"] == "unknown"


# --------------------------------------------------------------- driver: sequence after the GO
def test_apply_proceeds_from_a_pre_freeze_go_without_a_pre_freeze_quiet_window(host):
    """The pre-freeze settle wait is the ordering defect; the runner proves quiescence in-freeze."""
    proc = host.run(mode="apply", seq=["PRE_GO"] + ["PRE_GO"] * 20)
    assert host.applied() == 1, (proc.stdout, proc.stderr)
    s = host.summary()
    assert s["apply_started"] is True
    assert "skipped" in host.receipt() or "quiescence is proven inside" in host.receipt()


def test_apply_never_runs_on_a_refused_pre_freeze_go(host):
    proc = host.run(mode="apply", seq=["PRE_GO_DEPLOY_READY"] + ["PRE_GO"] * 20)
    assert host.applied() == 0
    # A self-contradicting GO is not routine: the engine claimed something it cannot have, so the
    # supervisor stops for a human rather than retrying into a state it does not understand.
    assert host.summary()["disposition"] == "NEEDS_HUMAN"


# --------------------------------------------------------------- engine: ordering, statically
def test_engine_engages_the_freeze_before_it_asks_for_quiescence():
    body = ENGINE.read_text()
    pre = body.index('preflight pre "$EXPECT_HEAD"')
    freeze = body.index("if ! freeze_engage;")
    quiesce = body.index('if [ "$QUIESCE_WAIT" -gt "$MAX_QUIESCE" ]')
    revalidate = body.index('preflight revalidate >"$ARCHIVE/preflight-revalidate.json"')
    merge = body.index('git -C "$INSTALL" merge --ff-only')
    assert pre < freeze < quiesce < revalidate < merge


def test_engine_refuses_a_foreign_freeze_instead_of_adopting_it():
    body = ENGINE.read_text()
    assert "estop_is_ours" in body and "drain_is_ours" in body
    assert "NEEDS_HUMAN" in body
    # clearing must be guarded by the same ownership predicate, not by presence alone
    release = body[body.index("freeze_release()"):]
    assert "drain_is_ours" in release[:3000]
    assert "estop_is_ours" in release[:3000]


def test_engine_aborts_when_the_freeze_epoch_is_unknown():
    body = ENGINE.read_text()
    assert "ABORTED_NO_FREEZE_EPOCH" in body


def test_engine_refuses_a_quiesce_budget_wider_than_qualified():
    body = ENGINE.read_text()
    assert "MAX_QUIESCE" in body and "exit 65" in body


def test_engine_has_no_unphased_preflight_call():
    body = ENGINE.read_text()
    assert "--phase pre" in body and "--phase \"$1\"" in body


# --------------------------------------------------------------- preflight: phase contract
def test_quiescence_gates_are_pending_before_the_freeze():
    """The pre phase must register the quiescence gates as deferred, never as passes."""
    source = (HARNESS / "release002_preflight.py").read_text()
    assert "QUIESCENCE_GATES" in source
    block = source[source.index("QUIESCENCE_GATES"):]
    assert "deferred=True" in block[:4000]
    assert "pending_gates" in source


def test_runtime_rows_assert_state_only_inside_the_freeze(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pf, "DRAIN_MARKER", str(home / ".drain_request.json"))
    monkeypatch.setattr(pf, "ESTOP_MARKER", str(home / "ESTOP"))
    (home / ".drain_request.json").write_text(json.dumps(
        {"principal": PRINCIPAL, "epoch": ctl._machine_epoch() or "",
         "requested_at": "2026-11-05T10:00:00+00:00"}), encoding="utf-8")
    (home / "ESTOP").write_text(json.dumps({"reason": PRINCIPAL}), encoding="utf-8")

    pre = pf.runtime_gate_rows("pre", {"gateway_state": "running"}, 1, HEAD,
                               principal=PRINCIPAL, marker_max_age=10 ** 9)
    names = {r[0] for r in pre}
    assert "runtime_state_frozen" not in names and "admission_freeze_owned" not in names

    inz = pf.runtime_gate_rows("infreeze", {"gateway_state": "draining"}, 1, HEAD,
                               principal=PRINCIPAL, marker_max_age=10 ** 9)
    rows = {r[0]: r for r in inz}
    assert rows["runtime_state_frozen"][1] is True
    assert rows["admission_freeze_owned"][1] is True
    assert rows["estop_owned_by_attempt"][1] is True


def test_runtime_rows_reject_a_foreign_owner(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pf, "DRAIN_MARKER", str(home / ".drain_request.json"))
    monkeypatch.setattr(pf, "ESTOP_MARKER", str(home / "ESTOP"))
    (home / ".drain_request.json").write_text(json.dumps(
        {"principal": "release-freeze:someone-else", "epoch": ctl._machine_epoch() or "",
         "requested_at": "2026-11-05T10:00:00+00:00"}), encoding="utf-8")
    (home / "ESTOP").write_text(json.dumps({"reason": "release-freeze:someone-else"}),
                                encoding="utf-8")
    rows = {r[0]: r for r in pf.runtime_gate_rows(
        "infreeze", {"gateway_state": "draining"}, 1, HEAD,
        principal=PRINCIPAL, marker_max_age=10 ** 9)}
    assert rows["admission_freeze_owned"][1] is False
    assert rows["estop_owned_by_attempt"][1] is False


# --------------------------------------------------------------- control: ownership + counters
def make_home(tmp_path: Path, *, marker=True, estop_reason=PRINCIPAL, agents=0, rows=()) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("CREATE TABLE runtime_epoch (epoch INTEGER, singleton INTEGER)")
        conn.execute("INSERT INTO runtime_epoch VALUES (23, 1)")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, last_activity_at REAL)")
        conn.execute("CREATE TABLE session_admissions (admission_id TEXT, status TEXT, "
                     "owner_epoch INTEGER, target_session_id TEXT)")
        for i, (status, last) in enumerate(rows):
            conn.execute("INSERT INTO sessions VALUES (?, ?)", (f"s{i}", last))
            conn.execute("INSERT INTO session_admissions VALUES (?, ?, 23, ?)",
                         (f"a{i}", status, f"s{i}"))
        conn.commit()
    finally:
        conn.close()
    (home / "gateway_state.json").write_text(json.dumps(
        {"gateway_state": "draining", "active_agents": agents, "pid": 1,
         "updated_at": ctl.datetime.now().astimezone().isoformat()}), encoding="utf-8")
    if marker:
        (home / ".drain_request.json").write_text(json.dumps(
            {"principal": PRINCIPAL, "epoch": ctl._machine_epoch() or "",
             "requested_at": ctl.datetime.now().astimezone().isoformat()}), encoding="utf-8")
    if estop_reason is not None:
        (home / "ESTOP").write_text(json.dumps({"reason": estop_reason}), encoding="utf-8")
    return home


def test_admission_report_requires_the_estop_to_be_this_attempts(tmp_path):
    home = make_home(tmp_path, estop_reason="release-freeze:old-attempt")
    report = ctl.admission_report(home=home, principal=PRINCIPAL)
    assert report["ok"] is False
    assert "estop_foreign_owner" in report["reasons"]


def test_admission_report_without_estop_fails_closed(tmp_path):
    report = ctl.admission_report(home=make_home(tmp_path, estop_reason=None), principal=PRINCIPAL)
    assert report["ok"] is False
    assert "estop_not_engaged" in report["reasons"]


def test_admission_report_rejects_a_marker_from_another_attempt(tmp_path):
    home = make_home(tmp_path)
    (home / ".drain_request.json").write_text(json.dumps(
        {"principal": "release-freeze:old-attempt", "epoch": ctl._machine_epoch() or "",
         "requested_at": ctl.datetime.now().astimezone().isoformat()}), encoding="utf-8")
    report = ctl.admission_report(home=home, principal=PRINCIPAL)
    assert report["ok"] is False
    assert report["facts"]["drain_marker_active"] is False


def test_admission_report_reports_contradictory_counters(tmp_path):
    import time as _time
    report = ctl.admission_report(home=make_home(tmp_path, agents=0,
                                                 rows=(("queued", _time.time()),)),
                                  principal=PRINCIPAL)
    assert report["facts"]["live_admissions"] >= 1
    assert report["ok"] is False
    assert "admission_counters_contradict" in report["reasons"]


def test_epoch_parsing_never_invents_a_time():
    assert ctl._epoch_of("2026-11-05T10:00:00+00:00") == pytest.approx(1793872800, abs=1)
    assert ctl._epoch_of(1793872800) == pytest.approx(1793872800)
    assert ctl._epoch_of("not-a-time") is None
    assert ctl._epoch_of(None) is None
    assert ctl._epoch_of("") is None


def test_marker_freshness_is_bounded(tmp_path):
    home = make_home(tmp_path)
    (home / ".drain_request.json").write_text(json.dumps(
        {"principal": PRINCIPAL, "epoch": ctl._machine_epoch() or "",
         "requested_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    report = ctl.admission_report(home=home, principal=PRINCIPAL, marker_max_age=180)
    assert report["facts"]["drain_marker_active"] is False
