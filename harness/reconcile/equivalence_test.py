#!/usr/bin/env python3.13
"""ADM-RECON-01 equivalence proof: the offline operator tool must produce exactly the
database effect of the runtime's own fenced resolution.

Lane A: the sanctioned runtime path, executed in-process on a disposable copy —
        ``hermes_state_runtime.resolve_unknown_session_input`` with the same
        ``_terminal_write=close_discarded_turn(..., notice=None)`` closure that
        ``SessionAuthority.resolve_unknown_as_owner`` passes for an operator discard.
Lane B: ``adm_recon.py --apply`` on a second, identical copy.

Both copies start from the same source file. Afterwards every touched row image, every
table row count and the schema fingerprint must agree, and Lane B must show exactly six
UPDATEs (3 session_admissions + 3 sessions) through the fixture audit triggers.

Usage: /usr/bin/python3.13 equivalence_test.py <source.db> [--workdir DIR] [--skip-audit]
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "adm_recon.py")
TRIGGER = "/home/hermes/work/restore002-fix/harness/trigger/qw-launch-apply-v2.sh"
PY = "/usr/bin/python3.13"

TARGETS = [
    ("40a8ac83611e428696d5fee98e4bc599",
     "local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901"),
    ("faf7f3066aca401d8b5c309c5d2f397d",
     "local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5"),
    ("e7aa5bc9b3204511846ad644a3279063",
     "local-efa627bea17ff79e9e8267f617e9f10b7eba2a3586e9092420510b8245b44187"),
]

INTEGRITY_TABLES = ("session_admissions", "sessions", "worker_executions", "state_meta")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'} {name}{(' :: ' + str(detail)[:400]) if detail and not cond else ''}")


def ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def row_images(path):
    """Full, ordered image of every row this transition is allowed to touch."""
    out = {}
    with ro(path) as conn:
        for aid, sid in TARGETS:
            out[f"admission:{aid}"] = dict(conn.execute(
                "SELECT * FROM session_admissions WHERE admission_id=?", (aid,)).fetchone() or {})
            out[f"session:{sid}"] = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone() or {})
            out[f"workers:{sid}"] = [dict(r) for r in conn.execute(
                "SELECT * FROM worker_executions WHERE session_id=? ORDER BY execution_id", (sid,))]
            out[f"messages:{sid}"] = [dict(r) for r in conn.execute(
                "SELECT id, role, content, active, display_kind FROM messages WHERE session_id=? ORDER BY id", (sid,))]
            out[f"leases:{sid}"] = [dict(r) for r in conn.execute(
                "SELECT * FROM session_turn_leases WHERE conversation_id=? ORDER BY conversation_id", (sid,))]
            out[f"meta:{aid}"] = [dict(r) for r in conn.execute(
                "SELECT key, value FROM state_meta WHERE key LIKE ? ORDER BY key", (f"%{aid}%",))]
        out["epoch"] = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
        out["status_histogram"] = {r[0]: r[1] for r in conn.execute(
            "SELECT status, count(*) FROM session_admissions GROUP BY status ORDER BY status")}
        out["outcome_histogram"] = {r[0] or "NULL": r[1] for r in conn.execute(
            "SELECT outcome, count(*) FROM session_admissions WHERE status='terminal' "
            "GROUP BY outcome ORDER BY outcome")}
        out["nonterminal_ids"] = sorted(r[0] for r in conn.execute(
            "SELECT admission_id FROM session_admissions WHERE status IN ('queued','started','unknown')"))
        out["schema"] = sorted((r[0], r[1], r[2] or "") for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE '_adm_audit%' "
            "ORDER BY type, name"))
        out["rowcounts"] = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                    "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            if "_fts" in name:          # FTS shadow tables: unchanged unless messages change
                continue
            out["rowcounts"][name] = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
    return out


def instrument(db, extra_exclude=()):
    """Fixture-only measurement device."""
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS _adm_audit(tbl TEXT, key TEXT, before TEXT, after TEXT)")
    watch = {
        "session_admissions": ("admission_id", ["status", "outcome", "owner_epoch", "generation"]),
        "sessions": ("id", ["runtime_revision", "runtime_generation", "message_count", "end_reason"]),
        "worker_executions": ("execution_id", ["status"]),
        "messages": ("id", ["role", "content", "active", "display_kind"]),
        "state_meta": ("key", ["value"]),
        "worker_receipts": ("execution_id", ["sequence", "payload_digest"]),
        "session_turn_leases": ("conversation_id", ["holder"]),
    }
    for t, (key, cols) in watch.items():
        oldj = ", ".join(f"'{c}', old.{c}" for c in cols)
        newj = ", ".join(f"'{c}', new.{c}" for c in cols)
        cond = " OR ".join(f"old.{c} IS NOT new.{c}" for c in cols)
        conn.execute(f"""CREATE TRIGGER _adm_audit_{t} AFTER UPDATE ON "{t}"
            WHEN {cond}
            BEGIN
              INSERT INTO _adm_audit(tbl, key, before, after)
              VALUES('{t}', CAST(old.{key} AS TEXT), json_object({oldj}), json_object({newj}));
            END""")
    conn.close()


def lane_a(db_path):
    """The runtime's own fenced resolution, exactly as SessionAuthority runs it."""
    sys.path.insert(0, "/home/hermes/.hermes/hermes-agent")
    os.environ.setdefault("HERMES_HOME", "/home/hermes/.hermes")
    from pathlib import Path

    from gateway.session_results import close_discarded_turn
    from hermes_state import SessionDB
    from hermes_state_runtime import resolve_unknown_session_input

    db = SessionDB(Path(db_path).resolve())
    epoch = None
    for aid, _sid in TARGETS:
        if epoch is None:
            with db._read_ctx() as conn:
                epoch = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()[0]
        gen = 1
        with db._read_ctx() as conn:
            row = conn.execute("SELECT generation FROM session_admissions WHERE admission_id=?", (aid,)).fetchone()
            gen = row[0] if row else gen
        resolve_unknown_session_input(
            db, epoch=epoch, admission_id=aid, generation=gen,
            _terminal_write=functools.partial(close_discarded_turn, db, notice=None))
    return epoch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--workdir", default=os.path.join(HERE, "fixture", "equiv"))
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--pristine", default=None,
                    help="path handed to --backup/--backup-sha256 (default: the source file)")
    ap.add_argument("--sequential", action="store_true",
                    help="do not keep both 13GB copies at once: run B, snapshot, delete, run A")
    ap.add_argument("--allow-runtime-migrations", action="store_true",
                    help="partial fixtures only: SessionDB's open runs schema migrations that a real "
                         "store already has, so B legitimately gains objects")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    a = os.path.join(args.workdir, "A-tool.db")
    b = os.path.join(args.workdir, "B-runtime.db")
    pre = os.path.join(args.workdir, "pre-backup.db")
    for p in (a, b, pre):
        if os.path.exists(p):
            os.remove(p)

    base = row_images(args.source)
    print(f"source={args.source} epoch={base['epoch']} hist={base['status_histogram']}")
    target_ids = {a for a, _ in TARGETS}
    present = {a: base[f"admission:{a}"].get("status") for a in target_ids}
    check("source/targets-present-unknown", all(v == "unknown" for v in present.values()), present)

    pristine = args.pristine or args.source
    import hashlib

    def sha(path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    pristine_sha = sha(pristine)
    print(f"pristine={pristine} sha256={pristine_sha}")

    # ---- Lane B: the operator tool -------------------------------------------------
    shutil.copy2(args.source, a)
    instrument(a)
    proc = subprocess.run([PY, TOOL, "--db", a, "--trigger-script", TRIGGER,
                           "--out", os.path.join(args.workdir, "out-B"), "--apply",
                           "--ignore-table", "_adm_audit", "--backup", pristine,
                           "--backup-sha256", pristine_sha],
                          capture_output=True, text=True)
    check("laneB/exit0", proc.returncode == 0, proc.stdout[-600:] + proc.stderr[-600:])
    writes = [dict(r) for r in ro(a).execute("SELECT tbl, key FROM _adm_audit ORDER BY tbl, key")]
    check("laneB/writes==6", len(writes) == 6, writes)
    check("laneB/writes-are-3+3",
          sorted((w["tbl"] for w in writes)) == ["session_admissions"] * 3 + ["sessions"] * 3, writes)
    ia = row_images(a)
    integ_a = {t: ro(a).execute(f"PRAGMA integrity_check('{t}')").fetchone()[0] for t in INTEGRITY_TABLES}

    # ---- Lane A: the runtime's own path -------------------------------------------
    if args.sequential:
        for suffix in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(a + suffix):
                os.remove(a + suffix)
    shutil.copy2(args.source, b)
    try:
        epoch = lane_a(b)
        check("laneA/ran", True)
        print(f"laneA epoch={epoch}")
    except BaseException as exc:              # noqa: BLE001 - report, never hide
        import traceback
        traceback.print_exc()
        check("laneA/ran", False, repr(exc)[:300])

    # ---- equivalence --------------------------------------------------------------
    ib = row_images(b)
    integ_b = {t: ro(b).execute(f"PRAGMA integrity_check('{t}')").fetchone()[0] for t in INTEGRITY_TABLES}
    diffs = {}
    for key in sorted(set(ia) | set(ib)):
        if key in ("rowcounts", "schema"):
            continue
        if ia.get(key) != ib.get(key):
            diffs[key] = {"tool": ia.get(key), "runtime": ib.get(key)}
    check("equiv/no-differences", not diffs, json.dumps(diffs, default=str)[:1500])

    # row counts: only the audit table may differ (it lives on A only)
    ra = dict(ia["rowcounts"]); rb = dict(ib["rowcounts"])
    ra.pop("_adm_audit", None); rb.pop("_adm_audit", None)
    check("equiv/schema-unchanged-by-tool", ia["schema"] == base["schema"], "tool altered sqlite_master")
    if args.allow_runtime_migrations:
        extra = sorted(set(rb) - set(ra))
        # migrations also seed state_meta keys on a store that never ran them
        skip = {"state_meta"}
        check("equiv/rowcounts", all(ra.get(k) == rb.get(k) for k in ra if k not in skip),
              {k: (ra.get(k), rb.get(k)) for k in ra if k not in skip and ra.get(k) != rb.get(k)})
        print(f"disclosed: state_meta rows {ra.get('state_meta')} vs {rb.get('state_meta')} "
              "(migration seeds on the partial fixture)")
        print(f"disclosed: runtime open added {len(extra)} migrated tables on this partial fixture: {extra[:6]}")
    else:
        check("equiv/schema-unchanged-by-runtime", ib["schema"] == base["schema"],
              "runtime open altered sqlite_master (migrations) - disclose")
        check("equiv/rowcounts", ra == rb,
              {k: (ra.get(k), rb.get(k)) for k in set(ra) | set(rb) if ra.get(k) != rb.get(k)})

    # both lanes must leave the store in the state the trigger reads as quiet
    # A live snapshot carries other, unrelated non-terminal admissions (in-flight work on other
    # sessions). The requirement is that exactly the three targets left the non-terminal set.
    base_nt, a_nt, b_nt = set(base["nonterminal_ids"]), set(ia["nonterminal_ids"]), set(ib["nonterminal_ids"])
    check("equiv/tool-removed-exactly-targets", base_nt - a_nt == target_ids and not (a_nt - base_nt),
          {"removed": sorted(base_nt - a_nt), "added": sorted(a_nt - base_nt)})
    check("equiv/runtime-removed-exactly-targets", base_nt - b_nt == target_ids and not (b_nt - base_nt),
          {"removed": sorted(base_nt - b_nt), "added": sorted(b_nt - base_nt)})
    for aid, _ in TARGETS:
        row = ia[f"admission:{aid}"]
        check(f"equiv/{aid[:8]}-terminal-interrupted",
              (row["status"], row["outcome"]) == ("terminal", "interrupted"), row)
        rrow = ib[f"admission:{aid}"]
        check(f"equiv/{aid[:8]}-runtime-agrees",
              (rrow["status"], rrow["outcome"]) == ("terminal", "interrupted"), rrow)
    for aid, sid in TARGETS:
        pre_rev = base[f"session:{sid}"]["runtime_revision"]
        check(f"equiv/{sid[-8:]}-revision+1", ia[f"session:{sid}"]["runtime_revision"] == pre_rev + 1,
              (pre_rev, ia[f"session:{sid}"]["runtime_revision"]))
        check(f"equiv/{sid[-8:]}-messages-unchanged",
              ia[f"messages:{sid}"] == base[f"messages:{sid}"], "")

    for tbl in INTEGRITY_TABLES:
        check(f"integrity/{tbl}", integ_a[tbl] == "ok", integ_a[tbl])
        check(f"integrity/{tbl}/runtime-copy", integ_b[tbl] == "ok", integ_b[tbl])

    summary = {"checks": len(results), "passed": sum(1 for _, c, _ in results if c),
               "diffs": sorted(diffs), "workdir": args.workdir}
    with open(os.path.join(args.workdir, "equivalence.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": [{"check": n, "ok": c} for n, c, _ in results],
                   "differences": diffs, "integrity": {"tool": integ_a, "runtime": integ_b}},
                  fh, indent=2, default=str)
    print(json.dumps(summary))
    if not args.keep:
        pass  # copies are large; the caller prunes
    return 0 if all(c for _, c, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
