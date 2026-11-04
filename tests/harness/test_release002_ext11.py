"""EXT-RESTORE-002-11: a missing journaled unit checksum must never skip exact-byte verification.

The finding: the independent ``--verify-only`` path scraped the regenerated unit's SHA-256 out of
the release journal's free-text notes. When the journal was missing, lacked a ``unit`` phase, or
carried a truncated note, the extraction yielded an empty string, ``verify_manifest`` treated that
as "no expectation" and skipped the exact-byte gate -- leaving only the ``TimeoutStopSec`` value
check, which any number of different unit files satisfy.

The correction has three layers, and each is tested here against isolated temporary repositories,
manifests, units and journals:

1. ``verify_unit_receipt`` -- the only permitted way to obtain the checksum; refuses unless a real
   receipt is bound to this candidate, this release attempt and this unit path;
2. ``verify_manifest`` -- the exact-byte gate is unconditional in ``post``/``released``; an absent
   or malformed checksum is a failed gate, never a skipped one;
3. the launcher and the engine -- neither can reach a manifest check without a valid receipt, and a
   missing manifest is a NO_GO rather than a skipped verification.

    env -u PYTHONPATH /usr/bin/python3.13 -m pytest tests/harness/test_release002_ext11.py -q
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

PY3 = "/usr/bin/python3.13"
CAND = "a" * 40
OTHER = "b" * 40
RUN = "20261104T010203Z"
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


def ctl_cli(*args, env=None, check_ok=False):
    proc = run([PY3, str(HARNESS / "release002_control.py"), *args], env=env)
    if check_ok and proc.returncode != 0:
        raise AssertionError(f"{args} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


# --------------------------------------------------------------------- journal fixtures
def journal_paths(tmp_path, *, cand=CAND, run_id=RUN, sha="c" * 64, deploy_head=None,
                  unit_after_deploy=True, unit_before_only=False, unit_path=None):
    """Write a journal with real receipts using the real ``journal`` CLI."""
    jp = tmp_path / "journal.json"
    ctl_cli("journal", "--path", str(jp), "--phase", "start", "--note", "mode=apply",
            "--candidate", cand, check_ok=True)
    if unit_before_only:
        # a unit receipt that predates the deploy proves nothing about the deployed bytes
        up = ["--unit-path", str(unit_path)] if unit_path else []
        ctl_cli("journal", "--path", str(jp), "--phase", "unit", "--note", "stale",
                "--candidate", cand, "--sha256", "d" * 64, *up, check_ok=True)
    ctl_cli("journal", "--path", str(jp), "--phase", "deploy", "--note", "deployed",
            "--candidate", cand, "--head", deploy_head or cand, check_ok=True)
    if unit_after_deploy:
        up = ["--unit-path", str(unit_path)] if unit_path else []
        ctl_cli("journal", "--path", str(jp), "--phase", "unit", "--note", "regenerated",
                "--candidate", cand, "--sha256", sha, *up, check_ok=True)
    data = json.loads(jp.read_text())
    data["run_id"] = run_id
    data.setdefault("release", "RESTORE-002")
    jp.write_text(json.dumps(data, indent=2, sort_keys=True))
    return jp


# --------------------------------------------------------------------------- layer 1
class TestUnitReceiptRecovery:
    def _rec(self, tmp_path, **kw):
        jp = journal_paths(tmp_path, **kw)
        return ctl.read_journal(jp)

    def test_valid_receipt_passes(self, tmp_path):
        j = self._rec(tmp_path, sha="e" * 64)
        rep = ctl.verify_unit_receipt(j, candidate=CAND, run_id=RUN)
        assert rep["ok"] is True, rep["failed"]
        assert rep["sha256"] == "e" * 64
        assert rep["run_id"] == RUN

    def test_missing_journal_refuses(self):
        rep = ctl.verify_unit_receipt(None, candidate=CAND)
        assert rep["ok"] is False
        assert "journal" in rep["failed"]

    def test_missing_unit_receipt_refuses(self, tmp_path):
        j = self._rec(tmp_path, unit_after_deploy=False)
        rep = ctl.verify_unit_receipt(j, candidate=CAND)
        assert rep["ok"] is False
        assert "unit_receipt" in rep["failed"]

    def test_unit_receipt_that_predates_the_deploy_is_not_accepted(self, tmp_path):
        j = self._rec(tmp_path, unit_before_only=True, unit_after_deploy=False)
        rep = ctl.verify_unit_receipt(j, candidate=CAND)
        assert rep["ok"] is False, rep
        assert "unit_receipt" in rep["failed"]

    @pytest.mark.parametrize("bad", ["", "0" * 63, "0" * 65, "Z" * 64,
                                     "sha256:" + "a" * 64, "a" * 63 + "-", None])
    def test_empty_or_malformed_checksum_refuses(self, tmp_path, bad):
        j = self._rec(tmp_path)
        if bad is None:
            for rec in j["phases"]:
                rec.pop("sha256", None)
        else:
            j["phases"][-1]["sha256"] = bad
        rep = ctl.verify_unit_receipt(j, candidate=CAND)
        assert rep["ok"] is False, bad
        assert "unit_checksum" in rep["failed"], rep["failed"]

    def test_wrong_candidate_refuses(self, tmp_path):
        j = self._rec(tmp_path)
        rep = ctl.verify_unit_receipt(j, candidate=OTHER)
        assert rep["ok"] is False
        assert "journal_candidate" in rep["failed"]

    def test_receipt_from_another_candidate_refuses(self, tmp_path):
        j = self._rec(tmp_path)
        j["phases"][-1]["candidate"] = OTHER
        rep = ctl.verify_unit_receipt(j, candidate=CAND)
        assert rep["ok"] is False
        assert "unit_receipt" in rep["failed"]

    def test_stale_journal_without_a_matching_deploy_refuses(self, tmp_path):
        j = self._rec(tmp_path, deploy_head=OTHER)
        rep = ctl.verify_unit_receipt(j, candidate=CAND)
        assert rep["ok"] is False
        assert "deploy_receipt" in rep["failed"]

    def test_wrong_run_id_refuses(self, tmp_path):
        j = self._rec(tmp_path)
        rep = ctl.verify_unit_receipt(j, candidate=CAND, run_id="19990101T000000Z")
        assert rep["ok"] is False
        assert "run_id" in rep["failed"]

    def test_journal_without_a_run_id_refuses(self, tmp_path):
        j = self._rec(tmp_path)
        j.pop("run_id", None)
        rep = ctl.verify_unit_receipt(j, candidate=CAND)
        assert rep["ok"] is False
        assert "journal_run_id" in rep["failed"]

    def test_wrong_unit_path_refuses(self, tmp_path):
        j = self._rec(tmp_path)
        for rec in j["phases"]:
            if rec["phase"] == "unit":
                rec["unit_path"] = "/etc/systemd/system/other.service"
        rep = ctl.verify_unit_receipt(j, candidate=CAND, unit_path="/etc/systemd/system/hermes-gateway.service")
        assert rep["ok"] is False
        assert "unit_path" in rep["failed"]


# --------------------------------------------------------------------------- layer 2
class TestUnitReceiptCLI:
    def test_cli_prints_only_the_checksum(self, tmp_path):
        jp = journal_paths(tmp_path, sha="f" * 64)
        proc = ctl_cli("unit-receipt", "--journal", str(jp), "--candidate", CAND,
                       "--run-id", RUN, "--print", "sha256")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "f" * 64
        assert json.loads(proc.stderr)["sha256"] == "f" * 64

    def test_cli_refuses_a_missing_journal(self, tmp_path):
        proc = ctl_cli("unit-receipt", "--journal", str(tmp_path / "absent.json"),
                       "--candidate", CAND, "--print", "sha256")
        assert proc.returncode == 2
        assert proc.stdout.strip() == ""
        assert "journal" in json.loads(proc.stderr)["failed"]

    def test_cli_refuses_a_corrupt_journal(self, tmp_path):
        jp = tmp_path / "journal.json"
        jp.write_text("{not json")
        proc = ctl_cli("unit-receipt", "--journal", str(jp), "--candidate", CAND, "--print", "sha256")
        assert proc.returncode == 2
        assert proc.stdout.strip() == ""

    def test_cli_rejects_an_unknown_print_field(self, tmp_path):
        """A typo must not degrade into "printed nothing, so nothing to compare"."""
        jp = journal_paths(tmp_path)
        proc = ctl_cli("unit-receipt", "--journal", str(jp), "--candidate", CAND, "--print", "sha")
        assert proc.returncode != 0
        assert proc.stdout.strip() == ""

    def test_journal_records_the_engine_attempt_id(self, tmp_path):
        jp = tmp_path / "j.json"
        ctl_cli("journal", "--path", str(jp), "--phase", "unit", "--note", "regenerated",
                "--candidate", CAND, "--sha256", "a" * 64, "--run-id", RUN, check_ok=True)
        data = json.loads(jp.read_text())
        assert data["run_id"] == RUN
        assert data["candidate"] == CAND
        assert data["phases"][-1]["sha256"] == "a" * 64

    def test_cli_refuses_a_wrong_run_id(self, tmp_path):
        jp = journal_paths(tmp_path)
        proc = ctl_cli("unit-receipt", "--journal", str(jp), "--candidate", CAND,
                       "--run-id", "19990101T000000Z", "--print", "sha256")
        assert proc.returncode == 2
        assert proc.stdout.strip() == ""
        assert "run_id" in json.loads(proc.stderr)["failed"]

    def test_cli_refuses_an_unusable_checksum(self, tmp_path):
        jp = journal_paths(tmp_path)
        data = json.loads(jp.read_text())
        data["phases"][-1]["sha256"] = "short"
        jp.write_text(json.dumps(data))
        proc = ctl_cli("unit-receipt", "--journal", str(jp), "--candidate", CAND, "--print", "sha256")
        assert proc.returncode == 2
        assert "unit_checksum" in json.loads(proc.stderr)["failed"]


# --------------------------------------------------------------------------- layer 3
def make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
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


def candidate_commit(repo: Path) -> str:
    (repo / "gateway" / "seed.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "candidate")
    return git(repo, "rev-parse", "HEAD")


def release_fixture(tmp_path, *, deploy=True):
    """A deployed release: install at the candidate, unit regenerated, drop-ins, manifest, journal."""
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    cand = candidate_commit(repo)
    inst = tmp_path / "install"
    run(["git", "clone", "-q", str(repo), str(inst)], check=True)
    run(["git", "-C", str(inst), "checkout", "-q", cand if deploy else base], check=True)

    launcher = tmp_path / "lib" / "hermes-release-002"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HARNESS / "hermes-release-002", launcher)

    unit = tmp_path / "etc" / "hermes-gateway.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(UNIT_V2, encoding="utf-8")
    dd = Path(str(unit) + ".d")
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "90-recovery-hold.conf").write_text(HOLD_DROPIN_TEXT, encoding="utf-8")
    (dd / "95-timeout-stop.conf").write_text(TIMEOUT_DROPIN_TEXT, encoding="utf-8")
    sha = ctl.sha256_file(unit)

    manifest = tmp_path / "manifest.json"
    ctl_cli("manifest", "--write", "--path", str(manifest), "--repo", str(repo),
            "--install", str(inst), "--launcher", str(launcher), "--candidate", cand,
            "--expected-install-head", base, "--unit", str(unit), check_ok=True)

    journal = journal_paths(tmp_path, cand=cand, sha=sha, unit_path=unit)
    return dict(repo=repo, base=base, cand=cand, inst=inst, launcher=launcher, unit=unit,
                sha=sha, manifest=manifest, journal=journal)


class TestReleasedManifestExactBytes:
    def _verify(self, fx, expected=None):
        args = ["manifest", "--verify", "--phase", "released", "--path", str(fx["manifest"]),
                "--repo", str(fx["inst"]), "--install", str(fx["inst"]),
                "--launcher", str(fx["launcher"]), "--candidate", fx["cand"]]
        if expected is not None:
            args += ["--expected-post-unit-sha256", expected]
        return ctl_cli(*args)

    def test_released_passes_with_the_journaled_checksum(self, tmp_path):
        fx = release_fixture(tmp_path)
        proc = self._verify(fx, fx["sha"])
        body = json.loads(proc.stdout)
        assert proc.returncode == 0, body["failed"]
        assert {"unit_timeout_released", "unit_sha256_released"} <= {g["gate"] for g in body["gates"]}

    def test_released_refuses_a_matching_timeout_with_different_bytes(self, tmp_path):
        """The heart of EXT-11: same TimeoutStopSec=300, different unit file, stale checksum."""
        fx = release_fixture(tmp_path)
        fx["unit"].write_text(UNIT_V2 + "# harmless-looking extra line\n", encoding="utf-8")
        proc = self._verify(fx, fx["sha"])          # checksum of the *original* bytes
        body = json.loads(proc.stdout)
        assert proc.returncode == 2
        assert "unit_sha256_released" in body["failed"], body["failed"]
        assert "unit_timeout_released" not in body["failed"]   # the value check alone would pass

    @pytest.mark.parametrize("omitted", [None, "", "deadbeef"])
    def test_released_refuses_without_a_usable_checksum(self, tmp_path, omitted):
        fx = release_fixture(tmp_path)
        proc = self._verify(fx, omitted)
        body = json.loads(proc.stdout)
        assert proc.returncode == 2
        assert "unit_sha256_released" in body["failed"], body["failed"]


# --------------------------------------------------------------------------- layer 4
class TestEngineVerifyOnly:
    """The composed path: the real engine's ``--verify-only`` must refuse without a receipt."""

    def _engine(self, tmp_path, fx):
        eng = tmp_path / "engine"
        shutil.copytree(HARNESS, eng, ignore=shutil.ignore_patterns("__pycache__"))
        # preflight is not what EXT-11 tests: isolate it so the receipt/manifest wiring is visible
        (eng / "release002_preflight.py").write_text(
            "import json,sys\njson.dump({'ok': True, 'failed': [], 'gates': []}, sys.stdout)\nsys.exit(0)\n",
            encoding="utf-8")
        venv = fx["inst"] / "venv" / "bin"
        venv.mkdir(parents=True, exist_ok=True)
        # the install must stay clean: a real deployment has venv/ ignored, so do the same locally
        exclude = fx["inst"] / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "venv/\n", encoding="utf-8")
        py = venv / "python"
        py.write_text(f'#!/bin/sh\nexec {PY3} "$@"\n', encoding="utf-8")
        py.chmod(0o755)

        shim = tmp_path / "bin"
        shim.mkdir()
        log = tmp_path / "systemctl.log"
        (shim / "systemctl").write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> {log}\n'
            'case "$*" in\n'
            '  *"-p Environment"*) echo "HERMES_RECOVERY_HOLD=1"; exit 0 ;;\n'
            'esac\n'
            'exit 0\n', encoding="utf-8")
        (shim / "systemctl").chmod(0o755)
        env = dict(HERMES_RELEASE_INSTALL=str(fx["inst"]), HERMES_HOME=str(fx["inst"] / "home"),
                   HERMES_RELEASE_HOME=str(fx["inst"] / "home"),
                   HERMES_RELEASE_JOURNAL=str(fx["journal"]),
                   HERMES_RELEASE_MANIFEST=str(fx["manifest"]),
                   HERMES_RELEASE_LAUNCHER=str(fx["launcher"]),
                   HERMES_RELEASE_QUALIFY="1", HERMES_RELEASE_QUALIFY_UNIT=str(fx["unit"]),
                   EXPECT_HEAD=fx["cand"], EXPECT_TREE=git(fx["inst"], "rev-parse", "HEAD^{tree}"),
                   PATH=f"{shim}:{os.environ['PATH']}")
        return eng, env, log

    def _verify_only(self, tmp_path, fx, eng=None, env=None, archive=None):
        eng, env0, log = self._engine(tmp_path, fx)
        env0 = dict(env0, **(env or {}))
        proc = run(["bash", str(eng / "release002_release.sh"), "--verify-only",
                    "--candidate", fx["cand"], "--archive", str(archive or tmp_path / "archive")],
                   env=env0)
        return proc, log

    def test_verify_only_passes_on_a_complete_release(self, tmp_path):
        fx = release_fixture(tmp_path)
        proc, _ = self._verify_only(tmp_path, fx)
        assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
        assert "journaled unit receipt" in proc.stdout

    def test_verify_only_rejects_a_wrong_attempt_id(self, tmp_path):
        fx = release_fixture(tmp_path)
        proc, _ = self._verify_only(tmp_path, fx, env={"HERMES_RELEASE_RUN_ID": "19990101T000000Z"})
        assert proc.returncode == 2
        assert "no valid regenerated-unit receipt" in proc.stderr + proc.stdout

    def test_verify_only_accepts_the_matching_attempt_id(self, tmp_path):
        fx = release_fixture(tmp_path)
        proc, _ = self._verify_only(tmp_path, fx, env={"HERMES_RELEASE_RUN_ID": RUN})
        assert proc.returncode == 0, proc.stdout[-2500:] + proc.stderr[-1500:]

    def test_verify_only_refuses_a_missing_journal(self, tmp_path):
        fx = release_fixture(tmp_path)
        fx["journal"].unlink()
        proc, _ = self._verify_only(tmp_path, fx)
        assert proc.returncode == 2, proc.stdout[-3000:]
        assert "no valid regenerated-unit receipt" in proc.stderr + proc.stdout

    def test_verify_only_refuses_a_journal_without_a_unit_receipt(self, tmp_path):
        fx = release_fixture(tmp_path)
        data = json.loads(fx["journal"].read_text())
        data["phases"] = [p for p in data["phases"] if p["phase"] != "unit"]
        fx["journal"].write_text(json.dumps(data))
        proc, _ = self._verify_only(tmp_path, fx)
        assert proc.returncode == 2
        assert "no valid regenerated-unit receipt" in proc.stderr + proc.stdout

    def test_verify_only_refuses_without_a_manifest(self, tmp_path):
        """The old code skipped the whole manifest check when the file was absent."""
        fx = release_fixture(tmp_path)
        fx["manifest"].unlink()
        proc, _ = self._verify_only(tmp_path, fx)
        assert proc.returncode == 2, proc.stdout[-3000:]
        assert "no release manifest" in proc.stderr + proc.stdout

    def test_verify_only_refuses_when_the_hold_is_absent(self, tmp_path):
        fx = release_fixture(tmp_path)
        eng, env, _ = self._engine(tmp_path, fx)
        shim = tmp_path / "bin" / "systemctl"
        shim.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "systemctl.log"}\nexit 0\n',
                        encoding="utf-8")
        proc = run(["bash", str(eng / "release002_release.sh"), "--verify-only",
                    "--candidate", fx["cand"], "--archive", str(tmp_path / "archive")], env=env)
        assert proc.returncode == 2
        assert "hold env entries: 0" in proc.stdout

    def test_verify_only_still_checks_identity_and_bytes(self, tmp_path):
        fx = release_fixture(tmp_path)
        fx["unit"].write_text(UNIT_V2 + "# drift\n", encoding="utf-8")   # bytes changed after receipt
        proc, _ = self._verify_only(tmp_path, fx)
        assert proc.returncode == 2
        assert "unit_sha256_released" in proc.stdout + proc.stderr

    def test_qualify_unit_hook_requires_the_flag(self, tmp_path):
        fx = release_fixture(tmp_path)
        eng, env, _ = self._engine(tmp_path, fx)
        env = dict(env)
        env.pop("HERMES_RELEASE_QUALIFY")
        proc = run(["bash", str(eng / "release002_release.sh"), "--verify-only",
                    "--candidate", fx["cand"]], env=env)
        assert proc.returncode == 64
        assert "HERMES_RELEASE_QUALIFY_UNIT" in proc.stderr


# --------------------------------------------------------------------------- no-bypass structure
class TestNoBypassPath:
    """Structural guards so the skip cannot be reintroduced by a later edit."""

    def test_engine_no_longer_scrapes_the_checksum_from_notes(self):
        src = (HARNESS / "release002_release.sh").read_text(encoding="utf-8")
        assert 'startswith("sha256=")' not in src
        assert 'unit-receipt --journal "$JOURNAL"' in src

    def test_engine_refuses_post_and_released_without_a_checksum(self):
        src = (HARNESS / "release002_release.sh").read_text(encoding="utf-8")
        assert "post|released)" in src
        assert "refusing to skip exact-byte proof" in src

    def test_every_manifest_verification_names_its_phase(self):
        """An omitted --phase defaults to pre, which is how EXT-10 hid; keep every site explicit."""
        for name in ("release002_release.sh", "hermes-release-002"):
            src = (HARNESS / name).read_text(encoding="utf-8")
            at = src.find("manifest --verify")
            seen = 0
            while at != -1:
                window = src[at:at + 240]
                assert "--phase" in window, f"{name}: invocation without --phase near {at}: {window[:120]}"
                seen += 1
                at = src.find("manifest --verify", at + 1)
            assert seen, name

    def test_launcher_cmd_verify_requires_a_receipt(self):
        src = (HARNESS / "hermes-release-002").read_text(encoding="utf-8")
        body = src.split("cmd_verify()", 1)[1].split("\ncmd_", 1)[0]
        assert "unit-receipt" in body
        assert "--expected-post-unit-sha256" in body
        assert body.index("unit-receipt") < body.index("--expected-post-unit-sha256")
