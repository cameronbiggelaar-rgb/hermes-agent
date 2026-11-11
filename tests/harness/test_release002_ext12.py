"""EXT-RESTORE-002-12 — release manifest completeness.

The defect this module pins down: the launcher staged six engine files while
``manifest --write`` pinned five, so ``prepare_engine()`` demanded a pin the manifest never wrote and
the first real production PLAN refused before any mutation. The correction is not the missing line;
it is that the set now lives in exactly one place (``release002_control.ENGINE_FILES`` /
``PINNED_ENGINE_FILES``), staging and pinning both read it, and an audit refuses unless the staged
files, the pinned map and the inventory are the same set — byte for byte.

These tests execute the real launcher end to end (bootstrap -> stage -> manifest verify -> plan) in
isolated Git repositories with disposable paths, then prove each failure mode separately.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
sys.path.insert(0, str(HARNESS))

import release002_control as ctl  # noqa: E402

PY3 = "/usr/bin/python3.13"
# Neither the system temp dir nor the Hermes home: the launcher refuses to run from /tmp, /var/tmp
# or /dev/shm, and that guard must stay exactly as strict as it is in production.
SCRATCH = Path(os.environ.get("HERMES_EXT12_SCRATCH", str(Path.home() / ".cache" / "hermes-release-002-tests")))
HARNESS_FILES = (
    "hermes-release-002",
    "release002_control.py",
    "release002_preflight.py",
    "release002_oldgate.py",
    "release002_release.sh",
    "restore002_session_inventory.py",
)
INVENTORY_SRC = "harness/restore002_session_inventory.py"


@pytest.fixture()
def ws():
    root = SCRATCH / "ext12" / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(args, **kw):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(kw.pop("env", {}) or {})
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@example.invalid")
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, env=env,
                          timeout=120, **kw)


def git(cwd, *args) -> str:
    proc = run(["git", "-C", str(cwd), *args])
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_repo(ws: Path, tamper=None) -> tuple[Path, str, str]:
    repo = ws / "repo"
    (repo / "harness").mkdir(parents=True)
    for name in HARNESS_FILES:
        shutil.copy2(HARNESS / name, repo / "harness" / name)
    (repo / "gateway").mkdir()
    (repo / "gateway" / "seed.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "gateway" / "seed.py").write_text("VALUE = 2\n", encoding="utf-8")
    if tamper is not None:
        tamper(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "candidate")
    return repo, base, git(repo, "rev-parse", "HEAD")


def bootstrap_lib(ws: Path, repo: Path, base: str) -> dict:
    """A real bootstrap into a throwaway library; nothing has been staged yet."""
    inst = ws / "install"
    assert run(["git", "clone", "-q", str(repo), str(inst)]).returncode == 0
    assert run(["git", "-C", str(inst), "checkout", "-q", base]).returncode == 0
    lib = ws / "lib"
    env = {
        "HERMES_RELEASE_LIB": str(lib),
        "HERMES_RELEASE_INSTALL": str(inst),
        "HERMES_RELEASE_REPO": str(repo),
        "HERMES_RELEASE_JOURNAL": str(ws / "journal.json"),
        "HERMES_HOME": str(ws / "home"),
        "HERMES_RELEASE_BOOTSTRAP_UNPRIVILEGED": "1",
        "HERMES_RELEASE_STAGE_UNPRIVILEGED": "1",
        "TMPDIR": str(ws),
    }
    (ws / "home").mkdir()

    boot = run(["bash", repo / "harness" / "hermes-release-002", "bootstrap"], env=env, cwd=ws)
    assert boot.returncode == 0, boot.stdout + boot.stderr
    return dict(repo=repo, base=base, inst=inst, lib=lib, launcher=lib / "hermes-release-002",
                env=env, tmp=ws)


def staged_release(ws: Path, tamper=None) -> dict:
    """A real bootstrap + stage into a throwaway library: the state PLAN actually sees."""
    repo, base, cand = make_repo(ws, tamper=tamper)
    fx = bootstrap_lib(ws, repo, base)
    stage = run([fx["launcher"], "stage", "--candidate", cand], env=fx["env"], cwd=ws)
    assert stage.returncode == 0, stage.stdout + stage.stderr
    fx.update(cand=cand, manifest_path=fx["lib"] / "manifest.json",
              engine_dir=fx["lib"] / "engine" / cand[:12],
              manifest=json.loads((fx["lib"] / "manifest.json").read_text(encoding="utf-8")),
              stage_out=stage.stdout + stage.stderr)
    return fx


def write_manifest(fx, payload) -> None:
    fx["manifest_path"].write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def audit(fx) -> subprocess.CompletedProcess:
    return run([PY3, str(fx["engine_dir"] / "harness" / "release002_control.py"), "engine-audit",
                "--manifest", str(fx["manifest_path"]), "--engine-dir", str(fx["engine_dir"])])


def plan(fx) -> subprocess.CompletedProcess:
    return run([fx["launcher"], "plan", "--candidate", fx["cand"]], env=fx["env"], cwd=fx["tmp"])


# ------------------------------------------------------------------ the single inventory
class TestSingleAuthoritativeInventory:
    def test_the_inventory_names_every_staged_file_including_the_inventory_script(self):
        # The EXT-12 file itself must be pinned: this is the regression that started all of it.
        assert INVENTORY_SRC in ctl.ENGINE_FILES
        assert ctl.LAUNCHER_REL == "harness/hermes-release-002"
        assert set(ctl.ENGINE_FILES).isdisjoint({ctl.LAUNCHER_REL})
        assert set(ctl.PINNED_ENGINE_FILES) == set(ctl.ENGINE_FILES) | {ctl.LAUNCHER_REL}

    def test_the_launcher_no_longer_carries_its_own_copy_of_the_list(self):
        body = (HARNESS / "hermes-release-002").read_text(encoding="utf-8")
        assert "ENGINE_FILES=(" not in body
        assert "harness/release002_release.sh harness/release002_control.py" not in body
        # ... and it asks the candidate's control for the list, then audits, in both paths.
        assert "engine-files --set" in body
        assert body.count("engine_audit ") >= 2

    def test_the_cli_reports_the_same_inventory_the_module_declares(self):
        staged = run([PY3, HARNESS / "release002_control.py", "engine-files"]).stdout.split()
        pinned = run([PY3, HARNESS / "release002_control.py", "engine-files", "--set", "pinned"]).stdout.split()
        assert staged == list(ctl.ENGINE_FILES)
        assert pinned == list(ctl.PINNED_ENGINE_FILES)

    def test_no_production_caller_can_supply_its_own_engine_list(self):
        """``engine_paths`` is a fixture hook; a production call site passing it is the drift bug."""
        body = (HARNESS / "release002_control.py").read_text(encoding="utf-8")
        assert "engine_paths=" not in body          # no call site may pin its own list
        assert "list(PINNED_ENGINE_FILES) if engine_paths is None" in body

    def test_the_manifest_pins_every_staged_file_and_the_launcher_blob(self, ws):
        repo, base, cand = make_repo(ws)
        unit = ws / "hermes-gateway.service"
        unit.write_text("[Service]\n", encoding="utf-8")
        dropins = []
        for name in ("90-recovery-hold.conf", "95-timeout-stop.conf"):
            p = ws / name
            p.write_text("x\n", encoding="utf-8")
            dropins.append(p)
        manifest = ctl.build_manifest(repo=repo, candidate=cand, install=repo, launcher=HARNESS / "hermes-release-002",
                                      unit=unit, dropins=dropins, expected_install_head=base)
        assert set(manifest["engine"]) == set(ctl.PINNED_ENGINE_FILES)
        assert set(ctl.ENGINE_FILES) <= set(manifest["engine"])
        for entry in manifest["engine"].values():
            assert ctl.SHA256_RE.match(entry["blob_sha256"])


# ------------------------------------------------------------------ staged versus pinned
class TestStagedVersusPinnedAudit:
    def test_a_correct_staging_audits_clean(self, ws):
        fx = staged_release(ws)
        proc = audit(fx)
        report = json.loads(proc.stdout)
        assert proc.returncode == 0 and report["ok"], report
        assert report["on_disk"] == sorted(ctl.ENGINE_FILES)
        assert report["pinned"] == sorted(ctl.PINNED_ENGINE_FILES)

    def test_a_missing_pin_is_refused(self, ws):
        """The EXT-12 defect, reproduced: a staged file the manifest never pinned."""
        fx = staged_release(ws)
        payload = json.loads(fx["manifest_path"].read_text(encoding="utf-8"))
        del payload["engine"][INVENTORY_SRC]
        write_manifest(fx, payload)
        proc = audit(fx)
        report = json.loads(proc.stdout)
        assert proc.returncode == 2 and not report["ok"]
        assert "manifest_engine_set_is_the_inventory" in report["failed"]
        assert "no_staged_file_missing_from_manifest" in report["failed"]
        assert report["gates"][3]["detail"] == INVENTORY_SRC
        # ... and the launcher refuses at PLAN instead of proceeding on a partial pin set.
        planned = plan(fx)
        assert planned.returncode == 65
        assert "no_staged_file_missing_from_manifest" in planned.stdout + planned.stderr

    def test_an_unexpected_manifest_entry_is_refused(self, ws):
        fx = staged_release(ws)
        payload = json.loads(fx["manifest_path"].read_text(encoding="utf-8"))
        payload["engine"]["harness/not-part-of-the-release.py"] = {"blob_sha256": "d" * 64}
        write_manifest(fx, payload)
        report = json.loads(audit(fx).stdout)
        assert not report["ok"]
        assert "no_unexpected_manifest_engine_entry" in report["failed"]
        assert "manifest_engine_set_is_the_inventory" in report["failed"]

    def test_an_extra_staged_file_is_refused(self, ws):
        fx = staged_release(ws)
        (fx["engine_dir"] / "harness" / "stowaway.py").write_text("# not pinned\n", encoding="utf-8")
        proc = audit(fx)
        report = json.loads(proc.stdout)
        assert proc.returncode == 2 and not report["ok"]
        assert report["failed"] == ["no_unexpected_staged_file"]

    def test_changed_staged_bytes_are_refused(self, ws):
        fx = staged_release(ws)
        target = fx["engine_dir"] / "harness" / "release002_preflight.py"
        target.write_text(target.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
        report = json.loads(audit(fx).stdout)
        assert not report["ok"]
        assert "staged_bytes_match_pins" in report["failed"]
        assert "harness/release002_preflight.py" in report["gates"][-1]["detail"]
        planned = plan(fx)
        assert planned.returncode == 65
        assert "staged_bytes_match_pins" in planned.stdout + planned.stderr

    def test_a_tampered_control_is_not_trusted_for_its_own_inventory(self, ws):
        """The inventory is read from the staged control, so its bytes are checked first."""
        fx = staged_release(ws)
        ctl_path = fx["engine_dir"] / "harness" / "release002_control.py"
        ctl_path.write_text(ctl_path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        planned = plan(fx)
        assert planned.returncode == 65
        assert "refusing to trust its inventory" in planned.stdout + planned.stderr

    def test_pycache_is_not_mistaken_for_a_release_file(self, ws):
        fx = staged_release(ws)
        cache = fx["engine_dir"] / "harness" / "__pycache__"
        cache.mkdir()
        (cache / "release002_control.cpython-313.pyc").write_bytes(b"\x00")
        assert audit(fx).returncode == 0

    def test_a_corrupted_manifest_is_refused(self, ws):
        fx = staged_release(ws)
        fx["manifest_path"].write_text("{not json\n", encoding="utf-8")
        proc = run([PY3, str(fx["engine_dir"] / "harness" / "release002_control.py"), "engine-audit",
                    "--manifest", str(fx["manifest_path"]), "--engine-dir", str(fx["engine_dir"])])
        assert proc.returncode == 3
        assert "manifest_unreadable" in proc.stdout
        assert plan(fx).returncode == 65


# ------------------------------------------------------------------ real launcher end to end
class TestLauncherIntegration:
    def test_bootstrap_stage_and_plan_get_past_prepare_engine(self, ws):
        fx = staged_release(ws)
        assert len(fx["manifest"]["engine"]) == len(ctl.PINNED_ENGINE_FILES)
        staged = sorted(p.relative_to(fx["engine_dir"]).as_posix()
                        for p in fx["engine_dir"].rglob("*") if p.is_file())
        assert staged == sorted(ctl.ENGINE_FILES)
        assert "engine inventory verified" in fx["stage_out"]

        planned = plan(fx)
        out = planned.stdout + planned.stderr
        # Past prepare_engine(), with a clean staged-versus-pinned audit ...
        assert "engine inventory verified" in out
        assert '"gate": "manifest_engine_set_is_the_inventory"' in out
        assert '"failed": []' in out
        # ... and past manifest verification, so the engine itself is now running.
        assert "manifest verified" in out
        assert "REFUSED" not in out, out
        # It stops on a *legitimate* operational NO_GO — this fixture installation is not the
        # authorised production identity — and not on a release-package defect.
        assert planned.returncode == 2
        assert "NO_GO: install identity" in out

    def test_plan_refuses_a_stale_candidate(self, ws):
        fx = staged_release(ws)
        (fx["repo"] / "gateway" / "seed.py").write_text("VALUE = 3\n", encoding="utf-8")
        git(fx["repo"], "commit", "-qam", "later")
        later = git(fx["repo"], "rev-parse", "HEAD")
        proc = run([fx["launcher"], "plan", "--candidate", later], env=fx["env"], cwd=fx["tmp"])
        out = proc.stdout + proc.stderr
        assert proc.returncode == 65
        # Refused before any engine runs: the manifest only speaks for the candidate it pinned.
        assert f"engine for {later} is not staged" in out or "manifest pins" in out
        assert "manifest verified" not in out

    def test_plan_refuses_a_wrong_candidate_tree(self, ws):
        fx = staged_release(ws)
        payload = json.loads(fx["manifest_path"].read_text(encoding="utf-8"))
        payload["candidate_tree"] = "e" * 40
        write_manifest(fx, payload)
        proc = plan(fx)
        assert proc.returncode == 65
        assert "candidate tree drifted" in proc.stdout + proc.stderr

    def test_plan_refuses_a_candidate_the_installation_cannot_see(self, ws):
        fx = staged_release(ws)
        git(fx["repo"], "commit", "-q", "--allow-empty", "-m", "unfetched")
        unfetched = git(fx["repo"], "rev-parse", "HEAD")
        proc = run([fx["launcher"], "plan", "--candidate", unfetched], env=fx["env"], cwd=fx["tmp"])
        out = proc.stdout + proc.stderr
        assert proc.returncode == 65
        assert f"engine for {unfetched} is not staged" in out or "manifest pins" in out
        assert "manifest verified" not in out

    def test_an_inventory_entry_outside_harness_is_refused(self, ws):
        """The inventory is candidate data: it may not name paths the staging loop would follow."""
        def tamper(repo):
            path = repo / "harness" / "release002_control.py"
            path.write_text(path.read_text(encoding="utf-8").replace(
                '    "harness/restore002_session_inventory.py",',
                '    "harness/restore002_session_inventory.py",\n    "../escape.py",'), encoding="utf-8")

        repo, base, cand = make_repo(ws, tamper=tamper)
        fx = bootstrap_lib(ws, repo, base)
        stage = run([fx["launcher"], "stage", "--candidate", cand], env=fx["env"], cwd=ws)
        assert stage.returncode == 65
        assert "outside harness/" in stage.stdout + stage.stderr
        assert not (fx["lib"] / "escape.py").exists()
