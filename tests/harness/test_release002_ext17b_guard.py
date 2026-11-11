"""EXT-17B: the isolation guard refuses everything that is not the private disposable run.

The guard is the only thing standing between the release launcher (which names production paths by
design) and the live installation. So these tests are entirely negative: for each isolation
property, remove it and require a refusal. ``check()`` is pure, which lets every branch run on the
host without a namespace; the final test runs the real CLI and requires exit 64, proving the same
refusal survives the actual entry point the scenario script calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "harness" / "qualify" / "ext17b_guard.py"
sys.path.insert(0, str(GUARD.parent))

import ext17b_guard

TOKEN = "0123456789abcdef0123456789abcdef"
SERVICE = "hermes-gateway.service"
NAMESPACE_ROOT = "         0       1000          1\n"


def make_mounts(tmp_path: Path, *, token: str = TOKEN, skip: str | None = None,
                unit: str | None = None) -> list[str]:
    """Four private mount points, each carrying the per-run sentinel."""
    paths = []
    for name in ("systemd", "lib", "var", "log"):
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        paths.append(str(path))
        if skip == name:
            continue
        (path / ext17b_guard.SENTINEL_NAME).write_text(token + "\n", encoding="utf-8")
    (tmp_path / "systemd" / SERVICE).write_text(
        unit if unit is not None else "[Service]\nEnvironment=HERMES_HOME=/scenario/home\n",
        encoding="utf-8",
    )
    return paths


def run_check(tmp_path: Path, paths: list[str], **overrides: object) -> str | None:
    kwargs: dict[str, object] = {
        "root": tmp_path,
        "token": TOKEN,
        "euid": 0,
        "uid_map": NAMESPACE_ROOT,
        "release_paths": paths,
        "systemctl_path": str(tmp_path / "bin" / "systemctl"),
        "home": "/scenario/home",
    }
    kwargs.update(overrides)
    return ext17b_guard.check(**kwargs)  # type: ignore[arg-type]


def make_shim(root: Path) -> None:
    shim = root / "bin" / "systemctl"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)


def test_complete_isolation_is_accepted(tmp_path: Path) -> None:
    paths = make_mounts(tmp_path)
    make_shim(tmp_path)
    assert run_check(tmp_path, paths) is None


def test_missing_sentinel_is_refused(tmp_path: Path) -> None:
    paths = make_mounts(tmp_path)
    make_shim(tmp_path)
    why = run_check(tmp_path, paths, token="")
    assert why is not None
    assert "EXT17B_SENTINEL is not set" in why


def test_euid_not_namespace_root_is_refused(tmp_path: Path) -> None:
    paths = make_mounts(tmp_path)
    make_shim(tmp_path)
    why = run_check(tmp_path, paths, euid=os.getuid())
    assert why is not None
    assert "not namespace root" in why


def test_host_uid_map_is_refused(tmp_path: Path) -> None:
    """The host's own map (``0 0 4294967295``) must not satisfy the namespace condition."""
    paths = make_mounts(tmp_path)
    make_shim(tmp_path)
    why = run_check(tmp_path, paths, uid_map="         0          0 4294967295\n")
    assert why is not None
    assert "not mapped from a host uid" in why


def test_one_unmounted_release_path_is_refused(tmp_path: Path) -> None:
    for missing in ("systemd", "lib", "var", "log"):
        case = tmp_path / missing
        case.mkdir()
        paths = make_mounts(case, skip=missing)
        make_shim(case)
        why = run_check(case, paths)
        assert why is not None, missing
        assert "not the private mount" in why


def test_stale_sentinel_is_refused(tmp_path: Path) -> None:
    paths = make_mounts(tmp_path, token="deadbeefdeadbeefdeadbeefdeadbeef")
    make_shim(tmp_path)
    why = run_check(tmp_path, paths)
    assert why is not None
    assert "sentinel absent or stale" in why


def test_staging_systemctl_is_refused(tmp_path: Path) -> None:
    paths = make_mounts(tmp_path)
    make_shim(tmp_path)
    why = run_check(tmp_path, paths, systemctl_path="/usr/bin/systemctl")
    assert why is not None
    assert "qualification substitute" in why


def test_unit_naming_production_home_is_refused(tmp_path: Path) -> None:
    paths = make_mounts(tmp_path, unit="[Service]\nEnvironment=HERMES_HOME=/home/hermes/.hermes\n")
    make_shim(tmp_path)
    why = run_check(tmp_path, paths)
    assert why is not None
    assert "HERMES_HOME" in why


def test_host_invocation_exits_64(tmp_path: Path) -> None:
    """The entry point the scenario calls refuses when it is not inside the private namespace."""
    env = {k: v for k, v in os.environ.items() if k != "EXT17B_SENTINEL"}
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--root", str(tmp_path)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 64, proc
    assert "EXT17B GUARD REFUSED" in proc.stderr


@pytest.mark.parametrize("sentinel", [None, "", "not-the-token"])
def test_cli_refuses_without_matching_sentinel(tmp_path: Path, sentinel: str | None) -> None:
    env = {k: v for k, v in os.environ.items() if k != "EXT17B_SENTINEL"}
    if sentinel is not None:
        env["EXT17B_SENTINEL"] = sentinel
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--root", str(tmp_path)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 64
