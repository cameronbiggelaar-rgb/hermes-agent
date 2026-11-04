"""EXT-RESTORE-002-04 controls: a release freeze must stop *every* producer, not just Telegram.

The uncovered seam was turn admission from non-messaging producers (API server, A2A, local
automation, worker reservations), which all funnel through ``session_ingress.execute_admission``.
These controls prove three things behaviourally:

* a frozen admission is answered with a settled failed turn and claims **nothing** — no busy tag,
  no viewer note, no session lookup, no turn;
* the gate is the first statement of ``execute_admission`` (so no later side effect can precede it);
* an unfrozen admission still proceeds into the normal path, so the gate cannot pass vacuously.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import drain_control as dc
from gateway import session_ingress

ROOT = Path(__file__).resolve().parents[2]
INGRESS = ROOT / "gateway" / "session_ingress.py"


def _runner(*, draining: bool):
    return SimpleNamespace(_external_drain_active=draining)


def _authority(*, draining: bool, sessions=None):
    # Deliberately incomplete: any access past the freeze gate would blow up, which is exactly how
    # these tests prove that nothing downstream ran.
    authority = SimpleNamespace(runner=_runner(draining=draining))
    if sessions is not None:
        authority.sessions = sessions
    return authority


@pytest.fixture()
def busy_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(session_ingress, "busy_turn_started", lambda admission_id: calls.append(admission_id))
    return calls


def test_frozen_admission_is_refused_before_any_work(busy_calls):
    result = asyncio.run(session_ingress.execute_admission(
        _authority(draining=True), SimpleNamespace(), {"admission_id": "adm-frozen"}))
    assert result["failed"] is True
    assert result["completed"] is False
    assert "release" in result["final_response"].lower()
    assert busy_calls == []


def test_unfrozen_admission_proceeds_past_the_gate(busy_calls):
    with pytest.raises(Exception):
        asyncio.run(session_ingress.execute_admission(
            _authority(draining=False), SimpleNamespace(), {"admission_id": "adm-open"}))
    assert busy_calls == ["adm-open"]  # reached the busy tag, so the gate did not swallow the turn


def test_a_burst_of_admissions_cannot_race_a_freeze(busy_calls):
    frozen = _authority(draining=True)

    async def burst():
        return await asyncio.gather(*[
            session_ingress.execute_admission(frozen, SimpleNamespace(), {"admission_id": f"adm-{i}"})
            for i in range(20)
        ])

    results = asyncio.run(burst())
    assert len(results) == 20
    assert all(r["failed"] is True for r in results)
    assert busy_calls == []


def test_marker_file_alone_is_enough_even_without_a_drain_watcher():
    assert dc.write_drain_request(principal="release-freeze:test", suppress_notification=True)
    try:
        assert dc.freeze_requested() is True
        assert dc.admission_frozen(None) is True  # no runner, no in-memory flag: the file decides
    finally:
        dc.clear_drain_request()
    assert dc.admission_frozen(None) is False


def test_an_operator_drain_refuses_turns_but_is_not_a_release_freeze():
    assert dc.write_drain_request(principal="drain-control")
    try:
        assert dc.admission_frozen(None) is True
        assert dc.freeze_requested() is False
    finally:
        dc.clear_drain_request()


def test_expired_marker_does_not_freeze_forever():
    assert dc.write_drain_request(principal="release-freeze:orphan")
    path = dc.drain_request_path()
    body = dc.read_drain_request()
    assert body is not None
    body["requested_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(__import__("json").dumps(body), encoding="utf-8")
    try:
        assert dc.read_drain_request() is not None
        assert dc.admission_frozen(None) is False
    finally:
        dc.clear_drain_request()


def test_the_freeze_gate_is_the_first_statement_of_execute_admission():
    tree = ast.parse(INGRESS.read_text(encoding="utf-8"))
    func = next(node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "execute_admission")
    first, second = func.body[0], func.body[1]
    assert isinstance(first, ast.ImportFrom) and first.module == "gateway.drain_control"
    assert "admission_frozen" in {alias.name for alias in first.names}
    assert isinstance(second, ast.If)
    assert isinstance(second.test, ast.Call) and second.test.func.id == "admission_frozen"
    assert isinstance(second.body[0], ast.Return)
