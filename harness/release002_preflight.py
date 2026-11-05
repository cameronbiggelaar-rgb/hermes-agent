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
from datetime import datetime
from pathlib import Path
from typing import Any

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
# EXT-16 qualification hook, identical in shape to the engine's HERMES_RELEASE_QUALIFY_UNIT: a
# temporary unit directory is only honoured with the explicit flag, so no environment can silently
# redirect a production release's unit or drop-in checks.
if os.environ.get("HERMES_RELEASE_QUALIFY") == "1" and os.environ.get("HERMES_RELEASE_QUALIFY_UNIT"):
    _qualify_unit = os.environ["HERMES_RELEASE_QUALIFY_UNIT"]
    UNIT_DIR = os.path.dirname(_qualify_unit)
    SERVICE = os.path.basename(_qualify_unit)
HOLD_DROPIN = f"{UNIT_DIR}/{SERVICE}.d/90-recovery-hold.conf"
TIMEOUT_DROPIN = f"{UNIT_DIR}/{SERVICE}.d/95-timeout-stop.conf"
DB = f"{HERMES_HOME}/state.db"
INDEX = f"{HERMES_HOME}/sessions/sessions.json"
GW_STATE = f"{HERMES_HOME}/gateway_state.json"
DRAIN_MARKER = f"{HERMES_HOME}/.drain_request.json"
ESTOP_MARKER = f"{HERMES_HOME}/ESTOP"

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

    def add(self, name: str, ok: bool | None, detail: str, *, critical: bool = True,
            deferred: bool = False) -> None:
        """Record one gate.

        ``deferred`` (EXT-16) marks a gate that this phase is not yet allowed to evaluate — a
        quiescence check before the admission freeze exists. It is neither a pass nor a failure: it
        is reported as ``status="pending"``, excluded from ``failed``, and named in
        ``pending_gates``. Manufacturing a pass for it would let a busy gateway look deployable.
        """
        self.rows.append({"gate": name, "ok": ok, "critical": critical, "detail": detail,
                          "deferred": bool(deferred),
                          "status": "pending" if deferred else ("pass" if ok is True else
                                                               "fail" if ok is False else "unevaluable")})

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows
                if r["critical"] and r["ok"] is not True and not r["deferred"]]

    @property
    def pending(self) -> list[str]:
        return [r["gate"] for r in self.rows if r["deferred"]]

    def emit(self) -> int:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "gates": self.rows,
            "failed": [r["gate"] for r in self.failed],
            "pending_gates": self.pending,
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


def _drain_marker(principal_prefix: str, *, principal: str | None = None,
                  max_age: float | None = None) -> dict:
    """Read the freeze marker without importing gateway code into this process.

    ``principal`` (EXT-16) demands exact ownership; the prefix test remains for the post-release
    phase, where the marker must be *some* release freeze. ``max_age`` rejects a marker inherited
    from an earlier attempt even when its principal happens to match (a leaked attempt id).
    """
    path = DRAIN_MARKER
    try:
        with open(path, encoding="utf-8") as fh:
            body = json.load(fh)
    except FileNotFoundError:
        return {"present": False, "ok": False, "detail": f"no freeze marker at {path}"}
    except Exception as exc:
        return {"present": True, "ok": False, "detail": f"{path} unreadable: {exc}"}
    observed = str(body.get("principal") or body.get("requested_by") or "")
    owned = (observed == principal) if principal is not None else observed.startswith(principal_prefix)
    age = None
    raw = body.get("requested_at") or body.get("updated_at")
    try:
        if isinstance(raw, (int, float)):
            age = time.time() - float(raw)
        elif raw:
            age = time.time() - datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        age = None
    stale = age is not None and max_age is not None and age > max_age
    detail = (f"principal={observed or 'absent'} owned_by_this_attempt={owned} age_s="
              f"{'unknown' if age is None else f'{age:.0f}'}"
              + (f" stale_budget_s={max_age:.0f}" if max_age is not None else ""))
    return {"present": True, "ok": owned and not stale, "detail": detail, "principal": observed,
            "age_s": age, "stale": stale}


def _stamp(raw: Any) -> float:
    """Epoch seconds from a gateway_state timestamp, ISO or numeric. Unparsable -> 0.0 (maximally
    stale) so a quiet-window check can never be satisfied by a timestamp it cannot read."""
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        if raw:
            return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        pass
    return 0.0


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


def _live_freeze_guards(*, install, home):
    """Ask the deployed modules, in the installation's own interpreter, whether the two mechanisms
    the producer paths consult are engaged *in the live HERMES_HOME*.

    This is the difference between "a marker file exists" and "the code that refuses a message, a
    cron tick, a wake or an RPC will actually refuse": the readers are the deployed ones, reading
    the same paths the running gateway reads. Read-only — nothing is engaged, cleared or rewritten.
    """
    code = (
        "import json, logging\n"
        "import gateway.drain_control as d, agent.estop as e\n"
        "print(json.dumps({'drain_requested': bool(d.drain_requested()),\n"
        "                  'estop_engaged': bool(e.is_engaged()),\n"
        "                  'estop_check_paused': bool(e.check_paused('release-preflight', logging.getLogger('release-preflight')))}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    try:
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(install), env=env,
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "detail": f"probe could not run: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "detail": f"probe failed rc={proc.returncode}: {proc.stderr.strip()[-300:]}"}
    try:
        body = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "detail": f"probe output unreadable: {proc.stdout.strip()[-200:]}"}
    ok = all(body.get(k) is True for k in ("drain_requested", "estop_engaged", "estop_check_paused"))
    return {"ok": ok, "detail": body}


def runtime_gate_rows(phase: str, gw_state: dict, pid: int | None, head: str,
                      principal_prefix: str = "release-freeze:", *,
                      principal: str | None = None,
                      marker_max_age: float | None = None,
                      freeze_since: float | None = None) -> list[tuple]:
    """The phase's runtime-state contract, as data so it can be tested without a live gateway.

      ``pre``        the preserved installation is serving: running, not draining, nothing frozen
      ``infreeze``   this attempt holds the freeze: draining, marker + ESTOP owned by this attempt
      ``revalidate`` identical to ``infreeze`` (checked again immediately before mutation)
      ``post``       the release holds its own freeze across the restart: draining, marker owned
      ``released``   the release finished: candidate running, no freeze left behind
      ``any``        structural only (pid/code checks), state not asserted

    ``principal`` (EXT-16) upgrades "a release freeze owns it" to "this attempt owns it". The
    pre-freeze phase never asserts quiescence; that is the caller's deferred gate.
    """
    rows: list[tuple] = []
    observed = gw_state.get("gateway_state")
    marker = _drain_marker(principal_prefix, principal=principal, max_age=marker_max_age)
    owned = bool(marker["ok"]) and (principal is None or marker.get("principal") == principal)
    if phase in {"infreeze", "revalidate"}:
        rows.append(("runtime_state_frozen", observed == "draining",
                     f"gateway_state={observed} (in-freeze phase)"))
        # B: the running gateway must ACKNOWLEDGE this attempt's freeze, not merely leave a
        # "draining" snapshot behind. gateway_state.json is written on transitions, so a stale
        # snapshot from an earlier drain would otherwise read as proof of an acknowledgement that
        # never happened for this attempt.
        stamp = gw_state.get("updated_at")
        try:
            ack_at = datetime.fromisoformat(str(stamp)).timestamp() if stamp else None
        except (TypeError, ValueError):
            ack_at = None
        fresh = (ack_at is not None and freeze_since is not None
                 and ack_at >= float(freeze_since) - 5.0)
        rows.append(("gateway_acknowledged_freeze", bool(observed == "draining" and fresh),
                     f"updated_at={stamp or 'missing'} freeze_since={freeze_since}"))
        rows.append(("admission_freeze_owned", owned, marker["detail"]))
        try:
            estop = json.loads(Path(ESTOP_MARKER).read_text(encoding="utf-8-sig"))
            reason = str((estop or {}).get("reason") or "") if isinstance(estop, dict) else ""
        except FileNotFoundError:
            reason = ""
        except Exception as exc:  # noqa: BLE001 - unreadable sentinel is not ownership
            reason = ""
            marker = dict(marker, detail=marker["detail"] + f"; ESTOP unreadable: {exc}")
        want = principal if principal is not None else f"{principal_prefix}{'*'}"
        estop_ok = reason == principal if principal is not None else reason.startswith(principal_prefix)
        rows.append(("estop_owned_by_attempt", estop_ok,
                     f"reason={reason or 'absent'} expected={want}"))
    elif phase == "post":
        # EXT-06: a correct post-release check runs *inside* the freeze it engaged. Requiring
        # "running" here would make a correct release unverifiable; accepting either would let a
        # verification pass with the service stopped. Draining + owned marker is the real contract.
        rows.append(("runtime_state_frozen", observed == "draining",
                     f"gateway_state={observed} (post-release verification runs inside the freeze)"))
        rows.append(("admission_freeze_held", owned, marker["detail"]))
    elif phase in {"pre", "released"}:
        rows.append(("runtime_state_running", observed == "running",
                     f"gateway_state={observed} exit_reason={gw_state.get('exit_reason')}"))
        rows.append(("no_preexisting_freeze", not marker["present"], marker["detail"]))
        if phase == "pre":
            rows.append(("no_preexisting_estop", not Path(ESTOP_MARKER).exists(),
                         f"{ESTOP_MARKER} present={Path(ESTOP_MARKER).exists()}"))
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
    ap.add_argument("--phase", choices=["pre", "infreeze", "revalidate", "post", "released", "any"],
                    default="pre",
                    help="pre = before the freeze (never asserts quiescence); infreeze = after the "
                         "admission freeze, proving quiescence; revalidate = again immediately before "
                         "mutation; post = after the restart with this run's freeze still engaged; "
                         "released = freeze lifted and the candidate serving")
    ap.add_argument("--freeze-principal-prefix", default="release-freeze:",
                    help="post phase: the drain marker must be owned by a release freeze")
    ap.add_argument("--principal", default=None,
                    help="this attempt's exact freeze principal (release-freeze:<attempt>); required by "
                         "infreeze/revalidate so a foreign or leaked marker is never mistaken for ours")
    ap.add_argument("--marker-max-age", type=float, default=180.0,
                    help="seconds a freeze marker stays credible (in-freeze phases); a stale marker "
                         "from an earlier attempt fails closed")
    ap.add_argument("--freeze-since", type=float, default=None,
                    help="epoch second the freeze was engaged; cron work claimed at/after it is a "
                         "producer leak and fails the in-freeze phase")
    ap.add_argument("--skip-oldgate", action="store_true",
                    help="developer dry runs only; the release engine never passes it")
    args = ap.parse_args()
    in_freeze = args.phase in {"infreeze", "revalidate"}
    post = args.phase in {"post", "released"}
    if (post or in_freeze) and not args.candidate:
        print(json.dumps({"phase": args.phase, "verdict": "NO_GO",
                          "failed": [f"{args.phase}_phase_requires_candidate"],
                          "detail": f"--phase {args.phase} needs the exact --candidate"}, indent=2))
        return 2
    if in_freeze and not args.principal:
        # Without an exact principal the phase cannot distinguish this attempt's freeze from a
        # foreign one, and "someone else's drain marker" would read as quiescence.
        print(json.dumps({"phase": args.phase, "verdict": "NO_GO",
                          "failed": ["in_freeze_phase_requires_principal"],
                          "detail": "--phase infreeze/revalidate needs --principal release-freeze:<attempt>"},
                         indent=2))
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
                                                 args.freeze_principal_prefix,
                                                 principal=args.principal if in_freeze else None,
                                                 marker_max_age=args.marker_max_age if in_freeze else None,
                                                 freeze_since=args.freeze_since if in_freeze else None):
            g.add(name, ok, detail)

    # 5c. Old-runtime admission coverage (EXT-09). The release replaces the running code, so during
    # the cutover the protection in force is the *deployed* drain/ESTOP machinery, not the guard the
    # candidate ships. Proving it here means the freeze engages something that already refuses.
    if args.phase in {"post", "released"}:
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

    # 6b. Quiescence is only *provable* inside a freeze. Before the freeze these gates are emitted as
    # pending — never as passes — because admission_report() requires the drain marker that only
    # phase B creates. The old driver evaluated them in `pre` and could therefore never reach GO on a
    # gateway carrying traffic (EXT-16 ordering defect).
    QUIESCENCE_GATES = ("no_active_agents", "no_live_admissions", "admission_counters_consistent",
                        "freeze_owned_by_attempt", "freeze_since_attributable",
                        "freeze_guards_engaged", "cron_inflight_zero",
                        "cron_no_claims_after_freeze", "quiet_window_elapsed")
    if in_freeze:
        adm = release002_control.admission_report(
            principal=args.principal, marker_max_age=args.marker_max_age)
        facts = adm["facts"]
        reasons = adm["reasons"]
        agents, live = facts.get("active_agents"), facts.get("live_admissions")
        g.add("no_active_agents", agents == 0 if type(agents) is int else None,
              f"gateway_state.active_agents={agents!r}")
        g.add("no_live_admissions", live == 0 if type(live) is int else None,
              f"live_admissions={live!r} epoch={facts.get('epoch')} "
              f"stale={facts.get('stale_admissions')!r}")
        g.add("admission_counters_consistent", "admission_counters_contradict" not in reasons,
              f"active_agents={agents!r} live_admissions={live!r}")
        g.add("freeze_owned_by_attempt",
              facts.get("drain_marker_active") is True and "estop_foreign_owner" not in reasons
              and "estop_not_owned_by_release" not in reasons,
              f"marker={facts.get('drain_marker_principal')!r} estop={facts.get('estop_reason')!r} "
              f"({facts.get('freeze_detail')})")
        # The freeze epoch the caller attributes scheduled work to must be the epoch the marker
        # records. A marker rewritten between engagement and sampling (or a caller that substituted
        # its own number) would make "no claims after the freeze" meaningless.
        marker_since = facts.get("drain_marker_requested_at_epoch")
        if args.freeze_since is None or not isinstance(marker_since, (int, float)):
            g.add("freeze_since_attributable", None,
                  f"--freeze-since={args.freeze_since!r} marker={marker_since!r}")
        else:
            skew = abs(float(marker_since) - float(args.freeze_since))
            g.add("freeze_since_attributable", skew <= 5.0,
                  f"marker_epoch={float(marker_since):.0f} caller={float(args.freeze_since):.0f} "
                  f"skew={skew:.1f}s")
        # Both mechanisms as the *deployed code* reads them, not just as files on disk.
        guards = _live_freeze_guards(install=Path(INSTALL), home=Path(HERMES_HOME))
        g.add("freeze_guards_engaged", guards["ok"], str(guards["detail"]))
        cron = release002_control.cron_report(since=args.freeze_since)
        g.add("cron_inflight_zero", cron["inflight"] == 0 if cron["evaluable"] else None, cron["detail"])
        if args.freeze_since is None:
            # Not knowing when the freeze began means post-freeze claims cannot be attributed; the
            # gate is unevaluable and the phase is NO_GO rather than "probably fine".
            g.add("cron_no_claims_after_freeze", None,
                  "no --freeze-since supplied: claims cannot be attributed to before/after the freeze")
        else:
            g.add("cron_no_claims_after_freeze",
                  cron["post_freeze_claims"] == 0 if cron["evaluable"] else None, cron["detail"])
        if "error" in gw_state:
            g.add("quiet_window_elapsed", None, "gateway_state unreadable; cannot date the last activity")
        else:
            since_last = time.time() - _stamp(gw_state.get("updated_at") or gw_state.get("last_activity_at"))
            g.add("quiet_window_elapsed", since_last >= args.quiet_window,
                  f"since_last_activity={since_last:.0f}s window={args.quiet_window:.0f}s")
    else:
        for name in QUIESCENCE_GATES:
            g.add(name, None, "pending: only evaluable inside the admission freeze", deferred=True)

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
        "pending_gates": g.pending,
        "verdict": "GO" if not g.failed else "NO_GO",
        "phase": args.phase,
        # EXT-16: a GO in the pre phase is a *pre-freeze* GO. Deployment is only ever ready once the
        # in-freeze quiescence gates have actually been evaluated, so this cannot be inferred from
        # `verdict` alone by a caller that forgets which phase it asked for.
        "deployment_ready": (not g.failed) and args.phase in {"revalidate"},
        "quiescence_proven": in_freeze and not g.failed and not g.pending,
        "expect": {"head": want_head, "tree": want_tree, "candidate": args.candidate},
        "freeze": {"principal": args.principal, "since": args.freeze_since,
                   "marker_max_age_s": args.marker_max_age},
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
