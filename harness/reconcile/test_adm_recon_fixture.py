#!/usr/bin/env python3.13
"""ADM-RECON-01 fixture tests: the tool must change exactly the three intended rows,
and must refuse (exit 64) or fail closed (exit 65) on every adversarial variant.

Each case runs against a disposable copy of `fixture/small.db`. Nothing here touches
production: the source DB is only read when the fixture is (re)built.

Run: /usr/bin/python3.13 test_adm_recon_fixture.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "adm_recon.py")
BASE = os.path.join(HERE, "fixture", "small.db")
TRIGGER = "/home/hermes/work/restore002-fix/harness/trigger/qw-launch-apply-v2.sh"
WORK = os.path.join(HERE, "fixture", "cases")
PY = "/usr/bin/python3.13"

TARGET_ADM = ["40a8ac83611e428696d5fee98e4bc599", "faf7f3066aca401d8b5c309c5d2f397d",
              "e7aa5bc9b3204511846ad644a3279063"]
TARGET_SES = ["local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901",
              "local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5",
              "local-efa627bea17ff79e9e8267f617e9f10b7eba2a3586e9092420510b8245b44187"]
AUDITED = ("session_admissions", "sessions", "worker_executions", "messages", "state_meta")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'} {name}{(' :: ' + str(detail)) if detail and not cond else ''}")


def fresh(case):
    d = os.path.join(WORK, case)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    db = os.path.join(d, "state.db")
    shutil.copy2(BASE, db)
    return d, db


def instrument(db):
    """Fixture-only measurement device: log every real UPDATE on the audited tables."""
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS _adm_audit(tbl TEXT, key TEXT, before TEXT, after TEXT, at REAL)")
    watch = {
        "session_admissions": ("admission_id", ["status", "outcome", "owner_epoch", "generation"]),
        "sessions": ("id", ["runtime_revision", "runtime_generation", "message_count", "end_reason"]),
        "worker_executions": ("execution_id", ["status"]),
        "messages": ("id", ["role", "content", "active", "display_kind"]),
        "state_meta": ("key", ["value"]),
    }
    for t in AUDITED:
        key, cols = watch[t]
        oldj = ", ".join(f"'{c}', old.{c}" for c in cols)
        newj = ", ".join(f"'{c}', new.{c}" for c in cols)
        cond = " OR ".join(f"old.{c} IS NOT new.{c}" for c in cols)
        conn.execute(f"""CREATE TRIGGER _adm_audit_{t} AFTER UPDATE ON "{t}"
            WHEN {cond}
            BEGIN
              INSERT INTO _adm_audit(tbl, key, before, after, at)
              VALUES('{t}', CAST(old.{key} AS TEXT), json_object({oldj}), json_object({newj}), julianday('now'));
            END""")
    conn.close()


def run(db, out, *extra, backup=None):
    cmd = [PY, TOOL, "--db", db, "--trigger-script", TRIGGER, "--out", out, *extra]
    if backup:
        cmd += ["--backup", backup]
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        payload = json.loads(p.stdout)
    except json.JSONDecodeError:
        payload = {"raw": p.stdout, "err": p.stderr}
    return p.returncode, payload


def rows(db, sql, params=()):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def apply_case(case, mutate=None, backup_from_base=True):
    d, db = fresh(case)
    if mutate:
        conn = sqlite3.connect(db, isolation_level=None)
        mutate(conn)
        conn.close()
    backup = os.path.join(d, "backup.db")
    if backup_from_base:
        shutil.copy2(db, backup)
    instrument(db)
    rc, payload = run(db, os.path.join(d, "out"), "--apply", "--ignore-table", "_adm_audit", backup=backup)
    return d, db, backup, rc, payload


def main():
    os.makedirs(WORK, exist_ok=True)

    # ---- 1. happy path: exactly six rows change, nothing else -------------------
    d, db, backup, rc, payload = apply_case("happy")
    check("happy/exit0", rc == 0, payload)
    audit = rows(db, "SELECT tbl, key, before, after FROM _adm_audit ORDER BY tbl, key")
    adm = [a for a in audit if a["tbl"] == "session_admissions"]
    ses = [a for a in audit if a["tbl"] == "sessions"]
    check("happy/audit-admissions==3", len(adm) == 3, len(adm))
    check("happy/audit-sessions==3", len(ses) == 3, len(ses))
    check("happy/audit-total==6", len(audit) == 6, [a["tbl"] for a in audit])
    check("happy/no-worker-or-message-or-meta-writes",
          not [a for a in audit if a["tbl"] in ("worker_executions", "messages", "state_meta")],
          [a["tbl"] for a in audit])
    for a in adm:
        b, af = json.loads(a["before"]), json.loads(a["after"])
        check(f"happy/transition-{a['key'][:8]}",
              (b["status"], b["outcome"] or None) == ("unknown", None)
              and (af["status"], af["outcome"]) == ("terminal", "interrupted"),
              (b["status"], af["status"], af["outcome"]))
    for a in ses:
        b, af = json.loads(a["before"]), json.loads(a["after"])
        check(f"happy/revision+1-{a['key'][-12:]}", af["runtime_revision"] == b["runtime_revision"] + 1,
              (b["runtime_revision"], af["runtime_revision"]))
    st = {r["status"]: r["n"] for r in rows(db, "SELECT status, count(*) n FROM session_admissions GROUP BY status")}
    check("happy/histogram", st == {"terminal": 7}, st)
    check("happy/decoy-messages-intact", rows(db, "SELECT count(*) n FROM messages")[0]["n"] == 69,
          rows(db, "SELECT count(*) n FROM messages")[0]["n"])
    check("happy/lease-intact", rows(db, "SELECT count(*) n FROM session_turn_leases")[0]["n"] == 1)
    ev = json.load(open(os.path.join(d, "out", "evidence.json"), encoding="utf-8"))
    check("happy/evidence-sample-after", ev["sample_after"]["stale_recent"] == 0, ev["sample_after"])
    check("happy/rowcounts-identical", ev["total_rowcounts_before"] == ev["total_rowcounts_after"])
    check("happy/evidence-checksummed", bool(ev.get("backup", {}).get("sha256")), ev.get("backup"))

    # ---- 2. idempotent second run: no second revision bump ---------------------
    rev_before = [r["runtime_revision"] for r in rows(db, "SELECT runtime_revision FROM sessions ORDER BY id")]
    rc2, payload2 = run(db, os.path.join(d, "out2"), "--apply", "--ignore-table", "_adm_audit", backup=backup)
    rev_after = [r["runtime_revision"] for r in rows(db, "SELECT runtime_revision FROM sessions ORDER BY id")]
    check("idempotent/exit0", rc2 == 0, payload2)
    check("idempotent/no-second-bump", rev_before == rev_after, (rev_before, rev_after))
    check("idempotent/no-new-audit-rows",
          len(rows(db, "SELECT * FROM _adm_audit")) == 6, len(rows(db, "SELECT * FROM _adm_audit")))

    # ---- 3. refusals: each variant must exit 64 with zero writes ---------------
    def flip_generation(c):
        c.execute("UPDATE session_admissions SET generation=2 WHERE admission_id=?", (TARGET_ADM[0],))

    def flip_status(c):
        c.execute("UPDATE session_admissions SET status='started' WHERE admission_id=?", (TARGET_ADM[1],))

    def add_message(c):
        c.execute("INSERT INTO messages(session_id, role, content, timestamp, active) VALUES(?,?,?,?,1)",
                  (TARGET_SES[2], "user", "late work", 1.0))

    def add_lease(c):
        c.execute("INSERT INTO session_turn_leases(conversation_id,holder,acquired_at,expires_at) "
                  "VALUES(?,?,?,?)", (TARGET_SES[0], "someone", 1.0, 9e9))

    def add_captured(c):
        c.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                  (f"admission-result:{TARGET_ADM[2]}", "{}"))

    def move_session_generation(c):
        c.execute("UPDATE sessions SET runtime_generation=2 WHERE id=?", (TARGET_SES[1],))

    refusals = [
        ("gen-mismatch", flip_generation, "ownership_mismatch"),
        ("status-started", flip_status, "admission_not_unknown"),
        ("transcript-arrived", add_message, "session_has_transcript"),
        ("lease-held", add_lease, "transcript_lease_held"),
        ("captured-result", add_captured, "captured_result_present"),
        ("session-generation", move_session_generation, "session_generation_moved"),
    ]
    for name, fn, reason in refusals:
        d2, db2, bk2, rc3, p3 = apply_case(f"refuse-{name}", mutate=fn)
        check(f"refuse/{name}/exit64", rc3 == 64, p3)
        check(f"refuse/{name}/reason", p3.get("reason") == reason, p3)
        n = rows(db2, "SELECT count(*) n FROM session_admissions WHERE status='terminal'")[0]["n"]
        check(f"refuse/{name}/nothing-written", n == 4, n)

    # ---- 4. backup guards -----------------------------------------------------
    d4, db4 = fresh("backup-guards")
    rc4, p4 = run(db4, os.path.join(d4, "out"), "--apply", "--ignore-table", "_adm_audit")
    check("backup/absent/exit64", rc4 == 64 and p4.get("reason") == "backup_missing", p4)
    stale = os.path.join(d4, "stale.db")
    shutil.copy2(BASE, stale)
    c = sqlite3.connect(stale, isolation_level=None)
    c.execute("UPDATE session_admissions SET status='started' WHERE admission_id=?", (TARGET_ADM[0],))
    c.close()
    rc5, p5 = run(db4, os.path.join(d4, "out"), "--apply", backup=stale)
    check("backup/not-current/exit64", rc5 == 64 and p5.get("reason") == "backup_not_current", p5)
    good = os.path.join(d4, "good.db")
    shutil.copy2(BASE, good)
    rc6, p6 = run(db4, os.path.join(d4, "out"), "--apply", "--backup-sha256", "0" * 64, backup=good)
    check("backup/digest-mismatch/exit64", rc6 == 64 and p6.get("reason") == "backup_digest_mismatch", p6)

    # ---- 5. epoch guard ------------------------------------------------------
    d7, db7 = fresh("epoch-guard")
    shutil.copy2(BASE, os.path.join(d7, "b.db"))
    rc7, p7 = run(db7, os.path.join(d7, "out"), "--apply", "--expect-epoch", "23",
                  backup=os.path.join(d7, "b.db"))
    check("epoch/mismatch/exit64", rc7 == 64 and p7.get("reason") == "epoch_not_expected", p7)

    # ---- 6. new work after the write: commit happens, verification fails closed
    def add_fourth_orphan(c):
        c.row_factory = sqlite3.Row
        row = dict(c.execute("SELECT * FROM session_admissions WHERE admission_id=?", (TARGET_ADM[3 - 3],)).fetchone())
        cols = [r[1] for r in c.execute("PRAGMA table_info(session_admissions)")]
        vals = []
        for col in cols:
            if col == "seq":
                vals.append(9999)
            elif col == "admission_id":
                vals.append("ffffffffffffffffffffffffffffffff")
            elif col == "target_session_id":
                vals.append(TARGET_SES[0])
            elif col == "request_id":
                vals.append("ffffffffffffffffffffffffffffffff")
            else:
                vals.append(row[col])
        c.execute(f"INSERT INTO session_admissions({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)

    d8, db8, bk8, rc8, p8 = apply_case("new-work", mutate=add_fourth_orphan)
    check("new-work/exit65", rc8 == 65, p8)
    check("new-work/targets-committed",
          rows(db8, "SELECT count(*) n FROM session_admissions WHERE status='terminal' "
                    "AND outcome='interrupted'")[0]["n"] == 3)
    failed = os.path.join(d8, "out", "postcheck-failed.json")
    check("new-work/failclosed-evidence", os.path.exists(failed), os.listdir(os.path.join(d8, "out")))
    if os.path.exists(failed):
        problems = json.load(open(failed, encoding="utf-8"))["problems"]
        check("new-work/names-the-new-row", any("ffffffff" in p or "stale_recent" in p for p in problems), problems)

    # ---- 7. dry-run is the default and writes nothing ------------------------
    d9, db9 = fresh("dry-run")
    rev_before = rows(db9, "SELECT sum(runtime_revision) s FROM sessions")[0]["s"]
    rc9, p9 = run(db9, os.path.join(d9, "out"))
    rev_after = rows(db9, "SELECT sum(runtime_revision) s FROM sessions")[0]["s"]
    check("dry-run/exit2", rc9 == 2, p9)
    check("dry-run/no-writes", rev_before == rev_after and
          rows(db9, "SELECT count(*) n FROM session_admissions WHERE status='unknown'")[0]["n"] == 3)

    # ---- 8. a read-only source file cannot be written even with --apply ------
    d10, db10 = fresh("readonly")
    os.chmod(db10, 0o444)
    rc10, p10 = run(db10, os.path.join(d10, "out"), "--apply")
    check("readonly/refused-or-gated", rc10 in (64, 70), p10)

    ok = sum(1 for _, c, _ in results if c)
    print(f"\n{ok}/{len(results)} checks passed")
    fails = [r for r in results if not r[1]]
    if fails:
        print("FAILURES:", json.dumps(fails, indent=2, default=str))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
