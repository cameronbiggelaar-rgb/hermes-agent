"""Adversarial controls for the RESTORE-002 release-orchestration library.

Covers the three release findings at the decision level:

* EXT-RESTORE-002-03 — the pinned identity manifest refuses on drift, absence and dirty state.
* EXT-RESTORE-002-04 — the admission report proves the freeze is engaged and nothing is still
  executing, and fails closed (never "probably idle") when it cannot tell.
* EXT-RESTORE-002-05 — the supervisor envelope covers quiesce + archive + deploy + restart +
  verify + one rollback, and an interrupted supervisor resolves deterministically from the journal.

Everything here is pure: no service, no restart, no write to a real database.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CTRL_PATH = ROOT / "harness" / "release002_control.py"


def _load_control():
    spec = importlib.util.spec_from_file_location("release002_control_uut", CTRL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ctrl = _load_control()

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "release-test",
    "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
    "GIT_COMMITTER_NAME": "release-test",
    "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
}
ENGINE_FILES = ("harness/release002_control.py", "harness/release002_release.sh",
                "harness/release002_preflight.py")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------- EXT-03 fixtures
@pytest.fixture()
def stage(tmp_path):
    """A candidate repo, an installation cloned one commit behind it, and the pinned side files."""
    repo = tmp_path / "candidate-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    deployed = commit(repo, {p: f"# deployed {p}\n" for p in ENGINE_FILES}, "deployed identity")
    candidate = commit(repo, {p: f"# candidate {p}\n" for p in ENGINE_FILES}, "candidate identity")

    install = tmp_path / "install"
    subprocess.run(["git", "clone", "-q", str(repo), str(install)], check=True, env=GIT_ENV)
    git(install, "reset", "--hard", "-q", deployed)

    system = tmp_path / "system"
    unit = system / "hermes-gateway.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nTimeoutStopSec=300s\n", encoding="utf-8")
    dropins = {}
    for name in ("90-recovery-hold.conf", "95-timeout-stop.conf"):
        path = system / f"{unit.name}.d" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n", encoding="utf-8")
        dropins[name] = path

    launcher = tmp_path / "lib" / "hermes-release-002"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    manifest = ctrl.build_manifest(
        repo=repo, candidate=candidate, install=install, launcher=launcher, unit=unit,
        dropins=list(dropins.values()), engine_paths=list(ENGINE_FILES),
        expected_install_head=deployed,
    )
    return {
        "repo": repo, "install": install, "launcher": launcher, "unit": unit,
        "dropins": dropins, "manifest": manifest, "candidate": candidate, "deployed": deployed,
        "tmp": tmp_path,
    }


def verify(stage_dict, **overrides):
    manifest = json.loads(json.dumps(stage_dict["manifest"]))
    for dotted, value in overrides.items():
        node = manifest
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return ctrl.verify_manifest(manifest, repo=stage_dict["repo"], install=stage_dict["install"],
                                launcher=stage_dict["launcher"], phase=stage_dict.get("phase", "pre"))


def test_manifest_roundtrip_is_green(stage):
    result = verify(stage)
    assert result["ok"] is True, result["failed"]
    assert result["failed"] == []
    assert result["candidate_commit"] == stage["candidate"]
    assert result["candidate_tree"] == stage["manifest"]["candidate_tree"]


def test_every_manifest_gate_is_evaluated_and_named(stage):
    result = verify(stage)
    names = [g["gate"] for g in result["gates"]]
    assert all(isinstance(g["ok"], bool) for g in result["gates"])
    for required in ("schema", "candidate_commit_shape", "candidate_available", "candidate_tree",
                     "launcher_sha256", "install_clean", "install_head_pre", "install_tree_pre",
                     "unit_sha256_pre", "supervisor_envelope"):
        assert required in names
    assert result["ok"] == (result["failed"] == [])


def test_uncommitted_worktree_edit_fails_closed(stage):
    (stage["repo"] / ENGINE_FILES[0]).write_text("# edited after the candidate commit\n", encoding="utf-8")
    result = verify(stage)
    assert result["ok"] is False
    assert f"worktree_matches_candidate:{ENGINE_FILES[0]}" in result["failed"]


def test_launcher_checksum_drift_fails_closed(stage):
    stage["launcher"].write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    assert "launcher_sha256" in verify(stage)["failed"]


def test_launcher_at_a_different_path_fails_closed(stage):
    moved = stage["tmp"] / "elsewhere" / "hermes-release-002"
    moved.parent.mkdir()
    moved.write_text(stage["launcher"].read_text(encoding="utf-8"), encoding="utf-8")
    staged = dict(stage, launcher=moved)
    assert "launcher_path" in verify(staged)["failed"]


def test_missing_dropin_fails_closed(stage):
    stage["dropins"]["95-timeout-stop.conf"].unlink()
    failed = verify(stage)["failed"]
    assert "dropin_pre:95-timeout-stop.conf" in failed


def test_modified_unit_fails_closed(stage):
    stage["unit"].write_text("[Service]\nTimeoutStopSec=210s\n", encoding="utf-8")
    assert "unit_sha256_pre" in verify(stage)["failed"]


def test_candidate_absent_from_the_installation_fails_closed(stage):
    bare = stage["tmp"] / "bare-install"
    bare.mkdir()
    git(bare, "init", "-q", "-b", "main")
    commit(bare, {"README": "unrelated\n"}, "unrelated")
    failed = verify(dict(stage, install=bare))["failed"]
    assert "candidate_available" in failed
    assert "candidate_tree_matches_install" in failed


def test_install_identity_drift_fails_closed(stage):
    git(stage["install"], "reset", "--hard", "-q", stage["candidate"])
    failed = verify(stage)["failed"]
    assert "install_head_pre" in failed and "install_tree_pre" in failed


def test_dirty_install_fails_closed(stage):
    (stage["install"] / "stray.txt").write_text("unexpected\n", encoding="utf-8")
    assert "install_clean" in verify(stage)["failed"]


def test_short_candidate_sha_fails_closed(stage):
    failed = verify(stage, candidate_commit="deadbeef")["failed"]
    assert "candidate_commit_shape" in failed and "candidate_available" in failed


def test_schema_bump_fails_closed(stage):
    assert "schema" in verify(stage, schema=ctrl.MANIFEST_SCHEMA + 1)["failed"]


def test_supervisor_envelope_shortfall_fails_closed(stage):
    required = ctrl.envelope_plan()["required_sec"]
    assert "supervisor_envelope" in verify(stage, supervisor={"runtime_max_sec": required - 1,
                                                              "required_sec": required})["failed"]


# --------------------------------------------------------------------------- EXT-05 envelope
def test_envelope_covers_every_phase_and_one_rollback():
    with_rollback = ctrl.required_envelope_seconds(stop_timeout=300, start_timeout=90)
    without = ctrl.required_envelope_seconds(stop_timeout=300, start_timeout=90, rollback=False)
    restart = 300 + 90 + ctrl.RESTART_MARGIN_SECONDS
    assert with_rollback - without == restart
    assert with_rollback == (ctrl.QUIESCE_MAX_SECONDS + ctrl.ARCHIVE_MAX_SECONDS
                             + ctrl.DEPLOY_MAX_SECONDS + 2 * restart + ctrl.VERIFY_MAX_SECONDS)
    assert with_rollback > 300  # the supervisor must outlive the stop budget, not equal it


def test_envelope_grows_with_the_stop_budget():
    small = ctrl.envelope_plan(stop_timeout=300)["required_sec"]
    large = ctrl.envelope_plan(stop_timeout=600)["required_sec"]
    assert large - small == 300 + 300  # stop budget + the rollback restart it implies


def test_runtime_max_is_a_whole_quarter_hour_and_never_below_the_requirement():
    plan = ctrl.envelope_plan()
    assert plan["runtime_max_sec"] % 300 == 0
    assert plan["runtime_max_sec"] >= plan["required_sec"] + plan["safety_sec"]


# --------------------------------------------------------------------------- EXT-04 admission
def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def make_home(tmp_path: Path, *, epoch: int | None = 7, agents: int | None = 0,
              state: str | None = "draining", marker: dict | None = None, estop: bool = True,
              rows: tuple[tuple[str, float | None], ...] = (), db: bool = True) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    if db:
        conn = sqlite3.connect(home / "state.db")
        try:
            conn.execute("CREATE TABLE runtime_epoch (epoch INTEGER, singleton INTEGER)")
            if epoch is not None:
                conn.execute("INSERT INTO runtime_epoch VALUES (?, 1)", (epoch,))
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, last_activity_at REAL)")
            conn.execute("CREATE TABLE session_admissions (admission_id TEXT, status TEXT, "
                         "owner_epoch INTEGER, target_session_id TEXT)")
            for i, (status, last) in enumerate(rows):
                session_id = f"s{i}"
                conn.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, last))
                conn.execute("INSERT INTO session_admissions VALUES (?, ?, ?, ?)",
                             (f"a{i}", status, epoch if epoch is not None else 0, session_id))
            conn.commit()
        finally:
            conn.close()
    if state is not None:
        payload = {"gateway_state": state, "updated_at": _iso(3), "pid": 1}
        if agents is not None:
            payload["active_agents"] = agents
        (home / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    if marker is not None:
        (home / ".drain_request.json").write_text(json.dumps(marker), encoding="utf-8")
    if estop:
        (home / "ESTOP").write_text("release-freeze:test\n", encoding="utf-8")
    return home


def freeze_marker(**overrides) -> dict:
    body = {"principal": "release-freeze:test", "epoch": ctrl._machine_epoch() or "",
            "requested_at": _iso(5), "suppress_notification": True}
    body.update(overrides)
    return body


def test_admission_report_is_green_only_when_frozen_and_idle(tmp_path):
    home = make_home(tmp_path, marker=freeze_marker())
    report = ctrl.admission_report(home=home)
    assert report["ok"] is True, report["reasons"]
    assert report["evaluable"] is True
    assert report["facts"]["live_admissions"] == 0
    assert report["mechanisms"] == ["drain_marker", "estop_sentinel"]


def test_active_work_is_proven_no_go(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(), agents=2))
    assert report["ok"] is False and "active_agents=2" in report["reasons"]


def test_executing_admissions_are_proven_no_go(tmp_path):
    now = time.time()
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(),
                                                  rows=(("started", now),)))
    assert report["ok"] is False and "live_admissions=1" in report["reasons"]


def test_undatable_admission_counts_as_live(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(),
                                                  rows=(("started", None),)))
    assert report["facts"]["live_admissions"] == 1
    assert report["ok"] is False


def test_stale_admission_is_reported_not_silently_dropped(tmp_path):
    old = time.time() - (ctrl.STALE_ADMISSION_HOURS + 4) * 3600
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(),
                                                  rows=(("started", old),)))
    assert report["ok"] is True
    assert report["facts"]["stale_admissions"] == 1
    assert report["facts"]["admission_rows"] == 1


def test_unfrozen_admission_is_proven_no_go(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=None))
    assert "freeze_not_engaged" in report["reasons"]
    assert report["facts"]["freeze_detail"] == "absent"


def test_operator_drain_does_not_masquerade_as_the_release_freeze(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(principal="drain-control")))
    assert "freeze_not_engaged" in report["reasons"]
    assert report["facts"]["drain_marker_present"] is True
    assert "not a release freeze" in report["facts"]["freeze_detail"]


def test_expired_freeze_marker_is_not_engaged(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(requested_at=_iso(7200))))
    assert "freeze_not_engaged" in report["reasons"]
    assert report["facts"]["freeze_detail"].startswith("expired")


def test_freeze_marker_from_another_instantiation_is_not_engaged(tmp_path):
    if not ctrl._machine_epoch():
        pytest.skip("no /proc epoch available on this host")
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(epoch="other-boot")))
    assert "freeze_not_engaged" in report["reasons"]
    assert "stale epoch" in report["facts"]["freeze_detail"]


def test_estop_sentinel_must_be_present(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(), estop=False))
    assert "estop_not_engaged" in report["reasons"]


def test_unreadable_gateway_state_is_unevaluable_not_idle(tmp_path):
    home = make_home(tmp_path, marker=freeze_marker())
    (home / "gateway_state.json").write_text("{not json", encoding="utf-8")
    report = ctrl.admission_report(home=home)
    assert report["ok"] is False
    assert report["evaluable"] is False
    assert "gateway_state_unreadable" in report["reasons"]


def test_boolean_active_agents_is_unevaluable_not_zero(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(), agents=True))
    assert report["ok"] is False and "active_agents_unevaluable" in report["reasons"]


def test_unreadable_database_is_unevaluable(tmp_path):
    report = ctrl.admission_report(home=make_home(tmp_path, marker=freeze_marker(), db=False))
    assert report["ok"] is False and report["evaluable"] is False
    assert "epoch_unevaluable" in report["reasons"]


def test_admission_cli_exit_codes(tmp_path, capsys):
    green = make_home(tmp_path / "green", marker=freeze_marker())
    assert ctrl.main(["admission", "--home", str(green)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    busy = make_home(tmp_path / "busy", marker=freeze_marker(), agents=1)
    assert ctrl.main(["admission", "--home", str(busy)]) == 2

    broken = make_home(tmp_path / "broken", marker=freeze_marker(), db=False)
    assert ctrl.main(["admission", "--home", str(broken)]) == 3


# --------------------------------------------------------------------------- marker rules
@pytest.mark.parametrize("marker,active,why", [
    (None, False, "absent"),
    ("not-a-dict", False, "unreadable"),
    ({"principal": "drain-control"}, False, "not a release freeze"),
    ({"principal": "release-freeze:x"}, True, "unstamped marker"),
    ({"principal": "release-freeze:x", "requested_at": "nonsense"}, True, "unparsable requested_at"),
    ({"principal": "release-freeze:x", "requested_at": _iso(10)}, True, "fresh"),
    ({"principal": "release-freeze:x", "requested_at": _iso(2 * 3600)}, False, "expired"),
])
def test_marker_state_rules(marker, active, why):
    result = ctrl.marker_state(marker)
    assert result["active"] is active
    assert why in result["why"]


def test_refresh_drain_marker_keeps_identity_and_retimes(tmp_path):
    home = make_home(tmp_path, marker=freeze_marker(requested_at=_iso(2 * 3600)))
    refreshed = ctrl.refresh_drain_marker(home=home)
    assert refreshed is not None
    assert refreshed["principal"] == "release-freeze:test"
    assert ctrl.marker_state(refreshed)["active"] is True
    assert ctrl.refresh_drain_marker(home=tmp_path / "empty") is None


# --------------------------------------------------------------------------- journal / recovery
def test_journal_roundtrip_and_phase_history(tmp_path):
    path = tmp_path / "journal.json"
    journal = ctrl.phase_record(None, phase="start", note="begin")
    journal = ctrl.phase_record(journal, phase="freeze", mutated=False)
    journal = ctrl.phase_record(journal, phase="deploy", mutated=True, restart_attempts=0)
    ctrl.write_journal(journal, path)
    loaded = ctrl.read_journal(path)
    assert loaded["phase"] == "deploy"
    assert loaded["mutated"] is True
    assert [p["phase"] for p in loaded["phases"]] == ["start", "freeze", "deploy"]
    assert ctrl.read_journal(tmp_path / "absent.json") is None


def test_mutation_is_sticky_once_recorded():
    journal = ctrl.phase_record(ctrl.phase_record(None, phase="deploy", mutated=True), phase="verify")
    assert journal["mutated"] is True


SETTLED_FACTS = {"install_head": "b" * 40, "candidate": "b" * 40, "deployed": "a" * 40,
                 "service_active": True, "freeze_present": True, "estop_present": True}


@pytest.mark.parametrize("journal,facts,action", [
    (None, SETTLED_FACTS, "none"),
    ({"phase": "quiesce", "result": None}, SETTLED_FACTS, "unfreeze"),
    ({"phase": "quiesce", "mutated": False}, {**SETTLED_FACTS, "freeze_present": False,
                                              "estop_present": False}, "unfreeze"),
    ({"phase": "deploy", "mutated": True}, SETTLED_FACTS, "complete_verification"),
    ({"phase": "deploy", "mutated": True}, {**SETTLED_FACTS, "service_active": False}, "rollback"),
    ({"phase": "deploy", "mutated": True}, {**SETTLED_FACTS, "service_active": None}, "needs_human"),
    ({"phase": "deploy", "mutated": True}, {**SETTLED_FACTS, "install_head": "c" * 40}, "needs_human"),
    ({"phase": "restart", "mutated": True}, {**SETTLED_FACTS, "install_head": None}, "needs_human"),
    ({"phase": "aborted", "mutated": False}, SETTLED_FACTS, "unfreeze"),
    ({"phase": "recover", "mutated": True}, {**SETTLED_FACTS, "install_head": "a" * 40}, "unfreeze"),
    ({"phase": "rollback", "mutated": True}, SETTLED_FACTS, "needs_human"),
    ({"phase": "rollback", "mutated": True}, {**SETTLED_FACTS, "install_head": "a" * 40}, "unfreeze"),
    ({"phase": "nonsense", "mutated": True}, SETTLED_FACTS, "needs_human"),
    ({"phase": "deploy", "result": "COMPLETE", "mutated": True}, SETTLED_FACTS, "unfreeze"),
    ({"phase": "deploy", "result": "COMPLETE"}, {**SETTLED_FACTS, "freeze_present": False,
                                                 "estop_present": False}, "none"),
    ({"phase": "deploy", "result": "ROLLED_BACK", "mutated": True}, SETTLED_FACTS, "unfreeze"),
])
def test_recovery_decisions_are_deterministic(journal, facts, action):
    decision = ctrl.decide_recovery(journal, facts)
    assert decision["action"] == action, decision
    assert decision["reason"]


def test_recovery_never_guesses_without_the_deployed_identity():
    decision = ctrl.decide_recovery({"phase": "deploy", "mutated": True},
                                    {"install_head": "a" * 40, "candidate": "b" * 40, "deployed": None})
    assert decision["action"] == "needs_human"


def test_recover_plan_cli_is_no_go_for_an_unknown_state(tmp_path, capsys):
    path = tmp_path / "journal.json"
    ctrl.write_journal({"phase": "deploy", "mutated": True}, path)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"f": "x\n"}, "c")
    code = ctrl.main(["recover-plan", "--journal", str(path), "--repo", str(repo),
                      "--candidate", "b" * 40, "--deployed", "a" * 40])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["action"] == "needs_human"


def test_recover_plan_without_a_journal_is_a_no_op(tmp_path, capsys):
    code = ctrl.main(["recover-plan", "--journal", str(tmp_path / "absent.json")])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["action"] == "none"
