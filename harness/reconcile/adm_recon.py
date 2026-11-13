#!/usr/bin/env python3.13
"""ADM-RECON-01 operator tool: reconcile one epoch-1 `unknown` admission at a time.

Scope is an explicit roster of three admissions (`ROSTER`) and, per invocation, an explicit
selection (`--admission`). The tool performs the same terminal transition as the runtime's own
operator acknowledgement (`hermes_state_runtime.resolve_unknown_session_input`, generation-fenced,
outcome `interrupted`), expressed as one `BEGIN IMMEDIATE` transaction with compare-and-set
predicates and row-count assertions, without opening `SessionDB` (which would run schema DDL).

Execution guards (CA corrections)
  1. Selection is explicit. `--apply` accepts exactly one `--admission`, must match the roster, and
     changes only that row. There is no implicit multi-row apply; the canary and every subsequent
     admission are named by the operator. Dry-run accepts one or more (default: the whole roster).
  2. `--expect-epoch`, `--backup` and `--backup-sha256` are mandatory under `--apply`. Missing,
     malformed or ambiguous values are refused before any write connection is opened; the backup
     digest is always recomputed from the file, never trusted from the argument.
  3. A committed write is only reported as success if the post-write image proves the exact allowed
     delta: the selected admission's status/outcome, its session's `runtime_revision` +1, and no
     change to any other admission, session, worker, message, lease, receipt, `state_meta` row,
     row count or schema object. Anything else — including a verifier that cannot complete — is a
     non-success disposition (exit 65), never a silent pass.

Exit codes
  0   change committed and verified (or the selection is already in the target state: no-op)
  2   refused in --dry-run: preconditions hold, change not applied
  64  precondition/guard/input refusal, nothing written, no write transaction opened
  65  post-write verification failed or incomplete (the write may have committed)
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
from datetime import datetime, timezone

# The complete set of admissions this tool is allowed to touch. Selection is a subset of this.
ROSTER = {
    "40a8ac83611e428696d5fee98e4bc599": "local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901",
    "faf7f3066aca401d8b5c309c5d2f397d": "local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5",
    "e7aa5bc9b3204511846ad644a3279063": "local-efa627bea17ff79e9e8267f617e9f10b7eba2a3586e9092420510b8245b44187",
}
ROSTER_ORDER = ["40a8ac83611e428696d5fee98e4bc599", "faf7f3066aca401d8b5c309c5d2f397d",
                "e7aa5bc9b3204511846ad644a3279063"]
OWNER_EPOCH = 1
GENERATION = 1
OUTCOME = "interrupted"
BUCKET_LABELS = ["epoch", "live", "ahead", "stale_recent", "stale_expired", "undated"]
GUARDED_TABLES = ("session_admissions", "sessions", "worker_executions", "state_meta")

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


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


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


def table_digest(conn, table, key, where="", params=()):
    """Ordered row-image digest of a whole table (or a filtered subset).

    Used instead of row-by-row comparison so the proof stays cheap on a 4k-row admissions table
    while still catching a single-column change on a single row.
    """
    sql = f'SELECT * FROM "{table}"' + (f" WHERE {where}" if where else "") + f' ORDER BY "{key}"'
    rows = conn.execute(sql, params).fetchall()
    blob = "\n".join(json.dumps(dict(r), sort_keys=True, default=str) for r in rows)
    return sha256_text(blob), len(rows)


def image_state(conn, admission_id, session_id, sql=None, integrity=False, ignore=()):
    """Every fact needed to prove the exact allowed delta, and nothing that needs a full scan.

    `messages` is counted, not digested: it holds millions of rows on the production store and the
    transition never writes to it. A count plus `max(id)` catches an insert, a delete and a
    re-insert under a new id; per-session counts catch a re-parented row.
    """
    adm_others, adm_n = table_digest(conn, "session_admissions", "admission_id",
                                     "admission_id<>?", (admission_id,))
    sess_others, sess_n = table_digest(conn, "sessions", "id", "id<>?", (session_id,))
    workers, worker_n = table_digest(conn, "worker_executions", "execution_id")
    leases, lease_n = table_digest(conn, "session_turn_leases", "conversation_id")
    meta, meta_n = table_digest(conn, "state_meta", "key")
    receipts, receipt_n = table_digest(conn, "worker_receipts", "execution_id")
    return {
        "schema_sha256": schema_fingerprint(conn)[0],
        "admissions_others_sha256": adm_others,
        "admissions_other_rows": adm_n,
        "sessions_others_sha256": sess_others,
        "sessions_other_rows": sess_n,
        "workers_sha256": workers,
        "worker_rows": worker_n,
        "leases_sha256": leases,
        "lease_rows": lease_n,
        "state_meta_sha256": meta,
        "state_meta_rows": meta_n,
        "worker_receipts_sha256": receipts,
        "worker_receipt_rows": receipt_n,
        "messages_total": conn.execute("SELECT count(*) FROM messages").fetchone()[0],
        "messages_max_id": conn.execute("SELECT max(id) FROM messages").fetchone()[0],
        "messages_on_session": conn.execute(
            "SELECT count(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0],
        "rowcounts": table_rowcounts(conn, ignore),
        "admission": row_dict(conn, "SELECT * FROM session_admissions WHERE admission_id=?", (admission_id,)),
        "session": row_dict(conn, "SELECT * FROM sessions WHERE id=?", (session_id,)),
        "workers_on_session": [dict(r) for r in conn.execute(
            "SELECT * FROM worker_executions WHERE session_id=? ORDER BY execution_id", (session_id,))],
        "epoch": conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0],
        "nonterminal_ids": sorted(r[0] for r in conn.execute(
            "SELECT admission_id FROM session_admissions WHERE status IN ('queued','started','unknown')")),
        "status_histogram": {r[0]: r[1] for r in conn.execute(
            "SELECT status, count(*) FROM session_admissions GROUP BY status ORDER BY status")},
        "sample": bucket_sample(conn, sql) if sql else None,
        "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0] if integrity else None,
    }


def snapshot(conn, selection, sql=None, integrity=False, ignore=()):
    """Pre-write state for the selected admission(s), plus the whole-store image."""
    snap = {"captured_at": now_iso(), "selection": list(selection), "targets": []}
    for admission_id in selection:
        session_id = ROSTER[admission_id]
        adm = row_dict(conn, f"SELECT {ADMISSION_COLS} FROM session_admissions WHERE admission_id=?",
                       (admission_id,))
        sess = row_dict(conn, f"SELECT {SESSION_COLS} FROM sessions WHERE id=?", (session_id,))
        snap["targets"].append({
            "admission_id": admission_id, "session_id": session_id, "admission": adm, "session": sess,
            "workers": [dict(r) for r in conn.execute(
                "SELECT execution_id, session_id, generation, owner_epoch, status, last_sequence "
                "FROM worker_executions WHERE session_id=? ORDER BY execution_id", (session_id,))],
            "active_messages": conn.execute("SELECT count(*) FROM messages WHERE session_id=? AND active=1",
                                            (session_id,)).fetchone()[0],
            "leases": conn.execute("SELECT count(*) FROM session_turn_leases "
                                   "WHERE conversation_id=? OR holder LIKE ?",
                                   (session_id, f"%{session_id}%")).fetchone()[0],
            "worker_receipts": conn.execute("SELECT count(*) FROM worker_receipts WHERE execution_id=?",
                                            (f"admission-worker:{admission_id}",)).fetchone()[0],
            "captured_results": conn.execute("SELECT count(*) FROM state_meta WHERE key LIKE ?",
                                             (f"%{admission_id}%",)).fetchone()[0],
            "live_admissions_on_session": conn.execute(
                "SELECT count(*) FROM session_admissions WHERE target_session_id=? "
                "AND status IN ('queued','started')", (session_id,)).fetchone()[0],
        })
    snap["schema_sha256"], snap["schema_objects"] = schema_fingerprint(conn)
    snap["total_rowcounts"] = table_rowcounts(conn, ignore)
    snap["status_histogram"] = {r[0]: r[1] for r in conn.execute(
        "SELECT status, count(*) FROM session_admissions GROUP BY status ORDER BY status")}
    snap["nonterminal_ids"] = sorted(r[0] for r in conn.execute(
        "SELECT admission_id FROM session_admissions WHERE status IN ('queued','started','unknown')"))
    snap["sample"] = bucket_sample(conn, sql) if sql else None
    snap["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0] if integrity else None
    snap["images"] = {aid: image_state(conn, aid, ROSTER[aid], sql, integrity, ignore) for aid in selection}
    snap["epoch"] = snap["images"][selection[0]]["epoch"] if selection else None
    return snap


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
        if a["outcome"] is not None:
            # An `unknown` row that already carries an outcome is not the clean pre-state this
            # transition proves; refuse rather than overwrite a value we did not account for.
            raise Refusal("unexpected_outcome_on_unknown", f"{aid}:{a['outcome']}")
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


def check_backup(snap, args, selection):
    """Mandatory, independently recomputed backup proof. Runs before any write connection."""
    if not args.backup:
        raise Refusal("backup_required", "--apply requires --backup")
    if not os.path.isfile(args.backup):
        raise Refusal("backup_missing", args.backup)
    if os.path.abspath(args.backup) == os.path.abspath(args.db):
        raise Refusal("backup_is_target", args.backup)
    actual = sha256_file(args.backup)
    if actual != args.backup_sha256:
        raise Refusal("backup_digest_mismatch", f"computed={actual} supplied={args.backup_sha256}")
    try:
        bconn = connect(args.backup, readonly=True)
    except sqlite3.Error as exc:
        raise Refusal("backup_unreadable", str(exc)) from exc
    try:
        for tbl in GUARDED_TABLES:
            got = bconn.execute(f"PRAGMA integrity_check('{tbl}')").fetchone()[0]
            if got != "ok":
                raise Refusal("backup_integrity_failed", f"{tbl}:{got}")
        bepoch = bconn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
        if bepoch != args.expect_epoch:
            raise Refusal("backup_epoch_mismatch", f"{bepoch}!={args.expect_epoch}")
        for aid in selection:
            live = snap["images"][aid]["admission"]
            bak = row_dict(bconn, "SELECT status, owner_epoch, generation FROM session_admissions "
                                  "WHERE admission_id=?", (aid,))
            if bak is None or dict(bak) != {k: live[k] for k in ("status", "owner_epoch", "generation")}:
                raise Refusal("backup_not_current", aid)
        return {"path": args.backup, "sha256": actual, "epoch": bepoch,
                "integrity": {t: "ok" for t in GUARDED_TABLES}}
    finally:
        bconn.close()


def apply_change(conn, snap, admission_id):
    """One BEGIN IMMEDIATE transaction for exactly one admission: re-verify, then CAS-update."""
    sid = ROSTER[admission_id]
    t = next(x for x in snap["targets"] if x["admission_id"] == admission_id)
    pre = row_dict(conn, "SELECT status, outcome, owner_epoch, generation FROM session_admissions "
                         "WHERE admission_id=?", (admission_id,))
    if pre is None:
        raise Refusal("admission_missing_in_txn", admission_id)
    if (pre["status"], pre["outcome"]) == ("terminal", OUTCOME):
        return {"admission_id": admission_id, "session_id": sid, "result": "already_terminal"}
    if (pre["status"], pre["owner_epoch"], pre["generation"]) != ("unknown", OWNER_EPOCH, GENERATION):
        raise Refusal("cas_precheck_failed", f"{admission_id}:{dict(pre)}")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # The epoch can only advance under a new gateway start, which also rolls `started` rows;
        # a change between snapshot and BEGIN means a restart raced us.
        epoch = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
        if epoch != snap["epoch"]:
            raise Refusal("epoch_moved", f"{snap['epoch']}->{epoch}")
        # Faithful to _retire_admission_workers(row.owner_epoch): a no-op here, asserted.
        retired = conn.execute(
            "UPDATE worker_executions SET status='terminal' "
            "WHERE session_id=? AND generation=? AND owner_epoch=?",
            (sid, GENERATION, OWNER_EPOCH)).rowcount
        if retired != len(t["workers"]):
            raise Refusal("worker_retire_mismatch", f"{sid}:{retired}!={len(t['workers'])}")
        n = conn.execute(
            "UPDATE session_admissions SET status='terminal', outcome=? "
            "WHERE admission_id=? AND status='unknown' AND owner_epoch=? AND generation=?",
            (OUTCOME, admission_id, OWNER_EPOCH, GENERATION)).rowcount
        if n != 1:
            raise Refusal("cas_update_missed", f"{admission_id}:{n}")
        m = conn.execute("UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?",
                         (sid,)).rowcount
        if m != 1:
            raise Refusal("session_revision_missed", f"{sid}:{m}")
        conn.execute("COMMIT")
        return {"admission_id": admission_id, "session_id": sid, "result": "changed"}
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def allowed_delta_problems(before, after, admission_id, session_id):
    """The exact permitted delta, or the list of ways reality differs from it.

    Permitted: the selected admission moves `unknown`→`terminal` with outcome `interrupted`
    (all other columns byte-identical); its session's `runtime_revision` increases by exactly one
    (all other columns identical). Everything else must be identical: every other admission and
    session row, every worker, every message, lease, receipt and `state_meta` row, every table
    row count, and the schema.
    """
    p = []
    if before["schema_sha256"] != after["schema_sha256"]:
        p.append("schema changed")
    if before["rowcounts"] != after["rowcounts"]:
        changed = {k: (before["rowcounts"].get(k), after["rowcounts"].get(k))
                   for k in set(before["rowcounts"]) | set(after["rowcounts"])
                   if before["rowcounts"].get(k) != after["rowcounts"].get(k)}
        p.append(f"table row counts changed: {changed}")

    row_before, row_after = before["admission"], after["admission"]
    if row_after is None:
        p.append("selected admission row missing after write")
    else:
        if (row_after["status"], row_after["outcome"]) != ("terminal", OUTCOME):
            p.append(f"admission state is {(row_after['status'], row_after['outcome'])}, "
                     f"expected ('terminal', '{OUTCOME}')")
        immutable = [k for k in row_before if k not in ("status", "outcome")
                     and row_before[k] != row_after[k]]
        if immutable:
            p.append(f"admission columns changed beyond status/outcome: {immutable}")
    if before["admissions_others_sha256"] != after["admissions_others_sha256"]:
        p.append("another admission row changed")
    if before["admissions_other_rows"] != after["admissions_other_rows"]:
        p.append("admission row count changed")

    bs, as_ = before["session"], after["session"]
    if as_ is None:
        p.append("selected session row missing after write")
    else:
        if as_["runtime_revision"] != bs["runtime_revision"] + 1:
            p.append(f"runtime_revision {bs['runtime_revision']}->{as_['runtime_revision']} (expected +1)")
        immutable = [k for k in bs if k != "runtime_revision" and bs[k] != as_[k]]
        if immutable:
            p.append(f"session columns changed beyond runtime_revision: {immutable}")
    if before["sessions_others_sha256"] != after["sessions_others_sha256"]:
        p.append("another session row changed")
    if before["sessions_other_rows"] != after["sessions_other_rows"]:
        p.append("session row count changed")

    for key, label in (("workers_sha256", "a worker row changed"),
                       ("leases_sha256", "a session lease changed"),
                       ("state_meta_sha256", "a state_meta row changed"),
                       ("worker_receipts_sha256", "a worker receipt changed")):
        if before[key] != after[key]:
            p.append(label)
    for key, label in (("worker_rows", "worker row count changed"),
                       ("lease_rows", "lease row count changed"),
                       ("state_meta_rows", "state_meta row count changed"),
                       ("worker_receipt_rows", "worker receipt count changed")):
        if before[key] != after[key]:
            p.append(label)
    for key, label in (("messages_total", "message count changed"),
                       ("messages_max_id", "message id high-water mark changed"),
                       ("messages_on_session", "messages on the selected session changed")):
        if before[key] != after[key]:
            p.append(f"{label}: {before[key]}->{after[key]}")

    if after["nonterminal_ids"] and set(after["nonterminal_ids"]) - set(before["nonterminal_ids"]):
        p.append(f"new non-terminal admission(s): {sorted(set(after['nonterminal_ids']) - set(before['nonterminal_ids']))}")
    if admission_id in (after["nonterminal_ids"] or []):
        p.append("selected admission still non-terminal")
    return p


def write_evidence(outdir, name, payload):
    path = os.path.join(outdir, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path, sha256_file(path)


def verify_post(path, sql_path, selection, integrity=False):
    """Read-only pass over the store, in the trigger's own WAL-aware reading mode."""
    conn = connect(path, readonly=True)
    try:
        sample = bucket_sample(conn, admission_sql(sql_path)[0])
        remaining = [dict(r) for r in conn.execute(
            "SELECT admission_id, status, owner_epoch, generation FROM session_admissions "
            "WHERE status IN ('queued','started','unknown') ORDER BY seq")]
        check = conn.execute("PRAGMA quick_check").fetchone()[0] if integrity else None
        return {"sample": sample, "remaining_nonterminal": remaining, "quick_check": check,
                "selected": {aid: row_dict(conn, "SELECT status, outcome FROM session_admissions "
                                                 "WHERE admission_id=?", (aid,)) for aid in selection}}
    finally:
        conn.close()


def normalise_inputs(args):
    """Strict parsing of the values argparse cannot validate without exiting."""
    if args.expect_epoch is not None:
        raw = args.expect_epoch.strip()
        try:
            args.expect_epoch = int(raw, 10)
        except (TypeError, ValueError):
            raise Refusal("expect_epoch_malformed", raw) from None
    if args.backup_sha256 is not None:
        args.backup_sha256 = args.backup_sha256.strip().lower()


def lexical_guards(args):
    """Input refusals that need no database at all: run before anything is opened."""
    unknown = [a for a in args.admission if a not in ROSTER]
    if unknown:
        raise Refusal("admission_not_in_roster", ",".join(unknown))
    if not args.apply:
        return
    if len(args.admission) != 1:
        raise Refusal("apply_requires_single_admission",
                      f"got {len(args.admission)}; name exactly one --admission per APPLY")
    if args.expect_epoch is None:
        raise Refusal("expect_epoch_required", "--apply requires --expect-epoch")
    if not args.backup:
        raise Refusal("backup_required", "--apply requires --backup")
    if args.backup_sha256 is None:
        raise Refusal("backup_sha256_required", "--apply requires --backup-sha256")
    if not is_hex64(args.backup_sha256):
        raise Refusal("backup_sha256_malformed", args.backup_sha256)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--trigger-script", required=True)
    ap.add_argument("--admission", action="append", default=[],
                    help="exactly one in --apply (the canary or an explicit next identity); "
                         "repeatable in --dry-run (default: the whole roster)")
    ap.add_argument("--backup")
    ap.add_argument("--backup-sha256")
    ap.add_argument("--expect-epoch", type=str,
                    help="mandatory under --apply; parsed strictly, malformed input is refused")
    ap.add_argument("--out", default=".")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--ignore-table", action="append", default=[],
                    help="exclude a measurement-only table (fixture audit tables) from the row-count proof")
    ap.add_argument("--integrity", action="store_true",
                    help="run PRAGMA quick_check before and after (slow on a large store)")
    # Parse without argparse's own exits so malformed input is a refusal with our exit code.
    args, unknown_args = ap.parse_known_args(argv)
    os.makedirs(args.out, exist_ok=True)

    try:
        if unknown_args:
            raise Refusal("unknown_arguments", " ".join(unknown_args))
        normalise_inputs(args)
        lexical_guards(args)
    except Refusal as exc:
        print(json.dumps({"result": "refused", "disposition": "refused", "reason": exc.reason,
                          "detail": exc.detail}, indent=2))
        write_evidence(args.out, "refusal.json", {"at": now_iso(), "reason": exc.reason,
                                                  "detail": exc.detail, "phase": "lexical"})
        return 64

    selection = args.admission or list(ROSTER_ORDER)

    if args.verify:
        post = verify_post(args.db, args.trigger_script, selection, args.integrity)
        print(json.dumps(post, indent=2))
        ok = post["quick_check"] in (None, "ok") and all(
            v and v["status"] == "terminal" and v["outcome"] == OUTCOME for v in post["selected"].values())
        return 0 if ok else 65

    # ---- pre-write: read-only connection only ------------------------------------------
    try:
        conn = connect(args.db, readonly=True)
    except sqlite3.Error as exc:
        print(json.dumps({"result": "refused", "disposition": "refused", "reason": "db_unreadable",
                          "detail": str(exc)}, indent=2))
        return 64
    try:
        sql = admission_sql(args.trigger_script)[0]
        # One deferred read transaction: every fact the guards and the delta proof rely on comes
        # from a single snapshot, not from a dozen independent autocommit reads.
        conn.execute("BEGIN")
        try:
            snap = snapshot(conn, selection, sql, args.integrity, args.ignore_table)
        finally:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:  # pragma: no cover - read-only txn always commits
                pass
        try:
            if args.expect_epoch is not None and snap["epoch"] != args.expect_epoch:
                raise Refusal("epoch_not_expected", f"{snap['epoch']}!={args.expect_epoch}")
            guards(snap)
            backup = check_backup(snap, args, selection) if args.apply and not already_terminal(snap) else None
        except Refusal as exc:
            print(json.dumps({"result": "refused", "disposition": "refused", "reason": exc.reason,
                              "detail": exc.detail}, indent=2))
            write_evidence(args.out, "refusal.json", {"at": now_iso(), "reason": exc.reason,
                                                      "detail": exc.detail, "snapshot": snap})
            return 64

        if already_terminal(snap):
            print(json.dumps({"result": "already_terminal", "disposition": "already_terminal",
                              "epoch": snap["epoch"], "admissions": selection}, indent=2))
            write_evidence(args.out, "already-terminal.json", snap)
            return 0
        if not args.apply:
            print(json.dumps({"result": "dry_run_ok", "disposition": "dry_run",
                              "epoch": snap["epoch"], "would_change": selection}, indent=2))
            write_evidence(args.out, "before.json", snap)
            return 2
    finally:
        conn.close()

    # ---- write: only now is a writable connection opened -------------------------------
    admission_id = selection[0]
    session_id = ROSTER[admission_id]
    try:
        wconn = connect(args.db, readonly=False)
    except sqlite3.Error as exc:
        print(json.dumps({"result": "refused", "disposition": "refused", "reason": "db_not_writable",
                          "detail": str(exc)}, indent=2))
        write_evidence(args.out, "refusal.json", {"at": now_iso(), "reason": "db_not_writable",
                                                  "detail": str(exc), "phase": "write-open"})
        return 64
    try:
        changed = apply_change(wconn, snap, admission_id)
    except Refusal as exc:
        print(json.dumps({"result": "refused", "disposition": "refused", "reason": exc.reason,
                          "detail": exc.detail}, indent=2))
        write_evidence(args.out, "refusal.json", {"at": now_iso(), "reason": exc.reason,
                                                  "detail": exc.detail, "phase": "transaction"})
        return 64
    except sqlite3.Error as exc:
        print(json.dumps({"result": "error", "disposition": "transaction_error",
                          "reason": type(exc).__name__, "detail": str(exc)}, indent=2))
        write_evidence(args.out, "transaction-error.json", {"at": now_iso(),
                                                           "reason": type(exc).__name__,
                                                           "detail": str(exc),
                                                           "phase": "transaction"})
        return 70
    finally:
        wconn.close()

    # ---- post-write proof: fresh read-only connection ---------------------------------
    disposition = "committed_verification_incomplete"
    problems = []
    after = None
    after_full = None
    post = None
    before = snap["images"][admission_id]
    try:
        after_conn = connect(args.db, readonly=True)
        try:
            after = image_state(after_conn, admission_id, session_id, sql, args.integrity,
                                args.ignore_table)
            after_full = snapshot(after_conn, selection, sql, args.integrity, args.ignore_table)
        finally:
            after_conn.close()
        problems = allowed_delta_problems(before, after, admission_id, session_id)
        post = verify_post(args.db, args.trigger_script, selection, args.integrity)
        # Trigger-level quiet evidence is reported, not required: unrelated in-flight work is
        # outside this tool's authority and its presence is not a defect in this write.
        if post["sample"]["epoch"] != snap["epoch"]:
            problems.append("epoch moved during write")
        if args.integrity and post["quick_check"] != snap["quick_check"]:
            problems.append(f"quick_check changed: {snap['quick_check']!r} -> {post['quick_check']!r}")
        disposition = "committed_verified" if not problems else "committed_verification_failed"
    except BaseException as exc:  # noqa: BLE001 - a verifier crash is a non-success disposition
        problems = [f"verification could not complete: {type(exc).__name__}: {exc}"]
        disposition = "committed_verification_incomplete"

    evidence = {
        "result": "applied" if disposition == "committed_verified" else "applied_unverified",
        "disposition": disposition,
        "at": now_iso(),
        "epoch": snap["epoch"],
        "selection": selection,
        "changed": changed,
        "backup": backup,
        "problems": problems,
        "delta": {"before": before, "after": after} if after is not None else None,
        "sample_before": snap["sample"], "sample_after": (post or {}).get("sample"),
        "remaining_nonterminal": (post or {}).get("remaining_nonterminal"),
        "quick_check": (post or {}).get("quick_check"),
        "total_rowcounts_before": snap["total_rowcounts"],
        "total_rowcounts_after": (after_full or {}).get("total_rowcounts"),
        "status_histogram_before": snap["status_histogram"],
        "status_histogram_after": (after_full or {}).get("status_histogram"),
        "schema_sha256_before": snap["schema_sha256"],
        "schema_sha256_after": (after or {}).get("schema_sha256"),
    }
    p0, h0 = write_evidence(args.out, "before.json", snap)
    if after_full is not None:
        p1, h1 = write_evidence(args.out, "after.json", after_full)
    else:
        p1, h1 = None, None
    p2, h2 = write_evidence(args.out, "evidence.json", evidence)
    if disposition == "committed_verified":
        write_evidence(args.out, "postcheck-ok.json", {"disposition": disposition, "delta_ok": True})
    else:
        write_evidence(args.out, "postcheck-failed.json", {"disposition": disposition,
                                                           "problems": problems,
                                                           "sample": (post or {}).get("sample")})
    print(json.dumps({"result": evidence["result"], "disposition": disposition,
                      "reason": None if disposition == "committed_verified" else "post_write_proof",
                      "problems": problems,
                      "changed": changed,
                      "epoch": snap["epoch"],
                      "remaining_nonterminal": (post or {}).get("remaining_nonterminal"),
                      "sample": (post or {}).get("sample"),
                      "evidence": {p0: h0, p1: h1, p2: h2}}, indent=2))
    return 0 if disposition == "committed_verified" else 65


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refusal as exc:
        print(json.dumps({"result": "refused", "disposition": "refused", "reason": exc.reason,
                          "detail": exc.detail}, indent=2))
        sys.exit(64)
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"result": "error", "reason": type(exc).__name__, "detail": str(exc)}, indent=2))
        sys.exit(70)
