"""Phase-aware, read-only preflight for the RESTORE-002 bounded release (mutates nothing).

    env -u PYTHONPATH /usr/bin/python3.13 harness/release002_preflight.py --phase pre  ...
    env -u PYTHONPATH /usr/bin/python3.13 harness/release002_preflight.py --phase post ...

Two verifiers, not one relaxed one (EXT-06). The release freezes admission before it stops the
service, so the post-deployment check runs while ``gateway_state=draining``. Requiring ``running``
there would make a correct release unverifiable; dropping the requirement would let a post-release
verification pass with the service down. So each phase asserts its own invariant:

  ``--phase pre``  install is the *pre-release* identity, the gateway is ``running`` and not
                   draining, no turn is in flight, the deployed runtime's own drain/ESTOP gates
                   cover every producer, and the candidate is present and descends from the install.
  ``--phase post`` install is the *candidate* identity, the service is active under the new code,
                   the freeze this run engaged is still engaged (``gateway_state=draining``), the
                   regenerated unit's effective ``TimeoutStopSec`` is the computed 300 s, and no
                   SIGKILL occurred.

Exit codes: 0 = every gate green (a release may be proposed), 2 = at least one gate failed,
3 = a gate could not be evaluated (treated as failed — fail closed).

Every check is a read: ``git`` in the live install, ``systemctl show/cat``, the journal, ``/proc``,
and a read-only SQLite session inventory. The inventory's classifier is loaded from the *candidate's
pinned git blob* (``gateway/session_resume_plan.py``), never imported from whichever gateway is
installed — the baseline predates that module (EXT-13A). Nothing is written except the optional
``--json`` file.
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

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from release002_oldgate import oldgate_report  # noqa: E402  (probe of the deployed runtime)
import release002_control  # noqa: E402  (pinned-blob loader; EXT-13A)

# The engine exports HERMES_RELEASE_INSTALL / HERMES_RELEASE_HOME; the defaults are the live host.
HERMES_HOME = os.environ.get("HERMES_RELEASE_HOME") or "/home/hermes/.hermes"
INSTALL = os.environ.get("HERMES_RELEASE_INSTALL") or f"{HERMES_HOME}/hermes-agent"
SERVICE = "hermes-gateway.service"
UNIT_DIR = "/etc/systemd/system"
HOLD_DROPIN = f"{UNIT_DIR}/{SERVICE}.d/90-recovery-hold.conf"
TIMEOUT_DROPIN = f"{UNIT_DIR}/{SERVICE}.d/95-timeout-stop.conf"
DB = f"{HERMES_HOME}/state.db"
INDEX = f"{HERMES_HOME}/sessions/sessions.json"
GW_STATE = f"{HERMES_HOME}/gateway_state.json"
DRAIN_MARKER = f"{HERMES_HOME}/.drain_request.json"

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


def _drain_marker(principal_prefix: str) -> dict:
    """Read the freeze marker without importing gateway code into this process."""
    path = DRAIN_MARKER
    try:
        with open(path, encoding="utf-8") as fh:
            body = json.load(fh)
    except FileNotFoundError:
        return {"present": False, "ok": False, "detail": f"no freeze marker at {path}"}
    except Exception as exc:
        return {"present": True, "ok": False, "detail": f"{path} unreadable: {exc}"}
    principal = str(body.get("principal") or body.get("requested_by") or "")
    owned = principal.startswith(principal_prefix)
    age = None
    try:
        age = time.time() - float(body.get("requested_at") or body.get("updated_at") or 0)
    except (TypeError, ValueError):
        age = None
    detail = f"principal={principal or 'absent'} owned_by_release_freeze={owned} age_s={age}"
    return {"present": True, "ok": owned, "detail": detail, "principal": principal, "age_s": age}


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


PLANNER_PATH = "gateway/session_resume_plan.py"


def session_state(quiet_window_s: float = QUIET_WINDOW_S, *, candidate: str | None = None,
                  source_repo: str | None = None) -> dict:
    """Read-only session inventory measured with the *candidate's pinned* planner (EXT-13A).

    ``gateway/session_resume_plan.py`` arrives with this release, so it does not exist in the
    installed baseline: importing ``gateway.session_resume_plan`` would resolve against whichever
    gateway happens to be installed and fail (which is exactly what refused the second production
    attempt). Instead the classifier is loaded from the candidate's own git blob via
    :func:`release002_control.load_pinned_module` — content-addressed, worktree independent, and
    identical before and after deployment. Nothing is imported from the installation, no ambient
    ``PYTHONPATH`` or virtualenv is consulted, and ``sys.path`` is not extended.

    Every failure (absent candidate, missing blob, a module that will not compile or lacks
    ``load_facts``/``classify``, an unreadable store, malformed rows) returns ``{"error": ...}`` so
    the caller records an *unevaluable* gate and the release fails closed. The store itself is only
    ever opened read-only, by the planner's own ``mode=ro`` connection.
    """
    repo = source_repo or INSTALL
    if not candidate:
        return {"error": "no candidate supplied: the session inventory must be measured with the "
                         "candidate's pinned planner, never with an installed import"}
    pin = release002_control.load_pinned_module(repo, candidate, PLANNER_PATH)
    evidence = {k: pin.get(k) for k in ("path", "commit", "blob", "sha256", "repo") if pin.get(k)}
    if not pin.get("ok"):
        return {"error": f"pinned planner unavailable: {pin.get('error')}", "planner": evidence}
    planner = pin["module"]
    now = time.time()
    try:
        facts = planner.load_facts(DB, INDEX)
        buckets = planner.classify(facts, now=now)
        open_facts = [f for f in facts if f.ended_at is None]
        fingerprint = hashlib.sha256(
            "\n".join(
                f"{f.session_id}|{f.started_at}|{f.message_count}|{f.ended_at}"
                for f in sorted(open_facts, key=lambda f: f.session_id)
            ).encode()
        ).hexdigest()
    except Exception as exc:  # noqa: BLE001 - any failure here is a fail-closed signal
        return {"error": f"pinned planner failed: {type(exc).__name__}: {exc}", "planner": evidence}
    recent = [f.session_id for f in open_facts if (f.last_ts or 0.0) >= now - quiet_window_s]
    return {
        "open": len(open_facts),
        "counts": {k: len(v) for k, v in buckets.items()},
        "fingerprint": fingerprint,
        "empty_open": sum(1 for f in open_facts if f.message_count == 0),
        "recently_active": recent,
        "quiet_window_s": quiet_window_s,
        "planner": evidence,
        "planner_source": "pinned-candidate-blob",
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


def runtime_gate_rows(phase: str, gw_state: dict, pid: int | None, head: str,
                      principal_prefix: str = "release-freeze:") -> list[tuple]:
    """The phase's runtime-state contract, as data so it can be tested without a live gateway.

      ``pre``       the preserved installation is serving: running, not draining, nothing frozen
      ``post``      the release holds its own freeze across the restart: draining, marker owned
      ``released``  the release finished: candidate running, no freeze left behind
      ``any``       structural only (pid/code checks), state not asserted
    """
    rows: list[tuple] = []
    observed = gw_state.get("gateway_state")
    marker = _drain_marker(principal_prefix)
    if phase == "post":
        # EXT-06: a correct post-release check runs *inside* the freeze it engaged. Requiring
        # "running" here would make a correct release unverifiable; accepting either would let a
        # verification pass with the service stopped. Draining + owned marker is the real contract.
        rows.append(("runtime_state_frozen", observed == "draining",
                     f"gateway_state={observed} (post-release verification runs inside the freeze)"))
        rows.append(("admission_freeze_held", marker["ok"], marker["detail"]))
    elif phase in {"pre", "released"}:
        rows.append(("runtime_state_running", observed == "running",
                     f"gateway_state={observed} exit_reason={gw_state.get('exit_reason')}"))
        rows.append(("no_preexisting_freeze", not marker["present"], marker["detail"]))
    rows.append(("runtime_state_pid_matches", gw_state.get("pid") == pid,
                 f"state pid={gw_state.get('pid')} systemd MainPID={pid}"))
    rows.append(("runtime_code_sha_matches_install", gw_state.get("code_sha") == head,
                 f"code_sha={gw_state.get('code_sha')} install HEAD={head}"))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--expect-head", default=DEFAULT_INSTALL_HEAD)
    ap.add_argument("--expect-tree", default=DEFAULT_INSTALL_TREE)
    ap.add_argument("--candidate", default=None,
                    help="commit SHA the release will deploy (checked for presence and ancestry)")
    ap.add_argument("--source-repo", dest="source_repo", default=None,
                    help="repository holding the candidate (default: the installation); the pinned "
                         "session planner is read from this repo's git objects, never from an import")
    ap.add_argument("--quiet-window", type=float, default=QUIET_WINDOW_S)
    ap.add_argument("--phase", choices=["pre", "post", "released", "any"], default="pre",
                    help="pre = before any mutation (gateway running); post = after the restart, "
                         "with this run's admission freeze still engaged (gateway draining)")
    ap.add_argument("--freeze-principal-prefix", default="release-freeze:",
                    help="post phase: the drain marker must be owned by a release freeze")
    ap.add_argument("--skip-oldgate", action="store_true",
                    help="developer dry runs only; the release engine never passes it")
    args = ap.parse_args()
    post = args.phase in {"post", "released"}
    if post and not args.candidate:
        print(json.dumps({"phase": args.phase, "verdict": "NO_GO",
                          "failed": ["post_phase_requires_candidate"],
                          "detail": "--phase post needs the exact --candidate"}, indent=2))
        return 2

    g = Gates()

    # 1. Installed identity — phase-selected (EXT-07): pre must still be the preserved installation,
    # post must be the candidate. One gate name, two expectations; the phase is in the detail string.
    rc, head = git("rev-parse", "HEAD")
    rc2, tree = git("rev-parse", "HEAD^{tree}")
    rc3, dirty = git("status", "--porcelain")
    if post:
        want_head = args.candidate
        _, want_tree = git("rev-parse", f"{args.candidate}^{{tree}}")
    else:
        want_head, want_tree = args.expect_head, args.expect_tree
    g.add("install_head", head == want_head if rc == 0 else None,
          f"[{args.phase}] expected={want_head} observed={head or 'unreadable'}")
    g.add("install_tree", (tree == want_tree) if (rc2 == 0 and bool(want_tree)) else None,
          f"[{args.phase}] expected={want_tree} observed={tree or 'unreadable'}")
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
        with open(GW_STATE, encoding="utf-8") as fh:
            gw_state = json.load(fh)
    except Exception as exc:
        gw_state = {"error": str(exc)}
    g.add("runtime_state_readable", "error" not in gw_state, gw_state.get("error", "gateway_state.json"))
    if "error" not in gw_state:
        for name, ok, detail in runtime_gate_rows(args.phase, gw_state, pid, head,
                                                 args.freeze_principal_prefix):
            g.add(name, ok, detail)

    # 5c. Old-runtime admission coverage (EXT-09). The release replaces the running code, so during
    # the cutover the protection in force is the *deployed* drain/ESTOP machinery, not the guard the
    # candidate ships. Proving it here means the freeze engages something that already refuses.
    if args.phase != "pre":
        g.add("old_runtime_coverage", True,
              "not applicable once the restart has happened (the candidate's admission guard is live)",
              critical=False)
    elif args.skip_oldgate:
        g.add("old_runtime_coverage", None, "--skip-oldgate: coverage not proven (developer dry run)")
    else:
        coverage = oldgate_report(INSTALL)
        interp = coverage["dynamic"].get("interpreter") or {}
        detail = (f"verdict={coverage['verdict']} producers={len(coverage['producers'])} "
                  f"uncovered={coverage['uncovered'] or 'none'} unevaluable={coverage['unevaluable'] or 'none'} "
                  f"interpreter={interp.get('source')}:{interp.get('path')} ({interp.get('version')})")
        g.add("old_runtime_coverage", coverage["ok"], detail)
        g.add("old_runtime_dynamic_gates", coverage["dynamic"].get("ok") is True,
              "; ".join(f"{x['gate']}={'ok' if x['ok'] is True else 'FAIL'}"
                        for x in coverage["dynamic"].get("gates", []))[:400])

    # 6. Quiet gate — no engineering turn should be mid-flight when the service is stopped.
    # A session store that cannot be read is *unevaluable*, never a crash: a traceback would emit no
    # JSON at all and the engine would have to guess what happened.
    try:
        state: dict = session_state(args.quiet_window, candidate=args.candidate,
                                    source_repo=args.source_repo)
    except Exception as exc:  # noqa: BLE001 - any failure here is a fail-closed signal
        state = {"error": f"session state unreadable: {type(exc).__name__}: {exc}"}
    if "error" in state:
        g.add("session_inventory", None, state["error"])
    else:
        planner = state.get("planner") or {}
        g.add("session_inventory", state["open"] > 0,
              f"open={state['open']} counts={state['counts']} empty={state['empty_open']} "
              f"planner={state.get('planner_source')}:{(planner.get('sha256') or '')[:12]}")
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
        "phase": args.phase,
        "expect": {"head": want_head, "tree": want_tree, "candidate": args.candidate},
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
