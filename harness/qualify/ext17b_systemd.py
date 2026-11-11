#!/usr/bin/env python3
"""Qualification-only systemctl / systemd-run / journalctl substitute (EXT-17B).

The release path must be exercised *as assembled*, including the systemd interactions the engine
performs: ``systemctl show`` property reads, ``daemon-reload``, one timed ``restart``, the transient
``systemd-run`` supervisor unit with its runtime cap, and ``journalctl`` evidence for the stop.

This substitute implements exactly those interactions against a scenario directory. It never talks to
the host's systemd and never touches a unit under the real ``/etc/systemd/system``: inside the
disposable environment that directory *is* a private tmpfs, and the unit it parses is the one the
deployed code regenerated there.

Properties are derived from the unit file and its drop-ins as systemd would derive them (last
assignment wins, drop-ins after the unit), so a wrong TimeoutStopSec in the fixture is detected by the
engine rather than hidden by the shim.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, UTC
from pathlib import Path

STATE_DIR = Path(os.environ["EXT17B_STATE"])
SERVICE = os.environ.get("EXT17B_SERVICE", "hermes-gateway.service")
UNIT_DIR = Path(os.environ.get("EXT17B_UNIT_DIR", "/etc/systemd/system"))
HOME = Path(os.environ["HERMES_HOME"])
INSTALL = Path(os.environ["EXT17B_INSTALL"])
SUBSTITUTE = Path(os.environ["EXT17B_SUBSTITUTE"])
LOG = STATE_DIR / "gateway.log"
CONFIG = STATE_DIR / "config.json"
SVC_STATE = STATE_DIR / "service.json"
UNITS_STATE = STATE_DIR / "units.json"

_TS = "%Y-%m-%dT%H:%M:%S+0000"


def cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(path: Path, body) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def svc() -> dict:
    return load(SVC_STATE, {"active": False, "pid": 0, "start_ts": None, "nrestarts": 0,
                            "failed": False})


def alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # Reap our own child if it exited, so a dead substitute is never reported active.
    try:
        done, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    if done == pid:
        state = svc()
        state["active"] = False
        state["pid"] = 0
        state["exit_status"] = status
        save(SVC_STATE, state)
        return False
    return True


def journal(line: str) -> None:
    stamp = datetime.now(UTC).strftime(_TS)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} qualification-host systemd[1]: {line}\n")


def unit_files() -> list[Path]:
    files = [UNIT_DIR / SERVICE]
    dropins = UNIT_DIR / f"{SERVICE}.d"
    if dropins.is_dir():
        files.extend(sorted(dropins.glob("*.conf")))
    return files


def unit_assignments() -> dict[str, str]:
    """Every ``Key=Value`` from the unit followed by its drop-ins; last assignment wins."""
    out: dict[str, str] = {}
    for path in unit_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";", "[")) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


def duration_us(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text) * 1_000_000)
    units = {"us": 1, "ms": 1000, "s": 1_000_000, "sec": 1_000_000, "m": 60_000_000,
             "min": 60_000_000, "h": 3_600_000_000}
    total, number, token = 0, "", ""
    for char in text:
        if char.isdigit() or char == ".":
            number += char
            continue
        if char.isspace():
            continue
        token += char
        if token in units:
            total += int(float(number) * units[token])
            number, token = "", ""
        else:
            continue
    return str(total) if total else ""


def properties() -> dict[str, str]:
    state = svc()
    assign = unit_assignments()
    pid = int(state.get("pid") or 0)
    running = bool(state.get("active")) and alive(pid)
    dropins = [str(p) for p in unit_files()[1:]]
    env = assign.get("Environment", "")
    if "HERMES_RECOVERY_HOLD=1" not in env:
        env = (env + " HERMES_RECOVERY_HOLD=1").strip()
    start = state.get("start_ts") or ""
    return {
        "Id": SERVICE,
        "LoadState": "loaded" if (UNIT_DIR / SERVICE).exists() else "not-found",
        "ActiveState": "active" if running else ("failed" if state.get("failed") else "inactive"),
        "SubState": "running" if running else ("failed" if state.get("failed") else "dead"),
        "MainPID": str(pid if running else 0),
        "ControlGroup": f"/system.slice/{SERVICE}",
        "NRestarts": str(state.get("nrestarts", 0)),
        "FragmentPath": str(UNIT_DIR / SERVICE),
        "DropInPaths": " ".join(dropins),
        "TimeoutStopUSec": duration_us(assign.get("TimeoutStopSec")),
        "KillMode": assign.get("KillMode", "control-group"),
        "Restart": assign.get("Restart", "no"),
        "Environment": env,
        "ExecMainStartTimestamp": start,
        "ExecMainPID": str(pid if running else 0),
    }


def stop_service() -> int:
    state = svc()
    pid = int(state.get("pid") or 0)
    if not pid or not alive(pid):
        state.update({"active": False, "pid": 0})
        save(SVC_STATE, state)
        return 0
    timeout = int(cfg().get("stop_timeout_override") or 0)
    if not timeout:
        raw = properties().get("TimeoutStopUSec") or "90000000"
        timeout = max(1, int(raw) // 1_000_000)
    journal(f"Stopping {SERVICE.replace('.service', '')}...")
    signal_sent = time.time()
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        os.kill(pid, signal.SIGTERM)
    killed = False
    while time.time() - signal_sent < timeout:
        if not alive(pid):
            break
        time.sleep(0.05)
    if alive(pid):
        killed = True
        journal(f"{SERVICE}: Main process exited, code=killed, status=9/KILL")
        journal(f"{SERVICE}: Killing process {pid} with signal KILL")
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            os.kill(pid, signal.SIGKILL)
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.05)
    elapsed = time.time() - signal_sent
    journal(f"Stopped {SERVICE}." if not killed else
            f"{SERVICE}: Main process exited, code=killed, status=9/KILL")
    state.update({"active": False, "pid": 0, "last_stop_sec": round(elapsed, 3),
                  "killed": killed})
    save(SVC_STATE, state)
    return 0


def interpreter() -> str:
    """The interpreter the unit's ExecStart names: the installation's own venv.

    ``sys.executable`` is not usable here — on this host ``/usr/bin/python3.13`` is a launcher that
    may report no executable path at all — and the release's own provenance checks expect the
    runtime to come from the installation under test.
    """
    return str(INSTALL / "venv" / "bin" / "python")


def start_service() -> int:
    state = svc()
    if state.get("active") and alive(int(state.get("pid") or 0)):
        return 0
    if cfg().get("fail_start"):
        journal(f"{SERVICE}: Failed to start the Hermes Agent Gateway (scenario fail_start).")
        state.update({"active": False, "pid": 0, "failed": True})
        save(SVC_STATE, state)
        return 1
    log = open(STATE_DIR / "gateway.service.log", "ab")
    env = {k: v for k, v in os.environ.items() if not k.startswith("EXT17B_")}
    env.update({"HERMES_HOME": str(HOME), "EXT17B_STATE": str(STATE_DIR),
                "EXT17B_INSTALL": str(INSTALL), "HERMES_RECOVERY_HOLD": "1"})
    env.pop("PYTHONPATH", None)
    proc = subprocess.Popen([interpreter(), str(SUBSTITUTE)], cwd=str(INSTALL), env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.time() + 30
    published = False
    while time.time() < deadline:
        try:
            body = json.loads((HOME / "gateway_state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            body = {}
        if body.get("pid") == proc.pid and body.get("gateway_state") in {"running", "starting", "draining"}:
            published = True
            break
        time.sleep(0.1)
    if not published:
        journal(f"{SERVICE}: gateway substitute did not publish state within 30s")
        return 1
    stamp = datetime.now(UTC).strftime("%a %Y-%m-%d %H:%M:%S UTC")
    state.update({"active": True, "pid": proc.pid, "start_ts": stamp, "failed": False,
                  "nrestarts": 0})
    save(SVC_STATE, state)
    journal(f"Started {SERVICE.replace('.service', '')}.")
    return 0


def cmd_systemctl(argv: list[str]) -> int:
    if not argv:
        print("systemctl: missing verb", file=sys.stderr)
        return 1
    verb = argv[0]
    if verb == "daemon-reload":
        unit_assignments()
        return 0
    if verb == "reset-failed":
        state = svc()
        state["failed"] = False
        save(SVC_STATE, state)
        return 0
    if verb == "list-units":
        return 0
    if verb == "is-active":
        props = properties()
        print(props["ActiveState"])
        return 0 if props["ActiveState"] == "active" else 3
    if verb == "restart":
        stop_service()
        rc = start_service()
        journal(f"{SERVICE}: restart completed rc={rc}")
        return rc
    if verb == "stop":
        return stop_service()
    if verb == "start":
        return start_service()
    if verb == "show":
        wanted: list[str] = []
        value_only = False
        for arg in argv[1:]:
            if arg == "--value":
                value_only = True
            elif arg.startswith("-p") and len(arg) > 2:
                wanted.extend(arg[2:].split(","))
            elif arg.startswith("--property="):
                wanted.extend(arg.split("=", 1)[1].split(","))
        props = properties()
        if value_only and len(wanted) == 1:
            print(props.get(wanted[0], ""))
            return 0
        names = wanted or list(props)
        for name in names:
            print(f"{name}={props.get(name, '')}")
        return 0
    print(f"systemctl: unsupported verb {verb}", file=sys.stderr)
    return 1


def cmd_journalctl(argv: list[str]) -> int:
    since = None
    for index, arg in enumerate(argv):
        if arg == "--since" and index + 1 < len(argv):
            since = argv[index + 1]
        elif arg.startswith("--since="):
            since = arg.split("=", 1)[1]
    try:
        text = LOG.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if since and since not in {"today", "yesterday"}:
        try:
            bound = datetime.fromisoformat(since).timestamp()
        except ValueError:
            bound = None
        if bound is not None:
            kept = []
            for line in text.splitlines():
                try:
                    stamp = datetime.strptime(line[:24], _TS).replace(
                        tzinfo=UTC).timestamp()
                except (ValueError, IndexError):
                    kept.append(line)
                    continue
                if stamp >= bound:
                    kept.append(line)
            text = "\n".join(kept) + ("\n" if kept else "")
    sys.stdout.write(text)
    return 0


def parse_run(argv: list[str]) -> tuple[dict, list[str]]:
    spec = {"unit": "", "runtime_max": 0, "env": {}, "cwd": None, "stdout": None,
            "wait": False, "properties": {}}
    command: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            command = argv[index + 1:]
            break
        if arg in {"--wait", "--pipe", "--collect", "--no-block", "--quiet"}:
            if arg in {"--wait", "--pipe"}:
                spec["wait"] = True
            index += 1
            continue
        if arg.startswith("--unit="):
            spec["unit"] = arg.split("=", 1)[1]
        elif arg.startswith("--property="):
            key, _, value = arg.split("=", 1)[1].partition("=")
            spec["properties"][key] = value
            if key == "RuntimeMaxSec":
                match = re.match(r"[\d.]+", value)
                spec["runtime_max"] = int(float(match.group(0))) if match else 0
            elif key == "WorkingDirectory":
                spec["cwd"] = value
            elif key == "StandardOutput" and value.startswith("append:"):
                spec["stdout"] = value.split("append:", 1)[1]
        elif arg.startswith("--setenv="):
            key, _, value = arg.split("=", 1)[1].partition("=")
            spec["env"][key] = value
        elif arg.startswith("-") and not command:
            pass
        else:
            command = argv[index:]
            break
        index += 1
    return spec, command


def cmd_systemd_run(argv: list[str]) -> int:
    spec, command = parse_run(argv)
    if not command:
        print("systemd-run: no command", file=sys.stderr)
        return 1
    env = dict(os.environ)
    env.update(spec["env"])
    out = open(spec["stdout"], "ab") if spec["stdout"] else subprocess.DEVNULL
    units = load(UNITS_STATE, {})
    proc = subprocess.Popen(command, env=env, cwd=spec["cwd"], stdout=out,
                            stderr=subprocess.STDOUT, start_new_session=True)
    units[spec["unit"] or f"run-{proc.pid}"] = {
        "pid": proc.pid, "runtime_max": spec["runtime_max"],
        "started": datetime.now(UTC).strftime(_TS),
        "properties": spec["properties"], "command": command}
    save(UNITS_STATE, units)
    if spec["wait"]:
        try:
            return proc.wait(timeout=spec["runtime_max"] + 600 if spec["runtime_max"] else None)
        except subprocess.TimeoutExpired:
            return 124
    if spec["runtime_max"]:
        subprocess.Popen([interpreter(), __file__, "watchdog", "--pid", str(proc.pid),
                          "--max", str(spec["runtime_max"]), "--unit", spec["unit"] or ""],
                         start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return 0


def cmd_watchdog(argv: list[str]) -> int:
    opts = dict(zip(argv[::2], argv[1::2]))
    pid = int(opts.get("--pid", "0"))
    limit = float(opts.get("--max", "0"))
    unit = opts.get("--unit", "")
    if not pid or not limit:
        return 2
    time.sleep(limit)
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return 0
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return 0
        time.sleep(0.2)
    journal(f"{unit}: RuntimeMaxSec={limit}s elapsed; sending SIGKILL")
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: ext17b_systemd.py systemctl|systemd-run|journalctl|watchdog ...", file=sys.stderr)
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "systemctl":
        return cmd_systemctl(rest)
    if mode == "systemd-run":
        return cmd_systemd_run(rest)
    if mode == "journalctl":
        return cmd_journalctl(rest)
    if mode == "watchdog":
        return cmd_watchdog(rest)
    print(f"unknown mode {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
