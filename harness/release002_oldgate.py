#!/usr/bin/env python3
"""Prove the *deployed* runtime refuses new admissions while the cutover freeze is engaged.

The release replaces the running code, so for the whole pre-deployment window the protection in
force is whatever ``e05128fe`` already does — not the ``execute_admission`` guard the candidate
ships. This probe answers one question with evidence: for every producer that can start a turn in
this installation, which deployed mechanism refuses it, and where is that mechanism?

Two independent mechanisms are in force during the cutover:

* the **drain marker** (``.drain_request.json``) — consulted by inbound handling, the busy-session
  path, the runtime RPC surface, internal wakes and client discovery (``gateway_state=draining``);
* the **ESTOP sentinel** (``ESTOP``) — consulted by cron dispatch, cron misfire replay, kanban
  watchers, the goals loop and its heartbeat, the API server's cron webhook and inbound handling.

Static half: every producer spec names a module, the function that must contain the gate, and the
gate symbol; the probe parses the deployed source and reports the exact line. A producer whose
gate cannot be found is *uncovered* — the probe never infers coverage from a neighbouring file.

Dynamic half: with a throwaway ``HERMES_HOME``, the probe engages each mechanism through the
deployed module's own writer and asserts the deployed reader refuses afterwards, then disengages
and asserts it admits again. Marker format drift between the engine and the running code is a
failure here, not a surprise during the release.

Exit codes: 0 covered, 2 uncovered (NO_GO), 3 unevaluable (also NO_GO — never treated as covered),
64 usage.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

SCHEMA = 1
DEFAULT_INSTALL = "/home/hermes/.hermes/hermes-agent"

# producer -> the deployed gate that refuses a *new* admission while the freeze is engaged.
# ``function`` is where the gate must appear; a symbol found elsewhere in the module does not count.
PRODUCERS: tuple[dict[str, str], ...] = (
    {"producer": "messaging_inbound", "mechanism": "drain_marker",
     "module": "gateway/run_inbound.py", "function": "_handle_message",
     "symbol": "_external_drain_active",
     "why": "a user message on an idle session is an external turn"},
    {"producer": "messaging_inbound", "mechanism": "estop_sentinel",
     "module": "gateway/run_inbound.py", "function": "_hm_estop_gate",
     "symbol": "paused_reply",
     "why": "the same turn is refused by the ESTOP gate as a second line"},
    {"producer": "busy_session_followup", "mechanism": "drain_marker",
     "module": "gateway/run_inbound.py", "function": "_hm_handle_running_session_message",
     "symbol": "_draining",
     "why": "a follow-up posted to a busy session must not start a new turn either"},
    {"producer": "cron_dispatch", "mechanism": "estop_sentinel",
     "module": "cron/scheduler_tick.py", "function": "_tick_admitted",
     "symbol": "check_paused",
     "why": "the scheduler tick fires cron sessions"},
    {"producer": "cron_misfire_replay", "mechanism": "estop_sentinel",
     "module": "cron/scheduler_provider.py", "function": "fire_overdue_jobs",
     "symbol": "check_paused",
     "why": "overdue jobs replay after a restart"},
    {"producer": "kanban_watchers", "mechanism": "estop_sentinel",
     "module": "gateway/kanban_watchers_common.py", "function": "_kanban_dispatch_allowed",
     "symbol": "check_paused",
     "why": "watchers inject engineering turns on card state"},
    {"producer": "goals_heartbeat", "mechanism": "estop_sentinel",
     "module": "gateway/run_goals.py", "function": "_heartbeat_poll_watch",
     "symbol": "check_paused",
     "why": "heartbeat polls fire turns"},
    {"producer": "goals_loop", "mechanism": "estop_sentinel",
     "module": "gateway/run_goals.py", "function": "_loop_wakeup_fire_one",
     "symbol": "check_paused",
     "why": "loop wakeups are injected as internal events and bypass the inbound gate"},
    {"producer": "api_cron_webhook", "mechanism": "estop_sentinel",
     "module": "gateway/platforms/api_server.py", "function": "_handle_cron_fire",
     "symbol": "check_paused",
     "why": "the API server fires scheduled jobs"},
    {"producer": "runtime_api_rpc", "mechanism": "drain_marker",
     "module": "gateway/run_api.py", "function": "_serve",
     "symbol": "_draining",
     "why": "the loopback RPC surface admits turns for clients"},
    {"producer": "internal_wake", "mechanism": "drain_marker",
     "module": "gateway/wake.py", "function": "admit_internal_event",
     "symbol": "_runner_draining",
     "why": "a wake admitted during a drain would never run"},
    {"producer": "finite_cli_prompt", "mechanism": "drain_marker",
     "module": "hermes_cli/gateway_runtime.py", "function": "_endpoint",
     "symbol": "GatewayDiscovery",
     "why": "a client that finds the owner draining does not attach, so chat -q never admits"},
)

# The candidate's EXT-04 guard closes ``execute_admission`` for finite prompts executed in-process.
# It is deliberately *not* relied on here: this entry records that the deployed runtime reaches
# that seam only through a client that first performs discovery (``finite_cli_prompt`` above).
RESIDUAL = {
    "seam": "gateway/session_ingress.py::execute_admission",
    "deployed_guard": None,
    "closed_by": "finite_cli_prompt (client discovery refuses a draining owner)",
    "note": ("the candidate adds the admission guard; the cutover does not assume it, because it is "
             "not running until after the restart"),
}


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _functions(tree: ast.AST, name: str) -> list[ast.AST]:
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


def _aliases(tree: ast.AST) -> dict[str, str]:
    """local name -> imported name, so ``from x import gate as _gate`` still counts as ``gate``."""
    out: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for alias in n.names:
                out[alias.asname or alias.name] = alias.name
        elif isinstance(n, ast.Import):
            for alias in n.names:
                if alias.asname:
                    out[alias.asname] = alias.name.split(".")[0]
    return out


def _refs(node: ast.AST, symbol: str, aliases: Optional[dict[str, str]] = None) -> list[int]:
    local = {name for name, imported in (aliases or {}).items() if imported == symbol}
    local.add(symbol)
    hits: set[int] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in local:
            hits.add(n.lineno)
        elif isinstance(n, ast.Attribute) and n.attr == symbol:
            hits.add(n.lineno)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in local:
            hits.add(n.lineno)
    return sorted(hits)


def _import_sources(tree: ast.AST) -> dict[str, tuple[str, Optional[str]]]:
    """local name -> (module, original name or None for a module import)."""
    out: dict[str, tuple[str, Optional[str]]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for alias in n.names:
                out[alias.asname or alias.name] = (n.module, alias.name)
        elif isinstance(n, ast.Import):
            for alias in n.names:
                out[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
    return out


def _module_file(root: Path, dotted: str) -> Optional[Path]:
    base = root / Path(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _defines(tree: ast.AST, name: str) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return True
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            return True
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            return True
    return False


def _defined_anywhere(root: Path, symbol: str, cache: dict[str, Any]) -> bool:
    """The gate must still exist in the deployed tree, wherever the consumer imports it from."""
    if symbol in cache.get("__defs__", {}):
        return cache["__defs__"][symbol]
    found = False
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        text = _read(path)
        if not text:
            continue
        if (f"def {symbol}(" in text or f"def {symbol} (" in text
                or f"{symbol} =" in text or f"{symbol}:" in text):
            found = True
            break
    cache.setdefault("__defs__", {})[symbol] = found
    return found


def _dangling(root: Path, tree: ast.AST, symbol: str, cache: dict[str, Any]) -> Optional[str]:
    """Return a reason when the gate is *named* at a call site but no longer exists in the code.

    A call site alone proves nothing: a renamed or deleted gate would still be "referenced".
    """
    if not _defined_anywhere(root, symbol, cache):
        return f"{symbol} is referenced but not defined anywhere in the deployed tree"
    return None


def oldgate_static(install: str | Path) -> dict[str, Any]:
    """Pure source-level proof: for each producer, the gate symbol inside the named function."""
    root = Path(install)
    rows: list[dict[str, Any]] = []
    cache: dict[str, Any] = {}
    for spec in PRODUCERS:
        module = spec["module"]
        if module not in cache:
            text = _read(root / module)
            if text is None:
                cache[module] = {"error": f"{module} is not readable"}
            else:
                try:
                    cache[module] = {"tree": ast.parse(text)}
                except SyntaxError as exc:  # pragma: no cover - deployed code parses
                    cache[module] = {"error": f"{module}: {exc}"}
        entry = cache[module]
        row: dict[str, Any] = {"producer": spec["producer"], "mechanism": spec["mechanism"], "module": module,
               "function": spec["function"], "symbol": spec["symbol"], "why": spec["why"]}
        if "error" in entry:
            row.update(ok=None, detail=entry["error"])
            rows.append(row)
            continue
        fns = _functions(entry["tree"], spec["function"])
        if not fns:
            row.update(ok=None, detail=f"{module} has no function {spec['function']!r}")
            rows.append(row)
            continue
        aliases = _aliases(entry["tree"])
        lines = sorted({ln for fn in fns for ln in _refs(fn, spec["symbol"], aliases)})
        dangling = _dangling(root, entry["tree"], spec["symbol"], cache) if lines else None
        if dangling:
            row.update(ok=False, detail=dangling, line=lines[0])
        else:
            row.update(ok=bool(lines),
                       detail=f"{module}:{','.join(str(ln) for ln in lines)}" if lines
                       else f"{spec['symbol']} not referenced in {spec['function']}")
        row["line"] = lines[0] if lines else None
        rows.append(row)
    return {
        "schema": SCHEMA,
        "install": str(root),
        "residual": RESIDUAL,
        "producers": rows,
        "covered": sum(1 for r in rows if r["ok"] is True),
        "uncovered": [r["producer"] + "/" + r["mechanism"] for r in rows if r["ok"] is False],
        "unevaluable": [r["producer"] + "/" + r["mechanism"] for r in rows if r["ok"] is None],
        "ok": all(r["ok"] is True for r in rows),
    }


_DRAIN_SCRIPT = """
import sys
from gateway.drain_control import write_drain_request, drain_requested, clear_drain_request
stage = sys.argv[1]
if stage == "absent":
    print("absent" if not drain_requested() else "already-draining")
elif stage == "engage":
    write_drain_request(principal=sys.argv[2], suppress_notification=True)
    print("draining" if drain_requested() else "NOT-DRAINING")
else:
    clear_drain_request()
    print("cleared" if not drain_requested() else "STILL-DRAINING")
"""

_ESTOP_SCRIPT = """
import sys
from agent.estop import engage, disengage, is_engaged
stage = sys.argv[1]
if stage == "absent":
    print("absent" if not is_engaged() else "already-engaged")
elif stage == "engage":
    engage(reason=sys.argv[2])
    print("engaged" if is_engaged() else "NOT-ENGAGED")
else:
    disengage()
    print("cleared" if not is_engaged() else "STILL-ENGAGED")
"""

_DISCOVERY_SCRIPT = """
import json, sys
from pathlib import Path
from hermes_cli.gateway_runtime import _endpoint, attachable
payload = json.loads(sys.argv[1])
result = _endpoint(payload, Path(sys.argv[2]))
print(json.dumps({"state": result.state, "reason": result.reason_code,
                  "attachable": bool(attachable(result))}))
"""


def _run(py: str, install: Path, home: Path, script: str, *args: str) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(install), PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run([py, "-c", script, *args], cwd=str(install), env=env,
                              capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": None, "detail": f"{py}: {exc}"}
    out = proc.stdout.strip().splitlines()
    return {"ok": proc.returncode == 0, "detail": (out[-1] if out else proc.stderr.strip()[-400:]),
            "rc": proc.returncode}


def _discovery_payload(home: Path) -> dict[str, Any]:
    """A minimal draining owner record with the fields ``_endpoint`` validates."""
    return {
        "runtime_protocol": 1,
        "state": "draining",
        "drain_reason": "release",
        "served_profiles": [{"home": str(home), "profile_id": "default"}],
        "capabilities": ["session-authority-v1"],
        "authority_epoch": 23,
        "instance_id": "0123456789abcdef",
        "api_origin": "http://127.0.0.1:8765",
        "supervisor": "systemd",
    }


def oldgate_dynamic(install: str | Path, *, py: Optional[str] = None,
                    home: Optional[str | Path] = None) -> dict[str, Any]:
    """Engage each mechanism through the deployed writer and read it back with the deployed reader."""
    root = Path(install)
    interpreter = py or str(root / "venv" / "bin" / "python")
    tmp = Path(home) if home else Path(tempfile.mkdtemp(prefix="oldgate-", dir=_scratch()))
    gates: list[dict[str, Any]] = []

    def gate(name: str, ok: Optional[bool], detail: str) -> None:
        gates.append({"gate": name, "ok": ok, "detail": detail})

    if not Path(interpreter).exists():
        gate("interpreter", None, f"{interpreter} is missing")
        return {"gates": gates, "ok": False, "evaluable": False, "home": str(tmp)}

    absent = _run(interpreter, root, tmp, _DRAIN_SCRIPT, "absent")
    gate("drain_starts_clear", absent["ok"] is True and absent["detail"] == "absent", str(absent["detail"]))
    engaged = _run(interpreter, root, tmp, _DRAIN_SCRIPT, "engage", "release-freeze:oldgate")
    gate("drain_reader_refuses_when_engaged",
         engaged["ok"] is True and engaged["detail"] == "draining", str(engaged["detail"]))
    cleared = _run(interpreter, root, tmp, _DRAIN_SCRIPT, "clear")
    gate("drain_reader_admits_when_cleared",
         cleared["ok"] is True and cleared["detail"] == "cleared", str(cleared["detail"]))

    estop_absent = _run(interpreter, root, tmp, _ESTOP_SCRIPT, "absent")
    gate("estop_starts_clear",
         estop_absent["ok"] is True and estop_absent["detail"] == "absent", str(estop_absent["detail"]))
    estop_engaged = _run(interpreter, root, tmp, _ESTOP_SCRIPT, "engage", "release-freeze:oldgate")
    gate("estop_reader_refuses_when_engaged",
         estop_engaged["ok"] is True and estop_engaged["detail"] == "engaged", str(estop_engaged["detail"]))
    estop_cleared = _run(interpreter, root, tmp, _ESTOP_SCRIPT, "clear")
    gate("estop_reader_admits_when_cleared",
         estop_cleared["ok"] is True and estop_cleared["detail"] == "cleared", str(estop_cleared["detail"]))

    seen = _run(interpreter, root, tmp, _DISCOVERY_SCRIPT, json.dumps(_discovery_payload(tmp)), str(tmp))
    verdict: dict[str, Any] = {}
    if seen["ok"] is True and seen["detail"].startswith("{"):
        try:
            verdict = json.loads(seen["detail"])
        except json.JSONDecodeError:  # pragma: no cover - defensive
            verdict = {}
    gate("client_discovery_refuses_draining_owner",
         verdict.get("state") == "draining" and verdict.get("attachable") is False,
         json.dumps(verdict) if verdict else str(seen["detail"]))

    if not home:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"gates": gates, "ok": all(g["ok"] is True for g in gates),
            "evaluable": all(g["ok"] is not None for g in gates), "home": str(tmp)}


def _scratch() -> str:
    base = os.environ.get("BH_AGENT_WORKSPACE") or os.environ.get("TMPDIR") or "/tmp"
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
        return base
    except OSError:  # pragma: no cover - environment
        return "/tmp"


def oldgate_report(install: str | Path, *, py: Optional[str] = None,
                   dynamic: bool = True) -> dict[str, Any]:
    static = oldgate_static(install)
    report: dict[str, Any] = {"schema": SCHEMA, "install": static["install"],
                              "producers": static["producers"], "residual": static["residual"],
                              "uncovered": static["uncovered"], "unevaluable": static["unevaluable"]}
    if dynamic:
        report["dynamic"] = oldgate_dynamic(install, py=py)
    else:
        report["dynamic"] = {"gates": [], "ok": True, "evaluable": True, "skipped": True}
    dyn = report["dynamic"]
    if not dyn.get("evaluable", False) or static["unevaluable"]:
        report["verdict"], report["ok"] = "unevaluable", False
    elif not static["ok"] or not dyn["ok"]:
        report["verdict"], report["ok"] = "uncovered", False
    else:
        report["verdict"], report["ok"] = "covered", True
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="old-runtime admission coverage probe")
    ap.add_argument("--install", default=DEFAULT_INSTALL)
    ap.add_argument("--python", dest="py", default=None)
    ap.add_argument("--static-only", action="store_true")
    ap.add_argument("--json", dest="json_path", default=None)
    args = ap.parse_args(argv)
    report = oldgate_report(args.install, py=args.py, dynamic=not args.static_only)
    body = json.dumps(report, indent=2)
    print(body)
    if args.json_path:
        Path(args.json_path).write_text(body + "\n", encoding="utf-8")
    if report["ok"]:
        return 0
    return 2 if report["verdict"] == "uncovered" else 3


if __name__ == "__main__":
    sys.exit(main())
