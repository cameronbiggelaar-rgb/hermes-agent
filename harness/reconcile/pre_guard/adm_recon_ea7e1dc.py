#!/usr/bin/env python3.13
"""ADM-RECON-01 operator tool: reconcile the three epoch-1 `unknown` admissions.

Scope (fixed, not configurable): the three admissions listed in TARGETS. The tool
performs the same terminal transition as the runtime's own operator acknowledgement
(`hermes_state_runtime.resolve_unknown_session_input`, generation-fenced, outcome
`interrupted`), expressed as one `BEGIN IMMEDIATE` transaction with compare-and-set
predicates and row-count assertions, without opening `SessionDB` (which would run
schema DDL).

Exit codes
  0   change committed (or, with --apply, already in the target state: no-op)
  2   refused in --dry-run: preconditions hold, change not applied
  64  precondition/guard refusal, nothing written
  65  post-write verification failed (write committed; run --verify)
  70  internal error

Read-only by default. `--apply` is the only way to write.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

TARGETS = [
    ("40a8ac83611e428696d5fee98e4bc599", "local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901"),
    ("faf7f3066aca401d8b5c309c5d2f397d", "local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5"),
    ("e7aa5bc9b3204511846ad644a3279063", "local-efa627bea17ff79e9e8267f617e9f10b7eba2a3586e9092420510b8245b44187"),
]
OWNER_EPOCH = 1
GENERATION = 1
OUTCOME = "interrupted"
BUCKET_LABELS = ["epoch", "live", "ahead", "stale_recent", "stale_expired", "undated"]

ADMISSION_SQL_FALLBACK = """
select (select epoch from runtime_epoch where singleton=1),
       (select count(*) from session_admissions
         where status in ('queued','started','unknown')
           and owner_epoch=(select epoch from runtime_epoch where singleton=1)),
       (select count(*) from session_admissions
         where status in ('queued','started','unknown')
           and owner_epoch>(select epoch from runtime_epoch where singleton=1)),
       (select count(*) from session_admissions a
          left join sessions s on s.id=a.target_session_id
         where a.status in ('queued','started','unknown')
           and a.owner_epoch<(select epoch from runtime_epoch where singleton=1)
           and (s.last_activity_at is null
                or (cast(strftime('%s','now') as real)-s.last_activity_at) <= 21600)),
       (select count(*) from session_admissions a
          left join sessions s on s.id=a.target_session_id
         where a.status in ('queued','started','unknown')
           and a.owner_epoch<(select epoch from runtime_epoch where singleton=1)
           and s.last_activity_at is not null
           and (cast(strftime('%s','now') as real)-s.last_activity_at) > 21600),
       (select count(*) from session_admissions
         where status in ('queued','started','unknown') and owner_epoch is null)
"""


class Refusal(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def now_iso():
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect(path, readonly):
    uri = f"file:{path}?mode={'ro' if readonly else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if readonly:
        conn.execute("PRAGMA query_only=1")
    return conn


def schema_fingerprint(conn):
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                        "ORDER BY type, name").fetchall()
    blob = "\n".join(f"{r['type']}|{r['name']}|{r['sql']}" for r in rows)
    return hashlib.sha256(blob.encode()).hexdigest(), len(rows)


def admission_sql(path):
    """Take the qualified trigger's ADMISSION_SQL verbatim from its script."""
    import re
    text = open(path, encoding="utf-8").read()
    m = re.search(r"ADMISSION_SQL='([^']*)'|ADMISSION_SQL=\"([^\"]*)\"", text)
    sql = (m.group(1) or m.group(2)) if m else None
    if not sql or not sql.strip().lower().startswith("select"):
        raise Refusal("admission_sql_unavailable", path)
    return sql.strip(), sha256_file(path)


def bucket_sample(conn, sql):
    row = conn.execute(sql).fetchone()
    return dict(zip(BUCKET_LABELS, [int(v) for v in row]))


def row_dict(conn, sql, params):
    r = conn.execute(sql, params).fetchone()
    return None if r is None else dict(r)


ADMISSION_COLS = ("seq, admission_id, request_id, principal_id, target_session_id, intent, status, "
                  "outcome, owner_epoch, generation, payload_digest")
SESSION_COLS = ("id, source, started_at, ended_at, end_reason, message_count, last_activity_at, "
                "runtime_generation, runtime_revision, parent_session_id, cwd")


def snapshot_target(conn, sql=None, integrity=False, ignore=()):
    """Every fact the guards and the evidence need, for all three targets."""
    snap = {"captured_at": now_iso(), "targets": []}
    for admission_id, session_id in TARGETS:
        adm = row_dict(conn, f"SELECT {ADMISSION_COLS} FROM session_admissions WHERE admission_id=?",
                       (admission_id,))
        sess = row_dict(conn, f"SELECT {SESSION_COLS} FROM sessions WHERE id=?", (session_id,))
        workers = [dict(r) for r in conn.execute(
            "SELECT execution_id, session_id, generation, owner_epoch, status, last_sequence "
            "FROM worker_executions WHERE session_id=? ORDER BY execution_id", (session_id,))]
        messages = conn.execute("SELECT count(*) FROM messages WHERE session_id=? AND active=1",
                                (session_id,)).fetchone()[0]
        leases = conn.execute("SELECT count(*) FROM session_turn_leases WHERE conversation_id=? OR holder LIKE ?",
                              (session_id, f"%{session_id}%")).fetchone()[0]
        receipts = conn.execute("SELECT count(*) FROM worker_receipts WHERE execution_id=?",
                                (f"admission-worker:{admission_id}",)).fetchone()[0]
        results = conn.execute("SELECT count(*) FROM state_meta WHERE key LIKE ?",
                               (f"%{admission_id}%",)).fetchone()[0]
        live = conn.execute("SELECT count(*) FROM session_admissions WHERE target_session_id=? "
                            "AND status IN ('queued','started')", (session_id,)).fetchone()[0]
        snap["targets"].append({
            "admission_id": admission_id, "session_id": session_id, "admission": adm, "session": sess,
            "workers": workers, "active_messages": messages, "leases": leases,
            "worker_receipts": receipts, "captured_results": results, "live_admissions_on_session": live,
        })
    snap["epoch"] = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
    snap["status_histogram"] = {r[0]: r[1] for r in conn.execute(
        "SELECT status, count(*) FROM session_admissions GROUP BY status ORDER BY status")}
    snap["schema_sha256"], snap["schema_objects"] = schema_fingerprint(conn)
    snap["total_rowcounts"] = table_rowcounts(conn, ignore)
    snap["nonterminal_ids"] = sorted(r[0] for r in conn.execute(
        "SELECT admission_id FROM session_admissions WHERE status IN ('queued','started','unknown')"))
    snap["sample"] = bucket_sample(conn, sql) if sql else None
    snap["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0] if integrity else None
    return snap


FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


def is_derived_fts(name):
    """FTS5 shadow tables hold a *derived* copy of the content table.

    Counting them costs minutes on the production store (millions of blob segments) and adds no
    guard value: their content is a pure function of ``messages``, which is counted, and the
    operator tool never writes a message row. They are also the one part of this store known to be
    malformed, so a count there would fail for reasons unrelated to the transition.
    """
    return "_fts" in name and name.endswith(FTS_SHADOW_SUFFIXES)


def table_rowcounts(conn, ignore=()):
    out = {}
    ignore = set(ignore or ())
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                "ORDER BY name").fetchall():
        if name in ignore or is_derived_fts(name):
            continue
        try:
            out[name] = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error as exc:  # pragma: no cover - fixture-only diagnostics
            out[name] = f"error:{exc}"
    return out


def already_terminal(snap):
    return all(t["admission"] and t["admission"]["status"] == "terminal"
               and t["admission"]["outcome"] == OUTCOME for t in snap["targets"])


def guards(snap):
    """Every refusal condition, evaluated on one consistent snapshot."""
    if snap["epoch"] is None:
        raise Refusal("no_runtime_epoch")
    for t in snap["targets"]:
        a, s = t["admission"], t["session"]
        aid = t["admission_id"]
        if a is None:
            raise Refusal("admission_missing", aid)
        if a["status"] == "terminal":
            if a["outcome"] != OUTCOME:
                raise Refusal("already_terminal_other_outcome", f"{aid}:{a['outcome']}")
            continue
        if a["status"] != "unknown":
            raise Refusal("admission_not_unknown", f"{aid}:{a['status']}")
        if a["owner_epoch"] != OWNER_EPOCH or a["generation"] != GENERATION:
            raise Refusal("ownership_mismatch", f"{aid}:epoch={a['owner_epoch']}:gen={a['generation']}")
        if a["intent"] != "queue":
            raise Refusal("intent_not_queue", f"{aid}:{a['intent']}")
        if s is None:
            raise Refusal("session_missing", t["session_id"])
        if s["runtime_generation"] != GENERATION:
            raise Refusal("session_generation_moved", f"{t['session_id']}:{s['runtime_generation']}")
        if s["source"] != "oneshot":
            raise Refusal("session_source_changed", f"{t['session_id']}:{s['source']}")
        if s["message_count"] != 0 or t["active_messages"] != 0:
            raise Refusal("session_has_transcript", f"{t['session_id']}:{s['message_count']}/{t['active_messages']}")
        if s["end_reason"] is not None:
            raise Refusal("session_ended", f"{t['session_id']}:{s['end_reason']}")
        if t["live_admissions_on_session"]:
            raise Refusal("live_admission_on_session", f"{t['session_id']}:{t['live_admissions_on_session']}")
        if t["leases"]:
            raise Refusal("transcript_lease_held", f"{t['session_id']}:{t['leases']}")
        if t["captured_results"]:
            raise Refusal("captured_result_present", f"{aid}:{t['captured_results']}")
        for w in t["workers"]:
            if w["generation"] != GENERATION or w["owner_epoch"] != OWNER_EPOCH:
                raise Refusal("foreign_worker_row", f"{t['session_id']}:{w['execution_id']}")
            if w["status"] != "terminal":
                raise Refusal("worker_still_live", f"{w['execution_id']}:{w['status']}")
        if not t["workers"]:
            raise Refusal("worker_row_missing", t["session_id"])


def apply_change(conn, snap, backup):
    """One BEGIN IMMEDIATE transaction: re-verify in-transaction, then CAS-update."""
    for t in snap["targets"]:
        a = row_dict(conn, "SELECT status, outcome, owner_epoch, generation FROM session_admissions "
                           "WHERE admission_id=?", (t["admission_id"],))
        if a is None:
            raise Refusal("admission_missing_in_txn", t["admission_id"])
        if (a["status"], a["outcome"]) == ("terminal", OUTCOME):
            continue
        if (a["status"], a["owner_epoch"], a["generation"]) != ("unknown", OWNER_EPOCH, GENERATION):
            raise Refusal("cas_precheck_failed", f"{t['admission_id']}:{dict(a)}")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # The epoch can only advance under a new gateway start, which also rolls
        # `started` rows; a change between snapshot and BEGIN means a restart raced us.
        epoch = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
        if epoch != snap["epoch"]:
            raise Refusal("epoch_moved", f"{snap['epoch']}->{epoch}")

        changed = []
        for t in snap["targets"]:
            aid, sid = t["admission_id"], t["session_id"]
            cur = row_dict(conn, "SELECT status, outcome FROM session_admissions WHERE admission_id=?", (aid,))
            if cur is None:
                raise Refusal("admission_missing_in_txn", aid)
            if (cur["status"], cur["outcome"]) == ("terminal", OUTCOME):
                changed.append({"admission_id": aid, "session_id": sid, "result": "already_terminal"})
                continue
            # Faithful to _retire_admission_workers(row.owner_epoch): a no-op here, asserted.
            retired = conn.execute(
                "UPDATE worker_executions SET status='terminal' "
                "WHERE session_id IN (?,?) AND generation=? AND owner_epoch=?",
                (sid, t["session_id"], GENERATION, OWNER_EPOCH)).rowcount
            if retired != len(t["workers"]):
                raise Refusal("worker_retire_mismatch", f"{sid}:{retired}!={len(t['workers'])}")
            n = conn.execute(
                "UPDATE session_admissions SET status='terminal', outcome=? "
                "WHERE admission_id=? AND status='unknown' AND owner_epoch=? AND generation=?",
                (OUTCOME, aid, OWNER_EPOCH, GENERATION)).rowcount
            if n != 1:
                raise Refusal("cas_update_missed", f"{aid}:{n}")
            m = conn.execute("UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?",
                             (sid,)).rowcount
            if m != 1:
                raise Refusal("session_revision_missed", f"{sid}:{m}")
            changed.append({"admission_id": aid, "session_id": sid, "result": "changed"})
        conn.execute("COMMIT")
        return changed
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def verify_post(path, sql_path, integrity=False):
    """Read-only pass over the store, in the trigger's own WAL-aware reading mode."""
    conn = connect(path, readonly=True)
    try:
        sample = bucket_sample(conn, admission_sql(sql_path)[0])
        remaining = [dict(r) for r in conn.execute(
            "SELECT admission_id, status, owner_epoch, generation FROM session_admissions "
            "WHERE status IN ('queued','started','unknown') ORDER BY seq")]
        check = conn.execute("PRAGMA quick_check").fetchone()[0] if integrity else None
        return {"sample": sample, "remaining_nonterminal": remaining, "quick_check": check}
    finally:
        conn.close()


def write_evidence(outdir, name, payload):
    path = os.path.join(outdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str, indent=2, sort_keys=True)
    return path, sha256_file(path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--trigger-script", required=True)
    ap.add_argument("--backup")
    ap.add_argument("--backup-sha256")
    ap.add_argument("--expect-epoch", type=int)
    ap.add_argument("--out", default=".")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--ignore-table", action="append", default=[],
                    help="exclude a measurement-only table (fixture audit tables) from the row-count proof")
    ap.add_argument("--integrity", action="store_true",
                    help="run PRAGMA quick_check before and after (slow on a large store)")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    if args.verify:
        post = verify_post(args.db, args.trigger_script, args.integrity)
        print(json.dumps(post, indent=2))
        return 0 if post["quick_check"] in (None, "ok") else 65

    conn = connect(args.db, readonly=not args.apply)
    try:
        sql = admission_sql(args.trigger_script)[0]
        snap = snapshot_target(conn, sql, args.integrity, args.ignore_table)
        try:
            if args.expect_epoch is not None and snap["epoch"] != args.expect_epoch:
                raise Refusal("epoch_not_expected", f"{snap['epoch']}!={args.expect_epoch}")
            if already_terminal(snap):
                print(json.dumps({"result": "already_terminal", "epoch": snap["epoch"],
                                  "admissions": [a for a, _ in TARGETS]}, indent=2))
                write_evidence(args.out, "already-terminal.json", snap)
                return 0
            if args.apply:
                if not args.backup:
                    raise Refusal("backup_missing", "--apply requires a verified pre-write backup")
                if not os.path.exists(args.backup):
                    raise Refusal("backup_missing", args.backup)
                actual = sha256_file(args.backup)
                if args.backup_sha256 and actual != args.backup_sha256:
                    raise Refusal("backup_digest_mismatch", f"{actual}!={args.backup_sha256}")
                bconn = connect(args.backup, readonly=True)
                try:
                    for admission_id, _ in TARGETS:
                        live = row_dict(conn, "SELECT status, owner_epoch, generation FROM "
                                              "session_admissions WHERE admission_id=?", (admission_id,))
                        bak = row_dict(bconn, "SELECT status, owner_epoch, generation FROM "
                                              "session_admissions WHERE admission_id=?", (admission_id,))
                        if bak is None or dict(bak) != dict(live):
                            raise Refusal("backup_not_current", admission_id)
                    bepoch = bconn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
                    if bepoch != snap["epoch"]:
                        raise Refusal("backup_epoch_mismatch", f"{bepoch}!={snap['epoch']}")
                finally:
                    bconn.close()
            guards(snap)
        except Refusal as exc:
            print(json.dumps({"result": "refused", "reason": exc.reason, "detail": exc.detail}, indent=2))
            write_evidence(args.out, "refusal.json", {"at": now_iso(), "reason": exc.reason,
                                                      "detail": exc.detail, "snapshot": snap})
            return 64
        if not args.apply:
            print(json.dumps({"result": "dry_run_ok", "epoch": snap["epoch"],
                              "would_change": [t["admission_id"] for t in snap["targets"]]}, indent=2))
            write_evidence(args.out, "before.json", snap)
            return 2
        changed = apply_change(conn, snap, args.backup)
    finally:
        conn.close()

    after = connect(args.db, readonly=True)
    try:
        post_snap = snapshot_target(after, sql, args.integrity, args.ignore_table)
    finally:
        after.close()
    post = verify_post(args.db, args.trigger_script, args.integrity)

    evidence = {"result": "applied", "at": now_iso(), "epoch": snap["epoch"], "changed": changed,
                "sample_before": snap["sample"], "sample_after": post["sample"],
                "remaining_nonterminal": post["remaining_nonterminal"],
                "quick_check": post["quick_check"],
                "total_rowcounts_before": snap["total_rowcounts"],
                "total_rowcounts_after": post_snap["total_rowcounts"],
                "status_histogram_before": snap["status_histogram"],
                "status_histogram_after": post_snap["status_histogram"],
                "backup": {"path": args.backup, "sha256": args.backup_sha256 or
                           (sha256_file(args.backup) if args.backup else None)}}
    p0, h0 = write_evidence(args.out, "before.json", snap)
    p1, h1 = write_evidence(args.out, "after.json", post_snap)
    p2, h2 = write_evidence(args.out, "evidence.json", evidence)
    print(json.dumps({"result": "applied", "changed": changed,
                      "evidence": {p0: h0, p1: h1, p2: h2}}, indent=2))

    problems = []
    if args.integrity and post["quick_check"] != snap["quick_check"]:
        problems.append(f"quick_check changed: {snap['quick_check']!r} -> {post['quick_check']!r}")
    if post["sample"]["epoch"] != snap["epoch"]:
        problems.append("epoch moved during write")
    if post["sample"]["stale_recent"] != 0:
        problems.append(f"stale_recent={post['sample']['stale_recent']}")
    if any(r["admission_id"] in {a for a, _ in TARGETS}
           for r in post["remaining_nonterminal"]):
        problems.append("targets still non-terminal")
    pre_ids = set(snap["nonterminal_ids"]) - {a for a, _ in TARGETS}
    post_ids = {r["admission_id"] for r in post["remaining_nonterminal"]}
    if post_ids - pre_ids:
        problems.append(f"new_nonterminal_admission={sorted(post_ids - pre_ids)}")
    if post["sample"]["undated"]:
        problems.append(f"undated={post['sample']['undated']}")
    if post["sample"]["ahead"]:
        problems.append(f"ahead={post['sample']['ahead']}")
    if evidence["total_rowcounts_before"] != evidence["total_rowcounts_after"]:
        problems.append("table row counts changed")
    if problems:
        write_evidence(args.out, "postcheck-failed.json", {"problems": problems, "sample": post["sample"]})
        return 65
    write_evidence(args.out, "postcheck-ok.json", {"sample": post["sample"]})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refusal as exc:
        print(json.dumps({"result": "refused", "reason": exc.reason, "detail": exc.detail}, indent=2))
        sys.exit(64)
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"result": "error", "reason": type(exc).__name__, "detail": str(exc)}, indent=2))
        sys.exit(70)
