#!/usr/bin/env python3.13
"""ADM-RECON-01 red controls: the defects the CA findings name, executed against the frozen
pre-guard tool, then the same inputs against the corrected tool.

The frozen original is `pre_guard/adm_recon_ea7e1dc.py`, byte-identical to the file published in
commit ea7e1dc641 (sha256 0bce8e4d…). Each case runs twice on two byte-identical copies of
`fixture/small.db`, with the same audit trigger installed, so "writes" below counts real UPDATEs
on the audited tables — not the tool's opinion of itself.

A case passes only when both halves hold:
  red   — the pre-guard tool exhibits the defect (exit 0 while doing the unguarded thing);
  green — the corrected tool refuses (64) or reports the delta unproven (65), with zero or
          disclosed writes respectively.

Run: /usr/bin/python3.13 red_controls.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRE = os.path.join(HERE, "pre_guard", "adm_recon_ea7e1dc.py")
POST = os.path.join(HERE, "adm_recon.py")
BASE = os.path.join(HERE, "fixture", "small.db")
TRIGGER = "/home/hermes/work/restore002-fix/harness/trigger/qw-launch-apply-v2.sh"
WORK = os.path.join(HERE, "fixture", "red")
PY = "/usr/bin/python3.13"

CANARY = "40a8ac83611e428696d5fee98e4bc599"
SECOND = "faf7f3066aca401d8b5c309c5d2f397d"
SES = {"40a8ac83611e428696d5fee98e4bc599": "local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901",
       "faf7f3066aca401d8b5c309c5d2f397d": "local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5"}
DECOY = "a683cb4bd9a9423c919bad9e5dd7f2b1"
EPOCH = "24"

results = []


def check(name, cond, detail: object = ""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'} {name}{(' :: ' + str(detail)) if detail and not cond else ''}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def instrument(db):
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("CREATE TABLE IF NOT EXISTS _adm_audit(tbl TEXT, key TEXT, before TEXT, after TEXT, at REAL)")
    watch = {
        "session_admissions": ("admission_id", ["status", "outcome", "owner_epoch", "generation"]),
        "sessions": ("id", ["runtime_revision", "runtime_generation", "message_count", "end_reason"]),
        "worker_executions": ("execution_id", ["status"]),
        "messages": ("id", ["role", "content", "active", "display_kind"]),
        "state_meta": ("key", ["value"]),
    }
    for t, (key, cols) in watch.items():
        oldj = ", ".join(f"'{c}', old.{c}" for c in cols)
        newj = ", ".join(f"'{c}', new.{c}" for c in cols)
        cond = " OR ".join(f"old.{c} IS NOT new.{c}" for c in cols)
        conn.execute(f"""CREATE TRIGGER _adm_audit_{t} AFTER UPDATE ON "{t}" WHEN {cond}
            BEGIN INSERT INTO _adm_audit(tbl, key, before, after, at)
            VALUES('{t}', CAST(old.{key} AS TEXT), json_object({oldj}), json_object({newj}),
                   julianday('now')); END""")
    conn.close()


def inject(db, body):
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"""CREATE TRIGGER _inject AFTER UPDATE OF status ON session_admissions
        WHEN NEW.admission_id='{CANARY}' BEGIN {body} END""")
    conn.close()


def case_dir(tag):
    d = os.path.join(WORK, tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    db = os.path.join(d, "state.db")
    shutil.copy2(BASE, db)
    backup = os.path.join(d, "backup.db")
    shutil.copy2(db, backup)
    return d, db, backup


def run(tool, db, backup, *, admission=None, epoch=EPOCH, give_sha=True, extra=()):
    out = os.path.join(os.path.dirname(db), "out-" + os.path.basename(tool)[:3])
    cmd = [PY, tool, "--db", db, "--trigger-script", TRIGGER, "--out", out, "--apply",
           "--ignore-table", "_adm_audit", "--backup", backup]
    if give_sha:
        cmd += ["--backup-sha256", sha256(backup)]
    if epoch is not None:
        cmd += ["--expect-epoch", epoch]
    if admission and "pre_guard" not in tool:
        cmd += ["--admission", admission]
    cmd += list(extra)
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        payload = json.loads(p.stdout)
    except json.JSONDecodeError:
        payload = {"raw": p.stdout.strip()[-400:], "err": p.stderr.strip()[-400:]}
    return p.returncode, payload


def writes(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT tbl, count(*) FROM _adm_audit GROUP BY tbl ORDER BY tbl").fetchall()
        adm = [r[1] for r in rows if r[0] == "session_admissions"]
        ses = [r[1] for r in rows if r[0] == "sessions"]
        return {"by_table": dict(rows), "admissions": adm[0] if adm else 0, "sessions": ses[0] if ses else 0}
    finally:
        conn.close()


def final(db, admissions):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {a: dict(zip(("status", "outcome"), conn.execute(
            "SELECT status, outcome FROM session_admissions WHERE admission_id=?", (a,)).fetchone()))
            for a in admissions}
    finally:
        conn.close()


def scenario(tag, mut=None, **kwargs):
    """Run pre and post on identical copies; return both observations."""
    d1, db1, b1 = case_dir(tag + "-pre")
    d2, db2, b2 = case_dir(tag + "-post")
    for db in (db1, db2):
        if mut:
            inject(db, mut)
        instrument(db)
    pre_rc, pre_p = run(PRE, db1, b1, **kwargs)
    post_rc, post_p = run(POST, db2, b2, **kwargs)
    adm = [CANARY, SECOND]
    return {
        "pre": {"rc": pre_rc, "reason": pre_p.get("reason"), "problems": pre_p.get("problems"),
                "writes": writes(db1), "final": final(db1, adm)},
        "post": {"rc": post_rc, "reason": post_p.get("reason"), "problems": post_p.get("problems"),
                 "disposition": post_p.get("disposition"), "writes": writes(db2), "final": final(db2, adm)},
    }


def main():
    os.makedirs(WORK, exist_ok=True)
    record = {"pre_tool": {"path": PRE, "sha256": sha256(PRE)},
              "post_tool": {"path": POST, "sha256": sha256(POST)},
              "fixture": {"path": BASE, "sha256": sha256(BASE)},
              "cases": {}}
    print(f"pre  {os.path.basename(PRE)} {record['pre_tool']['sha256'][:12]}")
    print(f"post {os.path.basename(POST)} {record['post_tool']['sha256'][:12]}")

    # ---- finding 1: an APPLY with no selection writes every roster row ----------
    r = scenario("f1-implicit-multi")
    record["cases"]["f1-implicit-multi"] = r
    check("f1/red-pre-guard-applies-three", r["pre"]["rc"] == 0 and r["pre"]["writes"]["admissions"] == 3,
          r["pre"])
    check("f1/green-post-refuses", r["post"]["rc"] == 64 and
          r["post"]["reason"] == "apply_requires_single_admission", r["post"])
    check("f1/green-post-zero-writes", r["post"]["writes"]["admissions"] == 0, r["post"]["writes"])

    # ---- finding 1: two named admissions in one APPLY --------------------------
    r = scenario("f1-two-named", admission=None)
    r2 = None
    d, db, b = case_dir("f1-two-named-post")
    instrument(db)
    cmd = [PY, POST, "--db", db, "--trigger-script", TRIGGER, "--out", os.path.join(d, "o"), "--apply",
           "--ignore-table", "_adm_audit", "--backup", b, "--backup-sha256", sha256(b),
           "--expect-epoch", EPOCH, "--admission", CANARY, "--admission", SECOND]
    p = subprocess.run(cmd, capture_output=True, text=True)
    r2 = {"rc": p.returncode, "payload": json.loads(p.stdout or "{}"), "writes": writes(db)}
    record["cases"]["f1-two-named-post"] = r2
    check("f1/two-named-refused", r2["rc"] == 64 and
          r2["payload"].get("reason") == "apply_requires_single_admission", r2)
    check("f1/two-named-zero-writes", r2["writes"]["admissions"] == 0, r2["writes"])

    # ---- finding 1: an identity outside the roster -----------------------------
    d, db, b = case_dir("f1-not-in-roster")
    instrument(db)
    cmd = [PY, POST, "--db", db, "--trigger-script", TRIGGER, "--out", os.path.join(d, "o"), "--apply",
           "--ignore-table", "_adm_audit", "--backup", b, "--backup-sha256", sha256(b),
           "--expect-epoch", EPOCH, "--admission", "deadbeef" * 8]
    p = subprocess.run(cmd, capture_output=True, text=True)
    payload = json.loads(p.stdout or "{}")
    record["cases"]["f1-not-in-roster"] = {"rc": p.returncode, "payload": payload, "writes": writes(db)}
    check("f1/not-in-roster-refused", p.returncode == 64 and
          payload.get("reason") == "admission_not_in_roster", payload)

    # ---- finding 2: no expected epoch ------------------------------------------
    r = scenario("f2-no-epoch", epoch=None, admission=CANARY)
    record["cases"]["f2-no-epoch"] = r
    check("f2/red-pre-guard-commits-without-epoch", r["pre"]["rc"] == 0 and
          r["pre"]["writes"]["admissions"] == 3, r["pre"])
    check("f2/green-post-refuses", r["post"]["rc"] == 64 and
          r["post"]["reason"] == "expect_epoch_required", r["post"])
    check("f2/green-post-zero-writes", r["post"]["writes"]["admissions"] == 0, r["post"]["writes"])

    # ---- finding 2: no backup digest -------------------------------------------
    r = scenario("f2-no-digest", give_sha=False, admission=CANARY)
    record["cases"]["f2-no-digest"] = r
    check("f2/red-pre-guard-commits-without-digest", r["pre"]["rc"] == 0 and
          r["pre"]["writes"]["admissions"] == 3, r["pre"])
    check("f2/green-post-refuses", r["post"]["rc"] == 64 and
          r["post"]["reason"] == "backup_sha256_required", r["post"])
    check("f2/green-post-zero-writes", r["post"]["writes"]["admissions"] == 0, r["post"]["writes"])

    # ---- finding 3: committed writes the old verification certified ------------
    injections = {
        "f3-wrong-outcome": (f"UPDATE session_admissions SET outcome='completed' WHERE admission_id='{CANARY}';",
                             "admission state", "terminal/completed"),
        "f3-revision-plus-2": (f"UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id='{SES[CANARY]}';",
                               "runtime_revision", None),
        "f3-worker-changed": (f"UPDATE worker_executions SET status='running' "
                              f"WHERE execution_id='admission-worker:{CANARY}';", "a worker row changed", None),
        "f3-unrelated-admission": (f"UPDATE session_admissions SET outcome='stale' WHERE admission_id='{DECOY}';",
                                   "another admission row changed", None),
        "f3-unrelated-session": ("UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id="
                                 f"(SELECT id FROM sessions WHERE id<>'{SES[CANARY]}' ORDER BY id LIMIT 1);",
                                 "another session row changed", None),
    }
    for tag, (body, expect, _) in injections.items():
        r = scenario(tag, mut=body, admission=CANARY)
        record["cases"][tag] = r
        check(f"{tag}/red-pre-guard-exit0", r["pre"]["rc"] == 0, r["pre"])
        check(f"{tag}/green-post-exit65", r["post"]["rc"] == 65 and
              r["post"]["disposition"] == "committed_verification_failed", r["post"])
        joined = " | ".join(r["post"]["problems"] or [])
        check(f"{tag}/green-problem-named", expect in joined, joined)
    # the wrong-outcome case is the sharpest: the store really holds an outcome the tool must never
    # produce, and the pre-guard tool reported success.
    wo = record["cases"]["f3-wrong-outcome"]
    check("f3-wrong-outcome/red-actually-wrong",
          wo["pre"]["final"][CANARY] == {"status": "terminal", "outcome": "completed"}, wo["pre"]["final"])

    # ---- parity: deviations the pre-guard tool also caught ---------------------
    parity = {
        "p-new-admission": ("INSERT INTO session_admissions(admission_id, request_id, principal_id, "
                            "target_session_id, lineage_json, payload_json, payload_digest, intent, status, "
                            "owner_epoch, generation) SELECT 'ffffffffffffffffffffffffffffffff', request_id||'-x', "
                            "principal_id, target_session_id, lineage_json, payload_json, payload_digest, 'queue', "
                            f"'unknown', 1, 1 FROM session_admissions WHERE admission_id='{CANARY}';"),
        "p-message-deleted": "DELETE FROM messages WHERE id=(SELECT max(id) FROM messages);",
    }
    for tag, body in parity.items():
        r = scenario(tag, mut=body, admission=CANARY)
        record["cases"][tag] = r
        check(f"{tag}/both-nonzero", r["pre"]["rc"] != 0 and r["post"]["rc"] == 65, r)

    # ---- structural: a control the original simply does not have --------------
    pre_src = open(PRE, encoding="utf-8").read()
    post_src = open(POST, encoding="utf-8").read()
    record["structural"] = {
        "pre_mentions_sqlite_master_in_verify": "sqlite_master" in pre_src.split("def verify_post")[-1],
        "pre_mentions_schema_fingerprint": "schema_fingerprint" in pre_src,
        "post_checks_schema": "schema_sha256" in post_src.split("def allowed_delta_problems")[1][:1200],
    }
    check("structural/pre-has-no-schema-assertion",
          record["structural"]["pre_mentions_sqlite_master_in_verify"] is False, record["structural"])
    check("structural/post-asserts-schema", record["structural"]["post_checks_schema"] is True,
          record["structural"])

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    record["summary"] = {"checks": total, "passed": passed,
                         "failed": [{"name": n, "detail": str(d)} for n, ok, d in results if not ok]}
    with open(os.path.join(HERE, "fixture", "red-controls-evidence.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    print(f"\n{passed}/{total} red-control checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
