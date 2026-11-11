#!/usr/bin/env python3
"""RESTORE-002 Fault B — stop-envelope reproduction and fix verification (read-only).

Reproduces the 2026-11-03 drain defect deterministically, at the real constants, from the phase
timings the gateway itself logged during the cutover stop, then re-runs the same model against the
corrected budget arithmetic in ``gateway/restart.py``.

This is a model, not a systemd run: a live SIGKILL proof needs a restart, which is owner-gated.
Its inputs are measured, not assumed — see ``PHASES`` provenance below.

Usage:  python3 harness/fault_b_repro.py [--json out.json]
Exit:   0 when the model shows the pre-fix envelope can be exceeded and the post-fix one cannot.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.restart import (  # noqa: E402
    SYSTEMD_POST_DRAIN_RESERVE_S,
    SYSTEMD_STOP_HEADROOM_S,
    resolve_systemd_capped_drain,
    resolve_systemd_timeout_stop_sec,
)

# Each phase's *worst-case* budget, and the value observed at the 2026-11-03 19:37:05 stop
# (gateway.log lines 10547-10568):
#   notify_active_sessions  +6.32s   -> budget: signal_interrupt_grace + send timeouts, 15s
#   drain                  180.01s   -> budget: restart_drain_timeout, 180s  (timed_out=True, 1 agent)
#   interrupt+settle        +3.14s   -> budget: TURN_SETTLE_SECONDS 45s (clamped to leash remaining)
#   kill+finalize+quiesce   +4.45s   -> budget: 15s
#   SessionDB close         +0.05s   -> budget: 10s
#   total observed          193.61s
PHASES = {
    "notify": {"observed": 6.32, "worst": 15.0},
    "drain": {"observed": 180.01, "worst": 180.0},
    "settle": {"observed": 3.14, "worst": 45.0},
    "finalize": {"observed": 4.45, "worst": 15.0},
    "close": {"observed": 0.05, "worst": 10.0},
}
# TimeoutStopSec values in play: shipped unit, the temporary drop-in that carried the cutover, and
# what the corrected generator now emits for a 180s drain / 30s cron floor.
UNITS = {
    "shipped_210": 210.0,
    "dropin_300": 300.0,
    "regenerated": float(resolve_systemd_timeout_stop_sec(180.0, 30.0)),
}


def envelope(unit: float, drain_budget: float, *, observed: bool) -> dict:
    """Total stop time for the given drain budget under ``unit``; verdict on whether it fits."""
    key = "observed" if observed else "worst"
    total = 0.0
    for name, phase in PHASES.items():
        total += drain_budget if name == "drain" else phase[key]
    return {"unit_s": unit, "drain_s": drain_budget, "total_s": round(total, 2),
            "fits": total <= unit, "margin_s": round(unit - total, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()

    rows = []
    # Pre-fix: the drain is never capped, so it is the configured 180s whatever the unit says.
    for name, unit in UNITS.items():
        for mode in ("observed", "worst"):
            rows.append({"case": f"pre_fix/{name}/{mode}", **envelope(unit, 180.0, observed=mode == "observed")})
    # Post-fix: the drain is capped by the live unit, then the same measured phases apply.
    for name, unit in UNITS.items():
        capped = resolve_systemd_capped_drain(180.0, unit)
        for mode in ("observed", "worst"):
            rows.append({"case": f"post_fix/{name}/{mode}", **envelope(unit, capped, observed=mode == "observed")})

    print(f"{'case':38} {'unit':>6} {'drain':>6} {'total':>7} {'margin':>7}  verdict")
    for r in rows:
        print(f"{r['case']:38} {r['unit_s']:6.0f} {r['drain_s']:6.0f} {r['total_s']:7.2f} "
              f"{r['margin_s']:7.2f}  {'fits' if r['fits'] else 'SIGKILL'}")

    by = {r["case"]: r for r in rows}
    checks = {
        # The defect, reproduced: at the shipped 210s unit the worst-case stop overruns the envelope.
        "shipped_unit_overruns_on_worst_case": not by["pre_fix/shipped_210/worst"]["fits"],
        # The drop-in masked it (which is why the cutover survived), and would not have on a slow settle.
        "dropin_masked_the_defect": by["pre_fix/dropin_300/worst"]["fits"],
        # Corrected code fits even on the stale unit, because the drain yields the reserve.
        "post_fix_fits_stale_unit": all(v["fits"] for k, v in by.items() if k.startswith("post_fix/shipped_210")),
        "post_fix_fits_regenerated_unit": all(v["fits"] for k, v in by.items() if k.startswith("post_fix/regenerated")),
        # The generator's own output must never trigger its own cap.
        "generator_self_consistent": resolve_systemd_capped_drain(180.0, UNITS["regenerated"]) == 180.0,
        "headroom_covers_reserve": SYSTEMD_STOP_HEADROOM_S >= SYSTEMD_POST_DRAIN_RESERVE_S,
    }
    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "generated_by": "harness/fault_b_repro.py",
            "phases": PHASES, "units": UNITS, "rows": rows, "checks": checks,
            "source": "gateway.log 2026-11-03 19:37:05-19:40:19 (PIDs 716744 -> 743641)",
        }, indent=1))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
