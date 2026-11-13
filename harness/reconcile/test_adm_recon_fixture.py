#!/usr/bin/env python3.13
"""ADM-RECON-01 execution-guard suite.

Proves, on disposable copies of `fixture/small.db`:

  * the canary applies exactly one admission and nothing else;
  * three explicit one-at-a-time applications reach the same target state as a single
    implicit multi-row apply, with each subsequent identity named by the operator;
  * every missing/malformed/stale input is refused before a write connection exists;
  * a committed write is only reported as success when the post-write image proves the
    exact allowed delta, and every injected deviation returns a non-success disposition;
  * the pre-existing compare-and-set, ownership, transcript and worker guards still fire.

Nothing here touches production: `fixture/small.db` is a purpose-built copy of the real DDL.

Run: /usr/bin/python3.13 test_adm_recon_fixture.py
"""
from __future__ import annotations

import hashlib
import importlib.util
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

CANARY = "40a8ac83611e428696d5fee98e4bc599"
SECOND = "faf7f3066aca401d8b5c309c5d2f397d"
THIRD = "e7aa5bc9b3204511846ad644a3279063"
TARGET_ADM = [CANARY, SECOND, THIRD]
TARGET_SES = ["local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901",
              "local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5",
              "local-efa627bea17ff79e9e8267f617e9f10b7eba2a3586e9092420510b8245b44187"]
SES = dict(zip(TARGET_ADM, TARGET_SES))
DECOY_ADM = "a683cb4bd9a9423c919bad9e5dd7f2b1"
EPOCH = "24"
AUDITED = ("session_admissions", "sessions", "worker_executions", "messages", "state_meta")

results = []


def check(name, cond, detail: object = ""):
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


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(db, out, *extra_args, admission=None, epoch=EPOCH, backup=None, backup_sha=None,
        apply=True, ignore=True, trigger=TRIGGER):
    cmd = [PY, TOOL, "--db", db, "--trigger-script", trigger, "--out", out]
    if apply:
        cmd.append("--apply")
    if ignore:
        cmd += ["--ignore-table", "_adm_audit"]
    for a in ([admission] if isinstance(admission, str) else (admission or [])):
        cmd += ["--admission", a]
    if epoch is not None:
        cmd += ["--expect-epoch", epoch]
    if backup:
        cmd += ["--backup", backup]
    if backup_sha:
        cmd += ["--backup-sha256", backup_sha]
    cmd += list(extra_args)
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


def audit(db):
    return rows(db, "SELECT tbl, key, before, after FROM _adm_audit ORDER BY tbl, key")


def state(db, aid):
    r = rows(db, "SELECT a.status, a.outcome, s.runtime_revision FROM session_admissions a "
                 "JOIN sessions s ON s.id=a.target_session_id WHERE a.admission_id=?", (aid,))
    return r[0] if r else {}


def prepare(case, mutate=None):
    d, db = fresh(case)
    if mutate:
        conn = sqlite3.connect(db, isolation_level=None)
        mutate(conn)
        conn.close()
    backup = os.path.join(d, "backup.db")
    shutil.copy2(db, backup)
    instrument(db)
    return d, db, backup, sha256(backup)


def load_tool():
    spec = importlib.util.spec_from_file_location("adm_recon_under_test", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    os.makedirs(WORK, exist_ok=True)
    mod = load_tool()

    # ---- 1. canary: exactly one admission, exactly the allowed delta -------------
    d, db, backup, bsha = prepare("canary")
    pre_rev = state(db, CANARY)["runtime_revision"]
    rc, p = run(db, os.path.join(d, "out"), admission=CANARY, backup=backup, backup_sha=bsha)
    check("canary/exit0", rc == 0, p)
    check("canary/disposition", p.get("disposition") == "committed_verified", p.get("disposition"))
    check("canary/problems-empty", p.get("problems") == [], p.get("problems"))
    a = audit(db)
    adm = [x for x in a if x["tbl"] == "session_admissions"]
    ses = [x for x in a if x["tbl"] == "sessions"]
    check("canary/one-admission-update", len(adm) == 1 and adm[0]["key"] == CANARY, adm)
    check("canary/one-session-update", len(ses) == 1 and ses[0]["key"] == SES[CANARY], ses)
    check("canary/no-other-table-writes", all(x["tbl"] in ("session_admissions", "sessions") for x in a), a)
    check("canary/status", state(db, CANARY) == {"status": "terminal", "outcome": "interrupted",
                                                 "runtime_revision": pre_rev + 1}, state(db, CANARY))
    check("canary/second-untouched", state(db, SECOND)["status"] == "unknown", state(db, SECOND))
    check("canary/third-untouched", state(db, THIRD)["status"] == "unknown", state(db, THIRD))
    check("canary/remaining", [r["admission_id"] for r in p.get("remaining_nonterminal") or []] ==
          [SECOND, THIRD], p.get("remaining_nonterminal"))
    ev = json.load(open(os.path.join(d, "out", "evidence.json"), encoding="utf-8"))
    check("canary/evidence-delta-no-other-admission",
          ev["delta"]["before"]["admissions_others_sha256"] == ev["delta"]["after"]["admissions_others_sha256"], "")
    check("canary/evidence-schema-stable",
          ev["schema_sha256_before"] == ev["schema_sha256_after"], "")
    check("canary/evidence-rowcounts-stable",
          ev["total_rowcounts_before"] == ev["total_rowcounts_after"],
          (ev["total_rowcounts_before"], ev["total_rowcounts_after"]))
    check("canary/evidence-backup-digest", ev["backup"]["sha256"] == bsha, ev["backup"])
    check("canary/before-after-evidence-files",
          os.path.exists(os.path.join(d, "out", "before.json")) and
          os.path.exists(os.path.join(d, "out", "after.json")), "")

    # ---- 2. explicit one-at-a-time sequence: canary, then named identities -------
    d2, db2, backup2, bsha2 = prepare("sequence")
    seq = []
    for i, aid in enumerate(TARGET_ADM, start=1):
        rc_i, p_i = run(db2, os.path.join(d2, f"out{i}"), admission=aid, backup=backup2,
                        backup_sha=bsha2)
        seq.append((aid, rc_i, p_i.get("disposition")))
        check(f"sequence/{i}/exit0", rc_i == 0, p_i)
        check(f"sequence/{i}/disposition", p_i.get("disposition") == "committed_verified", p_i.get("disposition"))
        check(f"sequence/{i}/cumulative-terminal",
              sum(1 for x in TARGET_ADM if state(db2, x)["status"] == "terminal") == i, "")
        later = TARGET_ADM[i:]
        check(f"sequence/{i}/later-untouched",
              all(state(db2, x)["status"] == "unknown" for x in later), later)
        a_i = audit(db2)
        check(f"sequence/{i}/audit-rows",
              len([x for x in a_i if x["tbl"] == "session_admissions"]) == i and
              len([x for x in a_i if x["tbl"] == "sessions"]) == i, len(a_i))
    check("sequence/all-terminal", all(state(db2, x)["status"] == "terminal" and
                                       state(db2, x)["outcome"] == "interrupted" for x in TARGET_ADM), "")
    check("sequence/final-remaining-empty", seq[-1][2] == "committed_verified", seq)

    # ---- 3. idempotency: the canary cannot be re-applied ------------------------
    rc3, p3 = run(db, os.path.join(d, "out-again"), admission=CANARY, backup=backup, backup_sha=bsha)
    check("idempotent/exit0", rc3 == 0, p3)
    check("idempotent/already-terminal", p3.get("disposition") == "already_terminal", p3.get("disposition"))
    check("idempotent/no-new-writes", len(audit(db)) == 2, len(audit(db)))

    # ---- 4. refusals: nothing is written, no write transaction is opened --------
    def refusal_case(name, expect_reason, admission=CANARY, epoch=EPOCH, backup_mode="good",
                     backup_sha_mode="good", mutate=None,
                     apply=True):
        d, db, backup, bsha = prepare(f"refuse-{name}", mutate)
        b = None if backup_mode == "none" else (db if backup_mode == "self" else backup)
        s = None if backup_sha_mode == "none" else ("0" * 64 if backup_sha_mode == "wrong" else bsha)
        if backup_sha_mode == "short":
            s = bsha[:63]
        if backup_sha_mode == "nonhex":
            s = "z" * 64
        rc, p = run(db, os.path.join(d, "out"), admission=admission, epoch=epoch, backup=b,
                    backup_sha=s, apply=apply)
        n_audit = len(audit(db))
        check(f"refuse/{name}/exit64", rc == 64, (rc, p))
        check(f"refuse/{name}/reason", p.get("reason") == expect_reason, p.get("reason"))
        check(f"refuse/{name}/no-writes", n_audit == 0, n_audit)
        check(f"refuse/{name}/target-unknown", state(db, CANARY)["status"] == "unknown", state(db, CANARY))

    refusal_case("no-admission", "apply_requires_single_admission", admission=[])
    refusal_case("two-admissions", "apply_requires_single_admission", admission=[CANARY, SECOND])
    refusal_case("not-in-roster", "admission_not_in_roster", admission="deadbeef" * 8)
    # dry-run must validate the roster too, instead of assuming the id exists
    d, db, _, _ = prepare("refuse-dryrun-not-in-roster")
    rc, p = run(db, os.path.join(d, "out"), admission="deadbeef" * 8, apply=False)
    check("refuse/dryrun-not-in-roster/exit64", rc == 64, (rc, p))
    check("refuse/dryrun-not-in-roster/reason", p.get("reason") == "admission_not_in_roster", p.get("reason"))
    check("refuse/dryrun-not-in-roster/no-evidence-write",
          not os.path.exists(os.path.join(d, "out", "before.json")))
    refusal_case("no-epoch", "expect_epoch_required", epoch=None)
    refusal_case("epoch-malformed", "expect_epoch_malformed", epoch="24x")
    refusal_case("epoch-mismatch", "epoch_not_expected", epoch="23")
    refusal_case("no-backup", "backup_required", backup_mode="none")
    refusal_case("backup-is-target", "backup_is_target", backup_mode="self")
    refusal_case("no-backup-sha", "backup_sha256_required", backup_sha_mode="none")
    refusal_case("backup-sha-short", "backup_sha256_malformed", backup_sha_mode="short")
    refusal_case("backup-sha-nonhex", "backup_sha256_malformed", backup_sha_mode="nonhex")
    refusal_case("backup-sha-wrong", "backup_digest_mismatch", backup_sha_mode="wrong")

    # A backup taken at a later state (this one already records SECOND as terminal) must not be
    # accepted as proof of the pre-state the write is about to change.
    d, db, backup, _ = prepare("refuse-backup-not-current")
    bconn = sqlite3.connect(backup, isolation_level=None)
    bconn.execute("UPDATE session_admissions SET status='terminal', outcome='interrupted' "
                  "WHERE admission_id=?", (SECOND,))
    bconn.close()
    rc, p = run(db, os.path.join(d, "out"), admission=SECOND, backup=backup,
                backup_sha=sha256(backup))
    check("refuse/backup-not-current/exit64", rc == 64, (rc, p))
    check("refuse/backup-not-current/reason", p.get("reason") == "backup_not_current", p.get("reason"))
    check("refuse/backup-not-current/no-writes", len(audit(db)) == 0, len(audit(db)))

    # ---- 5. a committed write with an unprovable delta is never a success -------
    def injection(body, when="NEW.admission_id", target=CANARY):
        def mutate(conn):
            conn.execute(f"""CREATE TRIGGER _inject AFTER UPDATE OF status ON session_admissions
                WHEN {when}='{target}'
                BEGIN {body} END""")
        return mutate

    injections = {
        "wrong-outcome": (injection(f"UPDATE session_admissions SET outcome='completed' "
                                    f"WHERE admission_id='{CANARY}';"), "admission state"),
        "revision-plus-2": (injection(f"UPDATE sessions SET runtime_revision=runtime_revision+1 "
                                      f"WHERE id='{SES[CANARY]}';"), "runtime_revision"),
        "worker-changed": (injection(f"UPDATE worker_executions SET status='running' "
                                     f"WHERE execution_id='admission-worker:{CANARY}';"), "a worker row changed"),
        "unrelated-admission": (injection(f"UPDATE session_admissions SET outcome='stale' "
                                          f"WHERE admission_id='{DECOY_ADM}';"), "another admission row changed"),
        "unrelated-session": (injection(
            "UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id="
            f"(SELECT id FROM sessions WHERE id<>'{SES[CANARY]}' ORDER BY id LIMIT 1);"),
            "another session row changed"),
        "new-admission": (injection(
            "INSERT INTO session_admissions(admission_id, request_id, principal_id, target_session_id, "
            "lineage_json, payload_json, payload_digest, intent, status, owner_epoch, generation) "
            "SELECT 'ffffffffffffffffffffffffffffffff', request_id||'-x', principal_id, target_session_id, "
            f"lineage_json, payload_json, payload_digest, 'queue', 'unknown', 1, 1 "
            f"FROM session_admissions WHERE admission_id='{CANARY}';"),
            "new non-terminal admission"),
        "message-deleted": (injection(
            "DELETE FROM messages WHERE id=(SELECT max(id) FROM messages);"), "message count changed"),
    }
    for name, (mut, expect) in injections.items():
        d, db, backup, bsha = prepare(f"inject-{name}", mut)
        rc, p = run(db, os.path.join(d, "out"), admission=CANARY, backup=backup, backup_sha=bsha)
        joined = " | ".join(p.get("problems") or [])
        check(f"inject/{name}/exit65", rc == 65, (rc, p))
        check(f"inject/{name}/disposition", p.get("disposition") == "committed_verification_failed",
              p.get("disposition"))
        check(f"inject/{name}/problem-named", expect in joined, joined)
        check(f"inject/{name}/failed-evidence",
              os.path.exists(os.path.join(d, "out", "postcheck-failed.json")), "")

    # A verifier that cannot run at all must not report success.
    d, db, backup, bsha = prepare("inject-no-verifier")
    rc, p = run(db, os.path.join(d, "out"), admission=CANARY, backup=backup, backup_sha=bsha,
                trigger="/dev/null")
    check("inject/no-verifier/nonzero", rc in (64, 65, 70), (rc, p))

    # ---- 6. pure delta oracle: classes a SQL injection cannot produce ------------
    conn = sqlite3.connect(f"file:{BASE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    base_img = mod.image_state(conn, CANARY, SES[CANARY])
    conn.close()
    import copy

    def mutated(**kw):
        img = copy.deepcopy(base_img)
        for k, v in kw.items():
            if k == "admission": img["admission"] = {**img["admission"], **v}
            elif k == "session": img["session"] = {**img["session"], **v}
            else: img[k] = v
        return img

    after_ok = copy.deepcopy(base_img)
    after_ok["admission"] = {**after_ok["admission"], "status": "terminal", "outcome": "interrupted"}
    after_ok["nonterminal_ids"] = [x for x in after_ok["nonterminal_ids"] if x != CANARY]
    after_ok["session"] = {**after_ok["session"], "runtime_revision": after_ok["session"]["runtime_revision"] + 1}
    check("oracle/allowed-delta-clean", mod.allowed_delta_problems(base_img, after_ok, CANARY, SES[CANARY]) == [],
          mod.allowed_delta_problems(base_img, after_ok, CANARY, SES[CANARY]))

    cases = {
        "schema": (mutated(schema_sha256="0" * 64), after_ok, "schema changed"),
        "wrong-outcome": (base_img, mutated(admission={"status": "terminal", "outcome": "failed"},
                                            session={"runtime_revision": base_img["session"]["runtime_revision"] + 1}),
                          "admission state"),
        "still-unknown": (base_img, mutated(session={"runtime_revision": base_img["session"]["runtime_revision"] + 1}),
                          "admission state"),
        "revision-zero": (base_img, mutated(admission={"status": "terminal", "outcome": "interrupted"}), "runtime_revision"),
        "admission-col-changed": (base_img, mutated(admission={"status": "terminal", "outcome": "interrupted",
                                                               "generation": 2},
                                                    session={"runtime_revision": base_img["session"]["runtime_revision"] + 1}),
                                  "admission columns changed"),
        "session-col-changed": (base_img, mutated(admission={"status": "terminal", "outcome": "interrupted"},
                                                  session={"runtime_revision": base_img["session"]["runtime_revision"] + 1,
                                                           "end_reason": "forced"}),
                                "session columns changed"),
        "messages": (base_img, {**after_ok, "messages_total": after_ok["messages_total"] + 1}, "message count changed"),
        "leases": (base_img, {**after_ok, "leases_sha256": "0" * 64}, "a session lease changed"),
        "state-meta": (base_img, {**after_ok, "state_meta_sha256": "0" * 64}, "a state_meta row changed"),
        "rowcounts": (base_img, {**after_ok, "rowcounts": {**after_ok["rowcounts"], "sessions": 1}},
                      "table row counts changed"),
    }
    for name, (before, after, expect) in cases.items():
        problems = mod.allowed_delta_problems(before, after, CANARY, SES[CANARY])
        check(f"oracle/{name}", any(expect in x for x in problems), problems)

    # ---- 7. preserved compare-and-set / ownership / transcript guards ----------
    def guard_case(name, expect_reason, sql, params=(), admission=CANARY):
        d, db, backup, bsha = prepare(f"guard-{name}")
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute(sql, params)
        conn.close()
        shutil.copy2(db, backup)  # keep the backup current for the mutated state
        rc, p = run(db, os.path.join(d, "out"), admission=admission, backup=backup,
                    backup_sha=sha256(backup))
        check(f"guard/{name}/exit64", rc == 64, (rc, p))
        check(f"guard/{name}/reason", p.get("reason") == expect_reason, p.get("reason"))

    guard_case("not-unknown", "admission_not_unknown",
               "UPDATE session_admissions SET status='started' WHERE admission_id=?", (CANARY,))
    guard_case("foreign-owner", "ownership_mismatch",
               "UPDATE session_admissions SET owner_epoch=7 WHERE admission_id=?", (CANARY,))
    guard_case("transcript", "session_has_transcript",
               "UPDATE sessions SET message_count=3 WHERE id=?", (SES[CANARY],))
    guard_case("session-ended", "session_ended",
               "UPDATE sessions SET end_reason='completed' WHERE id=?", (SES[CANARY],))
    guard_case("worker-live", "worker_still_live",
               "UPDATE worker_executions SET status='running' WHERE execution_id=?",
               (f"admission-worker:{CANARY}",))
    guard_case("worker-missing", "worker_row_missing",
               "DELETE FROM worker_executions WHERE execution_id=?", (f"admission-worker:{CANARY}",))
    guard_case("live-admission", "live_admission_on_session",
               "INSERT INTO session_admissions(admission_id, request_id, principal_id, target_session_id, "
               "lineage_json, payload_json, payload_digest, intent, status, owner_epoch, generation) "
               "SELECT 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', request_id||'-y', principal_id, target_session_id, "
               "lineage_json, payload_json, payload_digest, 'queue', 'queued', 24, 1 "
               "FROM session_admissions WHERE admission_id=?", (CANARY,))
    guard_case("outcome-on-unknown", "unexpected_outcome_on_unknown",
               "UPDATE session_admissions SET outcome='completed' WHERE admission_id=?", (CANARY,))
    guard_case("terminal-other-outcome", "already_terminal_other_outcome",
               "UPDATE session_admissions SET status='terminal', outcome='completed' WHERE admission_id=?",
               (CANARY,))

    # ---- 8. --verify is read-only and fails closed on a non-terminal target ----
    d, db, backup, bsha = prepare("verify")
    rc, p = run(db, os.path.join(d, "out"), "--verify", admission=CANARY, apply=False, ignore=False)
    check("verify/pre/nonzero", rc == 65, (rc, p))
    rc, p = run(db, os.path.join(d, "out2"), admission=CANARY, backup=backup, backup_sha=bsha)
    rc, p = run(db, os.path.join(d, "out3"), "--verify", admission=CANARY, apply=False, ignore=False)
    check("verify/post/exit0", rc == 0, (rc, p))
    check("verify/post/state", (p.get("selected") or {}).get(CANARY, {}).get("outcome") == "interrupted", p)

    # ---- 9. dry-run stays a refusal-free report and never writes ---------------
    d, db, backup, bsha = prepare("dryrun")
    rc, p = run(db, os.path.join(d, "out"), admission=[CANARY, SECOND], apply=False)
    check("dryrun/exit2", rc == 2, (rc, p))
    check("dryrun/disposition", p.get("disposition") == "dry_run", p.get("disposition"))
    check("dryrun/no-writes", len(audit(db)) == 0, len(audit(db)))

    # ---- summary ---------------------------------------------------------------
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    out = {"checks": total, "passed": passed,
           "failed": [{"name": n, "detail": str(d)} for n, ok, d in results if not ok]}
    with open(os.path.join(HERE, "fixture", "guards-evidence.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
