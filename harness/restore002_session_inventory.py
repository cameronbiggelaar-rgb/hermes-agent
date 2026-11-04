"""Read-only inventory of open session records, including the eight unresolved ones.

Run:
    env -u PYTHONPATH /usr/bin/python3.13 harness/restore002_session_inventory.py \\
        --candidate <40-hex> [--source-repo PATH] [--json PATH]

Classification is *not* reimplemented here: it calls the candidate's own ``load_facts`` and
``classify`` so the inventory and the planner can never disagree — and so a pre-deployment inventory
works when the installation predates ``gateway/session_resume_plan.py`` (EXT-13A). The module is
loaded from the pinned candidate blob by :func:`release002_control.load_pinned_module`; nothing is
imported from the installed gateway, no ``PYTHONPATH`` entry is consulted and ``sys.path`` is not
extended for it. ``--candidate`` is mandatory unless ``--allow-unpinned-planner`` is passed (a
developer convenience that production callers never use and that is labelled in the output).

The store is opened by the planner with ``mode=ro`` and ``PRAGMA query_only=1``; the only write is
the optional ``--json`` file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import release002_control  # noqa: E402  (pinned-blob loader; no gateway import)

PLANNER_PATH = "gateway/session_resume_plan.py"

DB = "/home/hermes/.hermes/state.db"
INDEX = "/home/hermes/.hermes/sessions/sessions.json"


def _planner(candidate: str | None, source_repo: str, *, allow_unpinned: bool = False) -> dict:
    """Pinned planner module + its identity evidence, or ``{"error": ...}`` (fail closed)."""
    if not candidate:
        if not allow_unpinned:  # pragma: no cover - guarded by argparse
            return {"error": "no --candidate: refusing to fall back to an installed planner"}
        try:
            from gateway.session_resume_plan import classify, load_facts
        except Exception as exc:  # noqa: BLE001 - explicit, labelled developer fallback
            return {"error": f"unpinned planner unavailable: {exc}"}
        return {"ok": True, "source": "unpinned-installed-import",
                "module": type("_P", (), {"classify": staticmethod(classify),
                                          "load_facts": staticmethod(load_facts)})()}
    pin = release002_control.load_pinned_module(source_repo, candidate, PLANNER_PATH,
                                                required=("classify", "load_facts"))
    if not pin.get("ok"):
        return {"error": f"pinned planner unavailable: {pin.get('error')}"}
    return {"ok": True, "source": "pinned-candidate-blob", "module": pin["module"],
            "path": pin["path"], "commit": pin["commit"], "blob": pin["blob"],
            "sha256": pin["sha256"], "repo": pin["repo"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--candidate", default=None,
                    help="candidate commit whose gateway/session_resume_plan.py classifies the inventory")
    ap.add_argument("--source-repo", dest="source_repo", default=None,
                    help="repository holding --candidate (default: this harness's parent repository)")
    ap.add_argument("--allow-unpinned-planner", dest="allow_unpinned", action="store_true",
                    help="developer dry runs only: import the *installed* planner (production never "
                         "passes this; the output is labelled unpinned-installed-import)")
    ap.add_argument("--store", default=DB,
                    help=f"session database to read (default: {DB}); opened read-only, never written")
    ap.add_argument("--index", default=INDEX,
                    help=f"session index JSON (default: {INDEX}); read-only, absent is tolerated")
    ap.add_argument("--summary", action="store_true",
                    help="record bucket counts and per-bucket id digests instead of the full id lists")
    args = ap.parse_args()

    source_repo = args.source_repo or os.path.dirname(os.path.dirname(_HERE))
    pin = _planner(args.candidate, source_repo, allow_unpinned=args.allow_unpinned)
    if not pin.get("ok"):
        print(json.dumps({"ok": False, "failed": ["session_planner"], "error": pin["error"]}, indent=2))
        return 2
    planner = pin["module"]
    classify, load_facts = planner.classify, planner.load_facts
    ACTIVE = getattr(planner, "ACTIVE", "active")
    HUMAN = getattr(planner, "HUMAN", "human")
    OBSOLETE = getattr(planner, "OBSOLETE", "obsolete")
    RESUMABLE = getattr(planner, "RESUMABLE", "resumable")

    now = time.time()
    try:
        facts = load_facts(args.store, args.index)
        buckets = classify(facts, now=now)
    except Exception as exc:  # noqa: BLE001 - unreadable/malformed store is a fail-closed result
        print(json.dumps({"ok": False, "failed": ["session_store"],
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2
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
        "database": args.store,
        "session_index": args.index,
        "planner_source": pin["source"],
        "planner": {k: pin[k] for k in ("path", "commit", "blob", "sha256", "repo") if k in pin},
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
