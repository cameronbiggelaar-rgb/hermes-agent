"""EXT-RESTORE-002-03/05 controls at the process boundary.

The launcher and the engine are the two halves of "one authoritative launch path": the launcher
pins and verifies, the engine refuses to mutate without the launcher's handshake. These tests run
both as real processes and assert the refusal codes/messages, so a refactor that quietly turns a
``die`` into a warning fails here.

Nothing in this file touches the live installation, the service or the release directories: the
launcher's ``HERMES_RELEASE_*`` overrides point at throwaway repositories, and the launcher copy
lives under ``$HOME`` because a real launcher is refused under ``/tmp``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "harness" / "hermes-release-002"
ENGINE = ROOT / "harness" / "release002_release.sh"
CTRL_PATH = ROOT / "harness" / "release002_control.py"

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


def _load_control():
    spec = importlib.util.spec_from_file_location("release002_control_launcher_uut", CTRL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ctrl = _load_control()


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def world():
    """A throwaway lib/install/repo layout outside /tmp, mirroring the real one."""
    base = Path(tempfile.mkdtemp(prefix="release-launcher-", dir=str(Path.home())))
    try:
        repo = base / "src"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        deployed = commit(repo, {p: f"# deployed {p}\n" for p in ENGINE_FILES}, "deployed")
        candidate = commit(repo, {p: f"# candidate {p}\n" for p in ENGINE_FILES}, "candidate")

        install = base / "install"
        subprocess.run(["git", "clone", "-q", str(repo), str(install)], check=True, env=GIT_ENV)
        git(install, "reset", "--hard", "-q", deployed)

        lib = base / "lib"
        lib.mkdir()
        launcher = lib / "hermes-release-002"
        shutil.copy2(LAUNCHER, launcher)

        unit = base / "system" / "hermes-gateway.service"
        unit.parent.mkdir()
        unit.write_text("[Service]\nTimeoutStopSec=300s\n", encoding="utf-8")
        dropins = []
        for name in ("90-recovery-hold.conf", "95-timeout-stop.conf"):
            path = unit.parent / f"{unit.name}.d" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {name}\n", encoding="utf-8")
            dropins.append(path)

        home = base / "home"
        home.mkdir()
        env = {
            **os.environ,
            "HERMES_RELEASE_LIB": str(lib),
            "HERMES_RELEASE_INSTALL": str(install),
            "HERMES_RELEASE_REPO": str(repo),
            "HERMES_RELEASE_JOURNAL": str(base / "var" / "journal.json"),
            "HERMES_HOME": str(home),
            "TMPDIR": str(base / "tmp"),
            "EXPECT_HEAD": deployed,
            "EXPECT_TREE": git(install, "rev-parse", "HEAD^{tree}"),
        }
        (base / "tmp").mkdir()

        def run(argv, *, cwd=None):
            return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                                  env=env, cwd=str(cwd or base), timeout=300)

        def write_manifest(*, launcher_path: Path | None = None, launcher_sha: str | None = None):
            path = launcher_path or launcher
            manifest = ctrl.build_manifest(
                repo=repo, candidate=candidate, install=install, launcher=path, unit=unit,
                dropins=dropins, engine_paths=list(ENGINE_FILES), expected_install_head=deployed)
            if launcher_sha is not None:
                manifest["launcher"]["sha256"] = launcher_sha
            (lib / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            return manifest

        yield {"base": base, "repo": repo, "install": install, "lib": lib, "launcher": launcher,
               "candidate": candidate, "deployed": deployed, "env": env, "run": run,
               "write_manifest": write_manifest}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_both_scripts_are_syntactically_valid():
    for script in (LAUNCHER, ENGINE):
        proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"


def test_launcher_status_is_read_only_without_a_manifest(world):
    proc = world["run"]([world["launcher"], "status"])
    assert proc.returncode == 0
    assert "manifest: absent" in proc.stdout


def test_launcher_refuses_every_command_before_staging(world):
    proc = world["run"]([world["launcher"], "plan", "--candidate", world["candidate"]])
    assert proc.returncode == 65 and "no manifest" in proc.stderr
    for command in ("verify", "recover"):
        proc = world["run"]([world["launcher"], command, "--candidate", world["candidate"]])
        assert proc.returncode == 65, (command, proc.stdout, proc.stderr)
        assert "needs root" in proc.stderr


def test_launcher_refuses_a_tampered_copy(world):
    world["write_manifest"]()
    world["launcher"].write_text(world["launcher"].read_text() + "\n# edited\n", encoding="utf-8")
    proc = world["run"]([world["launcher"], "plan", "--candidate", world["candidate"]])
    assert proc.returncode == 65
    assert "checksum does not match" in proc.stderr


def test_launcher_refuses_a_copy_the_manifest_does_not_pin(world):
    other = world["lib"] / "hermes-release-002.copy"
    shutil.copy2(world["launcher"], other)
    world["write_manifest"](launcher_path=other)
    proc = world["run"]([world["launcher"], "plan", "--candidate", world["candidate"]])
    assert proc.returncode == 65
    assert "the manifest pins" in proc.stderr


def test_launcher_refuses_to_run_from_inside_the_installation(world):
    inside = world["install"] / "bin" / "hermes-release-002"
    inside.parent.mkdir()
    shutil.copy2(world["launcher"], inside)
    world["write_manifest"](launcher_path=inside)
    proc = world["run"]([inside, "plan", "--candidate", world["candidate"]])
    assert proc.returncode == 65
    assert "inside the installation" in proc.stderr


def test_launcher_refuses_a_temporary_location(world, tmp_path):
    staged = tmp_path / "hermes-release-002"
    shutil.copy2(world["launcher"], staged)
    world["write_manifest"](launcher_path=staged)
    proc = world["run"]([staged, "plan", "--candidate", world["candidate"]])
    assert proc.returncode == 65
    assert "temporary path" in proc.stderr


def test_launcher_apply_needs_root(world):
    world["write_manifest"]()
    proc = world["run"]([world["launcher"], "apply", "--candidate", world["candidate"]])
    assert proc.returncode == 65
    assert "needs root" in proc.stderr


def test_launcher_rejects_an_unknown_command(world):
    proc = world["run"]([world["launcher"], "deploy", "--candidate", world["candidate"]])
    assert proc.returncode == 65
    assert "unknown argument" in proc.stderr


def test_engine_refuses_a_candidate_the_installation_cannot_see(world):
    absent = "0" * 40
    proc = world["run"](["bash", ENGINE, "--plan", "--candidate", absent])
    assert proc.returncode == 64
    assert "not present" in proc.stderr


def test_engine_refuses_to_mutate_without_an_approval(world):
    proc = world["run"](["bash", ENGINE, "--apply", "--candidate", world["candidate"]])
    assert proc.returncode == 65
    assert "HERMES_RELEASE_APPROVAL must equal" in proc.stderr


def test_engine_refuses_to_mutate_without_the_launcher_handshake(world):
    env = dict(world["env"])
    env["HERMES_RELEASE_APPROVAL"] = world["candidate"]
    proc = subprocess.run(["bash", str(ENGINE), "--apply", "--candidate", world["candidate"]],
                          capture_output=True, text=True, env=env, cwd=str(world["base"]), timeout=300)
    assert proc.returncode == 65
    assert "launch token" in proc.stderr


def test_engine_refuses_a_mismatched_launcher_token(world):
    env = dict(world["env"])
    env.update({"HERMES_RELEASE_APPROVAL": world["candidate"],
                "HERMES_RELEASE_LAUNCH_TOKEN": "a" * 64,
                "HERMES_RELEASE_TOKEN_EXPECTED": "b" * 64})
    proc = subprocess.run(["bash", str(ENGINE), "--apply", "--candidate", world["candidate"]],
                          capture_output=True, text=True, env=env, cwd=str(world["base"]), timeout=300)
    assert proc.returncode == 65
    assert "launch token" in proc.stderr


def test_engine_rejects_an_unknown_argument(world):
    proc = world["run"](["bash", ENGINE, "--deploy", "--candidate", world["candidate"]])
    assert proc.returncode == 64
    assert "unknown argument" in proc.stderr


def test_engine_refuses_a_candidate_that_is_not_a_full_sha(world):
    proc = world["run"](["bash", ENGINE, "--plan", "--candidate", "deadbeef"])
    assert proc.returncode == 64
    assert "full 40-character commit SHA" in proc.stderr


# ---------------------------------------------------------------- dry-run (plan) performs nothing

def _staged_engine(base: Path, preflight_body: str) -> Path:
    """Copy the real engine + control library, with a stub preflight, into a staging dir."""
    stage = base / "stage"
    (stage / "harness").mkdir(parents=True)
    for name in ("release002_release.sh", "release002_control.py"):
        shutil.copy2(ROOT / "harness" / name, stage / "harness" / name)
    stub = stage / "harness" / "release002_preflight.py"
    stub.write_text(preflight_body, encoding="utf-8")
    return stage


GO_PREFLIGHT = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"verdict": "GO", "gates": {"identity": True, "no_active_turns": True},
                  "failed": []}))
sys.exit(0)
"""

NOGO_PREFLIGHT = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"verdict": "NO_GO", "gates": {"identity": True, "no_active_turns": False,
                  "recent_session_activity": False}, "failed": ["no_active_turns"]}))
sys.exit(2)
"""


def test_engine_plan_reaches_go_without_touching_anything(world):
    stage = _staged_engine(world["base"], GO_PREFLIGHT)
    archive = world["base"] / "archive"
    proc = world["run"](["bash", stage / "harness" / "release002_release.sh", "--plan",
                         "--candidate", world["candidate"], "--archive", archive],
                        cwd=stage)
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])
    assert "DRY RUN RESULT: GO" in proc.stdout
    assert "merge --ff-only" in proc.stdout
    assert git(world["install"], "rev-parse", "HEAD") == world["deployed"]
    assert git(world["install"], "status", "--porcelain") == ""
    assert not (world["base"] / "var" / "journal.json").exists()
    assert not (world["env"]["HERMES_HOME"] and (Path(world["env"]["HERMES_HOME"]) / "ESTOP").exists())
    assert not list(Path(world["env"]["HERMES_HOME"]).glob("**/*drain*"))


def test_engine_plan_still_prints_the_runbook_when_the_host_is_busy(world):
    stage = _staged_engine(world["base"], NOGO_PREFLIGHT)
    archive = world["base"] / "archive-no"
    proc = world["run"](["bash", stage / "harness" / "release002_release.sh", "--plan",
                         "--candidate", world["candidate"], "--archive", archive],
                        cwd=stage)
    assert proc.returncode == 2
    assert "preflight verdict NO_GO" in proc.stdout
    assert "DRY RUN RESULT: NO_GO" in proc.stdout
    assert "release the freeze" in proc.stdout
    assert git(world["install"], "rev-parse", "HEAD") == world["deployed"]
