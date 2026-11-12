"""EXT-17A: the quiet-window trigger envelope (arm, deadline, single fire, refusal, disarm).

The trigger decides *when* the one bounded attempt starts; it must never change what the attempt is
allowed to do, never fire twice, never claim an attempt it did not start, and never widen git trust.
Each test drives the versioned correction (``harness/trigger/qw-launch-apply-v2.sh``) in a sandbox
with a fake launcher, so the assertions are about the trigger's own behaviour rather than about the
installed one on the host.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# The suite normally drives the versioned correction in-tree; TRIGGER_UNDER_TEST lets the same
# assertions be pointed at a mutated copy, which is how the red control proves they bite.
TRIGGER = Path(os.environ.get("TRIGGER_UNDER_TEST") or REPO / "harness" / "trigger" / "qw-launch-apply-v2.sh")
CAND = "3652a3702dae8d2917b91b6949da580efd3ef496"
HEAD = "3652a3702dae8d2917b91b6949da580efd3ef496"


def sandbox(tmp_path: Path, *, agents: int = 0) -> dict[str, Path]:
    """A state dir, an observation instrument pair and a launcher that records its invocation."""
    state = tmp_path / "state"
    state.mkdir()
    pw = tmp_path / "gateway_state.json"
    pw.write_text(json.dumps({"active_agents": agents, "gateway_state": "ready"}))
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("create table session_admissions (owner_epoch integer, status text)")
    con.execute("insert into session_admissions values (1, 'ended')")
    con.commit()
    con.close()

    calls = tmp_path / "launcher-calls"
    launcher = tmp_path / "launcher"
    launcher.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            echo "CALL $*" >>"{calls}"
            echo "ENV_COUNT=${{GIT_CONFIG_COUNT:-}}" >>"{calls}"
            echo "ENV_KEY0=${{GIT_CONFIG_KEY_0:-}} VALUE0=${{GIT_CONFIG_VALUE_0:-}}" >>"{calls}"
            echo "ENV_KEY1=${{GIT_CONFIG_KEY_1:-}} VALUE1=${{GIT_CONFIG_VALUE_1:-}}" >>"{calls}"
            echo "ENV_EXPECT=${{HERMES_RELEASE_EXPECT_HEAD:-}}" >>"{calls}"
            echo "supervisor started detached: hermes-release-TESTRUN.service (log /dev/null)"
            exit ${{FAKE_LAUNCHER_RC:-0}}
            """
        )
    )
    launcher.chmod(0o755)

    # systemctl must see no release unit; PATH-stub keeps the host's real units out of the test.
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "systemctl").write_text("#!/bin/bash\nexit 0\n")
    (stub / "systemctl").chmod(0o755)
    return {"state": state, "pw": pw, "db": db, "launcher": launcher, "calls": calls, "stub": stub}


def run(
    box: dict[str, Path],
    *,
    need: int = 2,
    interval: int = 1,
    deadline: int = 30,
    candidate: str = CAND,
    launcher_rc: int | None = None,
    timeout: int = 40,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{box['stub']}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/home/hermes"),
        "TRIGGER_QUALIFY": "1",
        "TRIGGER_STATE": str(box["state"]),
        "TRIGGER_PW": str(box["pw"]),
        "TRIGGER_DB": str(box["db"]),
        "TRIGGER_LAUNCHER": str(box["launcher"]),
        "TRIGGER_NEED": str(need),
        "TRIGGER_INTERVAL": str(interval),
        "TRIGGER_DEADLINE_S": str(deadline),
    }
    if launcher_rc is not None:
        env["FAKE_LAUNCHER_RC"] = str(launcher_rc)
    return subprocess.run(
        ["bash", str(TRIGGER), "--candidate", candidate, "--expect-head", HEAD],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def fired_record(box: dict[str, Path]) -> dict:
    return json.loads((box["state"] / "attempt-fired.json").read_text())


def test_trigger_fires_exactly_once_and_propagates_success(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    result = run(box)
    assert result.returncode == 0, result.stderr
    calls = box["calls"].read_text()
    assert calls.count("CALL apply") == 1
    record = fired_record(box)
    assert record["status"] == "fired"
    assert record["engine_run_id"] == "TESTRUN"
    assert record["candidate_commit"] == CAND
    assert record["expect_install_head"] == HEAD

    # Trust is granted for exactly two repositories — never the wildcard that R-12 removed.
    assert "ENV_COUNT=2" in calls
    assert "VALUE0=/home/hermes/.hermes/hermes-agent" in calls
    assert "VALUE1=/home/hermes/work/restore002-fix" in calls
    assert "*" not in calls
    assert f"ENV_EXPECT={HEAD}" in calls


def test_a_refused_launch_is_recorded_as_failed_and_exits_non_zero(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    result = run(box, launcher_rc=64)
    assert result.returncode == 4, result.stderr
    record = fired_record(box)
    assert record["status"] == "failed"
    assert record["apply_rc"] == 64
    assert record["engine_run_id"] == ""
    assert "no retry from this trigger" in (box["state"] / "qw-launcher.log").read_text()


def test_a_launcher_that_never_returns_is_abandoned_at_the_deadline(tmp_path: Path) -> None:
    """The deadline bounds the call, not only the wait for a quiet window.

    A launcher that never returns used to leave the trigger blocked with the record stuck at
    ``pending``: nobody could tell whether an attempt had started, and the trigger never disarmed.
    """
    box = sandbox(tmp_path)
    pidfile = tmp_path / "child.pid"
    box["launcher"].write_text(
        "#!/bin/bash\n"
        f'echo "CALL apply" >>"{box["calls"]}"\n'
        f'echo $$ >"{pidfile}"\n'
        "sleep 60\n"
        "exit 0\n"
    )
    box["launcher"].chmod(0o755)
    result = run(box, need=1, interval=1, deadline=5, timeout=90)
    assert result.returncode == 5, result.stdout + result.stderr
    record = fired_record(box)
    assert record["status"] == "failed"
    assert record["apply_rc"] == 124
    assert "DEADLINE" in (box["state"] / "qw-launcher.log").read_text()
    assert pidfile.exists(), "the launcher never ran"
    pid = int(pidfile.read_text().strip())
    proc = Path(f"/proc/{pid}")
    # /proc is the honest check here: os.kill() from this process is blocked by the suite's own
    # live-system guard, and a process that is gone from /proc is gone.
    for _ in range(30):
        if not proc.exists():
            break
        time.sleep(0.1)
    assert not proc.exists(), f"the launcher child {pid} outlived the deadline"


def test_a_launcher_that_reports_no_run_id_is_not_reported_as_fired(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    # a launcher that exits 0 without printing the supervisor line started nothing
    box["launcher"].write_text("#!/bin/bash\necho 'nothing started'\nexit 0\n")
    box["launcher"].chmod(0o755)
    result = run(box)
    assert result.returncode == 4
    assert fired_record(box)["status"] == "failed"


def test_second_arming_refuses_and_does_not_invoke_the_launcher_again(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    assert run(box).returncode == 0
    before = box["calls"].read_text()
    second = run(box)
    assert second.returncode == 76, second.stderr
    assert box["calls"].read_text() == before
    assert "attempt-fired record already present" in (box["state"] / "qw-launcher.log").read_text()


def test_a_journal_without_a_fired_record_still_refuses(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    (box["state"] / "journal.json").write_text("{}")
    assert run(box).returncode == 76
    assert not box["calls"].exists()


def test_deadline_disarms_without_firing(tmp_path: Path) -> None:
    box = sandbox(tmp_path, agents=1)
    result = run(box, need=2, interval=1, deadline=3)
    assert result.returncode == 3, result.stderr
    assert not box["calls"].exists()
    assert not (box["state"] / "attempt-fired.json").exists()
    assert "disarmed without applying (attempt unspent)" in (box["state"] / "qw-launcher.log").read_text()


def test_an_unreadable_instrument_never_counts_as_quiet(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    box["pw"].write_text("not json")
    result = run(box, need=2, interval=1, deadline=3)
    assert result.returncode == 3
    assert not box["calls"].exists()


def test_signal_before_the_fire_disarms_and_leaves_no_attempt_record(tmp_path: Path) -> None:
    """A TERM during the watch is a disarm with a record, not a silent exit.

    The signal is delivered from a shell wrapper rather than from Python: tests/conftest.py's
    live-system guard blocks os.kill() of a PID outside the test's own subtree, and the wrapper keeps
    the delivery inside the sandbox where it belongs.
    """
    box = sandbox(tmp_path, agents=1)
    wrapper = tmp_path / "disarm.sh"
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            export PATH="{box['stub']}:$PATH"
            export TRIGGER_QUALIFY=1 TRIGGER_STATE="{box['state']}" TRIGGER_PW="{box['pw']}"
            export TRIGGER_DB="{box['db']}" TRIGGER_LAUNCHER="{box['launcher']}"
            export TRIGGER_NEED=6 TRIGGER_INTERVAL=2 TRIGGER_DEADLINE_S=120
            bash "{TRIGGER}" --candidate {CAND} --expect-head {HEAD} &
            pid=$!
            sleep 2
            kill -TERM "$pid"
            wait "$pid"
            echo "rc=$?"
            """
        )
    )
    wrapper.chmod(0o755)
    result = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, check=False, timeout=60)
    assert "rc=143" in result.stdout, (result.stdout, result.stderr)
    assert not box["calls"].exists()
    assert not (box["state"] / "attempt-fired.json").exists()
    assert "DISARMED by SIGTERM" in (box["state"] / "qw-launcher.log").read_text()


def test_an_invalid_candidate_refuses_before_observing(tmp_path: Path) -> None:
    box = sandbox(tmp_path)
    for bad in ("3c3e24a5", "ZZZZ" * 10, ""):
        result = run(box, candidate=bad)
        assert result.returncode == 64, (bad, result.returncode)
    assert not box["calls"].exists()


def test_v1_remains_the_installed_trigger() -> None:
    """The correction is versioned in the repo; EXT-17A has no authority to install it."""
    installed = Path("/usr/local/lib/hermes-release/qw-launch-apply.sh")
    if not installed.exists():  # pragma: no cover - host without the trigger
        return
    try:
        text = installed.read_text()
    except PermissionError:  # root-owned 0700: presence is enough to prove it was not replaced
        return
    assert "GIT_CONFIG_VALUE_0='*'" in text  # v1 still carries the wildcard: unchanged by this work
    assert "trigger_version" not in text


def test_supervisor_unit_keeps_a_real_envelope() -> None:
    """The supervisor is Type=exec with a real cap, cgroup kill and no restart policy."""
    launcher = (REPO / "harness" / "hermes-release-002").read_text()
    block = launcher[launcher.index("local args=("):]
    block = block[: block.index("\n  )\n")]
    for needed in (
        "--property=Type=exec",
        '--property=RuntimeMaxSec="$runtime_max"',
        "--property=TimeoutStopSec=420",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--collect",
    ):
        assert needed in block, needed
    assert "Restart=" not in block


def test_trigger_script_is_syntax_clean_and_executable_in_a_sandbox(tmp_path: Path) -> None:
    assert shutil.which("bash")
    check = subprocess.run(["bash", "-n", str(TRIGGER)], capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr
