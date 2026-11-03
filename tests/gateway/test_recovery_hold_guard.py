"""The operator recovery hold must stop automatic resumption of interrupted sessions.

The hold exists because a gateway restart during an incident otherwise synthesizes turns for every
fresh interrupted session (``resume_pending``), which is exactly the "sessions resumed unexpectedly"
failure. It ships as an environment flag on the unit, so it has to be honoured before any database
work and without depending on gateway state.
"""
import os
import sys
import types

import pytest

from gateway.run_startup import GatewayStartupMixin


@pytest.fixture(autouse=True)
def _stub_gateway_run(monkeypatch):
    """The fall-through tests only need ``from gateway.run import ...`` to resolve.

    Importing the real module reads the installed home (``manifest.json``), which the test home-IO
    guard refuses; a stub keeps the assertion about *reaching* real work honest.
    """
    stub = types.ModuleType("gateway.run")
    stub._AGENT_PENDING_SENTINEL = object()  # type: ignore[attr-defined]
    stub._auto_continue_freshness_window = lambda *a, **k: 0  # type: ignore[attr-defined]
    stub._is_fresh_gateway_interruption = lambda *a, **k: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway.run", stub)


def test_hold_short_circuits_before_touching_the_store(monkeypatch):
    monkeypatch.setenv("HERMES_RECOVERY_HOLD", "1")

    class Exploding:
        def __getattr__(self, name):  # any attribute access means the guard ran too late
            raise AssertionError(f"hold must return before touching gateway state: {name!r}")

    # Bypass instance binding: the guard is a plain function over a stub ``self``.
    assert GatewayStartupMixin._schedule_resume_pending_sessions(Exploding()) == 0


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_the_documented_value_holds(monkeypatch, value):
    """A typo'd value must not silently disable resumption for every operator."""
    monkeypatch.setenv("HERMES_RECOVERY_HOLD", value)

    class Stop(Exception):
        pass

    class Probe:
        def __getattr__(self, name):
            raise Stop()  # we reached real work: the guard did not fire

    with pytest.raises(Stop):
        GatewayStartupMixin._schedule_resume_pending_sessions(Probe())


def test_hold_is_inert_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_RECOVERY_HOLD", raising=False)

    class Stop(Exception):
        pass

    class Probe:
        def __getattr__(self, name):
            raise Stop()

    with pytest.raises(Stop):
        GatewayStartupMixin._schedule_resume_pending_sessions(Probe())
