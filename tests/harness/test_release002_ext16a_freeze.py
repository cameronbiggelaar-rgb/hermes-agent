"""EXT-16A: the freeze epoch must survive the freeze keep-alive.

Defect (EXT-16A, D-2): ``freeze_engage`` derived ``FREEZE_SINCE`` from the drain marker's
``requested_at``, and the keep-alive re-stamps ``requested_at`` every 45 s. After the first refresh
the recorded epoch trailed the marker by more than the 5 s attribution tolerance, so
``freeze_since_attributable`` failed on every sample from the third one onward and the engine
aborted a freeze that was in fact healthy. Inproduction that is a release that can never reach
APPLY on a gateway that takes longer than ~50 s to drain.

Fix: ``freeze_since`` is written once at engagement and never moved; the keep-alive only moves
``requested_at``. Attribution reads ``freeze_since`` and falls back to ``requested_at`` only for a
marker written before the field existed.

    env -u PYTHONPATH /usr/bin/python3 -m pytest tests/harness/test_release002_ext16a.py -q
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

import release002_control as ctrl  # noqa: E402
import release002_preflight as pre  # noqa: E402

ENGINE = ROOT / "harness" / "release002_release.sh"
SINCE = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
PRINCIPAL = "EXT-16A-qualifier"


def epoch_of(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "gateway_state.json").write_text(json.dumps({
        "gateway_state": "draining", "active_agents": 0, "pid": 1, "code_sha": "0" * 40,
        "epoch": 23, "updated_at": datetime.now().astimezone().isoformat()}))
    return home


def write_marker(home: Path, **extra: object) -> Path:
    path = home / ".drain_request.json"
    body = {
        "action": "drain",
        "principal": PRINCIPAL,
        "requested_at": SINCE,
        "freeze_since": SINCE,
        "epoch": 23,
        "reason": "release-002 EXT-16A qualification",
    }
    body.update(extra)
    path.write_text(json.dumps(body))
    return path


def observed(home: Path) -> dict:
    return ctrl.admission_report(home=home, principal=PRINCIPAL)["facts"]


def test_keepalive_moves_requested_at_but_not_the_freeze_epoch(home: Path) -> None:
    write_marker(home)
    before = observed(home)
    refreshed = ctrl.refresh_drain_marker(home=home)
    assert refreshed is not None
    after = observed(home)

    assert after["drain_marker_requested_at_epoch"] > before["drain_marker_requested_at_epoch"]
    assert after["drain_marker_freeze_epoch"] == before["drain_marker_freeze_epoch"]
    assert after["drain_marker_freeze_epoch"] == pytest.approx(epoch_of(SINCE), abs=1.0)
    assert json.loads((home / ".drain_request.json").read_text())["freeze_since"] == SINCE


def test_attribution_survives_the_tolerance_window(home: Path) -> None:
    """The exact failure: after the first refresh the skew exceeded 5 s for good."""
    write_marker(home)
    for _ in range(3):
        ctrl.refresh_drain_marker(home=home)
    facts = observed(home)
    skew = facts["drain_marker_requested_at_epoch"] - facts["drain_marker_freeze_epoch"]
    assert skew > 0, "the keep-alive should have moved requested_at"
    assert abs(epoch_of(SINCE) - facts["drain_marker_freeze_epoch"]) <= 5.0


def test_marker_without_freeze_since_still_attributes(home: Path) -> None:
    """Backwards compatibility: a marker written before the field existed."""
    path = write_marker(home)
    body = json.loads(path.read_text())
    body.pop("freeze_since")
    path.write_text(json.dumps(body))
    facts = observed(home)
    assert facts["drain_marker_freeze_epoch"] is None
    assert facts["drain_marker_requested_at_epoch"] == pytest.approx(epoch_of(SINCE), abs=1.0)


def test_engagement_writes_freeze_since_once() -> None:
    """The engine records the epoch beside requested_at and refuses to re-date its own marker."""
    text = ENGINE.read_text()
    engage = text[text.index("freeze_engage() {"):text.index("  FROZEN=1")]
    assert 'body["freeze_since"] = body.get("requested_at")' in engage
    assert 'if not body.get("freeze_since")' in engage, "a resumed attempt must keep the original epoch"
    assert 'payload["requested_at"]' in engage


def test_gate_prefers_the_immutable_epoch() -> None:
    src = Path(str(pre.__file__)).read_text()
    gate = src[src.index('marker_since = facts.get("drain_marker_freeze_epoch")'):src.index("skew = abs")]
    assert 'facts.get("drain_marker_requested_at_epoch")' in gate, "fallback for older markers"
    assert "if marker_since is None" in gate
