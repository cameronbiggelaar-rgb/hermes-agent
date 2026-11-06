"""Bounded, idempotent *planning* for interrupted-session recovery (RESTORE-002).

No side effects: opens the session database read-only, reads the session index, prints a plan. It
never resumes, marks, ends or deletes anything.

Why a planner instead of "resume the backlog": after the 2026-11-03 cutover the gateway reported 498
open session rows, which reads like 498 interrupted turns. It is not. Resumption is gated by
``SessionEntry.resume_pending`` + ``resume_reason in _AUTO_RESUME_REASONS``, and by a freshness window
(``_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT`` = 1h) — an open row with no marker resumes nothing, and a
marker older than the window is a dead letter. The open rows are mostly rows that were never closed
by pre-cutover CLI/TUI paths, not work that was lost.

So the planner is evidence-first:
  * classify every session as completed / active / resumable / obsolete / needs_human;
  * refuse to plan at all while ``HERMES_RECOVERY_HOLD`` is set (hold removal is not permission);
  * batch only sessions the owner names explicitly, never the whole bucket;
  * cap batch size, total, and stop early on a memory floor;
  * skip anything already resumed, drifted from its planned identity, or past its marker window.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# Mirrors gateway/run.py; kept as data here so the plan does not import the runner graph.
AUTO_RESUME_REASONS = frozenset({"restart_timeout", "shutdown_timeout", "restart_interrupted"})
FRESHNESS_WINDOW_S = 3600.0
IDLE_ACTIVE_GRACE_S = 6 * 3600.0

COMPLETED, ACTIVE, RESUMABLE, OBSOLETE, HUMAN = (
    "completed", "active", "resumable", "obsolete", "needs_human")


@dataclass(frozen=True)
class SessionFact:
    session_id: str
    session_key: str = ""
    source: str = ""
    started_at: float | None = None
    last_activity_at: float | None = None
    message_count: int = 0
    ended_at: float | None = None
    routing_session_id: str | None = None
    resume_pending: bool = False
    resume_reason: str | None = None
    suspended: bool = False
    active_turn: bool = False
    in_index: bool = False

    @property
    def last_ts(self) -> float | None:
        return self.last_activity_at or self.started_at


def fresh_enough(fact: SessionFact, *, now: float, window_s: float = FRESHNESS_WINDOW_S) -> bool:
    """Same rule as ``_is_fresh_gateway_interruption``: unknown timestamps count as fresh."""
    if window_s <= 0:
        return True
    ts = fact.last_ts
    return True if ts is None else (now - ts) <= window_s


def classify(facts: Iterable[SessionFact], *, now: float, window_s: float = FRESHNESS_WINDOW_S,
             idle_grace_s: float = IDLE_ACTIVE_GRACE_S) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {COMPLETED: [], ACTIVE: [], RESUMABLE: [], OBSOLETE: [], HUMAN: []}
    for f in facts:
        if f.ended_at is not None:
            out[COMPLETED].append(f.session_id)
        elif f.routing_session_id is not None and f.routing_session_id != f.session_id:
            out[HUMAN].append(f.session_id)
        elif f.active_turn:
            out[ACTIVE].append(f.session_id)
        elif f.resume_pending and f.resume_reason in AUTO_RESUME_REASONS and not f.suspended:
            out[RESUMABLE if fresh_enough(f, now=now, window_s=window_s) else OBSOLETE].append(f.session_id)
        elif (f.last_ts or 0.0) >= now - idle_grace_s:
            out[ACTIVE].append(f.session_id)
        else:
            out[OBSOLETE].append(f.session_id)
    return out


@dataclass
class RecoveryPlan:
    generated_at: float
    hold_active: bool
    buckets: dict[str, list[str]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    batches: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    refused: str | None = None
    limits: dict[str, float | int] = field(default_factory=dict)
    memory: dict[str, float | int | bool | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_recovery(
    facts: Sequence[SessionFact],
    *,
    requested_ids: Sequence[str] = (),
    already_resumed: Iterable[str] = (),
    hold_active: bool = True,
    mem_available_bytes: int | None = None,
    mem_floor_bytes: int = 1_536 * 1024 * 1024,
    per_session_mem_bytes: int = 64 * 1024 * 1024,
    batch_size: int = 5,
    max_total: int = 25,
    window_s: float = FRESHNESS_WINDOW_S,
    now: float | None = None,
) -> RecoveryPlan:
    """Plan at most ``max_total`` resumptions, ``batch_size`` at a time; never over the memory floor.

    Refusals (no batches at all): the recovery hold is active, or memory is already below the floor.
    Skips (reported, never batched): unrequested, wrong bucket, identity drift, already resumed,
    marker too old, or a limit reached.
    """
    now = time.time() if now is None else now
    facts = list(facts)
    by_id = {f.session_id: f for f in facts}
    buckets = classify(facts, now=now, window_s=window_s)
    plan = RecoveryPlan(
        generated_at=now, hold_active=hold_active, buckets=buckets,
        counts={k: len(v) for k, v in buckets.items()},
        limits={"batch_size": batch_size, "max_total": max_total, "mem_floor_bytes": mem_floor_bytes,
                "per_session_mem_bytes": per_session_mem_bytes, "freshness_window_s": window_s},
    )
    mem = mem_available_bytes if mem_available_bytes is not None else _mem_available()
    plan.memory = {"available_bytes": mem, "floor_bytes": mem_floor_bytes,
                   "below_floor": mem is not None and mem < mem_floor_bytes}
    if hold_active:
        plan.refused = "recovery_hold_active"
        return plan
    if mem is not None and mem < mem_floor_bytes:
        plan.refused = "memory_below_floor"
        return plan

    resumable = set(buckets[RESUMABLE])
    done = set(already_resumed)
    budget = mem
    batch: list[str] = []
    for sid in dict.fromkeys(requested_ids):
        if (len(plan.batches) * batch_size) + len(batch) >= max_total:
            plan.skipped.append({"session_id": sid, "reason": "max_total_reached"})
            continue
        fact = by_id.get(sid)
        if fact is None:
            plan.skipped.append({"session_id": sid, "reason": "unknown_session"})
            continue
        if sid in done:
            plan.skipped.append({"session_id": sid, "reason": "already_resumed"})
            continue
        if sid not in resumable:
            if sid in buckets[HUMAN]:
                reason = HUMAN
            elif (fact.resume_pending and not fact.suspended
                  and fact.resume_reason in AUTO_RESUME_REASONS
                  and not fresh_enough(fact, now=now, window_s=window_s)):
                reason = "marker_expired"
            else:
                reason = "not_resumable"
            plan.skipped.append({"session_id": sid, "reason": reason})
            continue
        if budget is not None:
            budget -= per_session_mem_bytes
            if budget < mem_floor_bytes:
                plan.refused = "memory_pressure_stop"
                plan.skipped.append({"session_id": sid, "reason": "memory_pressure_stop"})
                break
        batch.append(sid)
        if len(batch) == batch_size:
            plan.batches.append({"index": len(plan.batches), "session_ids": batch})
            batch = []
    if batch:
        plan.batches.append({"index": len(plan.batches), "session_ids": batch})
    plan.memory["projected_available_bytes"] = budget
    return plan


def _mem_available() -> int | None:
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_facts(db_path: str, sessions_json: str | None = None) -> list[SessionFact]:
    """Read-only join of session rows, routing entries and the session index."""
    index: dict[str, dict[str, Any]] = {}
    if sessions_json and os.path.exists(sessions_json):
        try:
            with open(sessions_json, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            index = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
        except (OSError, ValueError):
            index = {}
    routing: dict[str, dict[str, Any]] = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        for key, entry_json in con.execute("SELECT session_key, entry_json FROM gateway_routing"):
            try:
                routing[key] = json.loads(entry_json) or {}
            except (TypeError, ValueError):
                routing[key] = {}
        facts: list[SessionFact] = []
        for sid, key, source, started, last, msgs, ended in con.execute(
            "SELECT id, session_key, source, started_at, last_activity_at, message_count, ended_at "
            "FROM sessions"
        ):
            key = key or ""
            entry = routing.get(key) or index.get(key) or {}
            facts.append(SessionFact(
                session_id=str(sid), session_key=key, source=source or "",
                started_at=_num(started), last_activity_at=_num(last),
                message_count=int(msgs or 0), ended_at=_num(ended),
                routing_session_id=(entry.get("session_id") if entry else None),
                resume_pending=bool(entry.get("resume_pending")),
                resume_reason=entry.get("resume_reason"),
                suspended=bool(entry.get("suspended")),
                active_turn=bool(entry.get("active_turn_started_at") or entry.get("active_turn_token")),
                in_index=key in index,
            ))
        return facts
    finally:
        con.close()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan (never perform) interrupted-session resumption.")
    ap.add_argument("--db", default=os.path.expanduser("~/.hermes/state.db"))
    ap.add_argument("--sessions-json", default=os.path.expanduser("~/.hermes/sessions/sessions.json"))
    ap.add_argument("--request", action="append", default=[], metavar="SESSION_ID")
    ap.add_argument("--already-resumed", action="append", default=[])
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--max-total", type=int, default=25)
    ap.add_argument("--assume-hold-released", action="store_true",
                    help="plan as if HERMES_RECOVERY_HOLD were cleared (still performs nothing)")
    args = ap.parse_args(argv)
    # Fail closed: the CLI cannot prove the hold is released, so it assumes it is on. The env var can
    # only ever confirm the hold, never clear it — clearing it is the owner's deliberate act.
    plan = plan_recovery(
        load_facts(args.db, args.sessions_json), requested_ids=args.request,
        already_resumed=args.already_resumed, hold_active=not args.assume_hold_released,
        batch_size=args.batch_size, max_total=args.max_total,
    )
    data = plan.to_dict()
    print(json.dumps(data, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
