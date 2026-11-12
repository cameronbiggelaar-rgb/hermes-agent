#!/usr/bin/env python3
"""EXT-17B — run the adversarial scenario matrix and collect one evidence record per case.

    python3.13 harness/qualify/ext17b_matrix.py [case ...]

Each case gets its own disposable root under ``<scratch>/ext17b/cases/<case>``, built with the
fixture defect that case is about, driven inside ``ext17b_run.sh`` (the private namespace) by
``ext17b_case.sh``, and then read back from the host: the journal, archive and installation are
bind-mounted out of the namespace, so the evidence survives the namespace's exit without any
process of the qualification run staying behind.

Results are appended to ``<scratch>/ext17b/out/matrix.jsonl`` as one JSON object per case.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
QUALIFY = REPO / "harness" / "qualify"
SCRATCH = Path("/home/hermes/.hermes/cache/scratch/ext17b")
CASES = SCRATCH / "cases"
OUT = SCRATCH / "out"
BASE_HEAD = "3652a3702dae8d2917b91b6949da580efd3ef496"

# case -> build flags. The flags are the *fixture defect*, never a weakened budget: stop_mode=hang
# means the fixture gateway does not stop, and the release must still settle inside its real 300 s.
FLAGS: dict[str, list[str]] = {
    "dirty": ["--dirty"],
    "foreign-freeze": ["--foreign-freeze"],
    "busy": ["--active-agents", "2"],
    # The deployed unit cannot come up once, and only once: the rollback restart must succeed.
    "rollback": ["--fail-start-on", "2"],
    # The deployed unit never comes up again: the rollback cannot be verified, so the run must stop
    # as NEEDS_HUMAN with the freeze held and the journal left open for an operator.
    "needs-human": ["--fail-start-after", "1"],
    "interrupt": ["--stop-mode", "hang"],
    "timeout": ["--stop-mode", "hang"],
    "trigger": [],
}
ORDER = ["dirty", "foreign-freeze", "busy", "rollback", "needs-human", "interrupt", "timeout",
         "trigger"]


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fixture_pids(root: Path) -> list[int]:
    """Pids of the fixture gateway processes started from this case's own installation."""
    out = subprocess.run(
        ["pgrep", "-f", f"{root}/install/venv/bin/python"], capture_output=True, text=True, check=False
    ).stdout
    return [int(pid) for pid in out.split() if pid.isdigit()]


def reap(root: Path) -> bool:
    """Kill whatever the case leaked, then report whether anything survived the kill."""
    for pid in fixture_pids(root):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.5)
    return not fixture_pids(root)


def leftover(root: Path) -> int:
    return len(fixture_pids(root))


def unit_text(unit: Path) -> str:
    """The regenerated unit's body, or an empty string when the case left no unit behind."""
    return unit.read_text(encoding="utf-8", errors="replace") if unit.exists() else ""


def step_logs(root: Path) -> list[Path]:
    """Every per-step transcript the case driver wrote, in name order.

    A case does not always run the same steps — the interrupted case detaches its apply and never
    writes step-apply.log — so the collector reads whatever is there instead of naming two files.
    """
    archive = root / "archive"
    return sorted(archive.glob("step-*.log")) if archive.is_dir() else []


def gates(report: dict[str, object]) -> list[dict[str, object]]:
    """The gate rows of a preflight report, typed so the collector reads them without guessing."""
    rows = report.get("gates")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def collect(case: str, root: Path, log: Path, candidate: str) -> dict[str, object]:
    journal = read_json(root / "var" / "restore-002" / "journal.json")
    raw_release = journal.get("release")
    release = raw_release if isinstance(raw_release, dict) else {}
    install = root / "install"
    unit = root / "systemd" / "hermes-gateway.service"
    dropins = sorted(p.name for p in (root / "systemd" / "hermes-gateway.service.d").glob("*.conf"))
    receipt = root / "archive" / "RECEIPT.md"
    receipt_text = receipt.read_text(encoding="utf-8") if receipt.exists() else ""
    state = read_json(root / "home" / "gateway_state.json")
    steps = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []:
        if line.startswith("CASE="):
            field = dict(part.split("=", 1) for part in line.split(" ") if "=" in part)
            steps[field.get("STEP", "?")] = int(field.get("RC", "-1"))
    before: dict[str, object] = read_json(root / "archive" / "preflight-before.json")
    engine_logs = sorted((root / "logs").glob("hermes-release-002-*.log"))
    engine_text = engine_logs[-1].read_text(encoding="utf-8", errors="replace") if engine_logs else ""
    reasons = [line for line in engine_text.splitlines()
               if "no quiescent window" in line or "interrupting the client" in line]
    return {
        "case": case,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate": candidate,
        "root": str(root),
        "steps": steps,
        "journal": {
            key: journal.get(key)
            for key in ("candidate", "mutated", "phase", "result", "run_id", "restart_attempts")
        },
        "journal_phases": [p.get("phase") for p in journal.get("phases", []) if isinstance(p, dict)],
        "release": {
            key: release.get(key)
            for key in ("deployed", "restored", "unit_sha256", "verified_at", "outcome")
        },
        "install": {
            "head": git(install, "rev-parse", "HEAD") if install.exists() else "",
            "tree": git(install, "rev-parse", "HEAD^{tree}") if install.exists() else "",
            "dirty": len(git(install, "status", "--porcelain").splitlines()) if install.exists() else -1,
        },
        "unit": {
            "present": unit.exists(),
            "sha256": hashlib.sha256(unit.read_bytes()).hexdigest() if unit.exists() else "",
            "dropins": dropins,
            "timeout_stop": next(
                (line.split("=", 1)[1] for line in unit_text(unit).splitlines()
                 if line.startswith("TimeoutStopSec=")), ""),
        },
        "refusal": next(
            (line.strip() for step_log in step_logs(root)
             for line in step_log.read_text(encoding="utf-8", errors="replace").splitlines()
             if "REFUSE" in line or "NO_GO" in line or "no quiescent" in line),
            "",
        ),
        "receipt": {
            "present": bool(receipt_text),
            "status": next((line.split("**")[1] for line in receipt_text.splitlines()
                            if line.startswith("- status:") and "**" in line), ""),
        },
        "gateway": {
            key: state.get(key)
            for key in ("gateway_state", "pid", "active_agents", "recovery_hold", "substitute")
        },
        "preflight": {
            "verdict": before.get("verdict"),
            "failed": [g.get("gate") for g in gates(before) if g.get("ok") is False],
            "detail": next((str(g.get("detail")) for g in gates(before)
                            if g.get("ok") is False and g.get("detail")), ""),
        },
        "settlement": [r.split("] ", 1)[-1] for r in reasons][:3],
        "needs_human": (root / "archive" / "NEEDS_HUMAN.txt").exists(),
        # Nothing from the fixture may outlive its case. The pattern names the fixture's own
        # interpreter rather than the bare root path — a bare path also matches the collector's own
        # command line and would report a phantom leftover — and the count is then taken again after
        # the fixture processes are killed, so the recorded number is what the case actually leaked.
        "leftover": leftover(root),
        "leftover_after_kill": 0 if reap(root) else leftover(root),
        "log": str(log),
    }


def main(argv: list[str]) -> int:
    cases = argv[1:] or ORDER
    candidate = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    OUT.mkdir(parents=True, exist_ok=True)
    CASES.mkdir(parents=True, exist_ok=True)
    records = []
    for case in cases:
        if case not in FLAGS:
            print(f"unknown case {case}", file=sys.stderr)
            return 64
        root = CASES / case
        log = OUT / f"case-{case}.log"
        print(f"== {case}: building {root}", flush=True)
        build = subprocess.run(
            [sys.executable, str(QUALIFY / "ext17b_build.py"), "--root", str(root),
             "--scenario", case, "--candidate", candidate, *FLAGS[case]],
            capture_output=True, text=True, check=False,
        )
        if build.returncode != 0:
            print(build.stderr[-2000:], file=sys.stderr)
            return 1
        print(f"== {case}: driving", flush=True)
        with log.open("w", encoding="utf-8") as handle:
            subprocess.run(
                [str(QUALIFY / "ext17b_run.sh"), str(root), str(QUALIFY / "ext17b_case.sh"), case],
                stdout=handle, stderr=subprocess.STDOUT, check=False,
                env={**os.environ, "EXT17B_CANDIDATE": candidate},
            )
        record = collect(case, root, log, candidate)
        records.append(record)
        with (OUT / "matrix.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        print(json.dumps({k: record[k] for k in ("case", "steps", "journal", "install", "receipt")},
                         indent=1), flush=True)
    print(f"== {len(records)} case(s) recorded in {OUT / 'matrix.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
