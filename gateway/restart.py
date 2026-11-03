"""Shared gateway restart constants and supervisor detection helpers."""

import logging
import math
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping

from hermes_cli.config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)

# EX_TEMPFAIL (sysexits.h): ask the service manager to restart after a graceful drain/reload.
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75
# EX_CONFIG (sysexits.h): fatal configuration error (token collision, no platforms);
# the s6 finish script maps it to exit 125 so the supervisor stops restarting.
# See #51228.
GATEWAY_FATAL_CONFIG_EXIT_CODE = 78


def map_fatal_config_exit_for_launchd(returncode: int) -> int:
    """Translate gateway EX_CONFIG so launchd can park the job.

    systemd uses ``RestartPreventExitStatus=78``; s6 maps 78→125. launchd cannot
    gate on a specific status, and unconditional ``KeepAlive=true`` respawns 78
    forever (#89477). The generated plist uses ``KeepAlive.SuccessfulExit=false``;
    mapping 78→0 is a deliberate stop. Exit 75 (please restart) and other
    failures pass through so KeepAlive still relaunches them. Negative ``wait()``
    codes (signals) are left to the caller.
    """
    if returncode == GATEWAY_FATAL_CONFIG_EXIT_CODE:
        return 0
    return returncode

# Set by ``hermes gateway run --external-supervisor``. Unlike systemd's INVOCATION_ID
# and launchd's XPC_SERVICE_NAME, this survives wrappers that replace the child
# environment (e.g. ``sudo env -i``).
EXTERNAL_GATEWAY_SUPERVISOR_ENV = "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"

# Forwarded by the stderr-timestamp launchd wrapper (hermes_cli/stderr_timestamp.py) to the gateway
# grandchild, which sees ``XPC_SERVICE_NAME=0``. Read only via :func:`launchd_job_label`.
LAUNCHD_LABEL_ENV = "HERMES_LAUNCHD_LABEL"

DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT = float(DEFAULT_CONFIG["agent"]["restart_drain_timeout"])
DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT = float(DEFAULT_CONFIG["gateway"]["signal_interrupt_grace_timeout"])
DEFAULT_GATEWAY_POST_INTERRUPT_GRACE_TIMEOUT = 5.0

# In-band restart waits for active turns to finish *before* ``stop()`` begins; distinct from
# ``restart_drain_timeout``, the force-interrupt budget once ``stop()`` runs (short under TimeoutStopSec).
DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT = float(DEFAULT_CONFIG["agent"]["restart_after_turn_timeout"])

# Floor under the ``stop()`` drain for cron jobs and api_server (/v1) runs. ``restart_drain_timeout``
# defaults to 0 because interrupting a *chat* turn is cheap and recoverable (user told, session
# resume_pending); an interrupted *cron* run is a permanent failure in jobs.json and an interrupted
# /v1 run fails its waiting caller — a 0s drain silently destroys work.
DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT = float(DEFAULT_CONFIG["agent"]["cron_drain_timeout"])
# Watchdog leash held back for post-drain work (interrupt agents, kill subprocesses, mark jobs,
# disconnect adapters). Waiting for cron past that trades a job killed *and recorded* for one
# SIGKILLed mid-write and wedged at ``last_status=running`` forever.
CRON_DRAIN_CLEANUP_RESERVE_S = 10.0
# systemd TimeoutStopSec headroom after the stop-path drain budget, and the floor when that
# budget is still the default immediate (0s) chat drain. Keep in lockstep with generate_systemd_unit().
# See #94759.
#
# The headroom is NOT slack for the drain: systemd starts its stop clock at SIGTERM, so whatever the
# drain spends is gone before interrupt/settle/finalize/quiesce/close even start. A 30s headroom was
# therefore smaller than ``run_runtime.settle_gateway_runtime``'s own 45s settle budget (plus its 10s
# tail reserve) — a unit could be "correct" by this function and still be SIGKILLed mid-settle. 120s
# covers the worst case for every phase after the drain starts: notify 15 + settle 45 + finalize 15
# + close 10 = 85s, plus margin. The 2026-11-03 stop measured 193.6s total with the drain already
# timed out (85s of phases still to come in the slow case), and it fits with 35s to spare. It also
# lands the generated unit on 300s for the shipped 180s drain — the exact ceiling that carried the
# cutover, so retiring the temporary drop-in changes no runtime behaviour.
SYSTEMD_STOP_HEADROOM_S = 120.0
SYSTEMD_TIMEOUT_STOP_SEC_FLOOR = 60.0
# Smallest stop budget this code can hand systemd: resolve_systemd_timeout_stop_sec() never returns
# less than the floor above. When the process is demonstrably a systemd service but the live
# TimeoutStopSec cannot be read (systemctl missing, no bus, unparsable or non-finite value), that
# floor is the only budget we can prove, so the drain is sized against it. Leaving the configured
# drain untouched instead — the old fail-open — re-armed the SIGKILL the cap exists to prevent, and
# did so silently on exactly the hosts where the unit is unknown (stale or hand-edited).
SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S = SYSTEMD_TIMEOUT_STOP_SEC_FLOOR
# Post-drain phases the assumed floor cannot fit (settle alone budgets 45s). A stop that has to run
# inside the floor therefore interrupts immediately: a turn cut at SIGTERM is recoverable
# (resume_pending, session state persisted), a SIGKILL mid-SQLite-write is not.
# Reserve the *live* TimeoutStopSec must still hold once the drain returns: the post-drain phases
# (interrupt, settle, finalize adapters, quiesce, close SessionDB, persist exit). Applied as a cap on
# the drain itself so a stale/undersized unit (e.g. the shipped 210s with a 180s drain) can never
# spend the whole envelope and leave the teardown to be SIGKILLed. Covers settle (45) + its tail
# reserve (10) + quiesce/SessionDB close (7.1s measured) and still leaves ~35s of margin at the
# measured 2026-11-03 cost profile.
SYSTEMD_POST_DRAIN_RESERVE_S = 90.0
# Margin below TimeoutStopSec at which our own shutdown watchdog must dump stacks and os._exit, so
# the forensic dump lands before systemd's SIGKILL (mirrors LAUNCHD_WATCHDOG_DUMP_MARGIN_S).
SYSTEMD_WATCHDOG_DUMP_MARGIN_S = 5.0

# launchd is the one supervisor whose stop budget the gateway cannot size:
# ``ExitTimeOut`` lives in the plist, and the per-user (gui) domain CLAMPS
# it — measured on macOS 26.6.1: plist 215 -> live 60, 90 -> 60, 60 -> 60,
# 30 -> 30. The gateway can only *read* the live value (``launchctl print
# gui/<uid>/<label>`` -> ``exit timeout = N``) and fit its SIGTERM-driven
# stop inside it. Draining past it is not "a longer drain" — launchd
# SIGKILLs at N seconds, mid-SQLite-write on a busy restart, which is the
# unclean-exit half of the state.db corruption class.
LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S = 60
LAUNCHD_STOP_CLEANUP_RESERVE_S = 10.0
# How far before launchd's SIGKILL the thread watchdog must fire so its
# faulthandler dump + os._exit actually land (the dump is sub-second; the
# margin covers a slow disk).
LAUNCHD_WATCHDOG_DUMP_MARGIN_S = 2.0

_LAUNCHD_EXIT_TIMEOUT_RE = re.compile(r"^\s*exit timeout\s*=\s*(\d+)\s*$", re.MULTILINE)


def parse_launchd_exit_timeout(print_output: object) -> float | None:
    """Extract ``exit timeout = N`` from ``launchctl print`` output."""
    match = _LAUNCHD_EXIT_TIMEOUT_RE.search(str(print_output or ""))
    if match is None:
        return None
    return float(match.group(1))


def launchd_job_label(environ: Mapping[str, str] | None = None) -> str | None:
    """The ``ai.hermes.*`` launchd job label in *environ*, or ``None`` (no darwin gate).

    launchd stamps ``XPC_SERVICE_NAME`` only on the job process it spawns. The generated plist
    runs the stderr-timestamp wrapper there, so the gateway grandchild reads ``XPC_SERVICE_NAME=0``
    and finds its label only in the wrapper's re-export, ``HERMES_LAUNCHD_LABEL``. Both go through
    the same ``ai.hermes`` predicate: app-coalition labels (``application.<bundle>…``, exported
    into IDE integrated terminals) are not our job. ONE seam for every reader of launchd identity
    (drain cap, restart route, control-socket supervisor declaration).
    """
    env = os.environ if environ is None else environ
    for var in ("XPC_SERVICE_NAME", LAUNCHD_LABEL_ENV):
        label = str(env.get(var, "") or "").strip()
        if label.startswith("ai.hermes"):
            return label
    return None


def launchd_service_label(environ: Mapping[str, str] | None = None, *, platform: str = sys.platform) -> str | None:
    """Return this process's ``ai.hermes.*`` launchd job label, or ``None`` off darwin.

    ``launchctl print`` reports ``exit timeout = 1`` for app-coalition labels — treating those as
    a budget would cap the drain to 0 for a gateway Ctrl+C'd in an IDE terminal, hence the
    predicate in :func:`launchd_job_label`. ``platform`` is data so the mapping logic is testable
    on any host.
    """
    if platform != "darwin":
        return None
    return launchd_job_label(environ)


def read_launchd_exit_timeout_s(
    label: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    platform: str = sys.platform,
) -> float | None:
    """Live ``ExitTimeOut`` (seconds) launchd enforces for this gateway's job.

    Returns ``None`` — meaning "no launchd budget applies" — when the process
    is not launchd-owned (non-darwin, or no ``ai.hermes`` job label — see :func:`launchd_job_label`), ``launchctl`` is missing
    or fails, or the print output carries no ``exit timeout`` line. Callers
    must treat ``None`` as fail-open: the configured drain stands unchanged.
    """
    label = label or launchd_service_label(environ, platform=platform)
    if not label:
        return None
    if uid is None:
        getuid = getattr(os, "getuid", None)  # absent on Windows; label is None there anyway
        if getuid is None:
            return None
        uid = getuid()
    domain = "system" if uid == 0 else f"gui/{uid}"
    try:
        proc = run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return parse_launchd_exit_timeout(proc.stdout)


def resolve_launchd_capped_drain(
    drain_timeout: float,
    launchd_exit_timeout_s: float | None,
    *,
    cleanup_reserve_s: float = LAUNCHD_STOP_CLEANUP_RESERVE_S,
) -> float:
    """Clamp a SIGTERM-driven stop drain to what launchd will actually allow.

    ``launchd_exit_timeout_s`` is the live ``exit timeout`` for this job (see
    :func:`read_launchd_exit_timeout_s`); ``None`` means no launchd budget
    applies and the configured drain is returned untouched. Otherwise the
    drain may use at most ``exit_timeout - cleanup_reserve_s`` so the
    post-drain teardown (interrupt agents, disconnect adapters, checkpoint
    and close SQLite) still completes before launchd escalates to SIGKILL.
    Never *extends* the drain — an operator who configured a short one
    keeps it.
    """

    drain = _seconds(drain_timeout)
    budget = _seconds(launchd_exit_timeout_s)  # None / non-numeric → 0.0 → no budget applies
    if budget <= 0.0:
        return drain
    return min(drain, max(budget - _seconds(cleanup_reserve_s), 0.0))


def resolve_systemd_capped_drain(
    drain_timeout: object, timeout_stop_s: object,
    cleanup_reserve_s: float = SYSTEMD_POST_DRAIN_RESERVE_S,
) -> float:
    """``drain_timeout`` capped to what the *live* systemd ``TimeoutStopSec`` leaves after
    ``cleanup_reserve_s`` for the post-drain teardown.

    systemd's stop clock starts at SIGTERM and covers the whole stop: drain + interrupt + settle +
    finalize + quiesce + close.  A drain sized equal to (or near) the unit timeout therefore leaves
    the teardown to be SIGKILLed — the exact failure a 300s drop-in was masking.  An unknown, zero,
    negative or non-finite budget keeps the configured drain unchanged: this function stays a pure
    "cap by a known budget".  Callers decide what an *unknown* budget means via
    :func:`systemd_stop_budget` — under systemd it must fail closed, not open.
    """
    drain = _seconds(drain_timeout)
    unit = _seconds(timeout_stop_s)
    reserve = _seconds(cleanup_reserve_s, SYSTEMD_POST_DRAIN_RESERVE_S)
    if not math.isfinite(unit) or unit <= 0.0:
        return drain
    # A unit smaller than the reserve still must not spend its whole envelope on the drain: give the
    # teardown everything (drain 0 -> interrupt now) rather than guaranteeing the SIGKILL.
    return max(0.0, min(drain, unit - reserve))


def coerce_stop_budget_s(value: object) -> float | None:
    """A usable stop budget in seconds, or ``None`` for absent/invalid data.

    One predicate for every reader of a supervisor stop budget: a probe that answers ``0``,
    ``-5``, ``nan``, ``inf`` or ``"180s"`` is not a budget, and treating it as one is how an
    unreadable unit turned into an unbounded drain.
    """
    try:
        budget = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(budget) or budget <= 0.0:
        return None
    return budget


_SYSTEMD_ENV_MARKERS = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "NOTIFY_SOCKET")
_SYSTEMD_CGROUP_PATH = "/proc/self/cgroup"


def systemd_managed(environ: Mapping[str, str] | None = None, cgroup_text: str | None = None) -> bool:
    """True when this process is a systemd *service*, i.e. some ``TimeoutStopSec`` will kill it.

    Env markers first (they survive a `/proc` read failure), then the cgroup unit path:
    ``0::/system.slice/hermes-gateway.service`` yes, ``.scope`` (login session, container,
    ``systemd-run --scope``) no — a scope's stop budget is not this process's budget, and treating
    it as one would cap the drain of a gateway started by hand from a login shell.
    """
    env = os.environ if environ is None else environ
    if any(str(env.get(var, "") or "").strip() for var in _SYSTEMD_ENV_MARKERS):
        return True
    if cgroup_text is None:
        try:
            with open(_SYSTEMD_CGROUP_PATH, encoding="utf-8") as fh:
                cgroup_text = fh.read()
        except OSError:
            return False
    return any(part.endswith(".service") for line in cgroup_text.splitlines() for part in line.split("/"))


def systemd_stop_budget(runner: object) -> tuple[float | None, str]:
    """``(budget_seconds, source)`` for the stop in progress on ``runner``.

    ``source`` is one of:

    * ``"probe"`` — the boot-time probe read a finite, positive live ``TimeoutStopSec``.
    * ``"managed-no-budget"`` — this *is* a systemd service but the budget is unknown or invalid.
      Callers must size the drain against :data:`SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S`, not the
      configured drain: an unreadable unit is the case the cap exists for.
    * ``"unmanaged"`` — no systemd stop clock applies (tests, bare doubles, launchd, a session
      scope), so the configured drain stands.

    A runner without the probe attribute at all is a duck-typed double, not a ``GatewayRunner``:
    it is never treated as managed.
    """
    if not hasattr(runner, "_systemd_timeout_stop_sec"):
        return None, "unmanaged"
    budget = coerce_stop_budget_s(getattr(runner, "_systemd_timeout_stop_sec", None))
    if budget is not None:
        return budget, "probe"
    return None, "managed-no-budget" if systemd_managed() else "unmanaged"


_STOP_BUDGET_WARNING_LOCK = threading.Lock()
_STOP_BUDGET_WARNING_SHOWN = False


def _warn_unverified_stop_budget() -> None:
    """Say, once per process, that the stop is being sized against an assumed budget."""
    global _STOP_BUDGET_WARNING_SHOWN
    with _STOP_BUDGET_WARNING_LOCK:
        if _STOP_BUDGET_WARNING_SHOWN:
            return
        _STOP_BUDGET_WARNING_SHOWN = True
    logger.warning(
        "Running as a systemd service but the live TimeoutStopSec is unreadable (systemctl missing, "
        "no bus, or an invalid value) — assuming the %.0fs floor (resolve_systemd_timeout_stop_sec "
        "minimum) and interrupting the drain immediately instead of spending the whole envelope. "
        "Check `systemctl show -p TimeoutStopUSec <unit>`, then `hermes gateway install --force`.",
        SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S,
    )


def resolve_stop_drain_budget(runner: object, drain_timeout: object) -> float:
    """``drain_timeout`` capped to the stop envelope this runner can prove, failing closed.

    Shared by the drain and the watchdog leash so both sit inside the same budget. Under systemd an
    unknown budget is *not* "no cap": it is the floor, which for the shipped 180s drain means drain
    0 (interrupt now) rather than a guaranteed SIGKILL.
    """
    budget, source = systemd_stop_budget(runner)
    if source == "managed-no-budget":
        _warn_unverified_stop_budget()
        budget = SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S
    return resolve_systemd_capped_drain(drain_timeout, budget)



def effective_stop_drain_timeout(runner: object) -> float:
    """Drain budget for the stop in progress on ``runner``.

    Signal-driven stops under launchd are timed by launchd's live
    ``ExitTimeOut`` (``runner._launchd_exit_timeout_s``, set at boot);
    everything else — in-band SIGUSR1 restart after the turn, ``--replace``
    takeover, tests — keeps the configured drain, except that any stop under
    systemd is capped by the live ``TimeoutStopSec`` (``runner._systemd_timeout_stop_sec``,
    set at boot from the same probe that logs the alignment warning). An unreadable budget on a
    systemd-managed host fails closed against the assumed floor (:func:`systemd_stop_budget`).
    Duck-typed and getattr-guarded on purpose: shutdown-path tests drive ``_stop_impl``
    from bare doubles that are not ``GatewayRunner`` instances.
    """
    drain = getattr(runner, "_restart_drain_timeout", DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT)
    if getattr(runner, "_stop_requested_by_signal", False):
        drain = resolve_launchd_capped_drain(drain, getattr(runner, "_launchd_exit_timeout_s", None))
    return resolve_stop_drain_budget(runner, drain)


def effective_stop_watchdog_delay(runner: object, watchdog_delay: float) -> float:
    """Thread-watchdog leash for the stop in progress on ``runner``.

    ``watchdog_delay`` is the supervisor-agnostic leash (effective drain +
    grace). Under launchd a signal-driven stop is SIGKILLed at the live
    ``ExitTimeOut`` — with a 60s budget the default leash (50 + 60 = 110s)
    can never fire, so the forensic stack dump and ``os._exit`` the watchdog
    exists for are lost. Clamp the leash to ``ExitTimeOut -
    LAUNCHD_WATCHDOG_DUMP_MARGIN_S`` so the dump lands before SIGKILL. Same
    duck-typing rules as :func:`effective_stop_drain_timeout`; the systemd clamp reuses the
    same stop-budget resolution, so an unreadable unit shortens the leash rather than losing
    the dump to SIGKILL.
    """
    leash = _seconds(watchdog_delay)
    if getattr(runner, "_stop_requested_by_signal", False):
        leash = resolve_launchd_capped_drain(
            leash, getattr(runner, "_launchd_exit_timeout_s", None), cleanup_reserve_s=LAUNCHD_WATCHDOG_DUMP_MARGIN_S,
        )
    # Under systemd the leash must fire before TimeoutStopSec or the forensic dump is lost to SIGKILL.
    budget, source = systemd_stop_budget(runner)
    if source == "managed-no-budget":
        _warn_unverified_stop_budget()
        budget = SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S
    return resolve_systemd_capped_drain(
        leash, budget,
        cleanup_reserve_s=SYSTEMD_WATCHDOG_DUMP_MARGIN_S,
    )


_TRUTHY = {"1", "true", "yes", "on"}


def is_global_startup_conflict(error_code: str | None) -> bool:
    """True when an adapter's fatal error is a single-writer ownership conflict.

    Adapters emit ``{scope}_lock`` with ``retryable=True`` so a *mid-run* reconnect can
    recover; at startup a live foreign holder is a configuration conflict (two gateways
    cannot poll one token), not a transient blip.  Matches by error CODE only, never text.

    ``BasePlatformAdapter._acquire_platform_lock`` emits ``{scope}_lock`` with ``retryable=True`` on
    purpose: a *mid-run* reconnect must be able to recover once the live holder exits or a stale record is
    cleared (#54167). This matches by error CODE only (the ``{scope}_lock`` / ``lock_conflict`` families
    every adapter emits for scoped-lock and identity conflicts), never by message text.
    """
    code = (error_code or "").strip().lower()
    return bool(code) and (code == "lock_conflict" or code.endswith("_lock"))


def is_gateway_supervisor_process(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this gateway process is owned by a supervisor that RESTARTS it.

    Selects the exit-75 restart route, so only markers of a manager with a restart policy count:
    systemd ``INVOCATION_ID``, launchd ``XPC_SERVICE_NAME`` (or the wrapper-forwarded
    ``HERMES_LAUNCHD_LABEL`` the grandchild sees), the s6 sentinel, or the explicit
    ``--external-supervisor`` opt-in. The generalized ``HERMES_SUPERVISED_CHILD`` launcher marker is
    deliberately NOT read here: the Windows Scheduled-Task launcher sets it without a restart policy
    (#113670), and routing its ``/restart`` through exit 75 would leave the gateway dead.
    """
    env = os.environ if environ is None else environ
    xpc_service = env.get("XPC_SERVICE_NAME", "")
    return bool(env.get("INVOCATION_ID") or env.get("HERMES_S6_SUPERVISED_CHILD") or (xpc_service and xpc_service != "0")
                or launchd_job_label(env)
                or str(env.get(EXTERNAL_GATEWAY_SUPERVISOR_ENV, "")).strip().lower() in _TRUTHY)


def is_supervised_gateway_launch(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this gateway was launched by a generated service/launcher rather than a shell.

    Superset of :func:`is_gateway_supervisor_process` that also honours ``HERMES_SUPERVISED_CHILD``,
    the marker every generated launcher exports (systemd unit, launchd plist, s6 run script, Windows
    Scheduled Task — see ``hermes_cli.main._apply_profile_override``). This is the identity the
    self-targeting guards key on: a kill or lifecycle command issued from inside such a gateway takes
    down the process hosting the caller with nobody at a terminal to bring it back (#113667).
    """
    env = os.environ if environ is None else environ
    if env.get("HERMES_SUPERVISED_CHILD"):
        return True
    return is_gateway_supervisor_process(environ)


def is_container_restart_context() -> bool:
    """In a container (Docker/Podman) the detached setsid restart path dies with the cgroup,
    so exit-75 service restart is the only viable path.  Own function so tests can mock it."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def _seconds(value: object, fallback: float = 0.0) -> float:
    """Non-negative float, or ``fallback`` on non-numeric input."""
    try:
        return max(float(value), 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _parse_timeout_keeping_zero(raw: object, default: float, *, finite: bool = False) -> float:
    """Parse a timeout where ``0`` is a deliberate disable (must NOT fall through
    to ``default``), unlike None / blank / non-numeric (/ non-finite) input."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return default if finite and not math.isfinite(value) else max(0.0, value)


def parse_restart_drain_timeout(raw: object) -> float:
    """Parse a configured drain timeout; falsy (incl. ``0``) falls back to the shared default."""
    return _parse_timeout_keeping_zero(raw or None, DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT)


def parse_restart_after_turn_timeout(raw: object) -> float:
    """Parse the after-turn wait cap for in-band restart (``0`` = legacy immediate drain)."""
    return _parse_timeout_keeping_zero(raw, DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT)


def parse_cron_drain_timeout(raw: object) -> float:
    """Parse the cron/api_server drain floor (``0`` = opt out; both interrupted on the chat budget).

    ``0`` is a deliberate opt-out — cron and api_server (/v1) work is then interrupted on the same budget as chat work, the
    pre-#82161 behaviour — and must not fall through to the default, unlike empty/missing input.
    """
    return _parse_timeout_keeping_zero(raw, DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT)


def parse_signal_interrupt_grace_timeout(raw: object) -> float:
    """Parse the unexpected-signal post-interrupt grace timeout."""
    return _parse_timeout_keeping_zero(raw, DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT, finite=True)


def resolve_cron_drain_budget(
    drain_timeout: float, cron_drain_timeout: float, *, watchdog_delay: float, elapsed: float = 0.0,
    cleanup_reserve_s: float = CRON_DRAIN_CLEANUP_RESERVE_S,
) -> float:
    """Seconds the stop drain may wait on in-flight cron and api_server (/v1) work.

    Clamped to what this process can honour: the watchdog hard-exits at ``watchdog_delay``,
    so waiting past that leash minus ``cleanup_reserve_s`` swaps a cleanly-interrupted job
    for a SIGKILL that leaves it wedged.  Never less than ``drain_timeout`` (only extends).
    """
    drain = _seconds(drain_timeout)
    floor = _seconds(cron_drain_timeout)
    if floor <= 0.0:
        return drain
    ceiling = _seconds(watchdog_delay) - _seconds(elapsed) - _seconds(cleanup_reserve_s, CRON_DRAIN_CLEANUP_RESERVE_S)
    return max(drain, min(floor, ceiling))


def resolve_systemd_timeout_stop_sec(
    drain_timeout: float, cron_drain_timeout: float = DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT, *,
    cleanup_reserve_s: float = CRON_DRAIN_CLEANUP_RESERVE_S, headroom_s: float = SYSTEMD_STOP_HEADROOM_S,
    floor_s: float = SYSTEMD_TIMEOUT_STOP_SEC_FLOOR,
) -> int:
    """Seconds systemd ``TimeoutStopSec`` must cover: the stop path may first wait
    ``cron_drain_timeout`` + ``cleanup_reserve_s`` for cron and api_server work, so sizing from the chat drain
    alone lets systemd SIGKILL an in-budget drain.  A zero cron timeout is an opt-out.

    ``restart_drain_timeout`` is only the chat-turn interrupt budget (default 0). See #94759.
    """
    drain = _seconds(drain_timeout)
    cron = _seconds(cron_drain_timeout)
    cron_budget = (cron + _seconds(cleanup_reserve_s)) if cron > 0.0 else 0.0
    return int(max(_seconds(floor_s), max(drain, cron_budget) + _seconds(headroom_s)))


def resolve_restart_exit_wait_budget(
    drain_timeout: float, after_turn_timeout: float, cron_drain_timeout: float, *, headroom: float = 15.0,
) -> float:
    """Seconds a CLI waits for the gateway PID to exit after SIGUSR1: the in-band after-turn
    deferral, then the longest supervisor stop envelope — the one systemd's ``TimeoutStopSec`` is
    sized from (cron drain + cleanup reserve, supervisor headroom, floor; #94759) — then
    observer ``headroom``.

    A non-finite drain means "wait indefinitely", not a crash in the integer envelope.
    """
    if not all(math.isfinite(_seconds(value)) for value in (drain_timeout, cron_drain_timeout)):
        return math.inf
    return _seconds(after_turn_timeout) + resolve_systemd_timeout_stop_sec(drain_timeout, cron_drain_timeout) + _seconds(headroom)
