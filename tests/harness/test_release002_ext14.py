"""EXT-14: the old-runtime probe must be evaluable off the deployed host, and must prove *provenance*.

The EXT-13 qualification reported ``old_runtime_coverage: unevaluable`` /
``old_runtime_dynamic_gates: interpreter FAIL``. Cause: the dynamic half defaulted its interpreter to
``<install>/venv/bin/python``, so any isolated baseline tree (a git clone — every qualification
workspace) was unevaluable even though the tree itself was fully covered by the static half.

Relaxing that gate is not the fix. These tests pin the two mechanisms that make the probe safe when
it resolves an interpreter elsewhere:

* an explicit, recorded interpreter *chain* (``--python`` → ``HERMES_RELEASE_OLDGATE_PYTHON`` →
  ``<install>/venv/bin/python`` → the probe's own interpreter), where a pinned path that does not
  exist fails closed instead of silently falling through;
* a **provenance** gate — every module the dynamic half exercised must resolve inside the install
  tree, so "covered" can never be claimed for code the probe did not run.

    env -u PYTHONPATH /usr/bin/python3.13 -m pytest tests/harness/test_release002_ext14.py -q
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

import release002_oldgate as oldgate  # noqa: E402

PY3 = "/usr/bin/python3.13"
INTERPRETER_ENV = "HERMES_RELEASE_OLDGATE_PYTHON"
# The subset the probe needs: the producer modules plus the modules the dynamic half imports.
SUBTREES = ("agent", "gateway", "cron", "hermes_cli", "hermes_platform")


def run(cmd, cwd=None, env=None, check=False):
    e = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    e.pop(INTERPRETER_ENV, None)
    if env:
        e.update(env)
    proc = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"{cmd} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


@pytest.fixture(scope="session")
def install_source(tmp_path_factory):
    """A clean candidate tree built from git — no ``venv/``, which is exactly the clone shape."""
    dest = tmp_path_factory.mktemp("ext14-src") / "install"
    dest.mkdir()
    listing = run(["git", "-C", str(ROOT), "ls-tree", "--name-only", "HEAD"], check=True).stdout.split()
    paths = [p for p in listing if p.endswith(".py")] + list(SUBTREES)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "HEAD", *paths],
                             capture_output=True)
    assert archive.returncode == 0, archive.stderr
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, capture_output=True)
    assert tar.returncode == 0, tar.stderr
    assert not (dest / "venv").exists(), "the fixture must look like a clone, not a deployed install"
    assert (dest / "gateway" / "run_inbound.py").exists()
    return dest


@pytest.fixture
def install(install_source, tmp_path):
    dest = tmp_path / "install"
    shutil.copytree(install_source, dest)
    return dest


def probe(install_path, tmp_path, extra=(), env=None):
    out = tmp_path / "oldgate.json"
    proc = run([PY3, str(HARNESS / "release002_oldgate.py"), "--install", str(install_path),
                "--json", str(out), *extra], cwd=tmp_path, env=env)
    return proc, (json.loads(out.read_text()) if out.exists() else None)


def provenance_gate(report):
    gates = {g["gate"]: g for g in report["dynamic"]["gates"]}
    return gates["interpreter_provenance"]


class TestInterpreterResolution:
    def test_probe_is_evaluable_without_an_installed_venv(self, install, tmp_path):
        """A clone has no ``venv/``: the probe must fall back, record that, and still prove coverage."""
        proc, report = probe(install, tmp_path)
        assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
        assert report["verdict"] == "covered", (report["uncovered"], report["unevaluable"],
                                                report["dynamic"]["gates"])
        assert report["dynamic"]["interpreter"]["source"] == "probe-interpreter"
        assert Path(report["dynamic"]["interpreter"]["path"]).resolve() == Path(sys.executable).resolve()
        assert report["dynamic"]["interpreter"]["version"], "the interpreter version must be recorded"

    def test_env_pinned_interpreter_is_used_and_recorded(self, install, tmp_path):
        proc, report = probe(install, tmp_path, env={INTERPRETER_ENV: PY3})
        assert proc.returncode == 0, proc.stdout[-2000:]
        assert report["dynamic"]["interpreter"]["source"] == INTERPRETER_ENV
        assert report["dynamic"]["interpreter"]["path"] == PY3

    def test_installed_venv_is_preferred_when_present(self, install, tmp_path):
        venv_bin = install / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(PY3)
        proc, report = probe(install, tmp_path)
        assert proc.returncode == 0, proc.stdout[-2000:]
        assert report["dynamic"]["interpreter"]["source"] == "installed-venv"

    def test_explicit_argument_beats_every_fallback(self, install):
        dynamic = oldgate.oldgate_dynamic(install, py=PY3, home=None)
        assert dynamic["interpreter"]["source"] == "explicit"
        assert dynamic["interpreter"]["path"] == PY3
        assert dynamic["ok"] is True, dynamic["gates"]

    def test_a_pinned_interpreter_that_does_not_exist_fails_closed(self, install, tmp_path):
        """A silent fallback would hide an operator typo behind a green gate."""
        missing = str(tmp_path / "no-such-python")
        proc, report = probe(install, tmp_path, extra=("--python", missing))
        assert proc.returncode == 3, proc.stdout[-2000:]
        assert report["verdict"] == "unevaluable"
        gate = {g["gate"]: g for g in report["dynamic"]["gates"]}["interpreter"]
        assert gate["ok"] is None
        assert "pinned by" in gate["detail"], gate["detail"]


class TestProvenance:
    def test_provenance_gate_reports_the_install_tree(self, install, tmp_path):
        proc, report = probe(install, tmp_path)
        assert proc.returncode == 0, proc.stdout[-2000:]
        gate = provenance_gate(report)
        assert gate["ok"] is True, gate["detail"]
        assert str(install.resolve()) in gate["detail"]

    def test_code_imported_from_another_tree_is_never_reported_covered(self, install, tmp_path):
        """An editable/site-packages copy elsewhere on sys.path must fail the probe, not pass it."""
        elsewhere = tmp_path / "elsewhere"
        shutil.copytree(install, elsewhere)
        # ``sitecustomize`` runs before any producer module: a meta-path finder pinned to another
        # copy makes every import resolve there, while the static half still reads this tree —
        # exactly the "imports from whichever gateway happens to be installed" hazard.
        (install / "sitecustomize.py").write_text(
            "import sys, importlib.abc, importlib.machinery\n"
            f"ELSE = {str(elsewhere)!r}\n"
            "ROOTS = ('gateway', 'agent', 'hermes_cli')\n"
            "class _Redirect(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname.split('.')[0] not in ROOTS:\n"
            "            return None\n"
            "        return importlib.machinery.PathFinder.find_spec(fullname, path or [ELSE])\n"
            "sys.meta_path.insert(0, _Redirect())\n", encoding="utf-8")
        proc, report = probe(install, tmp_path)
        assert proc.returncode == 2, (proc.returncode, proc.stdout[-2000:])
        assert report["verdict"] == "uncovered", report["verdict"]
        gate = provenance_gate(report)
        assert gate["ok"] is False, gate["detail"]
        assert str(elsewhere) in gate["detail"], gate["detail"]


class TestNegativeControls:
    def test_removed_admission_gate_is_never_reported_covered(self, install, tmp_path):
        """Deliberately delete a producer's gate: the probe must say uncovered, not covered."""
        target = install / "gateway" / "wake.py"
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        start = next(i for i, ln in enumerate(lines)
                     if ln.lstrip().startswith(("def admit_internal_event",
                                                "async def admit_internal_event")))
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith(("def ", "class ", "@"))), len(lines))
        hits = [i for i in range(start, end) if "_runner_draining" in lines[i]]
        assert hits, "fixture drift: the drain gate is no longer in admit_internal_event"
        for i in hits:
            # Rename the call site rather than blanking the line: the module must still parse, so the
            # probe has to report "not referenced" instead of hiding behind a syntax error.
            lines[i] = lines[i].replace("_runner_draining", "_renamed_gate")
        target.write_text("".join(lines), encoding="utf-8")

        proc, report = probe(install, tmp_path, extra=("--static-only",))
        assert proc.returncode == 2, (proc.returncode, proc.stdout[-2000:])
        assert report["verdict"] == "uncovered"
        assert "internal_wake/drain_marker" in report["uncovered"], report["uncovered"]

    def test_a_renamed_gate_definition_is_not_assumed_covered(self, install, tmp_path):
        target = install / "agent" / "estop.py"
        text = target.read_text(encoding="utf-8")
        assert "def paused_reply" in text
        target.write_text(text.replace("def paused_reply", "def _renamed_paused_reply"),
                          encoding="utf-8")
        proc, report = probe(install, tmp_path, extra=("--static-only",))
        assert proc.returncode == 2, (proc.returncode, proc.stdout[-2000:])
        assert report["verdict"] == "uncovered"
        assert any("not defined" in p["detail"] for p in report["producers"] if p["ok"] is False)


def test_preflight_reports_which_interpreter_answered_the_dynamic_half():
    """The gate detail must carry provenance: an operator cannot re-run a check whose interpreter is
    unknown, and "unevaluable" is only actionable once the probe says what it tried."""
    text = (HARNESS / "release002_preflight.py").read_text(encoding="utf-8")
    assert "interpreter={interp.get('source')}:{interp.get('path')}" in text
    assert "old_runtime_dynamic_gates" in text


class TestDependencies:
    def test_an_interpreter_without_the_runtime_dependencies_is_unevaluable(self, install, tmp_path):
        """``-S`` drops site-packages, so ``gateway.drain_control`` cannot import its deps.

        That is not "uncovered": no admission gate was ever exercised, and claiming failure here
        would send an operator hunting for a missing gate that is in fact present.
        """
        shim = tmp_path / "bare-python"
        shim.write_text(f"#!/bin/sh\nexec {PY3} -S \"$@\"\n", encoding="utf-8")
        shim.chmod(0o755)
        dynamic = oldgate.oldgate_dynamic(install, py=str(shim))
        assert dynamic["evaluable"] is False, dynamic["gates"]
        gate = {g["gate"]: g for g in dynamic["gates"]}["interpreter_provenance"]
        assert gate["ok"] is None, gate
        assert "No module named" in gate["detail"], gate["detail"]
        assert "interpreter_provenance" not in [g["gate"] for g in dynamic["gates"] if g["ok"] is True]
