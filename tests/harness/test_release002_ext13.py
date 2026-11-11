"""EXT-RESTORE-002-13 — the baseline/candidate boundary.

The defect this module pins down: the release tool qualified itself in a world where
``install == repo == candidate``. Two assumptions survived that modelling and both fired on the
real RESTORE-001 baseline (``e05128fe``), which predates ``gateway/session_resume_plan.py``:

1. the pre-deployment session inventory imported that planner from the *installed* gateway, so an
   installation that legitimately does not have the module could not be measured — the
   ``session_inventory`` gate became unevaluable;
2. pre-phase manifest verification read candidate blobs out of the *installed* worktree, so six
   ``worktree_matches_candidate`` gates failed by construction before any merge could have put the
   candidate there.

The correction separates three identities (installed baseline / candidate / staged engine) and
loads the classifier from the candidate's own git blob instead of from whichever gateway happens to
be installed. These tests prove the separation, prove each failure mode still fails closed, and
prove the pre/post inventories agree without touching the store.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(TESTS))

import release002_control as ctl  # noqa: E402
import release002_preflight as pre  # noqa: E402

# Reuse the real-launcher fixture from EXT-12: bootstrap/stage against disposable Git repositories.
from test_release002_ext12 import (  # noqa: E402
    SCRATCH,
    bootstrap_lib,
    git,
    make_repo,
    run,
    staged_release,
)

PLANNER_REL = "gateway/session_resume_plan.py"
PY3 = "/usr/bin/python3.13"
INVENTORY = HARNESS / "restore002_session_inventory.py"

# --------------------------------------------------------------------------------------------------
# A candidate-only planner: the module the baseline does not have. Deliberately self-contained (it
# imports nothing from ``gateway``) so loading it can only have come from the pinned blob.
# --------------------------------------------------------------------------------------------------
PLANNER_SRC = '''\
"""Candidate-only session planner (test double loaded from the pinned candidate blob).

It mirrors the two features of the real module that matter to the loader: it declares
``@dataclass`` types -- dataclasses resolves the defining module through ``sys.modules`` while the
class is built, which is how the first version of the loader failed on the real baseline -- and it
opens the store read-only.
"""
import sqlite3
from dataclasses import dataclass

SENTINEL = "pinned-planner-v1"
ACTIVE, IDLE, HUMAN, OBSOLETE, RESUMABLE = "active", "idle", "human", "obsolete", "resumable"


@dataclass
class Fact:
    session_id: str
    started_at: float | None = None
    last_activity_at: float | None = None
    message_count: int = 0
    ended_at: float | None = None
    in_index: bool = False
    routing_session_id: str | None = None
    resume_pending: bool = False
    resume_reason: str | None = None
    suspended: bool = False

    @property
    def last_ts(self):
        return self.last_activity_at or self.started_at


def load_facts(db_path, sessions_json=None):
    """Read-only; the same protection the production planner uses (mode=ro + query_only)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        con.execute("PRAGMA query_only=1")
        rows = list(con.execute(
            "SELECT id, started_at, last_activity_at, message_count, ended_at FROM sessions"))
    finally:
        con.close()
    return [Fact(session_id=str(r[0]), started_at=r[1], last_activity_at=r[2],
                 message_count=int(r[3] or 0), ended_at=r[4]) for r in rows]


def classify(facts, now=None):
    out = {ACTIVE: [], RESUMABLE: [], OBSOLETE: []}
    for fact in facts:
        if fact.ended_at is not None:
            continue
        (out[ACTIVE] if fact.message_count else out[RESUMABLE]).append(fact.session_id)
    return out
'''

# A planner that is present but cannot supply what the inventory needs: it must be refused rather
# than silently treated as an empty inventory.
INCOMPLETE_SRC = '''\
def load_facts(db_path, sessions_json=None):
    return []
'''

# What a *wrong* resolution would find: importing the installed gateway planner explodes. Any test
# that passes while this is on PYTHONPATH proved it never imported the installed module.
HOSTILE_SRC = '''\
raise RuntimeError("the installed gateway planner was imported")
'''

GO_PREFLIGHT = '''\
import sys
sys.exit(0)
'''
NOGO_PREFLIGHT = '''\
import sys
print("stub preflight: operational NO_GO")
sys.exit(2)
'''


@pytest.fixture()
def ws():
    root = SCRATCH / "ext13" / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relocated_unit(ws: Path, fx: dict) -> tuple[dict, Path]:
    """A private copy of the live unit + drop-ins, regenerated the way the release writes them.

    The staged manifest pins the *live* systemd unit (read-only: the fixture never writes to /etc).
    For post/released verification the release regenerates that unit with the pinned
    ``TimeoutStopSec`` embedded, so this copy reproduces those exact bytes in a throwaway directory
    and re-points the manifest at it. Nothing under /etc is created, modified or removed.
    """
    src = Path(fx["manifest"]["pre_release"]["unit"]["path"])
    etc = ws / "etc"
    etc.mkdir()
    unit = etc / src.name
    want = fx["manifest"]["post_release"]["unit"]["timeout_stop_sec"]
    lines = []
    seen = False
    for line in src.read_text(encoding="utf-8").splitlines():
        if line.split("=", 1)[0].strip() in ("TimeoutStopSec", "TimeoutStopUSec"):
            lines.append(f"TimeoutStopSec={int(want)}")
            seen = True
        else:
            lines.append(line)
    if not seen:
        lines.extend(["", "[Service]", f"TimeoutStopSec={int(want)}"])
    unit.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name in sorted((fx["manifest"]["pre_release"].get("dropins") or {})):
        (etc / f"{src.name}.d").mkdir(exist_ok=True)
        (etc / f"{src.name}.d" / name).write_bytes((src.parent / f"{src.name}.d" / name).read_bytes())
    manifest = json.loads(json.dumps(fx["manifest"]))
    for phase in ("pre_release", "post_release"):
        manifest[phase]["unit"]["path"] = str(unit)
    manifest["pre_release"]["unit"]["sha256"] = sha256(unit)
    return manifest, unit


def make_store(path: Path) -> Path:
    """A tiny but real session store: one closed row, one open row with work, one empty open row."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT, source TEXT, "
                    "started_at REAL, last_activity_at REAL, message_count INTEGER, ended_at REAL)")
        con.execute("CREATE TABLE gateway_routing (session_key TEXT, entry_json TEXT)")
        con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", [
            ("closed", "k1", "telegram", 1000.0, 1010.0, 4, 1015.0),
            ("open-with-work", "k2", "telegram", 2000.0, 2010.0, 3, None),
            ("open-empty", "k3", "cli", 3000.0, 3000.0, 0, None),
        ])
        con.commit()
    finally:
        con.close()
    return path


def inventory(ws: Path, repo: Path, cand: str, store: Path, *extra, pythonpath=None):
    env = {"PYTHONPATH": pythonpath} if pythonpath else {}
    args = [PY3, str(INVENTORY), "--candidate", cand, "--source-repo", str(repo),
            "--store", str(store), "--index", str(ws / "sessions.json"), "--summary", *extra]
    # Never inherit an ambient PYTHONPATH: this path must not depend on import resolution.
    return run(args, env=env, cwd=ws)


# --------------------------------------------------------------------------------------------------
# EXT-13A — pinned planner
# --------------------------------------------------------------------------------------------------
class TestPinnedPlanner:
    def test_the_baseline_really_does_predate_the_module(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        assert ctl.blob_sha256(repo, base, PLANNER_REL) is None
        assert ctl.blob_sha256(repo, cand, PLANNER_REL) is not None
        assert ctl.blob_bytes(repo, base, PLANNER_REL) is None
        assert ctl.blob_bytes(repo, cand, PLANNER_REL) == PLANNER_SRC.encode()

    def test_pinned_load_reads_the_blob_and_touches_no_import_state(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        before_path = list(sys.path)
        before_mods = set(sys.modules)
        before_file = Path(sys.executable)

        pin = ctl.load_pinned_module(repo, cand, PLANNER_REL)

        assert pin["ok"] is True, pin
        assert pin["module"].SENTINEL == "pinned-planner-v1"
        assert pin["commit"] == cand and pin["path"] == PLANNER_REL
        assert pin["sha256"] == hashlib.sha256(PLANNER_SRC.encode()).hexdigest()
        assert pin["blob"] == git(repo, "rev-parse", f"{cand}:{PLANNER_REL}")
        # No ambient import machinery was used to get there: the path is untouched, no gateway
        # module (least of all the planner) appeared, and the process interpreter is unchanged.
        assert sys.path == before_path
        assert Path(sys.executable) == before_file
        assert not [m for m in sys.modules if m.startswith("hermes_release_pinned_module")]
        gateway_mods = {m for m in set(sys.modules) - before_mods if m.split(".")[0] == "gateway"}
        assert gateway_mods == set()

    def test_missing_module_fails_closed(self, ws):
        repo, base, cand = make_repo(ws)
        pin = ctl.load_pinned_module(repo, cand, PLANNER_REL)
        assert pin["ok"] is False
        assert "absent" in pin["error"]

    def test_a_module_without_the_required_attributes_is_refused(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            INCOMPLETE_SRC, encoding="utf-8"))
        pin = ctl.load_pinned_module(repo, cand, PLANNER_REL)
        assert pin["ok"] is False
        assert "classify" in pin["error"]

    def test_unsafe_paths_and_malformed_commits_are_refused(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        assert ctl.load_pinned_module(repo, cand, "../gateway/session_resume_plan.py")["ok"] is False
        assert ctl.load_pinned_module(repo, cand, "/etc/passwd")["ok"] is False
        assert ctl.load_pinned_module(repo, "deadbeef", PLANNER_REL)["ok"] is False

    def test_preflight_session_state_refuses_an_absent_planner(self, ws):
        repo, base, cand = make_repo(ws)
        state = pre.session_state(candidate=cand, source_repo=str(repo))
        assert "error" in state
        assert "pinned planner unavailable" in state["error"]

    def test_preflight_session_state_refuses_to_guess_without_a_candidate(self):
        state = pre.session_state(candidate=None, source_repo=str(ROOT))
        assert "error" in state
        assert "never with an installed import" in state["error"]


# --------------------------------------------------------------------------------------------------
# EXT-13A — the inventory: pinned, read-only, fail-closed, identical pre and post
# --------------------------------------------------------------------------------------------------
class TestInventory:
    def test_inventory_classifies_with_the_pinned_candidate_blob(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        store = make_store(ws / "state.db")
        proc = inventory(ws, repo, cand, store)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["planner_source"] == "pinned-candidate-blob"
        assert payload["planner"]["commit"] == cand
        assert payload["planner"]["sha256"] == hashlib.sha256(PLANNER_SRC.encode()).hexdigest()
        assert payload["open_sessions"] == 2
        assert payload["empty_open"] == 1
        assert payload["counts"] == {"active": 1, "resumable": 1, "obsolete": 0}
        assert [row["id"] for row in payload["unresolved"]] == ["open-empty"]

    def test_an_install_time_pythonpath_cannot_hijack_the_classifier(self, ws):
        """The requirement is *no* dependence on ambient resolution — so poison it and re-run."""
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        hostile = ws / "hostile" / "gateway"
        hostile.mkdir(parents=True)
        (hostile / "session_resume_plan.py").write_text(HOSTILE_SRC, encoding="utf-8")
        (hostile.parent / "__init__.py").write_text("", encoding="utf-8")
        store = make_store(ws / "state.db")

        proc = inventory(ws, repo, cand, store, pythonpath=str(hostile.parent))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["planner_source"] == "pinned-candidate-blob"

    def test_inventory_is_argparse_mandatory_on_a_candidate(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        store = make_store(ws / "state.db")
        proc = run([PY3, str(INVENTORY), "--store", str(store)], cwd=ws)
        assert proc.returncode != 0
        assert "candidate" in (proc.stdout + proc.stderr)

    def test_the_store_is_read_only_in_practice(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        store = make_store(ws / "state.db")
        before = (sha256(store), store.stat().st_mtime_ns, store.stat().st_size)
        os.chmod(store, 0o444)  # a mode=ro reader is fine; anything else would fail here
        try:
            proc = inventory(ws, repo, cand, store)
        finally:
            os.chmod(store, 0o644)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (sha256(store), store.stat().st_mtime_ns, store.stat().st_size) == before
        assert not list(ws.glob("state.db-*")), "the inventory must not create journal/WAL files"

    def test_a_malformed_store_fails_closed(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        store = ws / "broken.db"
        store.write_bytes(b"this is not a sqlite database\n")
        proc = inventory(ws, repo, cand, store)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["failed"] == ["session_store"]
        assert "Error" in payload["error"]

    def test_an_unreadable_store_fails_closed(self, ws):
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        proc = inventory(ws, repo, cand, ws / "does-not-exist.db")
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["failed"] == ["session_store"]

    def test_pre_and_post_inventories_are_identical_without_mutating_the_store(self, ws):
        """Pre: source = the release repository. Post: source = the installed candidate clone."""
        repo, base, cand = make_repo(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        store = make_store(ws / "state.db")
        before = (sha256(store), store.stat().st_mtime_ns)

        pre_run = inventory(ws, repo, cand, store)
        installed = ws / "installed"
        assert run(["git", "clone", "-q", str(repo), str(installed)]).returncode == 0
        assert run(["git", "-C", str(installed), "checkout", "-q", cand]).returncode == 0
        post_run = inventory(ws, installed, cand, store)

        assert pre_run.returncode == 0 and post_run.returncode == 0
        a, b = json.loads(pre_run.stdout), json.loads(post_run.stdout)
        assert a["open_sessions_fingerprint_sha256"] == b["open_sessions_fingerprint_sha256"]
        assert a["bucket_ids_sha256"] == b["bucket_ids_sha256"]
        assert a["planner"]["sha256"] == b["planner"]["sha256"]
        assert (sha256(store), store.stat().st_mtime_ns) == before


# --------------------------------------------------------------------------------------------------
# EXT-13B — three identities
# --------------------------------------------------------------------------------------------------
class TestManifestIdentities:
    def test_pre_accepts_a_baseline_install_and_a_candidate_source(self, ws):
        fx = staged_release(ws)
        result = ctl.verify_manifest(fx["manifest"], repo=str(fx["repo"]), install=str(fx["inst"]),
                                     launcher=str(fx["launcher"]), phase="pre",
                                     source=str(fx["repo"]), engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is True, result["failed"]

    def test_pre_never_looks_for_candidate_files_in_the_baseline_worktree(self, ws):
        fx = staged_release(ws, tamper=lambda r: (r / PLANNER_REL).write_text(
            PLANNER_SRC, encoding="utf-8"))
        assert not (fx["inst"] / PLANNER_REL).exists(), "the baseline must not have the module"
        result = ctl.verify_manifest(fx["manifest"], repo=str(fx["repo"]), install=str(fx["inst"]),
                                     launcher=str(fx["launcher"]), phase="pre",
                                     source=str(fx["repo"]), engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is True, result["failed"]
        assert not [g for g in result["gates"] if g["gate"].startswith("worktree_matches_candidate")]

    def test_a_wrong_source_repository_fails_closed(self, ws):
        fx = staged_release(ws)
        foreign = ws / "foreign"
        foreign.mkdir()
        git(foreign, "init", "-q", "-b", "main")
        (foreign / "README").write_text("unrelated\n", encoding="utf-8")
        git(foreign, "add", "-A")
        git(foreign, "commit", "-qm", "unrelated")
        result = ctl.verify_manifest(fx["manifest"], repo=str(foreign), install=str(fx["inst"]),
                                     launcher=str(fx["launcher"]), phase="pre",
                                     source=str(foreign), engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is False
        assert "candidate_available" in result["failed"]

    def test_a_wrong_baseline_identity_fails_closed(self, ws):
        fx = staged_release(ws)
        at_candidate = ws / "at-candidate"
        assert run(["git", "clone", "-q", str(fx["repo"]), str(at_candidate)]).returncode == 0
        assert run(["git", "-C", str(at_candidate), "checkout", "-q", fx["cand"]]).returncode == 0
        result = ctl.verify_manifest(fx["manifest"], repo=str(fx["repo"]),
                                     install=str(at_candidate), launcher=str(fx["launcher"]),
                                     phase="pre", source=str(fx["repo"]),
                                     engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is False
        assert "install_head_pre" in result["failed"]

    def test_a_modified_staged_file_fails_closed(self, ws):
        fx = staged_release(ws)
        victim = fx["engine_dir"] / "harness" / "release002_preflight.py"
        victim.write_text(victim.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        result = ctl.verify_manifest(fx["manifest"], repo=str(fx["repo"]), install=str(fx["inst"]),
                                     launcher=str(fx["launcher"]), phase="pre",
                                     source=str(fx["repo"]), engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is False
        assert "staged_file_bytes:harness/release002_preflight.py" in result["failed"]

    def test_pre_without_a_staged_engine_is_unevaluable(self, ws):
        fx = staged_release(ws)
        result = ctl.verify_manifest(fx["manifest"], repo=str(fx["repo"]), install=str(fx["inst"]),
                                     launcher=str(fx["launcher"]), phase="pre",
                                     source=str(fx["repo"]))
        assert result["ok"] is False
        assert "staged_engine_dir_supplied" in result["failed"]

    def test_a_stale_unit_pin_fails_closed(self, ws):
        fx = staged_release(ws)
        manifest = json.loads(json.dumps(fx["manifest"]))
        manifest["pre_release"]["unit"]["sha256"] = "0" * 64
        result = ctl.verify_manifest(manifest, repo=str(fx["repo"]), install=str(fx["inst"]),
                                     launcher=str(fx["launcher"]), phase="pre",
                                     source=str(fx["repo"]), engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is False
        assert "unit_sha256_pre" in result["failed"]

    def test_post_validates_the_candidate_installation(self, ws):
        fx = staged_release(ws)
        deployed = ws / "deployed"
        assert run(["git", "clone", "-q", str(fx["repo"]), str(deployed)]).returncode == 0
        assert run(["git", "-C", str(deployed), "checkout", "-q", fx["cand"]]).returncode == 0
        manifest, unit = relocated_unit(ws, fx)
        result = ctl.verify_manifest(manifest, repo=str(fx["repo"]), install=str(deployed),
                                     launcher=str(fx["launcher"]), phase="post",
                                     expected_post_unit_sha256=sha256(unit),
                                     source=str(fx["repo"]), engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is True, result["failed"]
        assert [g["gate"] for g in result["gates"] if g["gate"].startswith("worktree_matches")]
        # ... and a unit tree whose *effective* stop timeout falls back to the old 210 s is
        # refused, so the gate is live. The unit ships 210 s and the operator drop-in supplies
        # 300 s, so the drop-in is what has to be rolled back here.
        assert "TimeoutStopSec=300" in unit.read_text(encoding="utf-8")
        dropin = unit.parent / f"{unit.name}.d" / "95-timeout-stop.conf"
        assert dropin.exists()
        dropin.write_text("[Service]\nTimeoutStopSec=210\n", encoding="utf-8")
        stale = ctl.verify_manifest(manifest, repo=str(fx["repo"]), install=str(deployed),
                                    launcher=str(fx["launcher"]), phase="post",
                                    expected_post_unit_sha256=sha256(unit),
                                    source=str(fx["repo"]), engine_dir=str(fx["engine_dir"]))
        assert stale["ok"] is False
        assert "unit_timeout_post" in stale["failed"]

    def test_post_refuses_the_old_baseline(self, ws):
        fx = staged_release(ws)
        unit = fx["manifest"]["pre_release"]["unit"]["path"]
        result = ctl.verify_manifest(fx["manifest"], repo=str(fx["repo"]), install=str(fx["inst"]),
                                     launcher=str(fx["launcher"]), phase="post",
                                     expected_post_unit_sha256=sha256(unit),
                                     source=str(fx["repo"]), engine_dir=str(fx["engine_dir"]))
        assert result["ok"] is False
        assert "install_head_post" in result["failed"]


# --------------------------------------------------------------------------------------------------
# EXT-13C — PLAN is conjunctive
# --------------------------------------------------------------------------------------------------
def stub_preflight(body: str):
    def tamper(repo: Path) -> None:
        (repo / "harness" / "release002_preflight.py").write_text(body, encoding="utf-8")
    return tamper


def plan(fx: dict, ws: Path, *, manifest: Path | None = None, source: Path | None = None,
         archive: Path | None = None):
    arch = archive or (ws / "arch")
    arch.mkdir(parents=True, exist_ok=True)
    env = dict(fx["env"])
    env.update({
        "EXPECT_HEAD": fx["base"],
        "EXPECT_TREE": git(fx["inst"], "rev-parse", "HEAD^{tree}"),
        "HERMES_RELEASE_MANIFEST": str(manifest or fx["manifest_path"]),
        "HERMES_RELEASE_SOURCE": str(source or fx["repo"]),
        # The launcher exports these in production (EXT-13B); PLAN is never run without it.
        "HERMES_RELEASE_LAUNCHER": str(fx["launcher"]),
    })
    return run(["bash", str(fx["engine_dir"] / "harness" / "release002_release.sh"), "--plan",
                "--candidate", fx["cand"], "--archive", str(arch)], env=env, cwd=ws)


class TestPlanIsConjunctive:
    def test_both_gates_passing_is_the_only_way_to_go(self, ws):
        fx = staged_release(ws, tamper=stub_preflight(GO_PREFLIGHT))
        proc = plan(fx, ws)
        assert proc.returncode == 0, proc.stdout[-2500:] + proc.stderr[-1500:]
        assert "DRY RUN RESULT: GO" in proc.stdout

    def test_preflight_pass_with_manifest_fail_is_no_go(self, ws):
        fx = staged_release(ws, tamper=stub_preflight(GO_PREFLIGHT))
        broken = json.loads(json.dumps(fx["manifest"]))
        broken["engine"]["harness/release002_control.py"]["blob_sha256"] = "0" * 64
        path = ws / "manifest-broken.json"
        path.write_text(json.dumps(broken), encoding="utf-8")
        proc = plan(fx, ws, manifest=path)
        assert proc.returncode == 2, proc.stdout[-2500:]
        assert "preflight rc=0" in proc.stdout and "manifest rc=2" in proc.stdout
        assert "DRY RUN RESULT: NO_GO" in proc.stdout
        assert "DRY RUN RESULT: GO" not in proc.stdout

    def test_manifest_pass_with_preflight_fail_is_no_go(self, ws):
        fx = staged_release(ws, tamper=stub_preflight(NOGO_PREFLIGHT))
        proc = plan(fx, ws)
        assert proc.returncode == 2, proc.stdout[-2500:]
        assert "preflight rc=2" in proc.stdout and "manifest rc=0" in proc.stdout
        assert "DRY RUN RESULT: NO_GO" in proc.stdout
        assert "DRY RUN RESULT: GO" not in proc.stdout

    def test_an_unevaluable_manifest_is_never_go(self, ws):
        fx = staged_release(ws, tamper=stub_preflight(GO_PREFLIGHT))
        proc = plan(fx, ws, manifest=ws / "absent-manifest.json")
        assert proc.returncode == 3, proc.stdout[-2500:]
        assert "DRY RUN RESULT: UNEVALUABLE" in proc.stdout
        assert "DRY RUN RESULT: GO" not in proc.stdout

    def test_a_wrong_source_repository_cannot_reach_go(self, ws):
        fx = staged_release(ws, tamper=stub_preflight(GO_PREFLIGHT))
        foreign = ws / "foreign"
        foreign.mkdir()
        git(foreign, "init", "-q", "-b", "main")
        (foreign / "README").write_text("unrelated\n", encoding="utf-8")
        git(foreign, "add", "-A")
        git(foreign, "commit", "-qm", "unrelated")
        proc = plan(fx, ws, source=foreign)
        assert proc.returncode == 2
        assert "manifest rc=2" in proc.stdout
        assert "DRY RUN RESULT: GO" not in proc.stdout

    def test_plan_mutates_nothing(self, ws):
        fx = staged_release(ws, tamper=stub_preflight(GO_PREFLIGHT))
        before = {
            "install": git(fx["inst"], "rev-parse", "HEAD"),
            "journal": (fx["lib"] / "journal.json").exists(),
            "manifest": sha256(fx["manifest_path"]),
            "engine": sha256(fx["engine_dir"] / "harness" / "release002_control.py"),
        }
        proc = plan(fx, ws)
        assert proc.returncode == 0
        assert git(fx["inst"], "rev-parse", "HEAD") == before["install"]
        assert (fx["lib"] / "journal.json").exists() == before["journal"]
        assert sha256(fx["manifest_path"]) == before["manifest"]
        assert sha256(fx["engine_dir"] / "harness" / "release002_control.py") == before["engine"]
