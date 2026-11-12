"""EXT-17C §1: the quiet window may only be qualified through a WAL-aware admission read.

`immutable=1` opens the main database file and never looks at the ``-wal``. A gateway that has just
committed an admission has not necessarily checkpointed it, so the old instrument reports zero live
admissions while a session is queued or running — and the trigger would spend the single attempt on
a host that is not quiet. These tests build a real WAL, commit an admission without checkpointing it,
and hold the writer open, then drive the versioned trigger (``qw-launch-apply-v2.sh``) exactly as the
qualification harness does.

The first test is the red control: it pins the stale read the correction removes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRIGGER = Path(os.environ.get("TRIGGER_UNDER_TEST") or REPO / "harness" / "trigger" / "qw-launch-apply-v2.sh")
CAND = "3652a3702dae8d2917b91b6949da580efd3ef496"
HEAD = "3652a3702dae8d2917b91b6949da580efd3ef496"
STALE_SQL = (
    "select count(*) from session_admissions where status in ('queued','started','unknown') "
    "and owner_epoch=(select epoch from runtime_epoch where singleton=1)"
)

DDL = """
create table sessions (id text primary key, last_activity_at real);
create table runtime_epoch (singleton integer primary key, epoch integer);
create table session_admissions (
    admission_id text primary key, owner_epoch integer, status text, target_session_id text);
"""


def box(tmp_path: Path) -> dict[str, Path]:
    """State dir, observation instruments and a launcher that records its one invocation."""
    state = tmp_path / "state"
    state.mkdir()
    pw = tmp_path / "gateway_state.json"
    pw.write_text(json.dumps({"active_agents": 0, "gateway_state": "ready"}))
    calls = tmp_path / "launcher-calls"
    launcher = tmp_path / "launcher"
    launcher.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            echo "CALL $*" >>"{calls}"
            echo "supervisor started detached: hermes-release-WALRUN.service (log /dev/null)"
            exit 0
            """
        )
    )
    launcher.chmod(0o755)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "systemctl").write_text("#!/bin/bash\nexit 0\n")
    (stub / "systemctl").chmod(0o755)
    return {"state": state, "pw": pw, "db": tmp_path / "state.db", "launcher": launcher,
            "calls": calls, "stub": stub}


def store(path: Path, *, epoch: int = 3, session_age: float = 0.0) -> sqlite3.Connection:
    """A WAL-mode store, checkpointed, with one open session and the writer left open."""
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma wal_autocheckpoint=0")
    con.executescript(DDL)
    con.execute("insert into runtime_epoch values (1, ?)", (epoch,))
    con.execute("insert into sessions values ('s-open', ?)", (time.time() - session_age,))
    con.commit()
    con.execute("pragma wal_checkpoint(truncate)")
    return con


def admit(con: sqlite3.Connection, admission: str, *, epoch: int, status: str = "queued") -> None:
    con.execute(
        "insert into session_admissions values (?, ?, ?, 's-open')", (admission, epoch, status))
    con.commit()


def cli(db: Path, uri_mode: str) -> int | str:
    result = subprocess.run(
        ["sqlite3", f"file:{db}?{uri_mode}", STALE_SQL], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return f"rc={result.returncode}"
    return int(result.stdout.strip())


def run(boxed: dict[str, Path], *, need: int = 1, interval: int = 0, deadline: int = 6,
        timeout: int = 40) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{boxed['stub']}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/home/hermes"),
        "TRIGGER_QUALIFY": "1",
        "TRIGGER_STATE": str(boxed["state"]),
        "TRIGGER_PW": str(boxed["pw"]),
        "TRIGGER_DB": str(boxed["db"]),
        "TRIGGER_LAUNCHER": str(boxed["launcher"]),
        "TRIGGER_NEED": str(need),
        "TRIGGER_INTERVAL": str(interval),
        "TRIGGER_DEADLINE_S": str(deadline),
    }
    return subprocess.run(["bash", str(TRIGGER), "--candidate", CAND, "--expect-head", HEAD],
                           env=env, check=False, capture_output=True, text=True, timeout=timeout)


def logtext(boxed: dict[str, Path]) -> str:
    return (boxed["state"] / "qw-launcher.log").read_text()


def wal_bytes(boxed: dict[str, Path]) -> int:
    wal = Path(str(boxed["db"]) + "-wal")
    return wal.stat().st_size if wal.exists() else 0


# --------------------------------------------------------------- the defect, pinned


def test_the_immutable_reader_misses_a_committed_uncheckpointed_admission(tmp_path: Path) -> None:
    boxed = box(tmp_path)
    con = store(boxed["db"])
    try:
        admit(con, "a-live", epoch=3)
        assert wal_bytes(boxed) > 0, "the fixture must leave frames in the WAL to be meaningful"
        # This is the instrument the trigger used before EXT-17C, and the reason for the correction:
        # the row is committed, the gateway would answer for it, and this read cannot see it.
        assert cli(boxed["db"], "immutable=1") == 0
        assert cli(boxed["db"], "mode=ro") == 1
    finally:
        con.close()


# ------------------------------------------------------------ the correction, end to end


def test_a_live_admission_in_a_hot_wal_blocks_the_quiet_window(tmp_path: Path) -> None:
    boxed = box(tmp_path)
    con = store(boxed["db"])
    try:
        admit(con, "a-live", epoch=3)
        result = run(boxed, need=2, interval=1, deadline=5)
        assert result.returncode == 3, result.stdout + result.stderr
        assert not boxed["calls"].exists(), "the launcher must not be invoked over live work"
        assert not (boxed["state"] / "attempt-fired.json").exists()
        text = logtext(boxed)
        assert "admission observation mode=ro epoch=3 live=1" in text
        assert "live=1" in text and "admissions=busy" in text
        assert f"wal_bytes={wal_bytes(boxed)}" in text
    finally:
        con.close()


def test_an_admission_committed_mid_window_resets_the_sample_count(tmp_path: Path) -> None:
    """Each sample is a fresh read: the window is consecutive observations, not one stale snapshot."""
    boxed = box(tmp_path)
    con = store(boxed["db"])
    env = {
        "PATH": f"{boxed['stub']}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/home/hermes"),
        "TRIGGER_QUALIFY": "1",
        "TRIGGER_STATE": str(boxed["state"]),
        "TRIGGER_PW": str(boxed["pw"]),
        "TRIGGER_DB": str(boxed["db"]),
        "TRIGGER_LAUNCHER": str(boxed["launcher"]),
        "TRIGGER_NEED": "3",
        "TRIGGER_INTERVAL": "1",
        "TRIGGER_DEADLINE_S": "6",
    }
    proc = subprocess.Popen(["bash", str(TRIGGER), "--candidate", CAND, "--expect-head", HEAD],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(1.2)          # the first sample has already read zero
        admit(con, "a-live-mid", epoch=3)
        out, err = proc.communicate(timeout=40)
    finally:
        con.close()
    assert proc.returncode == 3, out + err
    assert not (boxed["state"] / "attempt-fired.json").exists()
    assert not boxed["calls"].exists()
    assert "live=1" in logtext(boxed)


def test_the_same_host_fires_once_the_admission_is_terminal(tmp_path: Path) -> None:
    """Control: nothing about the correction widens the window beyond the live-admission test."""
    boxed = box(tmp_path)
    con = store(boxed["db"])
    try:
        admit(con, "a-done", epoch=3, status="terminal")
        result = run(boxed, need=1)
        assert result.returncode == 0, result.stdout + result.stderr
        record = json.loads((boxed["state"] / "attempt-fired.json").read_text())
        assert record["status"] == "fired"
        assert record["admission_read_mode"] == "mode=ro"
        assert record["trigger_version"] == "v2.1"
        assert boxed["calls"].read_text().count("CALL apply") == 1
    finally:
        con.close()


# --------------------------------------------------------------------- stale epochs


def test_a_stale_epoch_row_younger_than_the_horizon_blocks_quiet(tmp_path: Path) -> None:
    """An epoch change does not prove the previous runtime finished draining."""
    boxed = box(tmp_path)
    con = store(boxed["db"], epoch=5)
    try:
        admit(con, "a-previous-epoch", epoch=4)
        result = run(boxed, need=2, interval=1, deadline=4)
        assert result.returncode == 3, result.stdout + result.stderr
        assert not boxed["calls"].exists()
        text = logtext(boxed)
        assert "live=0" in text and "stale_recent=1" in text and "admissions=busy" in text
    finally:
        con.close()


def test_a_stale_epoch_row_past_the_horizon_is_reported_but_not_live(tmp_path: Path) -> None:
    boxed = box(tmp_path)
    con = store(boxed["db"], epoch=5, session_age=7 * 3600)
    try:
        admit(con, "a-abandoned", epoch=4)
        result = run(boxed, need=1)
        assert result.returncode == 0, result.stdout + result.stderr
        text = logtext(boxed)
        assert "live=0" in text and "stale_recent=0" in text and "stale_expired=1" in text
    finally:
        con.close()


def test_an_undated_admission_blocks_quiet(tmp_path: Path) -> None:
    """A queued row with no owner epoch cannot be placed in any runtime: ambiguous, so not quiet."""
    boxed = box(tmp_path)
    con = store(boxed["db"])
    try:
        con.execute("insert into session_admissions values ('a-undated', null, 'queued', 's-open')")
        con.commit()
        result = run(boxed, need=2, interval=1, deadline=4)
        assert result.returncode == 3
        assert not boxed["calls"].exists()
        assert "undated=1" in logtext(boxed)
    finally:
        con.close()


# --------------------------------------------------------------- unreadable at arm


def test_an_unreadable_store_refuses_at_arm(tmp_path: Path) -> None:
    boxed = box(tmp_path)
    boxed["db"].write_text("this is not a database\n")
    result = run(boxed, need=1, deadline=4)
    assert result.returncode == 64, result.stdout + result.stderr
    assert "REFUSE at arm: admission state is unreadable" in logtext(boxed)
    assert not boxed["calls"].exists()
    assert not (boxed["state"] / "attempt-fired.json").exists()


def test_a_store_without_a_runtime_epoch_refuses_at_arm(tmp_path: Path) -> None:
    boxed = box(tmp_path)
    con = sqlite3.connect(boxed["db"])
    con.execute("pragma journal_mode=wal")
    con.executescript("create table session_admissions (admission_id text primary key,"
                      " owner_epoch integer, status text, target_session_id text);")
    con.commit()
    try:
        result = run(boxed, need=1, deadline=4)
    finally:
        con.close()
    assert result.returncode == 64, result.stdout + result.stderr
    assert "REFUSE at arm" in logtext(boxed)
    assert not boxed["calls"].exists()
