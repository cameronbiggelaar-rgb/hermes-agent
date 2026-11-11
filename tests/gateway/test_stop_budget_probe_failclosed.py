"""EXT-RESTORE-002-01 — negative controls for an unavailable or invalid systemd stop budget.

The 2026-11-03 defect had a second half: the runtime cap read ``TimeoutStopSec`` from a boot-time
probe and, when that probe failed, left the configured drain untouched. On a host whose unit really
is 210s with a 180s drain, "no answer from the probe" therefore re-armed the exact SIGKILL the cap
exists to prevent — silently, and only on the hosts whose units are stale or hand-edited. These
controls pin the fail-closed contract: on a systemd-managed host an unknown or invalid budget
degrades the drain to the assumed floor (immediate interrupt), it never restores the configured
drain; and off systemd (tests, launchd, a login session scope, bare doubles) nothing changes.
"""

import logging
import math

import pytest

from gateway import restart as rs


class _Runner:
    """Minimal stand-in for GatewayRunner: only the stop-budget seam is exercised."""

    def __init__(self, budget, *, drain=180.0, signal_stop=False, launchd=None):
        self._systemd_timeout_stop_sec = budget
        self._restart_drain_timeout = drain
        self._stop_requested_by_signal = signal_stop
        self._launchd_exit_timeout_s = launchd


# Every shape a broken probe can return. `None` is the documented "could not read" answer from
# shutdown_forensics._systemd_timeout_stop_us; the rest are what a hand-edited unit, a truncated
# `systemctl show`, or a float() on a duration string can produce.
INVALID_BUDGETS = [None, 0, 0.0, -5.0, float("nan"), float("inf"), float("-inf"), "180s", "", object()]


@pytest.fixture
def managed(monkeypatch):
    """Pretend this interpreter is a systemd service (it is not, under pytest)."""
    monkeypatch.setattr(rs, "systemd_managed", lambda *a, **k: True)
    monkeypatch.setattr(rs, "_STOP_BUDGET_WARNING_SHOWN", False)


@pytest.fixture
def unmanaged(monkeypatch):
    monkeypatch.setattr(rs, "systemd_managed", lambda *a, **k: False)


@pytest.mark.parametrize("budget", INVALID_BUDGETS)
def test_invalid_budget_never_restores_the_configured_drain(managed, budget):
    """The regression that matters: an unreadable unit must not buy a 180s drain."""
    runner = _Runner(budget)
    assert rs.effective_stop_drain_timeout(runner) == 0.0
    assert rs.effective_stop_drain_timeout(runner) < runner._restart_drain_timeout


@pytest.mark.parametrize("budget", INVALID_BUDGETS)
def test_invalid_budget_shortens_the_watchdog_leash_below_the_assumed_floor(managed, budget):
    """The forensic dump must still land before a 60s floor: leash <= floor - dump margin."""
    leash = rs.effective_stop_watchdog_delay(_Runner(budget), 400.0)
    assert leash == rs.SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S - rs.SYSTEMD_WATCHDOG_DUMP_MARGIN_S
    assert leash < rs.SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S


def test_assumed_floor_is_the_smallest_unit_this_code_can_generate():
    """The assumption is the generator's own minimum, not an invented number."""
    assert rs.SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S == rs.SYSTEMD_TIMEOUT_STOP_SEC_FLOOR
    smallest = rs.resolve_systemd_timeout_stop_sec(0.0, rs.DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT)
    assert rs.SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S <= smallest


@pytest.mark.parametrize("budget", [0.0, -5.0, float("nan"), float("inf"), "180s", "", object()])
def test_known_small_budget_still_interrupts_immediately(managed, budget):
    """Not just "unknown": a known-but-unusable answer is the same failure, same treatment."""
    assert rs.effective_stop_drain_timeout(_Runner(budget)) == 0.0


@pytest.mark.parametrize("budget", [210.0, 240.0, 300.0, 300000000 / 1_000_000.0])
def test_known_budget_is_capped_and_never_extended(managed, budget):
    drain = rs.effective_stop_drain_timeout(_Runner(budget))
    assert drain == max(0.0, min(180.0, budget - rs.SYSTEMD_POST_DRAIN_RESERVE_S))
    assert drain <= 180.0


def test_unknown_budget_off_systemd_keeps_the_configured_drain(unmanaged):
    """A probe failure must not punish a non-systemd supervisor (launchd, tests, a login scope)."""
    runner = _Runner(None)
    assert rs.effective_stop_drain_timeout(runner) == 180.0
    assert rs.effective_stop_watchdog_delay(runner, 400.0) == 400.0


def test_duck_typed_runner_without_the_seam_is_untouched(monkeypatch):
    """Shutdown-path doubles never carry the attribute; absence is not "managed and unknown"."""

    class _Bare:
        _restart_drain_timeout = 180.0

    monkeypatch.setattr(rs, "systemd_managed", lambda *a, **k: True)
    assert rs.effective_stop_drain_timeout(_Bare()) == 180.0
    assert rs.systemd_stop_budget(_Bare()) == (None, "unmanaged")


def test_unreadable_budget_is_loud_once(managed, caplog):
    runner = _Runner(None)
    with caplog.at_level(logging.WARNING, logger="gateway.restart"):
        rs.effective_stop_drain_timeout(runner)
        rs.effective_stop_drain_timeout(runner)
        rs.effective_stop_watchdog_delay(runner, 400.0)
    hits = [r for r in caplog.records if "TimeoutStopSec is unreadable" in r.getMessage()]
    assert len(hits) == 1, "the fail-closed path must warn, and must not spam per phase"
    assert "60s floor" in hits[0].getMessage()


def test_probe_failure_is_visible_in_the_stop_budget_source(managed):
    assert rs.systemd_stop_budget(_Runner(None)) == (None, "managed-no-budget")
    assert rs.systemd_stop_budget(_Runner(300.0)) == (300.0, "probe")


@pytest.mark.parametrize("value,expected", [
    (300, 300.0), ("300", 300.0), (0.5, 0.5), (0, None), (-1, None), (None, None),
    (float("nan"), None), (float("inf"), None), ("180s", None), ("", None), ([], None),
])
def test_coerce_stop_budget(value, expected):
    assert rs.coerce_stop_budget_s(value) == expected


def test_systemd_managed_detection(monkeypatch):
    for var in rs._SYSTEMD_ENV_MARKERS:
        assert rs.systemd_managed({var: "x"}, cgroup_text="") is True
    assert rs.systemd_managed({}, cgroup_text="0::/system.slice/hermes-gateway.service\n") is True
    # A login session / container / systemd-run --scope: no service unit owns this stop budget.
    assert rs.systemd_managed({}, cgroup_text="0::/user.slice/user-1000.slice/session-3.scope\n") is False
    assert rs.systemd_managed({}, cgroup_text="") is False


def test_unreadable_proc_and_no_env_is_not_managed(monkeypatch):
    """The cgroup read can fail on a hardened host; that must not fabricate a budget or a cap."""
    monkeypatch.setattr(rs, "_SYSTEMD_CGROUP_PATH", "/nonexistent/cgroup")
    assert rs.systemd_managed({}) is False


def test_assumed_cap_is_exactly_the_floor_arithmetic():
    """Pins the number the warning promises: 60s floor - 90s reserve -> 0s drain, 55s leash."""
    assert rs.resolve_systemd_capped_drain(180.0, rs.SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S) == 0.0
    assert rs.resolve_systemd_capped_drain(
        400.0, rs.SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S, cleanup_reserve_s=rs.SYSTEMD_WATCHDOG_DUMP_MARGIN_S,
    ) == 55.0
    assert math.isfinite(rs.SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S)


def test_launchd_stop_still_honours_its_own_budget_first(monkeypatch):
    """A signal stop under launchd keeps the launchd clamp; systemd's assumption must not override it."""
    monkeypatch.setattr(rs, "systemd_managed", lambda *a, **k: True)
    runner = _Runner(None, signal_stop=True, launchd=60.0)
    # launchd caps 180 -> 50, then the systemd assumption caps it to 0: the smaller budget wins.
    assert rs.effective_stop_drain_timeout(runner) == 0.0
    # ...and with a readable systemd budget the launchd clamp is what binds.
    monkeypatch.setattr(rs, "systemd_managed", lambda *a, **k: False)
    assert rs.effective_stop_drain_timeout(_Runner(300.0, signal_stop=True, launchd=60.0)) == 50.0
