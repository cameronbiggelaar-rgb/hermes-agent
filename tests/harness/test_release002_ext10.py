"""EXT-RESTORE-002-10: the released verification path must be able to pass.

The External CA found that the independent post-release verification could never succeed:
``cmd_verify`` verified the manifest with the *pre* phase (the old installed identity), while the
engine's ``--verify-only`` path drove ``released`` — a phase ``verify_manifest`` did not accept.

These tests prove the corrected contract end to end with a real temporary Git repository, a real
launcher, a real unit file and drop-ins, and the real ``manifest --verify`` CLI:

* before deployment, ``pre`` passes and ``released`` refuses;
* after a simulated deployment, ``released`` passes;
* wrong HEAD, a changed unit, a missing hold drop-in and a remaining freeze each make ``released``
  refuse.

    env -u PYTHONPATH /usr/bin/python3.13 -m pytest tests/harness/test_release002_ext10.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
sys.path.insert(0, str(HARNESS))

import release002_control as ctl  # noqa: E402
import release002_preflight as pf  # noqa: E402

PY3 = "/usr/bin/python3.13"

UNIT_V1 = "[Service]\nTimeoutStopUSec=1min\nRestart=always\n"
UNIT_V2 = "[Unit]\nDescription=regenerated\n[Service]\nTimeoutStopSec=300\nRestart=always\n"
HOLD_DROPIN_TEXT = "[Service]\nEnvironment=HERMES_RECOVERY_HOLD=1\n"
TIMEOUT_DROPIN_TEXT = "[Service]\nTimeoutStopSec=300\n"


def run(cmd, cwd=None, env=None, check=False):
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)
    if env:
        e.update(env)
    proc = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"{cmd} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def git(repo, *args):
    return run(["git", "-C", str(repo), *args], check=True).stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "harness").mkdir()
    for name in ("hermes-release-002", "release002_control.py", "release002_preflight.py",
                 "release002_oldgate.py", "release002_release.sh",
                 "restore002_session_inventory.py"):
        shutil.copy2(HARNESS / name, repo / "harness" / name)
    (repo / "gateway").mkdir()
    (repo / "gateway" / "seed.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def candidate_commit(repo: Path) -> str:
    (repo / "gateway" / "seed.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "candidate")
    return git(repo, "rev-parse", "HEAD")


def install_clone(tmp_path: Path, repo: Path) -> Path:
    inst = tmp_path / "install"
    run(["git", "clone", "-q", str(repo), str(inst)], check=True)
    run(["git", "-C", str(inst), "checkout", "-q", git(repo, "rev-parse", "HEAD~1")], check=True)
    return inst


def _verify(mpath, repo, inst, launcher, cand, phase, expected_sha=None):
    args = [PY3, str(HARNESS / "release002_control.py"), "manifest", "--verify",
            "--phase", phase, "--path", str(mpath), "--repo", str(repo),
            "--install", str(inst), "--launcher", str(launcher), "--candidate", cand]
    if expected_sha:
        args += ["--expected-post-unit-sha256", expected_sha]
    return run(args)


def _fixture(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    cand = candidate_commit(repo)
    inst = install_clone(tmp_path, repo)   # checked out at base (the pre-release identity)

    launcher = tmp_path / "lib" / "hermes-release-002"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HARNESS / "hermes-release-002", launcher)

    unit = tmp_path / "etc" / "systemd" / "system" / "hermes-gateway.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(UNIT_V1, encoding="utf-8")
    dropin_dir = unit.parent / f"{unit.name}.d"
    dropin_dir.mkdir(parents=True, exist_ok=True)
    (dropin_dir / "90-recovery-hold.conf").write_text(HOLD_DROPIN_TEXT, encoding="utf-8")
    (dropin_dir / "95-timeout-stop.conf").write_text(TIMEOUT_DROPIN_TEXT, encoding="utf-8")

    mpath = tmp_path / "manifest.json"
    run([PY3, str(HARNESS / "release002_control.py"), "manifest", "--write",
         "--path", str(mpath), "--repo", str(repo), "--install", str(inst),
         "--launcher", str(launcher), "--candidate", cand,
         "--expected-install-head", base, "--unit", str(unit)], check=True)
    return repo, base, cand, inst, launcher, unit, mpath


def _deploy(inst, unit, cand):
    """Simulate the release's one deployment: install at the candidate, unit regenerated to 300s."""
    run(["git", "-C", str(inst), "checkout", "-q", cand], check=True)
    unit.write_text(UNIT_V2, encoding="utf-8")
    return ctl.sha256_file(unit)


class TestReleasedManifestVerification:
    def test_before_deployment_pre_passes_released_refuses(self, tmp_path):
        repo, base, cand, inst, launcher, unit, mpath = _fixture(tmp_path)
        pre = _verify(mpath, repo, inst, launcher, cand, "pre")
        assert pre.returncode == 0, json.loads(pre.stdout)
        rel = _verify(mpath, repo, inst, launcher, cand, "released")
        assert rel.returncode == 2, rel.stdout
        assert "install_head_released" in json.loads(rel.stdout)["failed"]

    def test_after_simulated_deployment_released_passes(self, tmp_path):
        repo, base, cand, inst, launcher, unit, mpath = _fixture(tmp_path)
        post_sha = _deploy(inst, unit, cand)
        rel = _verify(mpath, repo, inst, launcher, cand, "released", expected_sha=post_sha)
        body = json.loads(rel.stdout)
        assert rel.returncode == 0, body
        # every identity gate must have been evaluated and passed
        names = {g["gate"] for g in body["gates"]}
        assert {"install_head_released", "install_tree_released", "unit_timeout_released",
                "unit_sha256_released", "dropin_released:90-recovery-hold.conf",
                "dropin_released:95-timeout-stop.conf"} <= names

    def test_wrong_head_refuses(self, tmp_path):
        repo, base, cand, inst, launcher, unit, mpath = _fixture(tmp_path)
        _deploy(inst, unit, cand)
        run(["git", "-C", str(inst), "checkout", "-q", base], check=True)  # drift back
        rel = _verify(mpath, repo, inst, launcher, cand, "released",
                      expected_sha=ctl.sha256_file(unit))
        assert rel.returncode == 2
        assert "install_head_released" in json.loads(rel.stdout)["failed"]

    def test_changed_unit_timeout_refuses(self, tmp_path):
        repo, base, cand, inst, launcher, unit, mpath = _fixture(tmp_path)
        _deploy(inst, unit, cand)
        unit.write_text("[Service]\nTimeoutStopSec=60\nRestart=always\n", encoding="utf-8")
        sha = ctl.sha256_file(unit)
        rel = _verify(mpath, repo, inst, launcher, cand, "released", expected_sha=sha)
        assert rel.returncode == 2
        assert "unit_timeout_released" in json.loads(rel.stdout)["failed"]

    def test_wrong_unit_bytes_refuse(self, tmp_path):
        repo, base, cand, inst, launcher, unit, mpath = _fixture(tmp_path)
        _deploy(inst, unit, cand)
        rel = _verify(mpath, repo, inst, launcher, cand, "released", expected_sha="0" * 64)
        assert rel.returncode == 2
        assert "unit_sha256_released" in json.loads(rel.stdout)["failed"]

    def test_missing_hold_dropin_refuses(self, tmp_path):
        repo, base, cand, inst, launcher, unit, mpath = _fixture(tmp_path)
        post_sha = _deploy(inst, unit, cand)
        (unit.parent / f"{unit.name}.d" / "90-recovery-hold.conf").unlink()
        rel = _verify(mpath, repo, inst, launcher, cand, "released", expected_sha=post_sha)
        assert rel.returncode == 2
        assert "dropin_released:90-recovery-hold.conf" in json.loads(rel.stdout)["failed"]

    def test_no_phase_default_is_not_released(self, tmp_path):
        """The old bug: a phase-less verify defaulted to ``pre``. It must never silently pass
        post-release as ``released`` — an omitted phase stays ``pre`` (the conservative default)."""
        repo, base, cand, inst, launcher, unit, mpath = _fixture(tmp_path)
        post_sha = _deploy(inst, unit, cand)
        # install is now at the candidate; a default (pre) verify must REFUSE, proving the default
        # did not quietly become a released check.
        args = [PY3, str(HARNESS / "release002_control.py"), "manifest", "--verify",
                "--path", str(mpath), "--repo", str(repo), "--install", str(inst),
                "--launcher", str(launcher), "--candidate", cand]
        proc = run(args)
        assert proc.returncode == 2
        assert "install_head_pre" in json.loads(proc.stdout)["failed"]


class TestReleasedRuntimeContract:
    """EXT-10 runtime half: operational gateway state, cleared admission freeze, intact hold."""

    def _rows(self, state, marker, monkeypatch, pid=4242, sha="c" * 40):
        monkeypatch.setattr(pf, "_drain_marker", lambda prefix="": marker)
        return dict((n, ok) for n, ok, _ in pf.runtime_gate_rows("released",
                    {"gateway_state": state, "pid": pid, "code_sha": sha, "exit_reason": None},
                    pid, sha))

    def test_released_requires_running_and_no_freeze(self, monkeypatch):
        rows = self._rows("running", {"present": False, "ok": False, "detail": "none"}, monkeypatch)
        assert rows["runtime_state_running"] is True
        assert rows["no_preexisting_freeze"] is True

    def test_released_refuses_a_remaining_freeze(self, monkeypatch):
        rows = self._rows("running", {"present": True, "ok": True, "detail": "owned"},
                          monkeypatch)
        assert rows["no_preexisting_freeze"] is False

    def test_released_refuses_a_non_running_gateway(self, monkeypatch):
        for state in ("draining", "stopped", "failed"):
            rows = self._rows(state, {"present": False, "ok": False, "detail": "none"},
                              monkeypatch)
            assert rows["runtime_state_running"] is False, state

    def test_released_preflight_cli_refuses_remaining_freeze(self, tmp_path):
        """Drive the real preflight CLI in the released phase against a home that still holds the
        drain marker, and confirm the freeze gate is what fails."""
        repo = make_repo(tmp_path)
        cand = candidate_commit(repo)
        inst = tmp_path / "install"
        run(["git", "clone", "-q", str(repo), str(inst)], check=True)
        run(["git", "-C", str(inst), "checkout", "-q", cand], check=True)
        home = tmp_path / "home"
        (home / "sessions").mkdir(parents=True)
        (home / "gateway_state.json").write_text(json.dumps(
            {"gateway_state": "running", "pid": 4242, "code_sha": cand, "exit_reason": None}))
        (home / ".drain_request.json").write_text(json.dumps(
            {"principal": "release-freeze:stale", "reason": "left behind"}))
        env = dict(HERMES_RELEASE_INSTALL=str(inst), HERMES_RELEASE_HOME=str(home))
        proc = run([PY3, str(HARNESS / "release002_preflight.py"), "--phase", "released",
                    "--candidate", cand, "--skip-oldgate"], env=env)
        assert proc.returncode == 2
        assert "no_preexisting_freeze" in json.loads(proc.stdout)["failed"]
