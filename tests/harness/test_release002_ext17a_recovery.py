"""EXT-17A: recovery decisions are bound to the journalled attempt and fail closed.

These tests drive the shipped control plane (``harness/release002_control.py recover-plan``) against a
throwaway git repository, so the install identity it reads is real (``rev-parse``, ``status``) while
nothing on the host is involved. The point is the decision, not the plumbing: a journal that cannot
say what happened must produce ``needs_human``; a journal that can must produce exactly one bounded
action; and a dirty worktree must never be read as "nothing was applied".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTROL = REPO / "harness" / "release002_control.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Two commits: ``deployed`` (older) and ``candidate`` (newer)."""
    repo = tmp_path / "install"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ext17a@example.invalid")
    git(repo, "config", "user.name", "EXT-17A")
    (repo / "a").write_text("deployed\n")
    git(repo, "add", "a")
    git(repo, "commit", "-qm", "deployed identity")
    deployed = git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("candidate\n")
    git(repo, "commit", "-qam", "candidate identity")
    candidate = git(repo, "rev-parse", "HEAD")
    return repo, candidate, deployed


def journal(candidate: str, phases: list[dict], *, result: str | None = None) -> dict:
    body: dict = {"release": "RESTORE-002", "run_id": "EXT17A-REC-TEST", "candidate": candidate,
                  "phases": phases, "phase": phases[-1]["phase"]}
    body["mutated"] = any(p.get("mutated") for p in phases)
    if result:
        body["result"] = result
    return body


def decide(tmp_path: Path, body: dict, *, repo: Path, service: str = "active") -> dict:
    """Run recover-plan with a deterministic service reading and no --deployed override."""
    path = tmp_path / "journal.json"
    path.write_text(json.dumps(body))
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    # mirrors `systemctl show <unit> -p ActiveState --value`, which is what the control plane reads
    (stub / "systemctl").write_text(f'#!/bin/bash\necho "{service}"\nexit 0\n')
    (stub / "systemctl").chmod(0o755)
    env = {**os.environ, "PYTHONPATH": "", "PATH": f"{stub}:{os.environ['PATH']}"}
    proc = subprocess.run(
        [sys.executable, str(CONTROL), "recover-plan", "--journal", str(path), "--repo", str(repo),
         "--candidate", body.get("candidate") or ""],
        capture_output=True, text=True, env=env, check=False,
    )
    return {"rc": proc.returncode, **(json.loads(proc.stdout or "{}"))}


def test_candidate_deployed_and_active_asks_for_verification(tmp_path: Path) -> None:
    repo, candidate, deployed = make_repo(tmp_path)
    body = journal(candidate, [
        {"phase": "deploy-intent", "note": "about to fast-forward", "head": deployed},
        {"phase": "deploy", "note": "deployed", "head": candidate, "mutated": True},
        {"phase": "verify", "note": "interrupted during verification"},
    ])
    out = decide(tmp_path, body, repo=repo)
    assert out["action"] == "complete_verification", out
    assert out["facts"]["install_clean"] is True
    assert out["facts"]["install_head"] == candidate
    # the rollback target came from the journal, not from a caller-supplied flag
    assert out["facts"]["deployed"] == deployed


def test_install_at_the_deployed_identity_unfreezes_without_restart(tmp_path: Path) -> None:
    repo, candidate, deployed = make_repo(tmp_path)
    git(repo, "reset", "-q", "--hard", deployed)
    body = journal(candidate, [
        {"phase": "deploy-intent", "note": "about to fast-forward", "head": deployed},
        {"phase": "deploy", "note": "interrupted", "mutated": True},
    ])
    out = decide(tmp_path, body, repo=repo)
    assert out["action"] == "unfreeze", out


def test_an_unknown_install_head_needs_a_human(tmp_path: Path) -> None:
    repo, candidate, deployed = make_repo(tmp_path)
    (repo / "a").write_text("something else\n")
    git(repo, "commit", "-qam", "unrelated")
    body = journal(candidate, [
        {"phase": "deploy-intent", "head": deployed},
        {"phase": "verify", "mutated": True},
    ])
    out = decide(tmp_path, body, repo=repo)
    assert out["action"] == "needs_human"
    assert "neither candidate nor deployed" in out["reason"]


def test_a_dirty_worktree_at_the_deployed_identity_is_not_clean_enough_to_unfreeze(tmp_path: Path) -> None:
    """The ``install_clean`` fact: matching HEAD with uncommitted bytes is still human work."""
    repo, candidate, deployed = make_repo(tmp_path)
    git(repo, "reset", "-q", "--hard", deployed)
    (repo / "a").write_text("half-applied bytes\n")
    body = journal(candidate, [
        {"phase": "deploy-intent", "head": deployed},
        {"phase": "deploy", "mutated": True},
    ])
    out = decide(tmp_path, body, repo=repo)
    assert out["facts"]["install_head"] == deployed
    assert out["facts"]["install_clean"] is False
    assert out["action"] == "needs_human"
    assert "dirty" in out["reason"]


def test_deploy_intent_alone_does_not_prove_nothing_changed(tmp_path: Path) -> None:
    """Intent recorded, mutation flag absent: release only when the install is provably untouched."""
    repo, candidate, deployed = make_repo(tmp_path)
    git(repo, "reset", "-q", "--hard", deployed)
    (repo / "a").write_text("touched anyway\n")
    body = journal(candidate, [{"phase": "deploy-intent", "head": deployed}])
    dirty = decide(tmp_path, body, repo=repo)
    assert dirty["action"] == "needs_human", dirty

    git(repo, "checkout", "--", "a")
    clean = decide(tmp_path, body, repo=repo)
    assert clean["action"] == "unfreeze", clean


def test_a_journal_that_cannot_name_what_it_deployed_needs_a_human(tmp_path: Path) -> None:
    repo, candidate, _ = make_repo(tmp_path)
    body = journal(candidate, [{"phase": "deploy", "mutated": True}])
    out = decide(tmp_path, body, repo=repo)
    assert out["action"] == "needs_human"
    assert out["rc"] == 2


def test_a_settled_abort_is_not_re_decided(tmp_path: Path) -> None:
    repo, candidate, deployed = make_repo(tmp_path)
    body = journal(candidate, [{"phase": "deploy-intent", "head": deployed},
                               {"phase": "aborted", "note": "no quiet window"}],
                   result="ABORTED_SAFE")
    out = decide(tmp_path, body, repo=repo)
    assert "already settled" in out["reason"]
    assert out["action"] in {"none", "unfreeze"}


def test_an_unreadable_phase_is_corruption_not_nothing_to_do(tmp_path: Path) -> None:
    repo, candidate, _ = make_repo(tmp_path)
    body = journal(candidate, [{"phase": "not-a-phase"}])
    out = decide(tmp_path, body, repo=repo)
    assert out["action"] == "needs_human"
    assert "corrupt" in out["reason"]
    assert out["rc"] == 2


def test_candidate_checked_out_but_service_down_rolls_back(tmp_path: Path) -> None:
    repo, candidate, deployed = make_repo(tmp_path)
    body = journal(candidate, [{"phase": "deploy-intent", "head": deployed},
                               {"phase": "restart", "mutated": True}])
    out = decide(tmp_path, body, repo=repo, service="inactive")
    assert out["action"] == "rollback", out


def test_every_phase_the_engine_can_record_is_accepted(tmp_path: Path) -> None:
    """The phase list must cover every ``PHASE=`` the engine can die in, or a real attempt looks corrupt."""
    engine = (REPO / "harness" / "release002_release.sh").read_text()
    phases = set(re.findall(r"PHASE=([a-z][a-z-]*)", engine))
    assert phases, "no PHASE assignments found: the engine shape changed"
    control = CONTROL.read_text()
    start = control.index("PHASES = (")
    declared = control[start : control.index(")", start)]
    missing = sorted(p for p in phases if f'"{p}"' not in declared)
    assert not missing, f"engine phases absent from PHASES: {missing}"
