#!/usr/bin/env python3
"""Release orchestration control for RESTORE-002: identity, envelope, freeze, journal, recovery.

Everything here is either a *pure* decision function or a read-only probe, so the shell engine and
the launcher can share one implementation and the qualification suite can test the decisions
without a service, a restart, or a database write.

Three External Chief Architect findings live here:

* EXT-RESTORE-002-03 (launcher integrity) — :func:`write_manifest` pins the launcher, engine,
  helper modules and candidate commit/tree by checksum; :func:`verify_manifest` fails closed on a
  missing candidate, drifted blob, dirty worktree, or unexpected install identity.
* EXT-RESTORE-002-04 (admission control) — :func:`admission_report` proves the freeze is engaged and
  that no admitted work is still executing; :func:`freeze_plan` names the two mechanisms.
* EXT-RESTORE-002-05 (supervisor lifecycle) — :func:`required_envelope_seconds` sizes the transient
  supervisor's ``RuntimeMaxSec``; :func:`decide_recovery` is the deterministic decision for a
  supervisor that died mid-release.

Exit codes: 0 ok, 2 NO_GO (a gate failed), 3 unevaluable (treated as NO_GO by callers), 64 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = 1
RELEASE = "RESTORE-002"
MANIFEST_DEFAULT = "/usr/local/lib/hermes-release/manifest.json"
JOURNAL_DEFAULT = "/var/lib/hermes-release/restore-002/journal.json"

# A regenerated unit checksum is only meaningful as an exact 64-character lowercase hex digest.
# Anything else (absent, truncated, upper-case, prefixed) is a NO_GO, never a skipped comparison.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The freeze engages two independent mechanisms so both halves of admission are closed:
#   drain marker  -> external inbound turns, cron dispatch, published gateway_state "draining"
#   ESTOP sentinel -> internal/self-injected turns, kanban watchers, goals loop and heartbeat ticks
FREEZE_MECHANISMS = ("drain_marker", "estop_sentinel")

# EXT-RESTORE-002-12 — one authoritative engine inventory.
#
# Before this constant existed the set of engine files lived in two places: the launcher's
# ``ENGINE_FILES`` array (what it staged and ran) and a literal list inside ``manifest --write``
# (what it pinned). The two drifted, the launcher required a pin the manifest never wrote, and the
# release refused at PLAN. Staging and pinning now both read these tuples; nothing else may carry a
# copy of the list.
#
# ``ENGINE_FILES``  — every file copied into the engine directory and executed from there.
# ``LAUNCHER_REL``  — the candidate's launcher blob. It is *not* staged (the installed launcher runs
#                     from the library root), but it is pinned so the manifest records which
#                     candidate launcher this release corresponds to, separately from
#                     ``manifest["launcher"]["sha256"]`` which describes the file actually installed.
ENGINE_FILES: tuple[str, ...] = (
    "harness/release002_release.sh",
    "harness/release002_control.py",
    "harness/release002_preflight.py",
    "harness/release002_oldgate.py",
    "harness/restore002_session_inventory.py",
)
LAUNCHER_REL = "harness/hermes-release-002"
PINNED_ENGINE_FILES: tuple[str, ...] = ENGINE_FILES + (LAUNCHER_REL,)

# Supervisor envelope: every stage the supervisor may have to run, upper-bounded.
QUIESCE_MAX_SECONDS = 180          # freeze -> three consecutive zero samples, 60 s apart
DEPLOY_MAX_SECONDS = 60            # ff-only checkout + unit regeneration
VERIFY_MAX_SECONDS = 90            # post-restart identity, unit and journal checks
ARCHIVE_MAX_SECONDS = 120          # pre-cutover archive of receipts and unit copies
RESTART_MARGIN_SECONDS = 30        # systemctl start/stop overhead around the stop budget
ENVELOPE_SAFETY_SECONDS = 120      # slack for slow disk/telemetry before RuntimeMaxSec kills us

PHASES = ("start", "stage", "preflight", "archive", "freeze", "quiesce", "revalidate",
          "aborted", "recover", "rollback",
          "deploy", "unit", "restart", "verify", "unfreeze", "report")
PHASES_AFTER_MUTATION = frozenset({"deploy", "unit", "restart", "verify"})
# Everything up to and including revalidation happens with the running installation untouched.
PRE_MUTATION_PHASES = frozenset({"start", "stage", "preflight", "archive", "freeze", "quiesce",
                                 "revalidate", "aborted"})
MAX_RESTART_ATTEMPTS = 1
SETTLED_RESULTS = frozenset({"COMPLETE", "ROLLED_BACK", "ABORTED_SAFE"})


# --------------------------------------------------------------------------- paths / io
def hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME") or "~/.hermes"
    return Path(os.path.expanduser(raw))


def drain_marker_path(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / ".drain_request.json"


def estop_marker_path(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / "ESTOP"


def state_db_path(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / "state.db"


def gateway_state_path(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / "gateway_state.json"


def read_text(path: str | Path) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

def sha256_file(path: str | Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _git(repo: str | Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - environment
        return 125, str(exc)
    return proc.returncode, (proc.stdout or "").strip()


def git_identity(repo: str | Path) -> dict[str, Any]:
    """HEAD/tree identity of a worktree, plus whether tracked/untracked files are dirty."""
    code, head = _git(repo, "rev-parse", "HEAD")
    if code != 0:
        return {"ok": False, "reason": f"git rev-parse failed in {repo}", "head": None, "tree": None}
    code, tree = _git(repo, "rev-parse", "HEAD^{tree}")
    code2, status = _git(repo, "status", "--porcelain")
    return {"ok": code == 0 and code2 == 0, "head": head, "tree": tree,
            "dirty_files": [ln for ln in status.splitlines() if ln.strip()] if code2 == 0 else None}


def object_present(repo: str | Path, commit: str) -> bool:
    code, _ = _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    return code == 0


def object_tree(repo: str | Path, commit: str) -> Optional[str]:
    code, out = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")
    return out if code == 0 else None


def blob_sha256(repo: str | Path, commit: str, path: str) -> Optional[str]:
    """SHA-256 of a file *at* a commit — independent of the worktree on disk."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{path}"],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return None
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


# --------------------------------------------------------------------------- manifest
# ------------------------------------------------------------------ unit / phase helpers
POST_RELEASE_TIMEOUT_STOP_SEC = 300.0
MANIFEST_SCHEMA = 2

_UNIT_SECONDS = {"us": 1e-6, "usec": 1e-6, "ms": 1e-3, "msec": 1e-3, "s": 1.0, "sec": 1.0,
                 "second": 1.0, "seconds": 1.0, "m": 60.0, "min": 60.0, "minute": 60.0,
                 "minutes": 60.0, "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0}


def parse_systemd_duration(text: str) -> Optional[float]:
    """``300``/``300s``/``5min``/``1min 30s`` -> seconds. ``None`` for infinity or nonsense."""
    value = (text or "").strip()
    if not value or value.lower() in {"infinity", "0"}:
        return None if value.lower() == "infinity" or not value else 0.0
    if value.isdigit():
        return float(value)
    total = 0.0
    seen = False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)", value):
        scale = _UNIT_SECONDS.get(unit.lower())
        if scale is None:
            return None
        total += float(number) * scale
        seen = True
    return total if seen else None


def unit_timeout_stop_seconds(unit_path: "str | Path") -> Optional[float]:
    """Effective ``TimeoutStopSec=`` of a unit file (the last assignment wins, as systemd does)."""
    text = read_text(Path(unit_path))
    if text is None:
        return None
    found: Optional[float] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        if key.strip() in ("TimeoutStopSec", "TimeoutStopUSec"):
            found = parse_systemd_duration(raw)
    return found


# ------------------------------------------------------------------- manifest (EXT-07)
def build_manifest(*, repo: str | Path, candidate: str, install: str | Path, launcher: str | Path,
                   unit: str | Path, dropins: "list[str | Path] | dict[str, str | Path]",
                   expected_install_head: str,
                   engine_paths: Optional[list[str]] = None) -> dict[str, Any]:
    """Describe every identity the release depends on, pinned by checksum.

    Two identity sets, because the release *changes* the identity it was staged against (EXT-07):

    * ``pre_release``  — what must be true up to the first mutation: the installation the release
      replaces, the unit file as it stands, and the drop-ins that must survive untouched.
    * ``post_release`` — what must be true afterwards: the candidate HEAD/tree, the regenerated
      unit's effective ``TimeoutStopSec`` and the same drop-in bytes (a release may not quietly
      retire the recovery hold or the timeout override).

    ``engine_paths`` exists only so fixtures can build synthetic manifests; every production caller
    must omit it and take ``PINNED_ENGINE_FILES``, so staging and pinning cannot drift apart again
    (EXT-12).
    """
    tree = object_tree(repo, candidate)
    install_ident = git_identity(install)
    dropin_map = {Path(p).name: sha256_file(p) for p in dropins}
    engine_list = list(PINNED_ENGINE_FILES) if engine_paths is None else list(engine_paths)
    return {
        "schema": MANIFEST_SCHEMA, "release": RELEASE,
        "candidate_commit": candidate, "candidate_tree": tree,
        "install_path": str(install),
        "pre_release": {
            "install_head": expected_install_head,
            "install_tree": object_tree(install, expected_install_head),
            "unit": {"path": str(unit), "sha256": sha256_file(unit)},
            "dropins": dict(dropin_map),
        },
        "post_release": {
            "install_head": candidate,
            "install_tree": tree,
            "unit": {"path": str(unit), "timeout_stop_sec": POST_RELEASE_TIMEOUT_STOP_SEC},
            "dropins": dict(dropin_map),
        },
        "observed_install_at_capture": {"head": install_ident.get("head"), "tree": install_ident.get("tree")},
        "engine": {p: {"blob_sha256": blob_sha256(repo, candidate, p)} for p in engine_list},
        "launcher": {"path": str(launcher), "sha256": sha256_file(launcher)},
        "supervisor": {"runtime_max_sec": envelope_plan()["runtime_max_sec"],
                       "required_sec": envelope_plan()["required_sec"]},
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def engine_inventory(*, pinned: bool = False) -> list[str]:
    """The one authoritative engine-file inventory (EXT-12).

    ``pinned=False`` returns the files the launcher stages and runs; ``pinned=True`` adds the
    candidate launcher blob, which the manifest pins alongside them.
    """
    return list(PINNED_ENGINE_FILES if pinned else ENGINE_FILES)


def audit_engine(manifest: dict[str, Any], *, engine_dir: str | Path) -> dict[str, Any]:
    """EXT-12: the staged engine, the pinned engine map and the inventory must be one set.

    Three independent sets can drift apart and each failure mode is named separately:

    * a staged file the manifest never pinned — the EXT-12 defect itself, silent before this audit
      because the manifest simply had no entry for it and nothing compared the sets;
    * a manifest entry outside the inventory — a pin nothing stages and nothing verifies, which
      makes the manifest look stricter than the release actually is;
    * a staged file whose bytes are not the pinned bytes.

    ``__pycache__`` and ``*.pyc`` are ignored: importing a sibling module is not a release file.
    Nothing is written and no service is touched, so this runs in PLAN as well as APPLY.
    """
    engine_dir = Path(engine_dir)
    raw = manifest.get("engine")
    pinned_map = raw if isinstance(raw, dict) else {}
    pinned = {str(k) for k in pinned_map}
    inventory = set(ENGINE_FILES)
    on_disk = {
        str(p.relative_to(engine_dir)).replace(os.sep, "/")
        for p in engine_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    } if engine_dir.is_dir() else set()

    gates: list[dict[str, Any]] = []

    def gate(name: str, ok: bool, detail: str = "") -> bool:
        gates.append({"gate": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    missing_pin = sorted(inventory - pinned)
    unexpected_pin = sorted(pinned - set(PINNED_ENGINE_FILES))
    missing_staged = sorted(inventory - on_disk)
    unexpected_staged = sorted(on_disk - inventory)

    gate("engine_dir_present", engine_dir.is_dir(), str(engine_dir))
    gate("inventory_plausible", len(ENGINE_FILES) >= 5, f"{len(ENGINE_FILES)} staged file(s)")
    gate("manifest_engine_set_is_the_inventory", pinned == set(PINNED_ENGINE_FILES),
         f"pinned={len(pinned)} inventory={len(PINNED_ENGINE_FILES)}")
    gate("no_staged_file_missing_from_manifest", not missing_pin, ",".join(missing_pin))
    gate("no_unexpected_manifest_engine_entry", not unexpected_pin, ",".join(unexpected_pin))
    gate("launcher_blob_pinned", LAUNCHER_REL in pinned, LAUNCHER_REL)
    gate("every_inventory_file_staged", not missing_staged, ",".join(missing_staged))
    gate("no_unexpected_staged_file", not unexpected_staged, ",".join(unexpected_staged))

    mismatched: list[str] = []
    for rel in sorted(inventory & on_disk):
        entry = pinned_map.get(rel)
        want = entry.get("blob_sha256") if isinstance(entry, dict) else None
        got = sha256_file(engine_dir / rel)
        if not (isinstance(want, str) and SHA256_RE.match(want) and got == want):
            mismatched.append(rel)
    gate("staged_bytes_match_pins", not mismatched, ",".join(mismatched))

    failed = [g["gate"] for g in gates if not g["ok"]]
    return {
        "ok": not failed,
        "failed": failed,
        "gates": gates,
        "inventory": sorted(inventory),
        "pinned": sorted(pinned),
        "on_disk": sorted(on_disk),
        "engine_dir": str(engine_dir),
    }


def verify_manifest(manifest: dict[str, Any], *, repo: str | Path, install: str | Path,
                    launcher: str | Path, phase: str = "pre",
                    expected_post_unit_sha256: Optional[str] = None) -> dict[str, Any]:
    """Fail-closed verification of the pinned identities, selected by release phase (EXT-07).

    ``phase="pre"``  — before any mutation: the install must still be the pre-release identity and
    the unit must still be the archived bytes.
    ``phase="post"`` — after the restart: the install must now be the *candidate* identity, the unit
    must be the regenerated one (``TimeoutStopSec`` at the computed value **and** a valid journaled
    64-hex checksum matching the file's actual bytes), and the drop-ins must be byte-identical to the
    staged ones. The checksum is mandatory here: passing ``None`` is a failed gate, not a skip.
    ``phase="released"`` — the independent post-release check: the same candidate identity set as
    ``post`` (candidate HEAD/tree, regenerated unit, both retained drop-ins), named with a
    ``released`` label so it can be driven without re-entering the freeze.
    ``phase="any"``  — structural checks only (used by ``stage``/``status``).

    Every check is reported, in order; ``ok`` is true only when all hold AND every gate could be
    evaluated (a missing file is a failure, never a skip).
    """
    gates: list[dict[str, Any]] = []

    def gate(name: str, ok: Optional[bool], detail: str = "") -> None:
        gates.append({"gate": name, "ok": bool(ok), "detail": detail})

    if phase not in {"pre", "post", "released", "any"}:
        return {"ok": False, "phase": phase, "gates": [{"gate": "phase", "ok": False,
                "detail": f"unknown phase {phase!r}"}], "failed": ["phase"]}

    pre = manifest.get("pre_release") or {}
    post = manifest.get("post_release") or {}
    cand = manifest.get("candidate_commit") or ""
    gate("schema", manifest.get("schema") == MANIFEST_SCHEMA, f"schema={manifest.get('schema')!r}")
    gate("candidate_commit_shape", len(cand) == 40 and all(c in "0123456789abcdef" for c in cand), cand)
    present = object_present(repo, cand) and object_present(install, cand)
    gate("candidate_available", present, f"repo={object_present(repo, cand)} install={object_present(install, cand)}")
    if present:
        gate("candidate_tree", object_tree(repo, cand) == manifest.get("candidate_tree"),
             f"manifest={manifest.get('candidate_tree')} observed={object_tree(repo, cand)}")
        gate("candidate_tree_matches_install", object_tree(install, cand) == manifest.get("candidate_tree"),
             f"install={object_tree(install, cand)}")
    else:
        gate("candidate_tree", False, "candidate absent")
        gate("candidate_tree_matches_install", False, "candidate absent")

    for path, pinned in (manifest.get("engine") or {}).items():
        want = (pinned or {}).get("blob_sha256")
        got = blob_sha256(repo, cand, path) if present else None
        gate(f"blob:{path}", bool(want) and got == want, f"pinned={want} observed={got}")
        on_disk = sha256_file(Path(repo) / path)
        gate(f"worktree_matches_candidate:{path}", on_disk == got,
             "worktree file differs from the pinned blob" if on_disk != got else "")

    want_launcher = (manifest.get("launcher") or {}).get("sha256")
    gate("launcher_sha256", sha256_file(launcher) == want_launcher and bool(want_launcher),
         f"pinned={want_launcher} observed={sha256_file(launcher)}")
    gate("launcher_path", str((manifest.get("launcher") or {}).get("path")) == str(launcher),
         str((manifest.get("launcher") or {}).get("path")))

    ident = git_identity(install)
    gate("install_readable", bool(ident.get("ok")), str(ident.get("reason") or ""))
    dirty = ident.get("dirty_files")
    gate("install_clean", dirty == [], f"{len(dirty or [])} changed path(s)")

    if phase in {"pre", "post", "released"}:
        want_set = pre if phase == "pre" else post
        label = phase
        gate(f"install_head_{label}", ident.get("head") == want_set.get("install_head"),
             f"expected={want_set.get('install_head')} observed={ident.get('head')}")
        gate(f"install_tree_{label}", ident.get("tree") == want_set.get("install_tree"),
             f"expected={want_set.get('install_tree')} observed={ident.get('tree')}")
        unit_want = (want_set.get("unit") or {})
        unit_path = unit_want.get("path") or (manifest.get("unit") or {}).get("path") \
            or "/etc/systemd/system/hermes-gateway.service"
        if phase == "pre":
            gate(f"unit_sha256_{label}", sha256_file(unit_path) == unit_want.get("sha256") and bool(unit_want.get("sha256")),
                 f"pinned={unit_want.get('sha256')} observed={sha256_file(unit_path)}")
        else:
            want_timeout = unit_want.get("timeout_stop_sec")
            observed = unit_timeout_stop_seconds(unit_path)
            gate(f"unit_timeout_{label}", observed is not None and want_timeout is not None
                 and abs(float(observed) - float(want_timeout)) < 1e-6,
                 f"expected TimeoutStopSec={want_timeout} observed={observed}")
            # EXT-11: the exact-byte comparison is mandatory in post/released. An absent, empty or
            # malformed checksum is a NO_GO gate, never a skipped one -- otherwise a missing journal
            # receipt would leave only the timeout-value check, which a different unit still passes.
            supplied = (expected_post_unit_sha256 or "").strip().lower()
            observed_sha = sha256_file(unit_path)
            gate(f"unit_sha256_{label}",
                 bool(SHA256_RE.match(supplied)) and observed_sha == supplied,
                 f"journal={supplied or '<absent>'} observed={observed_sha}")
        for name, want in (want_set.get("dropins") or {}).items():
            path = Path(unit_path).parent / f"{Path(unit_path).name}.d" / name
            got = sha256_file(path)
            gate(f"dropin_{label}:{name}", bool(want) and got == want, f"pinned={want} observed={got}")

    sup = manifest.get("supervisor") or {}
    plan = envelope_plan()
    runtime_max = sup.get("runtime_max_sec")
    gate("supervisor_envelope", isinstance(runtime_max, int) and runtime_max >= plan["required_sec"],
         f"runtime_max_sec={runtime_max} required_sec={plan['required_sec']}")
    failed = [row["gate"] for row in gates if row["ok"] is not True]
    return {"ok": not failed, "phase": phase, "gates": gates, "failed": failed,
            "candidate_commit": cand, "candidate_tree": manifest.get("candidate_tree")}


def verify_unit_receipt(journal: Optional[dict[str, Any]], *, candidate: str,
                        run_id: Optional[str] = None,
                        unit_path: Optional[str | Path] = None) -> dict[str, Any]:
    """Fail-closed recovery of the regenerated unit checksum from the release journal (EXT-11).

    The independent ``--verify-only`` path needs the regenerated unit's SHA-256 to prove *exact
    bytes*. Recovering it by scanning journal notes is not enough: a missing journal, a journal
    without a ``unit`` receipt, or a truncated note used to yield an empty string, which the
    manifest check then treated as "no expectation" and skipped. This function is the only way the
    checksum may be obtained, and it refuses rather than returning anything unusable.

    A receipt is accepted only when **all** of the following hold:

    * the journal exists, parses, and names ``release == RELEASE`` and a 40-hex ``candidate``;
    * that recorded candidate equals *candidate* (and *run_id* matches when supplied);
    * a ``deploy`` receipt names ``head == candidate`` (the attempt actually deployed it);
    * a ``unit`` receipt follows that deploy receipt, carries the same ``candidate``, and records
      a ``sha256`` that is exactly 64 lowercase hex characters.

    Returns ``{"ok": True, "sha256": ..., "run_id": ..., "unit_path": ...}`` or
    ``{"ok": False, "failed": [gate, ...]}``. Callers treat ``ok is not True`` as NO_GO.
    """
    gates: list[dict[str, Any]] = []

    def gate(name: str, ok: Optional[bool], detail: str = "") -> None:
        gates.append({"gate": name, "ok": bool(ok), "detail": detail})

    def done() -> dict[str, Any]:
        failed = [row["gate"] for row in gates if row["ok"] is not True]
        return {"ok": not failed, "failed": failed, "gates": gates}

    want_cand = (candidate or "").strip().lower()
    gate("candidate_shape",
         len(want_cand) == 40 and all(c in "0123456789abcdef" for c in want_cand), want_cand)
    if journal is None or not isinstance(journal, dict):
        gate("journal", False, "missing or unreadable")
        return done()
    gate("release", journal.get("release") == RELEASE, str(journal.get("release")))
    journal_cand = str(journal.get("candidate") or "").strip().lower()
    gate("journal_candidate", journal_cand == want_cand,
         f"journal={journal_cand or '<absent>'} expected={want_cand}")
    journal_run = str(journal.get("run_id") or "").strip()
    gate("journal_run_id", bool(journal_run), journal_run or "<absent>")
    if run_id is not None:
        gate("run_id", journal_run == str(run_id).strip(),
             f"journal={journal_run or '<absent>'} expected={run_id}")

    phases = journal.get("phases") or []
    deploy_at = None
    for i, ph in enumerate(phases):
        if ph.get("phase") != "deploy":
            continue
        head = str(ph.get("head") or "").strip().lower()
        if head == want_cand:
            deploy_at = i
    gate("deploy_receipt", deploy_at is not None,
         f"no deploy receipt for candidate {want_cand}" if deploy_at is None
         else f"index={deploy_at}")

    unit_record: Optional[dict[str, Any]] = None
    if deploy_at is not None:
        for ph in phases[deploy_at + 1:]:
            if ph.get("phase") == "unit":
                unit_record = ph
    if deploy_at is None:
        gate("unit_receipt", False, "no bounded deploy receipt to follow")
    elif unit_record is None:
        gate("unit_receipt", False, "no unit receipt after the deploy receipt")
    else:
        rec_cand = str(unit_record.get("candidate") or "").strip().lower()
        gate("unit_receipt", rec_cand == want_cand,
             f"receipt candidate={rec_cand or '<absent>'} expected={want_cand}")
        recorded = str(unit_record.get("sha256") or "").strip().lower()
        gate("unit_checksum", bool(SHA256_RE.match(recorded)),
             f"recorded={recorded or '<absent>'}" if recorded else "no sha256 recorded")
        if unit_path is not None:
            rec_path = str(unit_record.get("unit_path") or "")
            gate("unit_path", rec_path == str(unit_path),
                 f"journal={rec_path or '<absent>'} expected={unit_path}")

    result = done()
    if result["ok"] and unit_record is not None:
        result.update({"sha256": str(unit_record.get("sha256")).strip().lower(),
                       "run_id": journal_run,
                       "unit_path": str((unit_record or {}).get("unit_path") or "")})
    return result


# --------------------------------------------------------------------------- envelope
def required_envelope_seconds(*, stop_timeout: int = 300, start_timeout: int = 90,
                              quiesce: int = QUIESCE_MAX_SECONDS,
                              rollback: bool = True) -> int:
    """Upper bound on one release: quiesce + archive + deploy + restart + verify (+ one rollback).

    A rollback restart is budgeted because the supervisor must still be alive to *run* it; a
    supervisor killed at the first restart's timeout would leave the service stopped.
    """
    restart = stop_timeout + start_timeout + RESTART_MARGIN_SECONDS
    total = quiesce + ARCHIVE_MAX_SECONDS + DEPLOY_MAX_SECONDS + restart + VERIFY_MAX_SECONDS
    if rollback:
        total += restart
    return total


def envelope_plan(*, stop_timeout: int = 300, start_timeout: int = 90) -> dict[str, Any]:
    required = required_envelope_seconds(stop_timeout=stop_timeout, start_timeout=start_timeout)
    # Round up to a whole 5 minutes so the unit is readable; never below the requirement.
    runtime_max = ((required + ENVELOPE_SAFETY_SECONDS + 299) // 300) * 300
    return {"required_sec": required, "runtime_max_sec": runtime_max,
            "safety_sec": ENVELOPE_SAFETY_SECONDS, "stop_timeout": stop_timeout,
            "start_timeout": start_timeout, "cover": ["quiesce", "archive", "deploy",
                                                      "restart", "verify", "rollback"]}


# --------------------------------------------------------------------------- admission
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def current_epoch(db: str | Path) -> Optional[int]:
    """The live runtime epoch (read-only). None when the table/file cannot be read."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only=1")
        row = conn.execute("SELECT epoch FROM runtime_epoch WHERE singleton=1").fetchone()
        return int(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        conn.close()


STALE_ADMISSION_HOURS = 6.0


def live_admissions(db: str | Path, epoch: int, *, now: Optional[float] = None) -> Optional[dict[str, int]]:
    """Split the live epoch's non-terminal admissions into still-live and provably stale.

    A row is *stale* only when its target session's ``last_activity_at`` is older than
    ``STALE_ADMISSION_HOURS`` — i.e. the session cannot still be executing that admission. Anything
    we cannot date (missing session row, NULL/garbage timestamp) stays **live**, so the gate fails
    closed. ``total`` is what the quiet gate uses minus ``stale``; both numbers are always reported.
    """
    import sqlite3
    now = time.time() if now is None else now
    try:
        conn = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only=1")
        rows = conn.execute(
            "SELECT a.admission_id, s.last_activity_at FROM session_admissions a "
            "LEFT JOIN sessions s ON s.id = a.target_session_id "
            "WHERE a.owner_epoch=? AND a.status IN ('queued','started','unknown')", (epoch,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    stale = 0
    for _admission_id, last in rows:
        try:
            if last is not None and (now - float(last)) > STALE_ADMISSION_HOURS * 3600:
                stale += 1
        except (TypeError, ValueError):
            continue
    return {"total": len(rows), "stale": stale, "live": len(rows) - stale}


FREEZE_PRINCIPAL_PREFIX = "release-freeze:"
DRAIN_MAX_AGE_SECONDS = 3600.0


def _machine_epoch() -> str:
    """This instantiation's identity ("<boot_id>:<pid1_start>"), or "" when unreadable.

    Mirrors ``gateway.drain_control.current_instantiation_epoch`` without importing the gateway
    package: the release must be able to reason about a marker while the install is mid-deploy.
    """
    boot = pid1 = ""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        pid1 = Path("/proc/1/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    return f"{boot}:{pid1}" if (boot or pid1) else ""


def marker_state(marker: Any, *, now: Optional[float] = None) -> dict[str, Any]:
    """Is *marker* a live release freeze? Same activeness rule the gateway applies, plus the
    release's own principal namespace, so an operator drain or a leaked marker cannot be reported
    as "the freeze is engaged".

    Lenient where the gateway is lenient: a marker with no ``requested_at``/``epoch`` is accepted
    (legacy shape) rather than treated as absent.
    """
    now = time.time() if now is None else now
    if marker is None:
        return {"present": False, "active": False, "why": "absent"}
    if not isinstance(marker, dict):
        return {"present": True, "active": False, "why": "unreadable"}
    principal = str(marker.get("principal") or "")
    if not principal.startswith(FREEZE_PRINCIPAL_PREFIX):
        return {"present": True, "active": False, "why": f"principal {principal!r} is not a release freeze"}
    epoch = marker.get("epoch")
    mine = _machine_epoch()
    if epoch and mine and str(epoch) != mine:
        return {"present": True, "active": False, "why": "stale epoch (survived an instantiation change)"}
    stamp = marker.get("requested_at")
    if stamp is None:
        return {"present": True, "active": True, "why": "unstamped marker"}
    try:
        age = now - datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return {"present": True, "active": True, "why": "unparsable requested_at"}
    if age > DRAIN_MAX_AGE_SECONDS:
        return {"present": True, "active": False, "why": f"expired ({age / 60:.0f} min old)"}
    return {"present": True, "active": True, "why": "fresh"}


def admission_report(*, home: Optional[Path] = None) -> dict[str, Any]:
    """Prove the freeze is engaged and nothing admitted is still executing.

    Fails closed: a state file that cannot be read, an unreadable database, or an unknown epoch is
    ``ok=False`` with an ``unevaluable`` reason — never "probably idle".
    """
    home = home or hermes_home()
    reasons: list[str] = []
    facts: dict[str, Any] = {}

    state = _read_json(gateway_state_path(home))
    if not isinstance(state, dict):
        reasons.append("gateway_state_unreadable")
        facts["gateway_state"] = None
    else:
        facts["gateway_state"] = state.get("gateway_state")
        facts["active_agents"] = state.get("active_agents")
        facts["updated_at"] = state.get("updated_at")

    marker = _read_json(drain_marker_path(home))
    freeze = marker_state(marker)
    facts["drain_marker_present"] = drain_marker_path(home).exists()
    facts["drain_marker_active"] = freeze["active"]
    facts["drain_marker_principal"] = (marker or {}).get("principal") if isinstance(marker, dict) else None
    facts["drain_marker_epoch"] = (marker or {}).get("epoch") if isinstance(marker, dict) else None
    facts["freeze_detail"] = freeze["why"]
    facts["estop_present"] = estop_marker_path(home).exists()

    db = state_db_path(home)
    epoch = current_epoch(db)
    facts["epoch"] = epoch
    if epoch is None:
        reasons.append("epoch_unevaluable")
        facts["live_admissions"] = None
    else:
        split = live_admissions(db, epoch)
        if split is None:
            reasons.append("admissions_unevaluable")
            facts["live_admissions"] = None
        else:
            facts["live_admissions"] = split["live"]
            facts["stale_admissions"] = split["stale"]
            facts["admission_rows"] = split["total"]

    if not isinstance(state, dict) or type(facts.get("active_agents")) is not int:
        if "gateway_state_unreadable" not in reasons:
            reasons.append("active_agents_unevaluable")
    elif facts["active_agents"] > 0:
        reasons.append(f"active_agents={facts['active_agents']}")
    if type(facts.get("live_admissions")) is int and facts["live_admissions"] > 0:
        reasons.append(f"live_admissions={facts['live_admissions']}")
    if not facts["drain_marker_active"]:
        reasons.append("freeze_not_engaged")
    if not facts["estop_present"]:
        reasons.append("estop_not_engaged")
    evaluable = not any(r.endswith("_unevaluable") or r == "gateway_state_unreadable" for r in reasons)
    return {"ok": not reasons, "evaluable": evaluable, "reasons": reasons, "facts": facts,
            "mechanisms": list(FREEZE_MECHANISMS)}


# --------------------------------------------------------------------------- journal
def read_journal(path: str | Path = JOURNAL_DEFAULT) -> Optional[dict[str, Any]]:
    data = _read_json(Path(path))
    return data if isinstance(data, dict) else None


def write_journal(journal: dict[str, Any], path: str | Path = JOURNAL_DEFAULT) -> dict[str, Any]:
    """Atomically replace the journal (fsync + rename), creating its directory if needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return journal


def phase_record(journal: Optional[dict[str, Any]], *, phase: str, mutated: bool = False,
                 restart_attempts: Optional[int] = None, result: Optional[str] = None,
                 note: str = "", extra: Optional[dict[str, Any]] = None,
                 run_id: Optional[str] = None) -> dict[str, Any]:
    """Append (or update) a phase in the journal, keeping the full ordered history.

    *extra* adds structured, machine-checkable fields to the record (EXT-11: the ``unit`` receipt
    carries ``candidate``, ``sha256`` and ``unit_path`` so the checksum is never recovered by
    scraping free text). Every record also pins ``candidate`` when the caller supplies it, so a
    journal can never be replayed against a different candidate.
    """
    journal = dict(journal or {})
    journal.setdefault("release", RELEASE)
    if run_id:
        journal["run_id"] = run_id          # the engine's attempt id, not a fresh timestamp
    else:
        journal.setdefault("run_id", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    journal.setdefault("phases", [])
    record: dict[str, Any] = {"phase": phase, "at": datetime.now(timezone.utc).isoformat(),
                             "note": note}
    for key, value in (extra or {}).items():
        if value is not None:
            record[key] = value
            if key == "candidate":
                journal["candidate"] = value      # journal-level: the attempt's one candidate
    journal["phases"].append(record)
    journal["phase"] = phase
    journal["mutated"] = bool(journal.get("mutated")) or bool(mutated)
    if restart_attempts is not None:
        journal["restart_attempts"] = int(restart_attempts)
    if result is not None:
        journal["result"] = result
    return journal


def decide_failure(phase: str, *, mutated: bool, restart_attempts: int = 0) -> dict[str, Any]:
    """What a failure at *phase* is allowed to do, decided before the release starts (EXT-08).

    The owner's rule is simple and this function exists so no code path can forget it: **a failure
    before the first mutation changes nothing and restarts nothing.** It releases the freeze marker
    this run created, journals the abort and exits NO_GO. The rollback arm exists to undo *deployed*
    changes, so reaching it with ``mutated=False`` would be a defect, not a safety measure — a drift
    found during pre-mutation revalidation is exactly that case.

    Actions:
      ``release_freeze``  pre-mutation abort: zero deployment changes, zero restarts, exit 2
      ``rollback``        mutation happened: restore the deployed identity and restart once
      ``needs_human``     mutation may have happened but state is unknown — keep the freeze, never
                          restart blind, and never restart a second time
    """
    if phase not in PHASES:
        return {"action": "needs_human", "restart": False, "exit_code": 2,
                "reason": f"unknown phase {phase!r}: refusing to guess"}
    if not mutated:
        if phase not in PRE_MUTATION_PHASES:
            return {"action": "needs_human", "restart": False, "exit_code": 2,
                    "reason": f"phase {phase!r} is post-mutation but nothing was recorded as mutated"}
        return {"action": "release_freeze", "restart": False, "exit_code": 2,
                "reason": f"NO_GO at {phase} before any mutation: nothing deployed, nothing restarted"}
    if restart_attempts >= MAX_RESTART_ATTEMPTS:
        return {"action": "needs_human", "restart": False, "exit_code": 2,
                "reason": f"mutation at {phase} failed after {restart_attempts} restart attempt(s): "
                          "keep the freeze and inspect"}
    return {"action": "rollback", "restart": True, "exit_code": 2,
            "reason": f"mutation at {phase} failed: restoring the deployed identity needs one restart"}


def decide_recovery(journal: Optional[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic action for a supervisor that stopped without settling the journal.

    *facts* the caller must supply (all optional; anything missing becomes NEEDS_HUMAN, never a
    guess)::

        {"install_head": str|None, "candidate": str|None, "deployed": str|None,
         "service_active": bool|None, "freeze_present": bool|None, "estop_present": bool|None}

    Actions:
      ``none``                  nothing was started (or it already settled) — verify and exit
      ``unfreeze``              no mutation happened; release the freeze and leave service state alone
      ``complete_verification`` mutation completed and the service looks healthy — finish verifying
      ``rollback``              mutation may have half-applied — restore deployed identity and restart
      ``needs_human``           state genuinely unknown — keep the freeze, page a human
    """
    if journal is None:
        return {"action": "none", "reason": "no journal: nothing was started"}
    if journal.get("result") in SETTLED_RESULTS:
        if facts.get("freeze_present") or facts.get("estop_present"):
            return {"action": "unfreeze", "reason": f"settled as {journal['result']} but a freeze marker remains"}
        return {"action": "none", "reason": f"already settled as {journal.get('result')}"}

    phase = journal.get("phase")
    mutated = bool(journal.get("mutated")) or phase in PHASES_AFTER_MUTATION
    head, cand, dep = facts.get("install_head"), facts.get("candidate"), facts.get("deployed")
    if phase not in PHASES:
        return {"action": "needs_human", "reason": f"journal is corrupt: unknown phase {phase!r}"}
    if not mutated:
        return {"action": "unfreeze", "reason": f"interrupted at {phase} before any mutation"}
    if head is None or not cand or not dep:
        return {"action": "needs_human", "reason": "cannot read install identity during recovery"}
    if phase == "rollback":
        if head == dep and facts.get("service_active") is True:
            return {"action": "unfreeze", "reason": "rollback completed and the deployed service is healthy"}
        return {"action": "needs_human", "reason": "rollback did not complete; keep the freeze and inspect"}
    if head == cand:
        if facts.get("service_active") is True:
            return {"action": "complete_verification", "reason": "candidate is deployed and the service is active"}
        if facts.get("service_active") is False:
            return {"action": "rollback", "reason": "candidate is checked out but the service is not active"}
        return {"action": "needs_human", "reason": "candidate deployed; service state unknown"}
    if head == dep:
        return {"action": "unfreeze", "reason": "install is still at the deployed identity; nothing applied"}
    return {"action": "needs_human", "reason": f"install HEAD {head} matches neither candidate nor deployed"}


def refresh_drain_marker(*, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Bump ``requested_at`` on the live marker without importing the gateway package.

    Identical effect to :func:`gateway.drain_control.refresh_drain_request`; kept here so the
    supervisor's keep-alive needs only the standard library.
    """
    path = drain_marker_path(home)
    body = _read_json(path)
    if not isinstance(body, dict) or not body:
        return None
    body = dict(body)
    body["requested_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return body


def service_active(unit: str = "hermes-gateway.service") -> Optional[bool]:
    try:
        proc = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState", "--value"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() == "active"


# --------------------------------------------------------------------------- CLI
def _emit(payload: dict[str, Any], code: int) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RESTORE-002 release control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest", help="write or verify the pinned identity manifest")
    m.add_argument("--write", action="store_true")
    m.add_argument("--verify", action="store_true")
    m.add_argument("--bootstrap", action="store_true",
                   help="write the initial launcher-only manifest (no candidate staged yet)")
    m.add_argument("--source-commit", default=None,
                   help="bootstrap: the commit the launcher was taken from")
    m.add_argument("--path", default=MANIFEST_DEFAULT)
    m.add_argument("--repo", default=os.getcwd())
    m.add_argument("--install", default="/home/hermes/.hermes/hermes-agent")
    m.add_argument("--launcher", default="/usr/local/lib/hermes-release/hermes-release-002")
    m.add_argument("--candidate", default="")
    m.add_argument("--expected-install-head", default="")
    m.add_argument("--unit", default="/etc/systemd/system/hermes-gateway.service")
    m.add_argument("--phase", default="pre", choices=["pre", "post", "released", "any"],
                   help="which pinned identity set to verify (EXT-07)")
    m.add_argument("--expected-post-unit-sha256", dest="expected_post_unit_sha256", default=None,
                   help="journaled checksum of the regenerated unit, when the caller has it")

    ef = sub.add_parser("engine-files", help="the authoritative engine inventory (EXT-12)")
    ef.add_argument("--set", dest="which", default="staged", choices=["staged", "pinned"])

    au = sub.add_parser("engine-audit", help="staged vs pinned engine inventory audit (EXT-12)")
    au.add_argument("--manifest", default=MANIFEST_DEFAULT)
    au.add_argument("--engine-dir", dest="engine_dir", required=True)

    a = sub.add_parser("admission", help="freeze/quiescence facts (read-only)")
    a.add_argument("--home", default="")

    e = sub.add_parser("envelope", help="supervisor envelope arithmetic")
    e.add_argument("--stop-timeout", type=int, default=300)
    e.add_argument("--start-timeout", type=int, default=90)

    r = sub.add_parser("recover-plan", help="decision for an interrupted supervisor")
    r.add_argument("--journal", default=JOURNAL_DEFAULT)
    r.add_argument("--repo", default="/home/hermes/.hermes/hermes-agent")
    r.add_argument("--candidate", default="")
    r.add_argument("--deployed", default="")

    f = sub.add_parser("failure", help="what a failure at a phase may do (EXT-08)")
    f.add_argument("--phase", required=True)
    f.add_argument("--mutated", action="store_true")
    f.add_argument("--restart-attempts", type=int, default=0)

    d = sub.add_parser("refresh-drain", help="keep-alive: re-stamp the freeze marker")
    d.add_argument("--home", default="")

    j = sub.add_parser("journal", help="read or append a phase")
    j.add_argument("--path", default=JOURNAL_DEFAULT)
    j.add_argument("--phase", default="")
    j.add_argument("--mutated", action="store_true")
    j.add_argument("--restart-attempts", type=int, default=None)
    j.add_argument("--result", default=None)
    j.add_argument("--note", default="")
    j.add_argument("--candidate", default=None, help="structured: the attempt's exact candidate")
    j.add_argument("--head", default=None, help="structured: deployed HEAD (deploy receipts)")
    j.add_argument("--sha256", dest="sha256", default=None,
                   help="structured: the regenerated unit checksum (unit receipts)")
    j.add_argument("--unit-path", default=None, help="structured: the unit file hashed")
    j.add_argument("--run-id", dest="run_id", default=None,
                   help="structured: the attempt id this record belongs to")

    u = sub.add_parser("unit-receipt",
                       help="fail-closed recovery of the regenerated unit checksum (EXT-11)")
    u.add_argument("--journal", default=JOURNAL_DEFAULT)
    u.add_argument("--candidate", required=True)
    u.add_argument("--run-id", default=None, help="when supplied, the journal run id must match")
    u.add_argument("--unit-path", default=None, help="when supplied, the journal path must match")
    u.add_argument("--print", dest="print_field", default=None, choices=["sha256"],
                   help="on success print only this field; JSON detail goes to stderr")

    args = parser.parse_args(argv)

    if args.cmd == "envelope":
        return _emit(envelope_plan(stop_timeout=args.stop_timeout, start_timeout=args.start_timeout), 0)

    if args.cmd == "admission":
        report = admission_report(home=Path(args.home) if args.home else None)
        # 0 frozen/idle, 2 proven NO_GO, 3 something could not be evaluated (never treated as idle).
        return _emit(report, 0 if report["ok"] else (2 if report["evaluable"] else 3))

    if args.cmd == "failure":
        decision = decide_failure(args.phase, mutated=args.mutated,
                                  restart_attempts=args.restart_attempts)
        return _emit(decision, decision["exit_code"])

    if args.cmd == "refresh-drain":
        body = refresh_drain_marker(home=Path(args.home) if args.home else None)
        return _emit({"refreshed": body is not None, "marker": body}, 0 if body else 3)

    if args.cmd == "journal":
        if not args.phase:
            journal = read_journal(args.path)
            return _emit({"journal": journal}, 0 if journal else 3)
        extra = {"candidate": args.candidate, "head": args.head,
                 "sha256": args.sha256, "unit_path": args.unit_path}
        journal = phase_record(read_journal(args.path), phase=args.phase, mutated=args.mutated,
                               restart_attempts=args.restart_attempts, result=args.result,
                               note=args.note, extra=extra, run_id=args.run_id)
        write_journal(journal, args.path)
        return _emit({"journal": journal}, 0)

    if args.cmd == "unit-receipt":
        receipt = verify_unit_receipt(read_journal(args.journal), candidate=args.candidate,
                                      run_id=args.run_id, unit_path=args.unit_path)
        if args.print_field:
            if receipt.get("ok") and receipt.get(args.print_field):
                print(receipt[args.print_field])
                json.dump(receipt, sys.stderr, sort_keys=True)
                sys.stderr.write("\n")
                return 0
            json.dump(receipt, sys.stderr, indent=2, sort_keys=True)
            sys.stderr.write("\n")
            return 2
        return _emit(receipt, 0 if receipt.get("ok") else 2)

    if args.cmd == "recover-plan":
        journal = read_journal(args.journal)
        facts = {"install_head": git_identity(args.repo).get("head"), "candidate": args.candidate or None,
                 "deployed": args.deployed or None, "service_active": service_active(),
                 "freeze_present": drain_marker_path().exists(), "estop_present": estop_marker_path().exists()}
        decision = decide_recovery(journal, facts)
        decision["facts"] = facts
        return _emit(decision, 0 if decision["action"] != "needs_human" else 2)

    if args.cmd == "manifest":
        if args.bootstrap:
            if args.write or args.verify:
                return _emit({"ok": False, "failed": ["bootstrap_conflicts_with_write_or_verify"]}, 64)
            launcher_sha = sha256_file(args.launcher)
            if not launcher_sha:
                return _emit({"ok": False, "failed": ["launcher_unreadable"], "launcher": args.launcher}, 65)
            if not args.source_commit or len(args.source_commit) != 40:
                return _emit({"ok": False, "failed": ["source_commit_required"]}, 64)
            payload = {
                "schema": MANIFEST_SCHEMA, "release": RELEASE, "state": "bootstrapped",
                "launcher": {"path": str(args.launcher), "sha256": launcher_sha},
                "source_commit": args.source_commit,
                "candidate_commit": "", "candidate_tree": None,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
            target = Path(args.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, target)   # atomic: a reader never sees a half-written manifest
            readback = _read_json(target)
            if not isinstance(readback, dict) or readback.get("launcher", {}).get("sha256") != launcher_sha:
                return _emit({"ok": False, "failed": ["manifest_readback_mismatch"], "path": str(target)}, 65)
            return _emit({"ok": True, "state": "bootstrapped", "path": str(target), "manifest": payload}, 0)
        if args.write:
            # EXT-12: the pinned set is the shared inventory, never a second literal list.
            manifest = build_manifest(
                repo=args.repo, candidate=args.candidate, install=args.install, launcher=args.launcher,
                unit=args.unit, dropins=[f"{args.unit}.d/90-recovery-hold.conf",
                                         f"{args.unit}.d/95-timeout-stop.conf"],
                expected_install_head=args.expected_install_head)
            Path(args.path).parent.mkdir(parents=True, exist_ok=True)
            Path(args.path).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            return _emit({"manifest": manifest}, 0)
        manifest = _read_json(Path(args.path))
        if not isinstance(manifest, dict):
            return _emit({"ok": False, "failed": ["manifest_unreadable"]}, 3)
        report = verify_manifest(manifest, repo=args.repo, install=args.install, launcher=args.launcher,
                                 phase=args.phase,
                                 expected_post_unit_sha256=args.expected_post_unit_sha256)
        return _emit(report, 0 if report["ok"] else 2)

    if args.cmd == "engine-files":
        for rel in engine_inventory(pinned=args.which == "pinned"):
            print(rel)
        return 0

    if args.cmd == "engine-audit":
        manifest = _read_json(Path(args.manifest))
        if not isinstance(manifest, dict):
            return _emit({"ok": False, "failed": ["manifest_unreadable"], "path": args.manifest}, 3)
        report = audit_engine(manifest, engine_dir=args.engine_dir)
        return _emit(report, 0 if report["ok"] else 2)

    return 64


if __name__ == "__main__":
    sys.exit(main())
