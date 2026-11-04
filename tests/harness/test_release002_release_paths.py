"""Negative controls for the RESTORE-002 release-path findings EXT-06…EXT-09.

Each class proves the *failure* the External CA named actually fails, and that the corrected path
succeeds — a verifier that cannot fail proves nothing.

    env -u PYTHONPATH /usr/bin/python3.13 -m pytest tests/harness/test_release002_release_paths.py -q
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
import release002_oldgate as oldgate  # noqa: E402
import release002_preflight as pf  # noqa: E402

PY3 = "/usr/bin/python3.13"
LAUNCHER = "harness/hermes-release-002"
UNIT_TEXT = """[Service]
TimeoutStopUSec=5min
Restart=always
"""


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


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "harness").mkdir()
    for name in ("hermes-release-002", "release002_control.py", "release002_preflight.py",
                 "release002_oldgate.py", "release002_release.sh"):
        shutil.copy2(HARNESS / name, repo / "harness" / name)
    (repo / "gateway").mkdir()
    (repo / "gateway" / "seed.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def install_clone(tmp_path: Path, repo: Path) -> Path:
    inst = tmp_path / "install"
    run(["git", "clone", "-q", str(repo), str(inst)], check=True)
    # The installation under release is the *pre-release* identity, not the candidate.
    run(["git", "-C", str(inst), "checkout", "-q", git(repo, "rev-parse", "HEAD~1")], check=True)
    return inst


def candidate_commit(repo: Path, mutate=lambda r: None) -> str:
    mutate(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "candidate")
    return git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------- EXT-06
class TestPhaseAwarePostflight:
    """EXT-06: the post-release check must not require a state the release itself forbids."""

    def _state(self, state, marker_owner="release-freeze:run-1", pid=4242, sha="c" * 40):
        return {"gateway_state": state, "pid": pid, "code_sha": sha, "exit_reason": None}

    def test_post_phase_accepts_the_freeze_it_engaged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pf, "_drain_marker",
                            lambda prefix="": {"present": True, "ok": True, "detail": "owned"})
        rows = dict((n, ok) for n, ok, _ in pf.runtime_gate_rows("post", self._state("draining"), 4242, "c" * 40))
        assert rows["runtime_state_frozen"] is True
        assert rows["admission_freeze_held"] is True
        assert rows["runtime_state_pid_matches"] is True

    def test_post_phase_rejects_running_gateway(self, monkeypatch):
        monkeypatch.setattr(pf, "_drain_marker",
                            lambda prefix="": {"present": True, "ok": True, "detail": "owned"})
        rows = dict((n, ok) for n, ok, _ in pf.runtime_gate_rows("post", self._state("running"), 4242, "c" * 40))
        assert rows["runtime_state_frozen"] is False, "a post-release check must not pass while unfrozen"

    def test_post_phase_rejects_foreign_or_absent_freeze(self, monkeypatch):
        for marker in ({"present": False, "ok": False, "detail": "none"},
                       {"present": True, "ok": False, "detail": "principal=someone-else"}):
            monkeypatch.setattr(pf, "_drain_marker", lambda prefix="", m=marker: m)
            rows = dict((n, ok) for n, ok, _ in pf.runtime_gate_rows("post", self._state("draining"), 4242, "c" * 40))
            assert rows["admission_freeze_held"] is False

    def test_pre_phase_still_requires_running_and_no_freeze(self, monkeypatch):
        monkeypatch.setattr(pf, "_drain_marker",
                            lambda prefix="": {"present": False, "ok": False, "detail": "none"})
        ok = dict((n, v) for n, v, _ in pf.runtime_gate_rows("pre", self._state("running"), 4242, "c" * 40))
        assert ok["runtime_state_running"] is True and ok["no_preexisting_freeze"] is True
        bad = dict((n, v) for n, v, _ in pf.runtime_gate_rows("pre", self._state("draining"), 4242, "c" * 40))
        assert bad["runtime_state_running"] is False

    def test_released_phase_requires_running_again(self, monkeypatch):
        monkeypatch.setattr(pf, "_drain_marker",
                            lambda prefix="": {"present": False, "ok": False, "detail": "none"})
        rows = dict((n, v) for n, v, _ in pf.runtime_gate_rows("released", self._state("running"), 4242, "c" * 40))
        assert rows["runtime_state_running"] is True
        rows = dict((n, v) for n, v, _ in pf.runtime_gate_rows("released", self._state("draining"), 4242, "c" * 40))
        assert rows["runtime_state_running"] is False

    def test_post_phase_cli_requires_candidate(self):
        proc = run([PY3, str(HARNESS / "release002_preflight.py"), "--phase", "post"])
        assert proc.returncode == 2
        body = json.loads(proc.stdout)
        assert body["failed"] == ["post_phase_requires_candidate"]

    def _fixture(self, tmp_path, state="draining", marker=True):
        import time as _t
        repo = make_repo(tmp_path)
        cand = candidate_commit(repo, lambda r: (r / "gateway" / "seed.py").write_text("VALUE = 2\n"))
        inst = tmp_path / "install"
        run(["git", "clone", "-q", str(repo), str(inst)], check=True)
        run(["git", "-C", str(inst), "checkout", "-q", cand], check=True)
        home = tmp_path / "home"
        (home / "sessions").mkdir(parents=True)
        (home / "gateway_state.json").write_text(json.dumps(
            {"gateway_state": state, "pid": 4242, "code_sha": cand, "exit_reason": None}))
        if marker:
            (home / ".drain_request.json").write_text(json.dumps(
                {"principal": "release-freeze:run-1", "requested_at": _t.time(), "reason": "cutover"}))
        env = dict(HERMES_RELEASE_INSTALL=str(inst), HERMES_RELEASE_HOME=str(home))
        return inst, home, cand, env

    def _postflight(self, tmp_path, phase="post", **kw):
        inst, home, cand, env = self._fixture(tmp_path, **kw)
        out = tmp_path / "pf.json"
        proc = run([PY3, str(HARNESS / "release002_preflight.py"), "--phase", phase,
                    "--candidate", cand, "--skip-oldgate", "--json", str(out)], env=env)
        return proc, json.loads(out.read_text()) if out.exists() else json.loads(proc.stdout)

    def test_frozen_postflight_verification_succeeds(self, tmp_path):
        """The exact EXT-06 negative control: a frozen cutover must VERIFY, not be rejected."""
        proc, body = self._postflight(tmp_path)
        failed = body["failed"]
        assert "runtime_state_frozen" not in failed, failed
        assert "admission_freeze_held" not in failed, failed
        rows = {r["gate"]: r for r in body["gates"]}
        assert rows["runtime_state_frozen"]["ok"] is True
        assert rows["admission_freeze_held"]["ok"] is True
        assert proc.returncode in (0, 2)  # 2 only for host-only gates a fixture cannot satisfy

    def test_postflight_still_fails_when_the_gateway_is_running(self, tmp_path):
        proc, body = self._postflight(tmp_path, state="running")
        assert "runtime_state_frozen" in body["failed"]
        assert proc.returncode == 2

    def test_postflight_fails_without_this_runs_freeze(self, tmp_path):
        proc, body = self._postflight(tmp_path, marker=False)
        assert "admission_freeze_held" in body["failed"]
        assert proc.returncode == 2

    def test_postflight_rejects_a_foreign_freeze(self, tmp_path):
        inst, home, cand, env = self._fixture(tmp_path)
        import time as _t
        (home / ".drain_request.json").write_text(json.dumps(
            {"principal": "someone-else", "requested_at": _t.time()}))
        proc = run([PY3, str(HARNESS / "release002_preflight.py"), "--phase", "post",
                    "--candidate", cand, "--skip-oldgate"], env=env)
        body = json.loads(proc.stdout)
        assert "admission_freeze_held" in body["failed"]

    def test_preflight_pre_still_requires_a_running_gateway(self, tmp_path):
        inst, home, cand, env = self._fixture(tmp_path, state="running", marker=False)
        base = git(inst, "rev-parse", "HEAD")
        proc = run([PY3, str(HARNESS / "release002_preflight.py"), "--phase", "pre",
                    "--expect-head", base, "--skip-oldgate"], env=env)
        body = json.loads(proc.stdout)
        assert "runtime_state_running" not in body["failed"], body["failed"]

    def test_engine_uses_phase_selecting_preflight(self):
        engine = (HARNESS / "release002_release.sh").read_text()
        assert "preflight post" in engine
        assert 'preflight pre "$EXPECT_HEAD" "$EXPECT_TREE"' in engine


# --------------------------------------------------------------------------- EXT-07
class TestSeparatePrePostIdentities:
    def _fixture(self, tmp_path):
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        cand = candidate_commit(repo, lambda r: (r / "gateway" / "seed.py").write_text("VALUE = 2\n"))
        inst = install_clone(tmp_path, repo)
        unit = tmp_path / "hermes-gateway.service"
        unit.write_text(UNIT_TEXT, encoding="utf-8")
        return repo, base, cand, inst, unit

    def _manifest(self, repo, base, cand, inst, unit):
        return ctl.build_manifest(
            repo=repo, candidate=cand, install=inst, launcher=repo / LAUNCHER,
            unit=unit, dropins=[], engine_paths=[], expected_install_head=base)

    def test_manifest_pins_two_identity_sets(self, tmp_path):
        repo, base, cand, inst, unit = self._fixture(tmp_path)
        m = self._manifest(repo, base, cand, inst, unit)
        assert m["pre_release"]["install_head"] == base
        assert m["post_release"]["install_head"] == cand
        assert m["post_release"]["install_tree"] == m["candidate_tree"]
        assert m["pre_release"]["unit"]["sha256"] == ctl.sha256_file(unit)
        assert m["post_release"]["unit"]["timeout_stop_sec"] == ctl.POST_RELEASE_TIMEOUT_STOP_SEC
        assert m["schema"] == ctl.MANIFEST_SCHEMA

    def test_pre_phase_passes_before_mutation(self, tmp_path):
        repo, base, cand, inst, unit = self._fixture(tmp_path)
        m = self._manifest(repo, base, cand, inst, unit)
        rep = ctl.verify_manifest(m, repo=repo, install=inst, launcher=repo / LAUNCHER, phase="pre",
                                  source=repo, engine_dir=repo)
        assert rep["ok"] is True, rep["failed"]
        assert rep["phase"] == "pre"

    def test_post_phase_fails_while_still_on_the_old_installation(self, tmp_path):
        """The exact defect EXT-07 names: post verification must not demand the old identity."""
        repo, base, cand, inst, unit = self._fixture(tmp_path)
        m = self._manifest(repo, base, cand, inst, unit)
        rep = ctl.verify_manifest(m, repo=repo, install=inst, launcher=repo / LAUNCHER, phase="post")
        assert rep["ok"] is False
        assert "install_head_post" in rep["failed"], rep["failed"]

    def test_post_phase_passes_after_a_real_swap(self, tmp_path):
        repo, base, cand, inst, unit = self._fixture(tmp_path)
        m = self._manifest(repo, base, cand, inst, unit)
        run(["git", "-C", str(inst), "fetch", "-q", str(repo), cand], check=True)
        run(["git", "-C", str(inst), "merge", "--ff-only", "-q", cand], check=True)
        rep = ctl.verify_manifest(m, repo=repo, install=inst, launcher=repo / LAUNCHER, phase="post",
                                  expected_post_unit_sha256=ctl.sha256_file(unit))
        assert rep["ok"] is True, rep["failed"]
        assert "install_head_post" not in rep["failed"]

    def test_post_phase_refuses_without_a_journaled_checksum(self, tmp_path):
        """EXT-11: an omitted checksum must be a gate failure, not a skipped comparison."""
        repo, base, cand, inst, unit = self._fixture(tmp_path)
        m = self._manifest(repo, base, cand, inst, unit)
        run(["git", "-C", str(inst), "fetch", "-q", str(repo), cand], check=True)
        run(["git", "-C", str(inst), "merge", "--ff-only", "-q", cand], check=True)
        for omitted in (None, "", "not-a-digest", "0" * 63):
            rep = ctl.verify_manifest(m, repo=repo, install=inst, launcher=repo / LAUNCHER,
                                      phase="post", expected_post_unit_sha256=omitted)
            assert rep["ok"] is False, omitted
            assert "unit_sha256_post" in rep["failed"], (omitted, rep["failed"])

    def test_post_phase_rejects_a_regenerated_unit_at_the_wrong_timeout(self, tmp_path):
        repo, base, cand, inst, unit = self._fixture(tmp_path)
        m = self._manifest(repo, base, cand, inst, unit)
        run(["git", "-C", str(inst), "fetch", "-q", str(repo), cand], check=True)
        run(["git", "-C", str(inst), "merge", "--ff-only", "-q", cand], check=True)
        unit.write_text("[Service]\nTimeoutStopUSec=210s\n", encoding="utf-8")
        rep = ctl.verify_manifest(m, repo=repo, install=inst, launcher=repo / LAUNCHER, phase="post")
        assert rep["ok"] is False
        assert "unit_timeout_post" in rep["failed"]

    def test_post_phase_honours_the_journaled_unit_checksum(self, tmp_path):
        repo, base, cand, inst, unit = self._fixture(tmp_path)
        m = self._manifest(repo, base, cand, inst, unit)
        run(["git", "-C", str(inst), "fetch", "-q", str(repo), cand], check=True)
        run(["git", "-C", str(inst), "merge", "--ff-only", "-q", cand], check=True)
        real = ctl.sha256_file(unit)
        ok = ctl.verify_manifest(m, repo=repo, install=inst, launcher=repo / LAUNCHER, phase="post",
                                 expected_post_unit_sha256=real)
        assert ok["ok"] is True, ok["failed"]
        bad = ctl.verify_manifest(m, repo=repo, install=inst, launcher=repo / LAUNCHER, phase="post",
                                  expected_post_unit_sha256="0" * 64)
        assert bad["ok"] is False and "unit_sha256_post" in bad["failed"]

    def test_engine_verifies_both_phases(self):
        engine = (HARNESS / "release002_release.sh").read_text()
        assert "manifest_verify pre" in engine
        assert 'manifest_verify post "$POST_UNIT_SHA"' in engine


# --------------------------------------------------------------------------- EXT-08
class TestPreMutationNoGoIsInert:
    def test_every_pre_mutation_phase_releases_only_the_freeze(self):
        for phase in sorted(ctl.PRE_MUTATION_PHASES):
            d = ctl.decide_failure(phase, mutated=False)
            assert d["action"] == "release_freeze", (phase, d)
            assert d["restart"] is False and d["exit_code"] == 2, (phase, d)

    def test_after_mutation_the_rollback_arm_is_taken(self):
        d = ctl.decide_failure("deploy", mutated=True)
        assert d["action"] in {"rollback", "restart_then_verify"}
        assert d["restart"] is True

    def test_restart_budget_is_one(self):
        assert ctl.MAX_RESTART_ATTEMPTS == 1
        d = ctl.decide_failure("verify", mutated=True, restart_attempts=1)
        assert d["action"] == "needs_human"

    def test_revalidate_block_never_calls_rollback(self):
        engine = (HARNESS / "release002_release.sh").read_text()
        start = engine.index("install HEAD drifted")
        end = engine.index('journal revalidate "identities and admission revalidated immediately before mutation"')
        block = engine[start:end]
        assert "rollback " not in block, "a pre-mutation drift must not restart the gateway"
        assert block.count("abort_before_mutation") >= 5

    def test_abort_path_does_not_touch_the_service(self):
        engine = (HARNESS / "release002_release.sh").read_text()
        body = engine[engine.index("abort_before_mutation() {"):engine.index("rollback() {")]
        for forbidden in ("systemctl", "git merge", "install -m", "gateway install"):
            assert forbidden not in body, f"pre-mutation NO_GO must not run '{forbidden}'"
        assert "exit 2" in body
        assert 'FROZEN" -eq 1' in body and 'ESTOP_OURS" -eq 1' in body

    def test_engine_wires_drift_to_the_inert_abort(self):
        engine = (HARNESS / "release002_release.sh").read_text()
        for phrase in ("install HEAD drifted", "install tree drifted", "worktree became dirty",
                       "unit file changed between archive", "pre-release manifest identities",
                       "admission is not frozen"):
            line = [l for l in engine.splitlines() if phrase in l]
            assert line and all("abort_before_mutation revalidate" in l for l in line), phrase


class TestEngineAbortsWithoutRestarting:
    """EXT-08 at engine level: a NO_GO before mutation changes nothing and restarts nothing."""

    def _fixture(self, tmp_path):
        repo = make_repo(tmp_path)
        base = git(repo, "rev-parse", "HEAD")
        cand = candidate_commit(repo, lambda r: (r / "gateway" / "seed.py").write_text("VALUE = 2\n"))
        inst = install_clone(tmp_path, repo)
        home = tmp_path / "home"
        (home / "sessions").mkdir(parents=True)
        shim = tmp_path / "bin"
        shim.mkdir()
        log = tmp_path / "systemctl.log"
        (shim / "systemctl").write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nexit 0\n")
        (shim / "systemctl").chmod(0o755)
        return repo, base, cand, inst, home, shim, log

    def test_plan_mode_aborts_with_zero_restarts_and_zero_mutation(self, tmp_path):
        repo, base, cand, inst, home, shim, log = self._fixture(tmp_path)
        env = dict(HERMES_RELEASE_INSTALL=str(inst), HERMES_HOME=str(home), HERMES_RELEASE_HOME=str(home),
                   EXPECT_HEAD=base, EXPECT_TREE=git(inst, "rev-parse", "HEAD^{tree}"),
                   SYSCALL_LOG=str(log), PATH=f"{shim}:{os.environ['PATH']}")
        proc = run(["bash", str(HARNESS / "release002_release.sh"), "--plan", "--candidate", cand], env=env)
        assert proc.returncode == 2, proc.stdout[-3000:]
        assert "DRY RUN RESULT: NO_GO" in proc.stdout + proc.stderr
        calls = log.read_text().splitlines() if log.exists() else []
        verbs = {c.split()[0] for c in calls if c.strip()}
        assert verbs <= {"show"}, f"a pre-mutation NO_GO must not act on the service: {verbs}"
        # Nothing deployed, nothing archived, no freeze engaged, no journal written.
        assert git(inst, "rev-parse", "HEAD") == base
        assert (home / ".drain_request.json").exists() is False
        assert not (tmp_path / "journal.json").exists()

    def test_plan_refuses_an_absent_candidate(self, tmp_path):
        repo, base, cand, inst, home, shim, log = self._fixture(tmp_path)
        env = dict(HERMES_RELEASE_INSTALL=str(inst), HERMES_HOME=str(home),
                   SYSCALL_LOG=str(log), PATH=f"{shim}:{os.environ['PATH']}")
        proc = run(["bash", str(HARNESS / "release002_release.sh"), "--plan", "--candidate", "a" * 40], env=env)
        assert proc.returncode == 64
        assert "is not present" in proc.stderr
        assert not (log.exists() and log.read_text().strip()), "a refused candidate must not reach the service"


# --------------------------------------------------------------------------- EXT-09
class TestVerifiedBootstrap:
    def _env(self, tmp_path, repo):
        lib = tmp_path / "lib"
        return dict(HERMES_RELEASE_LIB=str(lib), HERMES_RELEASE_REPO=str(repo),
                    HERMES_RELEASE_INSTALL=str(tmp_path / "install"),
                    HERMES_RELEASE_BOOTSTRAP_UNPRIVILEGED="1"), lib

    def _boot(self, tmp_path, repo, extra=(), env=None):
        base_env, lib = self._env(tmp_path, repo)
        if env:
            base_env.update(env)
        proc = run(["bash", str(repo / LAUNCHER), "bootstrap", *extra], cwd=tmp_path, env=base_env)
        return proc, lib

    def test_bootstrap_installs_a_verified_launcher(self, tmp_path):
        repo = make_repo(tmp_path)
        proc, lib = self._boot(tmp_path, repo)
        assert proc.returncode == 0, proc.stderr + proc.stdout
        sha = ctl.sha256_file(repo / LAUNCHER)
        assert ctl.sha256_file(lib / "hermes-release-002") == sha
        manifest = json.loads((lib / "manifest.json").read_text())
        assert manifest["launcher"]["sha256"] == sha
        assert manifest["launcher"]["path"] == str(lib / "hermes-release-002")
        assert manifest["state"] == "bootstrapped"
        assert manifest["source_commit"] == git(repo, "rev-parse", "HEAD")

    def test_bootstrap_is_idempotent(self, tmp_path):
        repo = make_repo(tmp_path)
        first, lib = self._boot(tmp_path, repo)
        assert first.returncode == 0, first.stderr
        stamp = (lib / "hermes-release-002").stat().st_mtime_ns
        second, _ = self._boot(tmp_path, repo)
        assert second.returncode == 0, second.stderr
        assert "already bootstrapped" in second.stdout
        assert (lib / "hermes-release-002").stat().st_mtime_ns == stamp

    def test_bootstrap_refuses_an_uncommitted_launcher(self, tmp_path):
        repo = make_repo(tmp_path)
        edited = (repo / LAUNCHER).read_text() + "\n# local edit\n"
        (repo / LAUNCHER).write_text(edited)
        proc, lib = self._boot(tmp_path, repo)
        assert proc.returncode == 65
        assert "differs from" in proc.stderr
        assert not (lib / "hermes-release-002").exists()

    def test_bootstrap_refuses_to_clobber_a_different_launcher(self, tmp_path):
        repo = make_repo(tmp_path)
        first, lib = self._boot(tmp_path, repo)
        assert first.returncode == 0, first.stderr
        (repo / LAUNCHER).write_text((repo / LAUNCHER).read_text() + "# v2\n")
        git(repo, "commit", "-qam", "v2")
        proc, _ = self._boot(tmp_path, repo)
        assert proc.returncode == 65
        assert "different launcher" in proc.stderr

    def test_bootstrap_force_keeps_the_previous_launcher(self, tmp_path):
        repo = make_repo(tmp_path)
        first, lib = self._boot(tmp_path, repo)
        old = ctl.sha256_file(lib / "hermes-release-002")
        (repo / LAUNCHER).write_text((repo / LAUNCHER).read_text() + "# v2\n")
        git(repo, "commit", "-qam", "v2")
        proc, _ = self._boot(tmp_path, repo, extra=("--force",))
        assert proc.returncode == 0, proc.stderr
        assert (lib / f"hermes-release-002.prev-{old[:8]}").exists()
        assert ctl.sha256_file(lib / "hermes-release-002") == ctl.sha256_file(repo / LAUNCHER)

    def test_bootstrap_dry_run_writes_nothing(self, tmp_path):
        repo = make_repo(tmp_path)
        proc, lib = self._boot(tmp_path, repo, extra=("--dry-run",))
        assert proc.returncode == 0, proc.stderr
        assert "dry run" in proc.stdout
        assert not lib.exists()

    def test_bootstrap_refuses_when_run_from_the_install_path(self, tmp_path):
        repo = make_repo(tmp_path)
        _, lib = self._env(tmp_path, repo)
        lib.mkdir(parents=True)
        shutil.copy2(repo / LAUNCHER, lib / "hermes-release-002")
        env = dict(HERMES_RELEASE_LIB=str(lib), HERMES_RELEASE_REPO=str(repo),
                   HERMES_RELEASE_INSTALL=str(tmp_path / "install"),
                   HERMES_RELEASE_BOOTSTRAP_UNPRIVILEGED="1")
        proc = run(["bash", str(lib / "hermes-release-002"), "bootstrap", "--dry-run"], cwd=tmp_path, env=env)
        assert proc.returncode == 65
        assert "already running from the install path" in proc.stderr

    def test_bootstrap_manifest_makes_a_premature_release_fail_closed(self, tmp_path):
        """Bootstrapped is not staged: verification must refuse, not silently pass."""
        repo = make_repo(tmp_path)
        _, lib = self._boot(tmp_path, repo)
        proc = run([PY3, str(HARNESS / "release002_control.py"), "manifest", "--verify",
                    "--path", str(lib / "manifest.json"), "--repo", str(repo),
                    "--install", str(tmp_path / "install"), "--launcher", str(lib / "hermes-release-002"),
                    "--phase", "pre"])
        assert proc.returncode == 2
        body = json.loads(proc.stdout)
        assert "candidate_commit_shape" in body["failed"]


class TestOldRuntimeProtection:
    """EXT-09: the proof must be about the *deployed* runtime, and must be able to fail.

    The repository's ``tests/home_io_guard`` forbids this process from reading the real hermes
    home, which is correct: the probe runs as a subprocess against the deployed tree, exactly as
    the release engine runs it.
    """

    DEPLOYED = "/home/hermes/.hermes/hermes-agent"

    def _probe(self, install, tmp_path, extra=()):
        out = tmp_path / "oldgate.json"
        proc = run([PY3, str(HARNESS / "release002_oldgate.py"), "--install", str(install),
                    "--json", str(out), *extra], cwd=tmp_path)
        return proc, json.loads(out.read_text()) if out.exists() else None

    def test_deployed_runtime_is_covered(self, tmp_path):
        if run(["test", "-e", f"{self.DEPLOYED}/gateway/run_inbound.py"]).returncode != 0:
            pytest.skip("deployed installation not readable here")
        proc, report = self._probe(self.DEPLOYED, tmp_path)
        assert proc.returncode == 0, proc.stdout[-3000:]
        assert report["verdict"] == "covered", (report["uncovered"], report["unevaluable"])
        assert report["uncovered"] == []
        assert report["producers"], "no producers were checked"
        assert report["ok"] is True

    def test_removed_producer_gate_is_reported_uncovered(self, tmp_path):
        """A producer that stops consulting the drain marker must never be reported as covered."""
        if run(["test", "-e", f"{self.DEPLOYED}/gateway/run_inbound.py"]).returncode != 0:
            pytest.skip("deployed installation not readable here")
        copy = tmp_path / "deployed"
        script = (
            "import shutil, sys\n"
            "src, dst = sys.argv[1], sys.argv[2]\n"
            "for sub in ('agent','gateway','cron','hermes_cli'):\n"
            "    shutil.copytree(f'{src}/{sub}', f'{dst}/{sub}',\n"
            "                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))\n"
            "p = f'{dst}/gateway/run_inbound.py'\n"
            "t = open(p).read()\n"
            "old = ('            if self._external_drain_active:\\n'\n"
            "       '                logger.info(\"Refusing new turn for session %s \u2014 external drain active.\", _quick_key)\\n'\n"
            "       '                return t(\"gateway.busy.draining_maintenance\")\\n')\n"
            "assert t.count(old) == 1, t.count(old)\n"
            "open(p,'w').write(t.replace(old, ''))\n"
        )
        run([PY3, "-c", script, self.DEPLOYED, str(copy)], check=True)
        proc, report = self._probe(copy, tmp_path, extra=("--static-only",))
        assert proc.returncode != 0
        assert report["verdict"] != "covered"
        assert "messaging_inbound/drain_marker" in report["uncovered"], report["uncovered"]

    def test_renamed_gate_definition_is_not_assumed_covered(self, tmp_path):
        """Referencing a gate that no longer exists is not coverage."""
        if run(["test", "-e", f"{self.DEPLOYED}/agent/estop.py"]).returncode != 0:
            pytest.skip("deployed installation not readable here")
        copy = tmp_path / "deployed"
        script = (
            "import shutil, sys\n"
            "src, dst = sys.argv[1], sys.argv[2]\n"
            "for sub in ('agent','gateway','cron','hermes_cli'):\n"
            "    shutil.copytree(f'{src}/{sub}', f'{dst}/{sub}',\n"
            "                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))\n"
            "p = f'{dst}/agent/estop.py'\n"
            "open(p,'w').write(open(p).read().replace('def paused_reply','def _renamed_paused_reply'))\n"
        )
        run([PY3, "-c", script, self.DEPLOYED, str(copy)], check=True)
        proc, report = self._probe(copy, tmp_path, extra=("--static-only",))
        assert proc.returncode != 0
        assert report["verdict"] == "uncovered", report["verdict"]
        assert report["uncovered"], "a gate that cannot be located must never be assumed covered"
        assert any("not defined" in p["detail"] for p in report["producers"] if p["ok"] is False)

    def test_engine_gates_on_old_runtime_coverage(self):
        text = (HARNESS / "release002_preflight.py").read_text()
        assert "oldgate_report(" in text
        assert "old_runtime_coverage" in text
