"""EXT-17B: the preflight's hold-source fallback cannot be reached outside a scenario.

Production reads ``HERMES_RECOVERY_HOLD`` out of ``/proc/<pid>/environ``. Inside the disposable user
namespace that read is refused by Yama (the release process is a sibling, not an ancestor, of the
gateway, and namespace root holds no ``CAP_SYS_PTRACE`` over the initial namespace), so the
qualification gateway publishes the same fact in its own state file. These tests pin the three
conditions that make that fallback safe and prove it degrades to the production behaviour — an
unevaluable gate — when any of them is missing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "harness" / "release002_preflight.py"


@pytest.fixture()
def preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Import the shipped preflight with its module-level paths pointed at a temp home."""
    monkeypatch.setenv("HERMES_RELEASE_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_RELEASE_INSTALL", str(tmp_path / "hermes-agent"))
    monkeypatch.delenv("EXT17B_SENTINEL", raising=False)
    spec = importlib.util.spec_from_file_location("ext17b_preflight_under_test", PREFLIGHT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_state(tmp_path: Path, **fields: object) -> dict[str, object]:
    body = {"substitute": True, "pid": 4242, "recovery_hold": True}
    body.update(fields)
    (tmp_path / "gateway_state.json").write_text(json.dumps(body), encoding="utf-8")
    return body


def test_no_sentinel_means_no_fallback(preflight: object, tmp_path: Path) -> None:
    write_state(tmp_path)
    hold, why = preflight.substitute_hold(4242)  # type: ignore[attr-defined]
    assert hold is None
    assert "no qualification sentinel" in why


def test_sentinel_but_real_state_is_not_consulted(
    preflight: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXT17B_SENTINEL", "tok")
    write_state(tmp_path, substitute=False)
    hold, why = preflight.substitute_hold(4242)  # type: ignore[attr-defined]
    assert hold is None
    assert "not a qualification substitute" in why


def test_pid_must_match(
    preflight: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXT17B_SENTINEL", "tok")
    write_state(tmp_path, pid=1)
    hold, why = preflight.substitute_hold(4242)  # type: ignore[attr-defined]
    assert hold is None
    assert "!=" in why


def test_hold_must_be_published_as_a_boolean(
    preflight: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXT17B_SENTINEL", "tok")
    write_state(tmp_path, recovery_hold="yes")
    hold, why = preflight.substitute_hold(4242)  # type: ignore[attr-defined]
    assert hold is None
    assert "no recovery_hold" in why


def test_matching_substitute_state_is_used(
    preflight: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXT17B_SENTINEL", "tok")
    write_state(tmp_path)
    hold, why = preflight.substitute_hold(4242)  # type: ignore[attr-defined]
    assert hold is True
    assert "qualification gateway state" in why
    write_state(tmp_path, recovery_hold=False)
    assert preflight.substitute_hold(4242)[0] is False  # type: ignore[attr-defined]


def test_unreadable_state_degrades_to_unevaluable(
    preflight: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXT17B_SENTINEL", "tok")
    (tmp_path / "gateway_state.json").write_text("{not json", encoding="utf-8")
    hold, why = preflight.substitute_hold(4242)  # type: ignore[attr-defined]
    assert hold is None
    assert "unreadable" in why
