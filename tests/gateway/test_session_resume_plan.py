"""RESTORE-002: the resumption planner must be explicit, bounded, idempotent and fail-closed."""
from __future__ import annotations

import json
import sqlite3

import pytest

from gateway.session_resume_plan import (
    ACTIVE, COMPLETED, HUMAN, OBSOLETE, RESUMABLE, SessionFact,
    classify, load_facts, plan_recovery,
)

NOW = 1_800_000_000.0
HOUR = 3600.0
GiB = 1024 ** 3


def fact(sid: str, *, ended=None, routing="same", pending=False, reason="restart_timeout",
         age_s=60.0, active=False, suspended=False, msgs=2) -> SessionFact:
    return SessionFact(
        session_id=sid, session_key=f"agent:main:telegram:dm:{sid}", started_at=NOW - age_s,
        last_activity_at=NOW - age_s, message_count=msgs, ended_at=ended,
        routing_session_id=None if routing is None else (sid if routing == "same" else "other"),
        resume_pending=pending, resume_reason=reason, suspended=suspended, active_turn=active)


def test_classify_separates_evidence_not_intent():
    facts = [
        fact("done", ended=NOW - 10),
        fact("pending", pending=True),
        fact("stale", pending=True, age_s=48 * HOUR),
        fact("nomarker_old", routing=None, age_s=40 * 24 * HOUR, msgs=0),
        fact("nomarker_recent", age_s=HOUR),
        fact("lease", pending=True, active=True),
        fact("mismatch", routing="other", pending=True),
    ]
    b = classify(facts, now=NOW)
    assert b[COMPLETED] == ["done"]
    assert b[RESUMABLE] == ["pending"]
    assert set(b[OBSOLETE]) == {"stale", "nomarker_old"}
    assert b[ACTIVE] == ["nomarker_recent", "lease"]
    assert b[HUMAN] == ["mismatch"]


def test_hold_blocks_every_batch():
    plan = plan_recovery([fact("pending", pending=True)], requested_ids=["pending"],
                         hold_active=True, mem_available_bytes=8 * GiB, now=NOW)
    assert plan.refused == "recovery_hold_active"
    assert plan.batches == []


def test_explicit_request_required_never_the_whole_bucket():
    facts = [fact(f"s{i}", pending=True) for i in range(10)]
    plan = plan_recovery(facts, requested_ids=[], hold_active=False, mem_available_bytes=8 * GiB, now=NOW)
    assert plan.batches == []
    assert plan.counts[RESUMABLE] == 10


def test_batches_are_capped_and_totals_bounded():
    facts = [fact(f"s{i}", pending=True) for i in range(30)]
    plan = plan_recovery(facts, requested_ids=[f"s{i}" for i in range(30)], hold_active=False,
                         mem_available_bytes=64 * GiB, batch_size=5, max_total=12, now=NOW)
    assert [len(b["session_ids"]) for b in plan.batches] == [5, 5, 2]
    assert sum(1 for s in plan.skipped if s["reason"] == "max_total_reached") == 18


def test_memory_pressure_stops_before_the_floor():
    facts = [fact(f"s{i}", pending=True) for i in range(5)]
    floor = 2 * GiB
    plan = plan_recovery(facts, requested_ids=[f"s{i}" for i in range(5)], hold_active=False,
                         mem_available_bytes=floor + 130 * 1024 * 1024, mem_floor_bytes=floor,
                         per_session_mem_bytes=64 * 1024 * 1024, batch_size=5, max_total=10, now=NOW)
    assert plan.refused == "memory_pressure_stop"
    assert sum(len(b["session_ids"]) for b in plan.batches) == 2  # 128 MiB of headroom, 3rd would cross


def test_already_below_floor_refuses_outright():
    plan = plan_recovery([fact("pending", pending=True)], requested_ids=["pending"], hold_active=False,
                         mem_available_bytes=GiB // 2, mem_floor_bytes=GiB, now=NOW)
    assert plan.refused == "memory_below_floor"
    assert plan.batches == []


def test_idempotent_replan_skips_what_already_ran():
    facts = [fact("a", pending=True), fact("b", pending=True)]
    first = plan_recovery(facts, requested_ids=["a", "b"], hold_active=False,
                          mem_available_bytes=8 * GiB, now=NOW)
    assert [b["session_ids"] for b in first.batches] == [["a", "b"]]
    second = plan_recovery(facts, requested_ids=["a", "b"], already_resumed=["a"], hold_active=False,
                           mem_available_bytes=8 * GiB, now=NOW)
    assert [b["session_ids"] for b in second.batches] == [["b"]]
    assert second.skipped == [{"session_id": "a", "reason": "already_resumed"}]


def test_expired_marker_and_human_mismatch_are_skipped_with_reasons():
    facts = [fact("stale", pending=True, age_s=48 * HOUR), fact("mismatch", routing="other", pending=True)]
    plan = plan_recovery(facts, requested_ids=["stale", "mismatch"], hold_active=False,
                         mem_available_bytes=8 * GiB, now=NOW)
    assert plan.batches == []
    assert {s["session_id"]: s["reason"] for s in plan.skipped} == {
        "stale": "marker_expired", "mismatch": HUMAN}


def test_non_resume_reason_is_not_resumable():
    plan = plan_recovery([fact("x", pending=True, reason="agent_close")], requested_ids=["x"],
                         hold_active=False, mem_available_bytes=8 * GiB, now=NOW)
    assert plan.batches == []
    assert plan.counts[RESUMABLE] == 0


def test_freshness_window_zero_disables_the_gate():
    plan = plan_recovery([fact("old", pending=True, age_s=48 * HOUR)], requested_ids=["old"],
                         hold_active=False, mem_available_bytes=8 * GiB, window_s=0, now=NOW)
    assert [b["session_ids"] for b in plan.batches] == [["old"]]


@pytest.fixture()
def store(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT, source TEXT, started_at TEXT,
                               last_activity_at TEXT, message_count INTEGER, ended_at TEXT);
        CREATE TABLE gateway_routing (session_key TEXT PRIMARY KEY, entry_json TEXT, updated_at TEXT);
    """)
    con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", [
        ("s-fresh", "agent:main:telegram:dm:a", "telegram", str(NOW - 600), str(NOW - 600), 4, None),
        ("s-done", "agent:main:telegram:dm:b", "telegram", str(NOW - 900), str(NOW - 900), 4, str(NOW - 800)),
        ("s-old", "agent:main:cli:dm:c", "cli", str(NOW - 30 * 24 * HOUR), None, 0, None),
    ])
    con.executemany("INSERT INTO gateway_routing VALUES (?,?,?)", [
        ("agent:main:telegram:dm:a", json.dumps({"session_id": "s-fresh", "resume_pending": True,
                                                 "resume_reason": "restart_timeout"}), str(NOW)),
        ("agent:main:cli:dm:c", json.dumps({"session_id": "s-old", "resume_pending": False}), str(NOW)),
    ])
    con.commit()
    con.close()
    return str(db)


def test_load_facts_joins_rows_routing_and_index(store, tmp_path):
    index = tmp_path / "sessions.json"
    index.write_text(json.dumps({"agent:main:cli:dm:c": {"session_id": "s-old", "resume_pending": False}}))
    facts = {f.session_id: f for f in load_facts(store, str(index))}
    assert set(facts) == {"s-fresh", "s-done", "s-old"}
    assert facts["s-fresh"].resume_pending and facts["s-fresh"].resume_reason == "restart_timeout"
    assert facts["s-fresh"].in_index is False and facts["s-old"].in_index is True
    assert facts["s-done"].ended_at is not None

    plan = plan_recovery(list(facts.values()), requested_ids=["s-fresh", "s-old", "s-done"],
                         hold_active=False, mem_available_bytes=8 * GiB, now=NOW)
    assert [b["session_ids"] for b in plan.batches] == [["s-fresh"]]
    assert {s["session_id"]: s["reason"] for s in plan.skipped} == {
        "s-old": "not_resumable", "s-done": "not_resumable"}
