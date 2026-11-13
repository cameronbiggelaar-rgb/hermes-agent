#!/usr/bin/env python3.13
"""Build a disposable ADM-RECON-01 fixture: the real DDL of the nine tables the
reconciliation touches, plus the three target rows verbatim and a set of decoys that
must survive untouched.

Source is opened read-only; the fixture is a new file. Never writes to production.
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = "file:/home/hermes/.hermes/state.db?mode=ro"
TABLES = ["runtime_epoch", "sessions", "session_admissions", "worker_executions",
          "worker_receipts", "state_meta", "messages", "session_turn_leases"]
TARGET_ADM = ["40a8ac83611e428696d5fee98e4bc599", "faf7f3066aca401d8b5c309c5d2f397d",
              "e7aa5bc9b3204511846ad644a3279063"]
TARGET_SES = ["local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901",
              "local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5",
              "local-efa627bea17ff79e9e8267f617e9f10b7eba2a3586e9092420510b8245b44187"]


def copy_table(dst, src, table, where=None, params=()):
    cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
    sql = f'SELECT {",".join(cols)} FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    rows = src.execute(sql, params).fetchall()
    if not rows:
        return 0
    dst.executemany(f'INSERT OR IGNORE INTO "{table}"({",".join(cols)}) VALUES({",".join("?" * len(cols))})',
                    [tuple(r) for r in rows])
    return len(rows)


def main(out):
    if os.path.exists(out):
        os.unlink(out)
    src = sqlite3.connect(SRC, uri=True, timeout=30)
    dst = sqlite3.connect(out, isolation_level=None)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA foreign_keys=OFF")

    for (name, sql) in src.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"):
        if name in TABLES:
            dst.execute(sql)

    copy_table(dst, src, "runtime_epoch")

    # decoy sessions: one busy terminal session with transcript, one live session
    decoys = src.execute(
        "SELECT id FROM sessions WHERE source='oneshot' AND message_count>0 AND id != ? "
        "ORDER BY started_at DESC LIMIT 1", (TARGET_SES[0],)).fetchall()
    decoy_ids = [r[0] for r in decoys]
    live = src.execute("SELECT id FROM sessions WHERE message_count=0 AND id NOT IN (?,?,?) "
                       "ORDER BY started_at DESC LIMIT 1", tuple(TARGET_SES)).fetchall()
    decoy_ids += [r[0] for r in live]

    for sid in TARGET_SES:
        copy_table(dst, src, "sessions", "id=?", (sid,))
    for sid in decoy_ids:
        copy_table(dst, src, "sessions", "id=?", (sid,))
    for aid in TARGET_ADM:
        copy_table(dst, src, "session_admissions", "admission_id=?", (aid,))
    for sid in TARGET_SES + decoy_ids:
        copy_table(dst, src, "worker_executions", "session_id=?", (sid,))
        copy_table(dst, src, "messages", "session_id=?", (sid,))
    # decoy admissions: real terminal rows from other epochs, plus a live one in the current epoch
    seqs = [r[0] for r in src.execute(
        "SELECT seq FROM session_admissions WHERE status='terminal' AND seq > 3000 ORDER BY seq LIMIT 4")]
    for s in seqs:
        copy_table(dst, src, "session_admissions", "seq=?", (s,))
        row = src.execute("SELECT target_session_id FROM session_admissions WHERE seq=?", (s,)).fetchone()
        copy_table(dst, src, "sessions", "id=?", (row[0],))
        copy_table(dst, src, "worker_executions", "session_id=?", (row[0],))
    copy_table(dst, src, "state_meta", "key LIKE 'gateway.%' LIMIT 5")
    dst.execute("INSERT OR IGNORE INTO session_turn_leases(conversation_id,holder,acquired_at,expires_at) "
                "VALUES(?,?,?,?)", (decoy_ids[0], "fixture-holder", 1.0, 9e9))

    dst.execute("PRAGMA foreign_keys=ON")
    dst.commit()
    counts = {t: dst.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in TABLES}
    print("fixture", out, counts)
    for t in ("session_admissions", "sessions"):
        print(t, dst.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
    dst.close()
    src.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fixture/small.db")
