"""EXT-17B: recovery re-verification must speak a phase the preflight can evaluate.

The engine's ``preflight`` helper maps an unknown phase to its usage exit (64), and
``verify_post_release`` treats any non-zero exit as ``NO_GO``. Recovery used to pass the name of the
mode it was in (``"$PHASE"`` = ``recover``), which is not one of the preflight's phases, so a
recovery of an already-deployed release could never prove anything: it reported an unevaluable
``NO_GO``, kept the freeze and left the same state for the next operator to hit again.

These tests pin the phase set the preflight accepts and the way the engine names the phase it is
proving, because the defect was in the *name*, not in any of the checks.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "harness" / "release002_release.sh"
PREFLIGHT = REPO / "harness" / "release002_preflight.py"


def accepted_phases() -> set[str]:
    """The ``--phase`` choices the shipped preflight actually accepts."""
    body = PREFLIGHT.read_text(encoding="utf-8")
    match = re.search(r'"--phase",\s*choices=\[([^\]]*)\]', body)
    assert match, "the preflight no longer declares its phases where this test can read them"
    return {name for name in re.findall(r'"([a-z]+)"', match.group(1))}


def verification_call_phases() -> list[str]:
    """The first argument of every ``verify_post_release`` call in the engine."""
    body = ENGINE.read_text(encoding="utf-8")
    calls = re.findall(r"verify_post_release\s+(\S+)", body)
    assert calls, "the engine no longer verifies a release after the restart"
    return calls


def test_the_preflight_declares_its_phases() -> None:
    phases = accepted_phases()
    assert {"pre", "infreeze", "revalidate", "post", "released"} <= phases


def test_the_preflight_cannot_evaluate_a_recovery_phase() -> None:
    """``recover`` is a mode, not a phase: asking for it is a usage error, not a verdict."""
    result = subprocess.run(
        [sys.executable, str(PREFLIGHT), "--phase", "recover"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "invalid choice" in (result.stderr + result.stdout)


def test_every_post_release_verification_names_an_evaluable_phase() -> None:
    """A variable here is the bug: the value that reached the preflight was the mode name."""
    phases = accepted_phases()
    for argument in verification_call_phases():
        literal = argument.strip('"')
        assert not argument.startswith("$"), (
            "verify_post_release must be given a phase literal; a variable let 'recover' through"
        )
        assert literal in phases, f"'{literal}' is not a phase the preflight can evaluate"
