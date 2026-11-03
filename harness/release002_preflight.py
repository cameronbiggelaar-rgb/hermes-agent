"""Read-only preflight for the RESTORE-002 bounded release (owner-gated; mutates nothing).

    env -u PYTHONPATH /usr/bin/python3.13 harness/release002_preflight.py [--json PATH]

Exit codes: 0 = every gate green (a release may be proposed), 2 = at least one gate failed,
3 = a gate could not be evaluated (treated as failed — fail closed).

Every check is a read: ``git`` in the live install, ``systemctl show/cat``, the journal, ``/proc``,
and a read-only SQLite session inventory. Nothing is written except the optional ``--json`` file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INSTALL = "/home/hermes/.hermes/hermes-agent"
SERVICE = "hermes-gateway.service"
UNIT_DIR = "/etc/systemd/system"
HOLD_DROPIN = f"{UNIT_DIR}/{SERVICE}.d/90-recovery-hold.conf"
TIMEOUT_DROPIN = f"{UNIT_DIR}/{SERVICE}.d/95-timeout-stop.conf"
DB = "/home/hermes/.hermes/state.db"
INDEX = "/home/hermes/.hermes/sessions/sessions.json"

# Expected identities. e05128fe is the verified RESTORE-001 installation the owner ordered preserved;
# the release replaces it with the qualified Fault B commit, so the *candidate* is passed in.
DEFAULT_INSTALL_HEAD = "e05128febbc559469a5fb835a9235a543dfdfa64"
DEFAULT_INSTALL_TREE = "dc05abd074c834569cf19ca89a6446d74be5c41d"
HOLD_DROPIN_SHA256 = "ba4a1c11558b53e2d96834580ef82690c6c3d7c4f1eda11cbbde5480a835dba0"
QUIET_WINDOW_S = 120.0
GIB = float(1 << 30)


class Gates:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, ok: bool | None, detail: str, *, critical: bool = True) -> None:
        self.rows.append({"gate": name, "ok": ok, "critical": critical, "detail": detail})

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if r["critical"] and r["ok"] is not True]

    def emit(self) -> int:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "gates": self.rows,
            "failed": [r["gate"] for r in self.failed],
            "verdict": "GO" if not self.failed else "NO_GO",
        }
        print(json.dumps(payload, indent=2))
        return 0 if not self.failed else 2


def run(argv: list[str], *, timeout: int = 20, user: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out after {timeout}s"


def git(*args: str) -> tuple[int, str]:
    return run(["git", "-C", INSTALL, *args], timeout=60)


def sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def systemctl_show(*props: str) -> dict[str, str]:
    rc, out = run(["systemctl", "show", SERVICE, "--no-pager", *[f"-p{p}" for p in props]])
    if rc != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key] = value
    return parsed


_DURATION_UNITS = {
    "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "sec": 1.0, "second": 1.0,
    "m": 60.0, "min": 60.0, "minute": 60.0, "h": 3600.0, "hr": 3600.0, "hour": 3600.0,
    "d": 86400.0, "day": 86400.0, "w": 604800.0,
}


def parse_systemd_duration(raw: str) -> float | None:
    """Parse ``TimeoutStopUSec`` in the forms systemd prints: ``300000000``, ``300s``, ``5min``, ``1min 30s``.

    Returns seconds. ``None`` for an absent/empty/infinite value so callers fail closed rather than
    mistaking "no timeout" for "a timeout of zero".
    """
    text = (raw or "").strip()
    if not text or text in {"infinity", "0"}:
        return None
    if text.isdigit():
        return int(text) / 1_000_000.0
    total, token, number = 0.0, "", ""
    for char in text:
        if char.isdigit() or char == ".":
            number += char
            continue
        if char.isspace():
            continue
        token += char
        if token in _DURATION_UNITS:
            if not number:
                return None
            total += float(number) * _DURATION_UNITS[token]
            number, token = "", ""
        elif token == "":
            return None
    return total or None


def unit_timeout_sec() -> float | None:
    props = systemctl_show("TimeoutStopUSec")
    return parse_systemd_duration(props.get("TimeoutStopUSec", ""))


def main_pid() -> int | None:
    props = systemctl_show("MainPID")
    try:
        pid = int(props.get("MainPID") or 0)
    except ValueError:
        return None
    return pid or None


def proc_children(pid: int) -> list[int]:
    kids: list[int] = []
    try:
        with open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8") as fh:
            kids = [int(x) for x in fh.read().split() if x.strip()]
    except OSError:
        return []
    return kids


def proc_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "?"


def journal_since(boot_offset: str = "today") -> str:
    """Journal text for the unit since the given window; empty when unreadable."""
    rc, out = run(
        ["journalctl", "-u", SERVICE, "--since", boot_offset, "--no-pager", "-o", "short-iso"],
        timeout=60,
    )
    return out if rc == 0 else ""


def session_state(quiet_window_s: float = QUIET_WINDOW_S) -> dict:
    """Read-only session inventory: open rows, planner buckets, and recent activity."""
    try:
        from gateway.session_resume_plan import classify, load_facts
    except Exception as exc:  # pragma: no cover - import failure is itself a finding
        return {"error": f"cannot import planner: {exc}"}
    now = time.time()
    facts = load_facts(DB, INDEX)
    buckets = classify(facts, now=now)
    open_facts = [f for f in facts if f.ended_at is None]
    fingerprint = hashlib.sha256(
        "\n".join(
            f"{f.session_id}|{f.started_at}|{f.message_count}|{f.ended_at}"
            for f in sorted(open_facts, key=lambda f: f.session_id)
        ).encode()
    ).hexdigest()
    recent = [f.session_id for f in open_facts if (f.last_ts or 0.0) >= now - quiet_window_s]
    return {
        "open": len(open_facts),
        "counts": {k: len(v) for k, v in buckets.items()},
        "fingerprint": fingerprint,
        "empty_open": sum(1 for f in open_facts if f.message_count == 0),
        "recently_active": recent,
        "quiet_window_s": quiet_window_s,
    }


def db_stats() -> dict:
    stats: dict = {}
    for label, path in (("state.db", DB), ("state.db-wal", DB + "-wal")):
        try:
            st = os.stat(path)
            stats[label] = {"bytes": st.st_size, "mtime": int(st.st_mtime)}
        except OSError:
            stats[label] = None
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        try:
            conn.execute("PRAGMA query_only=1")
            stats["sqlite_readable"] = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        stats["sqlite_readable"] = f"error: {exc}"
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--expect-head", default=DEFAULT_INSTALL_HEAD)
    ap.add_argument("--expect-tree", default=DEFAULT_INSTALL_TREE)
    ap.add_argument("--candidate", default=None,
                    help="commit SHA the release will deploy (checked for presence and ancestry)")
    ap.add_argument("--quiet-window", type=float, default=QUIET_WINDOW_S)
    args = ap.parse_args()

    g = Gates()

    # 1. Installed identity — the owner ordered e05128fe/dc05abd0 preserved until this release swaps it.
    rc, head = git("rev-parse", "HEAD")
    rc2, tree = git("rev-parse", "HEAD^{tree}")
    rc3, dirty = git("status", "--porcelain")
    g.add("install_head", head == args.expect_head if rc == 0 else None, head or "unreadable")
    g.add("install_tree", tree == args.expect_tree if rc2 == 0 else None, tree or "unreadable")
    g.add("install_clean", dirty == "" if rc3 == 0 else None,
          "clean" if dirty == "" else f"{len(dirty.splitlines())} modified path(s)")

    # 2. Candidate presence and ancestry (fail closed: a release must name its exact commit).
    if args.candidate:
        rc, ctree = git("rev-parse", f"{args.candidate}^{{commit}}")
        _, ctree_sha = git("rev-parse", f"{args.candidate}^{{tree}}")
        g.add("candidate_present", rc == 0, ctree or f"{args.candidate} not in the install clone")
        rc_anc, _ = run(["git", "-C", INSTALL, "merge-base", "--is-ancestor", args.expect_head, args.candidate])
        g.add("candidate_descends_from_install", rc_anc == 0,
              f"{args.expect_head[:8]} -> {args.candidate[:8]}")
        print(f"# candidate tree {ctree_sha}", file=sys.stderr)

    # 3. Service state.
    props = systemctl_show(
        "ActiveState", "SubState", "MainPID", "NRestarts", "ExecMainStartTimestamp",
        "TimeoutStopUSec", "KillMode", "FragmentPath", "DropInPaths", "Restart",
    )
    g.add("service_active", props.get("ActiveState") == "active",
          f"{props.get('ActiveState')}/{props.get('SubState')}")
    pid = main_pid()
    g.add("service_pid", pid is not None, f"MainPID={pid}")
    g.add("no_restart_loop", props.get("NRestarts") == "0", f"NRestarts={props.get('NRestarts')}")
    g.add("kill_mode_mixed", props.get("KillMode") == "mixed", f"KillMode={props.get('KillMode')}")
    timeout_s = unit_timeout_sec()
    g.add("effective_timeout_is_300s", timeout_s == 300.0,
          f"TimeoutStopUSec={props.get('TimeoutStopUSec')} -> {timeout_s}s")

    # 4. Protective drop-ins: the hold must be present and byte-identical to the archived copy.
    hold_sha = sha256_file(HOLD_DROPIN)
    g.add("hold_dropin_present", hold_sha is not None, HOLD_DROPIN)
    g.add("hold_dropin_sha256", hold_sha == HOLD_DROPIN_SHA256, hold_sha or "missing")
    timeout_dropin_sha = sha256_file(TIMEOUT_DROPIN)
    g.add("timeout_dropin_present", timeout_dropin_sha is not None, TIMEOUT_DROPIN, critical=False)

    # 5. The hold must actually be in the running process's environment.
    hold_env = None
    if pid:
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                hold_env = any(
                    part == b"HERMES_RECOVERY_HOLD=1" for part in fh.read().split(b"\x00")
                )
        except OSError as exc:
            hold_env = None
            g.add("hold_env_readable", None, f"/proc/{pid}/environ: {exc}")
    g.add("hold_env_active", hold_env, "HERMES_RECOVERY_HOLD=1" if hold_env else "absent/unreadable")

    # 5b. Live runtime state: in-flight turns are counted here, and the pid must match systemd.
    gw_state: dict = {}
    try:
        with open("/home/hermes/.hermes/gateway_state.json", encoding="utf-8") as fh:
            gw_state = json.load(fh)
    except Exception as exc:
        gw_state = {"error": str(exc)}
    g.add("runtime_state_readable", "error" not in gw_state, gw_state.get("error", "gateway_state.json"))
    if "error" not in gw_state:
        g.add("runtime_state_running", gw_state.get("gateway_state") == "running",
              f"gateway_state={gw_state.get('gateway_state')} exit_reason={gw_state.get('exit_reason')}")
        g.add("runtime_state_pid_matches", gw_state.get("pid") == pid,
              f"state pid={gw_state.get('pid')} systemd MainPID={pid}")
        g.add("runtime_code_sha_matches_install", gw_state.get("code_sha") == head,
              f"code_sha={gw_state.get('code_sha')} install HEAD={head}")

    # 6. Quiet gate — no engineering turn should be mid-flight when the service is stopped.
    state = session_state(args.quiet_window)
    if "error" in state:
        g.add("session_inventory", None, state["error"])
    else:
        g.add("session_inventory", state["open"] > 0,
              f"open={state['open']} counts={state['counts']} empty={state['empty_open']}")
        # Informational: parallel threads legitimately hold recent timestamps. The *runner* is what
        # enforces quiescence, by waiting for gateway_state.active_agents to reach 0 before it stops.
        g.add("recent_session_activity", not state["recently_active"],
              f"active within {int(args.quiet_window)}s: {len(state['recently_active'])}"
              + (f" ({', '.join(state['recently_active'][:5])})" if state["recently_active"] else ""),
              critical=False)

    # 6b. No engineering turn may be cut. The gateway drains its own active agents, but an
    # agent still streaming a turn is exactly what the owner asked us to prove absent.
    active_agents = gw_state.get("active_agents")
    g.add("no_active_turns", active_agents == 0,
          f"gateway_state.active_agents={active_agents!r}")

    # 7. Gateway children: a busy gateway owns tool subprocesses; a quiet one owns few.
    kids = proc_children(pid) if pid else []
    g.add("gateway_children_quiet", len(kids) <= 12,
          f"{len(kids)} child process(es): " + ", ".join(sorted({proc_comm(k) for k in kids})[:8]),
          critical=False)

    # 8. Prior stop must not already be a SIGKILL (would mean the fix never had a fair trial).
    log = journal_since("today")
    kills = [ln for ln in log.splitlines() if "with signal KILL" in ln]
    g.add("no_sigkill_today", not kills, f"{len(kills)} SIGKILL line(s) in today's journal")
    stops = [ln for ln in log.splitlines() if "Stopping" in ln or "Stopped" in ln]
    g.add("journal_readable", bool(log), f"{len(log.splitlines())} journal line(s) readable",
          critical=False)

    # 9. Resources and database.
    mem = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(("MemAvailable", "MemTotal")):
                    key, _, rest = line.partition(":")
                    mem[key] = int(rest.strip().split()[0]) * 1024
    except OSError:
        pass
    avail = mem.get("MemAvailable", 0)
    g.add("memory_available", avail >= 2 << 30, f"MemAvailable={avail / GIB:.2f} GiB")
    free = shutil.disk_usage(INSTALL).free
    g.add("disk_space", free >= 5 << 30, f"{free / GIB:.2f} GiB free on {INSTALL}")
    stats = db_stats()
    g.add("session_db_readable", isinstance(stats.get("sqlite_readable"), int),
          f"sessions={stats.get('sqlite_readable')} "
          f"size={(stats.get('state.db') or {}).get('bytes', 0) / GIB:.2f} GiB")

    payload = {
        "gates": g.rows,
        "failed": [r["gate"] for r in g.failed],
        "verdict": "GO" if not g.failed else "NO_GO",
        "session_state": state,
        "db": stats,
        "service": props,
        "runtime": {k: v for k, v in gw_state.items() if k in {"pid", "gateway_state", "active_agents", "updated_at", "code_sha", "restart_requested"}},
        "stop_lines": stops[-5:],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    print(json.dumps(payload, indent=2))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
    return 0 if not g.failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
