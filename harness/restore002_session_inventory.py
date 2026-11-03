"""Read-only inventory of open session records, including the eight unresolved ones.

Run:
    env -u PYTHONPATH /usr/bin/python3.13 harness/restore002_session_inventory.py [--json PATH]

Classification is *not* reimplemented here: it calls ``gateway.session_resume_plan.load_facts`` and
``classify`` so the inventory and the planner can never disagree. Opens state.db with mode=ro and
``PRAGMA query_only=1``; the only write is the optional ``--json`` file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.session_resume_plan import (  # noqa: E402  (path bootstrap must run first)
    ACTIVE, HUMAN, OBSOLETE, RESUMABLE, classify, load_facts,
)

DB = "/home/hermes/.hermes/state.db"
INDEX = "/home/hermes/.hermes/sessions/sessions.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--summary", action="store_true",
                    help="record bucket counts and per-bucket id digests instead of the full id lists")
    args = ap.parse_args()

    now = time.time()
    facts = load_facts(DB, INDEX)
    buckets = classify(facts, now=now)
    open_facts = [f for f in facts if f.ended_at is None]
    # The unresolved set the owner asked to preserve: open rows that carry no work at all.
    empty = [f for f in open_facts if f.message_count == 0]
    unresolved = sorted(empty, key=lambda f: f.started_at or 0.0)

    fingerprint = hashlib.sha256(
        "\n".join(
            f"{f.session_id}|{f.started_at}|{f.message_count}|{f.ended_at}"
            for f in sorted(open_facts, key=lambda f: f.session_id)
        ).encode()
    ).hexdigest()

    payload = {
        "generated_at_epoch": now,
        "database": DB,
        "session_index": INDEX,
        "open_sessions": len(open_facts),
        "open_sessions_fingerprint_sha256": fingerprint,
        "counts": {k: len(v) for k, v in buckets.items()},
        "bucket_ids_sha256": {
            k: hashlib.sha256("\n".join(sorted(v)).encode()).hexdigest() for k, v in buckets.items()
        },
        "buckets": None if args.summary else buckets,
        "empty_open": len(empty),
        "unresolved": [
            {
                "id": f.session_id,
                "started_at": f.started_at,
                "last_activity_at": f.last_activity_at,
                "message_count": f.message_count,
                "tool_call_count": None,
                "ended_at": f.ended_at,
                "in_index": f.in_index,
                "routing_session_id": f.routing_session_id,
                "resume_pending": f.resume_pending,
                "resume_reason": f.resume_reason,
                "suspended": f.suspended,
                "bucket": next((k for k, ids in buckets.items() if f.session_id in ids), None),
            }
            for f in unresolved
        ],
    }

    text = json.dumps(payload, indent=2, sort_keys=False)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
