#!/usr/bin/env python3
"""Qualification-only gateway substitute for the EXT-17B disposable release environment.

This process is *not* the Hermes gateway. It is the smallest stand-in that keeps the contract the
release actually depends on, so that a real assembled launcher -> supervisor -> engine run can be
exercised without a live gateway or production state:

  * it publishes ``gateway_state.json`` (state, pid, code_sha, heartbeat, active_agents);
  * it reads the drain marker and the ESTOP sentinel through the *deployed* modules when they can be
    imported, so the freeze the engine writes is observed exactly where a real gateway observes it;
  * it reports ``draining`` while the marker is present and ``running`` when it is gone;
  * it stops promptly on SIGTERM, and can be told (scenario config) to die from SIGKILL instead, or
    to ignore SIGTERM, which is how the restart-verdict and timeout paths are exercised.

Nothing here writes outside ``HERMES_HOME`` and the scenario state directory.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, UTC
from pathlib import Path

STATE_DIR = Path(os.environ["EXT17B_STATE"])
HOME = Path(os.environ["HERMES_HOME"])
INSTALL = Path(os.environ["EXT17B_INSTALL"])
CONFIG = STATE_DIR / "config.json"
LOG = STATE_DIR / "gateway.out"


def cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def head_of(path: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=False,
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def marker_paths() -> tuple[Path, Path]:
    """The paths the deployed code uses, when it can be imported; else the documented literals."""
    try:
        sys.path.insert(0, str(INSTALL))
        import agent.estop as estop_mod
        import gateway.drain_control as drain_mod

        drain = Path(drain_mod.drain_request_path())
        estop = Path(getattr(estop_mod, "estop_path", lambda: HOME / "ESTOP")())
        return drain, estop
    except Exception:
        return HOME / ".drain_request.json", HOME / "ESTOP"


def state_path() -> Path:
    return HOME / "gateway_state.json"


def sessions_open() -> int:
    db = HOME / "state.db"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return 0
    try:
        row = conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, TypeError, ValueError):
        return 0
    finally:
        conn.close()


def publish(state: str) -> None:
    body = {
        "gateway_state": state,
        "pid": os.getpid(),
        "code_sha": head_of(INSTALL),
        "updated_at": now_iso(),
        "active_agents": 0,
        "open_sessions": sessions_open(),
        "exit_reason": None,
        "substitute": True,
        # The release preflight reads the hold out of /proc/<pid>/environ. Inside a user namespace
        # a sibling process cannot be ptraced (Yama applies and namespace root lacks
        # CAP_SYS_PTRACE in the initial namespace), so the substitute publishes the same fact it
        # was started with. Only the qualification preflight consults this field.
        "recovery_hold": os.environ.get("HERMES_RECOVERY_HOLD") == "1",
    }
    tmp = state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_path())


def main() -> int:
    mode = str(cfg().get("stop_mode", "clean"))
    drain, estop = marker_paths()
    log = open(LOG, "a", encoding="utf-8")

    def on_term(_signum, _frame):
        if mode == "hang":
            log.write(f"{now_iso()} gateway: ignoring SIGTERM (scenario stop_mode=hang)\n")
            log.flush()
            return
        if mode == "sigkill":
            log.write(f"{now_iso()} gateway: dying from SIGKILL (scenario stop_mode=sigkill)\n")
            log.flush()
            os.kill(os.getpid(), signal.SIGKILL)
            return
        publish("stopped")
        log.write(f"{now_iso()} gateway: clean stop\n")
        log.flush()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    log.write(f"{now_iso()} gateway substitute started pid={os.getpid()} home={HOME}\n")
    log.flush()
    last = ""
    deadline = time.time() + 30
    while not state_path().exists() and time.time() < deadline:
        publish("starting")
        time.sleep(0.2)
    while True:
        frozen = drain.exists() and estop.exists()
        state = "draining" if frozen else "running"
        if state != last:
            log.write(f"{now_iso()} gateway: state={state} drain={drain.exists()} estop={estop.exists()}\n")
            log.flush()
            last = state
        publish(state)
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
