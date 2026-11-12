"""EXT-17B: recovery must refuse while the release that owns the journal is still running.

Observed in the disposable environment (case ``interrupt``): ``apply --detach`` was interrupted with
TERM during the restart — which by design does *not* stop the supervisor — and ``recover`` was then
run immediately. Recovery decided ``complete_verification`` ("candidate is deployed and the service
is active"), journalled ``recovered by completing verification`` and restored admission, all while
the supervisor was still inside its 300 s stop. Five minutes later the supervisor finished, rolled
back, and journalled ``ROLLED_BACK`` over the top of the claimed completion. Two contradictory
terminal outcomes existed for one attempt, and the second one was the wrong one to trust first.

These tests execute the shipped guard block — extracted from ``harness/hermes-release-002``, not
paraphrased — with a stubbed ``systemctl`` and a real process that looks like a running engine.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[2] / "harness" / "hermes-release-002"
START = "guard_no_live_supervisor() {"


def guard_source() -> str:
    """The shipped guard, from its definition to the first column-0 closing brace."""
    text = LAUNCHER.read_text(encoding="utf-8")
    start = text.index(START)
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def run_guard(tmp_path: Path, *, units: str = "", active_state: str = "inactive") -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "bin"
    stub.mkdir(parents=True, exist_ok=True)
    (stub / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  list-units) printf "%s" "{units}" ;;\n'
        f'  show) echo "{active_state}" ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (stub / "systemctl").chmod(0o755)
    script = tmp_path / "guard.sh"
    script.write_text(
        "#!/bin/bash\nset -u\n"
        'die() { echo "REFUSED: $*" >&2; exit 65; }\n'
        + guard_source()
        + "guard_no_live_supervisor\n"
        + 'echo "RECOVERY ALLOWED"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = {"PATH": f"{stub}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(
        ["/bin/bash", str(script)], capture_output=True, text=True, check=False, env=env, timeout=120
    )


def fake_engine() -> subprocess.Popen[bytes]:
    """A process whose argv carries the engine's, so ``pgrep -f`` sees it as one.

    It ends by itself: tests/conftest.py blocks signals to processes outside the test subtree, and
    the fixture does not need to be killed to make its point.
    """
    return subprocess.Popen(
        # "sleep 3; true" and not "sleep 3": bash replaces a lone simple command with it, which
        # would drop the argv this fixture needs.
        ["/bin/bash", "-c", "sleep 3; true", "release002_release.sh", "--apply"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def test_the_guard_is_shipped_and_wired_into_recovery() -> None:
    source = guard_source()
    assert "recovery refused" in source
    assert "list-units" in source
    # The guard is worthless if cmd_recover does not call it: that wiring is the defect.
    assert "require_root; prepare_engine; guard_no_live_supervisor" in LAUNCHER.read_text(
        encoding="utf-8"
    )


def test_recovery_is_allowed_when_no_supervisor_is_alive(tmp_path: Path) -> None:
    result = run_guard(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "RECOVERY ALLOWED" in result.stdout


def test_recovery_is_refused_while_an_engine_is_running(tmp_path: Path) -> None:
    engine = fake_engine()
    try:
        time.sleep(0.2)
        result = run_guard(tmp_path)
    finally:
        engine.wait(timeout=60)
    assert result.returncode == 65, result.stdout + result.stderr
    assert "a release engine is still running" in result.stderr


def test_recovery_is_refused_while_a_supervisor_unit_is_active(tmp_path: Path) -> None:
    result = run_guard(
        tmp_path,
        units="hermes-release-20260101T000000Z-abcdef.service loaded active running RESTORE-002\n",
        active_state="active",
    )
    assert result.returncode == 65, result.stdout + result.stderr
    assert "is still active" in result.stderr


def test_a_finished_supervisor_unit_does_not_block_recovery(tmp_path: Path) -> None:
    """The unit a crashed attempt left behind is not a running release."""
    result = run_guard(
        tmp_path,
        units="hermes-release-20260101T000000Z-abcdef.service loaded inactive dead RESTORE-002\n",
        active_state="inactive",
    )
    assert result.returncode == 0, result.stderr
    assert "RECOVERY ALLOWED" in result.stdout
