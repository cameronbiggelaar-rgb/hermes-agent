"""EXT-16A: the APPLY mutual-exclusion guard must not match its own helper processes.

Defect (EXT-16A, D-1): ``harness/release002_release.sh`` refused every APPLY with

    refusing to mutate: another release engine is already running (pids: <n>)

because it looked for another engine with

    pgrep -f 'release002_release\\.sh --apply' | grep -v -e "^$$" -e "^$PPID$"

and the command-substitution subshell of that very pipeline inherits the engine's argv, so it
matches the pattern with a pid that is neither ``$$`` nor ``$PPID``. The guard therefore always
fired, on any host, before the lock and before the freeze: the corrected quiesce-first sequence
could reach PLAN but never APPLY.

The fix keeps a match only when the matched process is **older** than the engine, which no forked
helper can be; the authoritative exclusion stays the ``flock`` on the release lock immediately
afterwards.

These tests execute the shipped guard bytes extracted from the engine, not a paraphrase of them.

    env -u PYTHONPATH /usr/bin/python3 -m pytest tests/harness/test_release002_ext16a.py -q
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "harness" / "release002_release.sh"

GUARD_START = "OTHER_ENGINES=$(pgrep -f 'release002_release\\.sh --apply'"
GUARD_END = '  echo "refusing to mutate: another release engine is already running'


def guard_source() -> str:
    """The shipped guard block, from the pgrep through the refusal branch."""
    text = ENGINE.read_text()
    start = text.index(GUARD_START)
    end = text.index('exec 9>"$LOCK"')
    return text[start:end]


def run_guard(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "release002_release.sh"
    script.write_text(
        "#!/bin/bash\nset -u\n"
        + guard_source()
        + 'printf "FOREIGN=[%s]\\n" "$OTHER_ENGINES"\n'
    )
    script.chmod(0o755)
    return subprocess.run(
        ["/bin/bash", str(script), "--apply"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
    )


def test_guard_text_is_shipped() -> None:
    src = guard_source()
    assert "_SELF_START" in src, "the guard lost its age comparison"
    assert "/proc/$$/stat" in src


def test_guard_ignores_its_own_pgrep_child(tmp_path: Path) -> None:
    """The regression: a lone engine must see no foreign engine."""
    res = run_guard(tmp_path)
    assert res.returncode == 0, res.stderr
    assert "FOREIGN=[]" in res.stdout, res.stdout + res.stderr


def test_guard_still_detects_an_older_engine(tmp_path: Path) -> None:
    """A genuinely older engine with the same argv must still be reported."""
    foreign = tmp_path / "foreign" / "release002_release.sh"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/bin/bash\nsleep 4\n")
    foreign.chmod(0o755)
    proc = subprocess.Popen(["/bin/bash", str(foreign), "--apply"])
    try:
        time.sleep(0.5)  # make the foreign engine strictly older than the guard
        res = run_guard(tmp_path / "guard")
        assert res.returncode == 65, res.stdout + res.stderr
        assert f"pids: {proc.pid}" in res.stderr, res.stdout + res.stderr
    finally:
        proc.wait(timeout=60)


@pytest.mark.parametrize("needle", ["exec 9>\"$LOCK\"", "flock -n 9"])
def test_lock_remains_the_authoritative_exclusion(needle: str) -> None:
    text = ENGINE.read_text()
    guard_at = text.index(GUARD_START)
    assert text.index(needle) > guard_at, "the advisory pgrep check must precede the lock, not replace it"
