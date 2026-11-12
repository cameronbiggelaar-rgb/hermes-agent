#!/usr/bin/env python3
"""Build one disposable RESTORE-002 release environment (EXT-17B).

Every scenario gets its own installation clone, HERMES_HOME, unit directory, journal and archive.
The environment is *not* privileged on the host: the runner (``ext17b_run.sh``) enters a private
user+mount namespace and mounts throwaway tmpfs filesystems over the production release paths
(``/etc/systemd/system``, ``/usr/local/lib/hermes-release``, ``/var/lib/hermes-release``, ``/var/log``)
before anything runs. Production paths are therefore addressed by the release exactly as they are in
production, while every write lands in the namespace.

Build only touches: the scenario root, and a shared clone of the installation used for speed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, UTC
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROD_INSTALL = Path("/home/hermes/.hermes/hermes-agent")
PROD_VENV_PY = PROD_INSTALL / "venv" / "bin" / "python"
PROD_UNIT = Path("/etc/systemd/system/hermes-gateway.service")
PROD_DROPINS = [
    Path("/etc/systemd/system/hermes-gateway.service.d/90-recovery-hold.conf"),
    Path("/etc/systemd/system/hermes-gateway.service.d/95-timeout-stop.conf"),
]
PRODUCTION_HEAD = "3652a3702dae8d2917b91b6949da580efd3ef496"
PRODUCTION_TREE = "3bfc1c842bf76e8a95356f4f0f3a91e75767992c"
SERVICE = "hermes-gateway.service"
SHIM = "#!/bin/sh\nexec /usr/bin/python3.13 {script} {mode} \"$@\"\n"


def run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(argv, check=False, capture_output=True, text=True, cwd=cwd, timeout=600)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr[-2000:]}")
    return proc.stdout.strip()


def git(root: Path, *args: str) -> str:
    return run(["git", "-C", str(root), *args])


def make_install(root: Path, base: Path, source: Path) -> Path:
    """Clone the production installation into the scenario, from a shared bare mirror of it.

    The clone is detached at the production identity and the identity is re-derived from the clone
    itself, so a wrong or drifted mirror can never be mistaken for the baseline.
    """
    install = root / "install"
    if not base.exists():
        base.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", "--bare", "--shared", str(PROD_INSTALL), str(base)])
    run(["git", "clone", "--quiet", "--shared", str(base), str(install)])
    git(install, "checkout", "--quiet", "--detach", PRODUCTION_HEAD)
    head = git(install, "rev-parse", "HEAD")
    tree = git(install, "rev-parse", "HEAD^{tree}")
    if head != PRODUCTION_HEAD or tree != PRODUCTION_TREE:
        raise SystemExit(f"install clone is not the production identity: {head} / {tree}")
    make_venv(install)
    # The candidate must be present in the installation for stage/plan to pin it; the launcher
    # fetches it itself when absent, which is the behaviour under test.
    git(install, "config", "user.email", "qualification@invalid")
    git(install, "config", "user.name", "EXT-17B qualification")
    return install


def make_venv(install: Path) -> None:
    """Give the fixture installation a real venv whose interpreter lives inside the tree.

    The old-runtime coverage gate resolves every imported release module and fails closed when a
    module or the interpreter comes from outside the installation it is auditing. A symlink to the
    production venv would therefore (correctly) be reported as foreign evidence. Instead the venv is
    created *by* the production interpreter, so ``sys.executable`` is ``<install>/venv/bin/python``,
    and the production site-packages are appended with a ``.pth`` file so the third-party
    dependencies resolve without copying them. Release modules are still imported from the
    installation itself through ``PYTHONPATH``, which is what the gate asserts.
    """
    venv = install / "venv"
    run([str(PROD_VENV_PY), "-m", "venv", "--without-pip", str(venv)])
    fixture_py = str(venv / "bin" / "python")
    probe = subprocess.run([fixture_py, "-c",
                            "import sysconfig, sys;"
                            "print(sysconfig.get_paths()['purelib']);"
                            "print('%d.%d' % sys.version_info[:2])"],
                           check=True, capture_output=True, text=True)
    purelib, version = probe.stdout.strip().splitlines()
    if version != subprocess.run([str(PROD_VENV_PY), "-c",
                                  "import sys; print('%d.%d' % sys.version_info[:2])"],
                                 check=True, capture_output=True, text=True).stdout.strip():
        raise SystemExit(f"fixture venv interpreter {version} is not the production {version}")
    target = Path(purelib)
    target.mkdir(parents=True, exist_ok=True)
    (target / "ext17b-production-deps.pth").write_text(
        subprocess.run([str(PROD_VENV_PY), "-c",
                        "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                       check=True, capture_output=True, text=True).stdout.strip() + "\n",
        encoding="utf-8")
    probe = subprocess.run([fixture_py, "-c", "import ruamel.yaml, psutil, yaml, sqlite3"],
                           check=False, capture_output=True, text=True)
    if probe.returncode != 0:
        raise SystemExit(f"fixture venv cannot import the release dependencies: {probe.stderr[-800:]}")


def make_home(home: Path, code_sha: str) -> None:
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    db = home / "state.db"
    now = time.time()
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, session_key TEXT, source TEXT,
            started_at REAL, last_activity_at REAL, message_count INTEGER, ended_at REAL);
        CREATE TABLE gateway_routing (session_key TEXT PRIMARY KEY, entry_json TEXT);
        CREATE TABLE runtime_epoch (singleton INTEGER PRIMARY KEY, epoch INTEGER);
        CREATE TABLE session_admissions (
            admission_id TEXT PRIMARY KEY, owner_epoch INTEGER, status TEXT, target_session_id TEXT);
        """
    )
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,NULL)",
                 ("fixture-open-1", "telegram:281922", "telegram", now - 7200, now - 3600, 12))
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                 ("fixture-closed-1", "telegram:1", "telegram", now - 9000, now - 8000, 4, now - 8000))
    conn.execute("INSERT INTO runtime_epoch VALUES (1, 1)")
    conn.commit()
    conn.close()
    (home / "sessions" / "sessions.json").write_text(json.dumps({
        "telegram:281922": {"session_id": "fixture-open-1", "source": "telegram",
                            "last_activity_at": now - 3600},
    }, indent=2), encoding="utf-8")
    (home / "gateway_state.json").write_text(json.dumps({
        "gateway_state": "starting", "pid": 0, "code_sha": code_sha,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_agents": 0, "open_sessions": 1, "exit_reason": None, "substitute": True,
    }, indent=2, sort_keys=True), encoding="utf-8")


def make_systemd(root: Path, home: Path) -> Path:
    unit_dir = root / "systemd"
    (unit_dir / f"{SERVICE}.d").mkdir(parents=True, exist_ok=True)
    text = PROD_UNIT.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if line.startswith("Environment=\"HERMES_HOME="):
            lines.append(f'Environment="HERMES_HOME={home}"')
            continue
        if line.startswith("ExecStart=") or line.startswith("ExecStopPost="):
            # The unit's own ExecStart is regenerated by the deployed code (phase "unit"); the seeded
            # value only has to name an interpreter that exists in this installation.
            lines.append(line.split("=", 1)[0] + f"={home}/hermes-agent/venv/bin/python"
                         + (" -m hermes_cli.main gateway run" if line.startswith("ExecStart=") else ""))
            continue
        if line.startswith(("User=", "Group=")):
            continue
        lines.append(line)
    (unit_dir / SERVICE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    for dropin in PROD_DROPINS:
        shutil.copy2(dropin, unit_dir / f"{SERVICE}.d" / dropin.name)
    return unit_dir


def make_shims(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = HERE / "ext17b_systemd.py"
    for name, mode in (("systemctl", "systemctl"), ("systemd-run", "systemd-run"),
                       ("journalctl", "journalctl")):
        path = bin_dir / name
        path.write_text(SHIM.format(script=script, mode=mode), encoding="utf-8")
        path.chmod(0o755)


def env_lines(root: Path, install: Path, home: Path) -> str:
    state = root / "state"
    pairs = {
        "EXT17B_ROOT": root,
        "EXT17B_STATE": state,
        "EXT17B_INSTALL": install,
        "EXT17B_UNIT_DIR": "/etc/systemd/system",
        "EXT17B_SERVICE": SERVICE,
        "EXT17B_SUBSTITUTE": HERE / "ext17b_gateway.py",
        "HOME": home,
        "HERMES_HOME": home,
        "HERMES_RELEASE_INSTALL": install,
        "HERMES_RELEASE_HOME": home,
        "HERMES_RELEASE_REPO": "/home/hermes/work/restore002-fix",
        "HERMES_RELEASE_JOURNAL": "/var/lib/hermes-release/restore-002/journal.json",
        "HERMES_RELEASE_ARCHIVE": root / "archive",
        "ARCHIVE": root / "archive",
        "PATH": f"{root / 'bin'}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
        "TMPDIR": state / "tmp",
    }
    return "".join(f'export {key}="{value}"\n' for key, value in pairs.items())


def build(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    if root.exists():
        shutil.rmtree(root)
    for sub in ("archive", "state", "state/tmp", "logs", "bin", "lib", "var"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    base = Path(args.base).resolve()
    install = make_install(root, base, Path(args.source))
    home = root / "home"
    make_home(home, PRODUCTION_HEAD)
    make_systemd(root, home)
    make_shims(root / "bin")
    (root / "state" / "tmp").mkdir(parents=True, exist_ok=True)
    config = {
        "scenario": args.scenario,
        "stop_mode": args.stop_mode,
        "fail_start": bool(args.fail_start),
        "fail_start_after": args.fail_start_after,
        "fail_start_on": args.fail_start_on,
        "ack_drain": not args.no_ack,
        "stop_timeout_override": args.stop_timeout_override,
        "runtime_max_override": args.runtime_max_override,
        "candidate": args.candidate,
        "active_agents": args.active_agents,
    }
    (root / "state" / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (root / "env.sh").write_text(env_lines(root, install, home), encoding="utf-8")
    if args.foreign_freeze:
        marker = home / ".drain_request.json"
        marker.write_text(json.dumps({
            "principal": "release-freeze:foreign-attempt",
            "requested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "freeze_since": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "suppress_notification": True, "reason": "fixture foreign freeze",
        }, indent=2, sort_keys=True), encoding="utf-8")
    if args.dirty:
        # Any tracked file will do: what the release must refuse is a *dirty installation*, i.e. a
        # worktree that no longer matches the commit it claims to be. The release harness files are
        # only added by `stage`, so dirt one of the files the clone always has.
        target = next(
            (path for path in (install / "AGENTS.md", install / "README.md") if path.exists()),
            None,
        )
        if target is None:
            raise SystemExit("fixture: no tracked file to dirty in the installation clone")
        target.write_text(target.read_text(encoding="utf-8") + "\n# fixture dirt\n", encoding="utf-8")
    if args.estop_foreign:
        (home / "ESTOP").write_text("foreign-attempt\n", encoding="utf-8")
    # The gateway substitute is started *inside* the namespace (step `start`), so its /proc entries
    # are owned by the same mapped user as the release process and the release can read, signal and
    # reap it exactly as systemd would in production.
    (root / "var").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "build.log").write_text(
        f"built {datetime.now(UTC).isoformat()} scenario={args.scenario}\n", encoding="utf-8")
    return root


def read_env(path: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r'export ([A-Z_0-9]+)="(.*)"$', line)
        if match:
            pairs[match.group(1)] = match.group(2)
    return {**os.environ, **pairs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--base", default="/home/hermes/.hermes/cache/scratch/ext17b/base.git")
    parser.add_argument("--source", default="/home/hermes/work/restore002-fix")
    parser.add_argument("--candidate", default="9601cc8a8ec1fc7fb9d534c653cf66e916b50c20")
    parser.add_argument("--scenario", default="clean")
    parser.add_argument("--stop-mode", default="clean", choices=["clean", "hang", "sigkill"])
    parser.add_argument("--fail-start", action="store_true")
    # Fail only the restarts after the first N starts: the deployed unit cannot come up.
    parser.add_argument("--fail-start-after", type=int, default=0)
    # Fail exactly this start and no other, so a rollback restart can still succeed.
    parser.add_argument("--fail-start-on", type=int, default=0)
    parser.add_argument("--no-ack", action="store_true")
    parser.add_argument("--stop-timeout-override", type=int, default=0)
    parser.add_argument("--runtime-max-override", type=int, default=0)
    parser.add_argument("--foreign-freeze", action="store_true")
    parser.add_argument("--estop-foreign", action="store_true")
    parser.add_argument("--dirty", action="store_true")
    # A busy host never presents a quiet window: the release must abort before mutation.
    parser.add_argument("--active-agents", type=int, default=0)
    print(build(parser.parse_args()))
