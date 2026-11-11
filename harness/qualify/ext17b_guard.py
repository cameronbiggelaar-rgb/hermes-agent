#!/usr/bin/env python3
"""Isolation guard for the EXT-17B disposable environment.

The release launcher and engine deliberately name production paths. The only thing that keeps them
away from production is the private user+mount namespace set up by ``ext17b_ns.sh``: every release
path it writes (unit directory, launcher library, journal, supervisor log) is a throwaway tmpfs or a
scenario-local bind mount there.

This guard is the enforcement point. It refuses (exit 64) unless *all* of the following hold:

  1. euid is 0 inside a user namespace whose uid map maps this user to root;
  2. each release path is the private mount, proven by a per-run random sentinel written by the
     namespace script — a token that exists nowhere on the host and cannot be guessed or replayed;
  3. ``systemctl`` resolves to the qualification substitute, not the host's;
  4. the seeded gateway unit names this scenario's HERMES_HOME, not the production home.

Because the sentinel is generated per run and written *inside* the new mount, running the same
command on the host — where the production unit directory is not a fresh tmpfs — can never satisfy
it. That is the negative property the tests assert.

``check()`` is pure so every refusal branch can be exercised on the host without a namespace; the
CLI is a thin wrapper over it.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

RELEASE_PATHS = ["/etc/systemd/system", "/usr/local/lib/hermes-release", "/var/lib/hermes-release",
                 "/var/log"]
SERVICE = "hermes-gateway.service"
SENTINEL_NAME = ".ext17b-sentinel"


def check(*, root: Path, token: str, euid: int, uid_map: str, release_paths: list[str],
          systemctl_path: str, home: str, service: str = SERVICE) -> str | None:
    """Return the refusal reason, or None when every isolation property holds."""
    if not token:
        return "EXT17B_SENTINEL is not set; this is not a qualified disposable run"
    if euid != 0:
        return f"euid is {euid}, not namespace root"
    # ``unshare -r`` maps this user onto namespace root, so the host's own map (``0 0 4294967295``)
    # and a map that simply contains 0 are both rejected: namespace root must come *from* a host uid.
    mapped = [line.split() for line in uid_map.splitlines() if line.strip()]
    if not any(len(f) >= 2 and f[0] == "0" and f[1] != "0" for f in mapped):
        return f"uid 0 is not mapped from a host uid in this namespace: {uid_map.strip()!r}"

    for path in release_paths:
        try:
            found = (Path(path) / SENTINEL_NAME).read_text(encoding="utf-8").strip()
        except OSError:
            found = ""
        if found != token:
            return f"{path} is not the private mount for this run (sentinel absent or stale)"

    expected = str(root / "bin" / "systemctl")
    if not systemctl_path or os.path.realpath(systemctl_path) != os.path.realpath(expected):
        return f"systemctl resolves to {systemctl_path!r}, not the qualification substitute"

    # The unit is read from the *private* unit mount (the first release path, /etc/systemd/system in
    # a real run), so a host unit can never satisfy the last condition either.
    try:
        text = (Path(release_paths[0]) / service).read_text(encoding="utf-8")
    except OSError as exc:
        return f"cannot read the seeded unit: {exc}"
    if not home or f"HERMES_HOME={home}" not in text:
        return "the seeded unit does not name this scenario's HERMES_HOME"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    try:
        uid_map = Path("/proc/self/uid_map").read_text(encoding="utf-8")
    except OSError as exc:
        uid_map = ""
        print(f"EXT17B GUARD NOTE: cannot read uid_map: {exc}", file=sys.stderr)
    why = check(
        root=Path(args.root),
        token=os.environ.get("EXT17B_SENTINEL", ""),
        euid=os.geteuid(),
        uid_map=uid_map,
        release_paths=[p for p in (os.environ.get("EXT17B_GUARD_PATHS") or "").split(":") if p]
        or RELEASE_PATHS,
        systemctl_path=shutil.which("systemctl") or "",
        home=os.environ.get("HERMES_HOME", ""),
    )
    if why is not None:
        print(f"EXT17B GUARD REFUSED: {why}", file=sys.stderr)
        return 64
    print(f"EXT17B GUARD OK root={args.root} euid=0 home={os.environ.get('HERMES_HOME', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
