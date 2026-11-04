# HERMES-RESTORE-002 — External CA review package (correction 7)

Consolidated evidence for the two remaining operational blockers after RESTORE-001 — the gateway
shutdown/drain defect (**Fault B**) and the interrupted-session inventory — plus the two release
prerequisites the External CA raised against PR #9 (EXT-01/02), the three release-orchestration
findings raised against the corrected candidate (EXT-03 launcher integrity, EXT-04 cutover
admission control, EXT-05 supervisor lifecycle, EXT-06..11 phase and checksum contracts), the
release-manifest completeness defect the first owner-approved attempt exposed (EXT-12), and the
single bounded release procedure.

Nothing in this package has been deployed, merged, restarted or activated. The live service is
exactly as RESTORE-001 left it.

## 0. Exact identity

| item | value |
|---|---|
| deployed installation (untouched) | HEAD `e05128febbc559469a5fb835a9235a543dfdfa64`, tree `dc05abd074c834569cf19ca89a6446d74be5c41d`, clean |
| **release candidate** (code + tests + tooling, incl. EXT-12) | **`d4a5ef0a038ba1f4a9796291a445d09713ea5a28`**, tree **`7cc9304fb8d11ae6100d002b4b66ad556ab90a5a`** |
| previous candidate (EXT-06..11, superseded by EXT-12 — its release authority is exhausted and does not transfer) | `7596026ab684d4b52935388bfc17bcaa948f2387`, tree `7e6042d786509439badf8eba554bd9890f039ab0` |
| previous candidate (EXT-06..10, superseded by EXT-11) | `a4f08d6315072e6057984ae74dbf289736f9d110`, tree `27f8916b1eafbad3328e09139c21c7654645fe99` |
| previous candidate (EXT-06..09, superseded by EXT-10) | `3b5c0fc4ee11efdd634b66aa1c0efc68feaec2ef`, tree `8c34454e0b115f3836eabe9f08fff8c2cb1b7a0b` |
| previous candidate (EXT-03/04/05) | `1cc36452f47cef911afde6da5139a56e9de2da7e`, tree `2ec9f4718f776f28e232b8aeffd5386f803a8258` |
| previous candidate (EXT-01/02 only) | `e8ed5b9b853aadef7b3830d0194275084b2449e8`, tree `913c0edc6d7cb9529259317a44a18fb63c0a2922` |
| earlier reviewed head (superseded) | `5d02e1da35ef8afcf5643f31d32283c7c1286740`, tree `b739c90e245e9fa74dfc154323e2353f03256de7` |
| branch | `restore002/drain-envelope` in `/home/hermes/work/restore002-fix`, pushed to `fork` |
| pull request | #9, draft, base `main` |
| this document | committed directly above the candidate; it changes no executable code, so the deployable identity stays `d4a5ef0a` / `7cc9304f` |
| authoritative launcher | `/usr/local/lib/hermes-release/hermes-release-002` (+ `manifest.json`, `engine/<candidate>/`) — **not installed yet**; nothing is written outside the repo until the owner authorises step 1 of §15 |

The candidate is a strict descendant of the reviewed head: `git diff 5d02e1da..d4a5ef0a` touches
`gateway/restart.py`, `gateway/run_shutdown.py`, `gateway/run_startup.py`, two test files, four
harness scripts and one evidence JSON. No file from the reviewed commit was reverted.

Live host at time of writing: `hermes-gateway.service` active/running, PID `743641` since
2026-11-03 19:40:20, `NRestarts=0`, `TimeoutStopUSec=5min`, `HERMES_RECOVERY_HOLD=1`, 498 open
session rows, no failed units.

Owner restrictions honoured throughout: no production restart, no systemd modification, no
recovery-hold removal, no interrupted-session activation, no merge, no deployment, no updater
execution, no `state.db` write (every handle opened `mode=ro` + `PRAGMA query_only=1`).

---

## 1. EXT-RESTORE-002-01 — fail-closed stop-budget probe

### 1.1 What the reviewed head actually did

The reviewed head introduced `resolve_systemd_capped_drain(drain, timeout_stop_s, reserve)` and
documented it as *fail-open on unknown budgets*: if the live `TimeoutStopUSec` could not be read,
the configured drain was preserved. That is defensible as arithmetic (a cap cannot cap against a
number that does not exist) but it is the wrong **policy** on a host we know is systemd-managed.

Failure path, before this correction:

1. `systemctl show -p TimeoutStopUSec` fails or returns `infinity`/garbage — a transient dbus
   error, a renamed unit, a container, a future systemd output format.
2. `_systemd_timeout_stop_sec` stays `None`, so `resolve_systemd_capped_drain` returns the full
   configured drain (180 s).
3. The unit's real `TimeoutStopSec` is small (a stale generated unit: 210 s, or the 60 s floor).
4. Drain 180 s + post-drain phases (85 s worst case) overruns the envelope; systemd sends
   **SIGKILL** — mid-settle, possibly mid-SQLite-write.

So an unreadable probe silently restored the exact envelope that Fault B proved unsafe. The
External CA's requirement is that this path fails **closed** instead.

### 1.2 The correction — cap vs policy, separated

`gateway/restart.py`:

| symbol | role |
|---|---|
| `SYSTEMD_UNKNOWN_BUDGET_ASSUMED_S = 60.0` | the budget assumed when systemd management is known but the value is unreadable; equals `SYSTEMD_TIMEOUT_STOP_SEC_FLOOR`, i.e. the smallest unit this code can itself generate |
| `coerce_stop_budget_s(value)` | validation only: accepts a finite positive float (or numeric/`"300s"`-free string forms the probe emits), rejects `None`, `0`, negatives, `nan`, `±inf`, unparsable |
| `systemd_stop_budget(runner)` | returns `(budget, state)` where `state ∈ {"unmanaged", "managed", "managed-no-budget"}` — so a caller can distinguish *no systemd clock* from *a systemd clock it cannot read* |
| `resolve_stop_drain_budget(runner, drain)` | the policy layer: `unmanaged` → configured drain (unchanged behaviour on launchd/dev/CI); `managed` → capped drain; **`managed-no-budget` → assumed 60 s floor → drain 0** |
| `_warn_unverified_stop_budget(...)` | one warning per process, at boot, when the state is `managed-no-budget` |

Why drain 0 rather than "the old 180 s": with an assumed 60 s envelope and a 90 s post-drain
reserve, no positive drain fits. A turn interrupted at SIGTERM is recoverable — the shutdown path
marks `resume_pending` with a recognised reason and the planner can resume it. A SIGKILL during a
SQLite write is not recoverable, and it is the failure Fault B was opened for. Failing closed here
means: **interrupt early, deliberately, and let the recovery machinery handle it.**

`resolve_systemd_capped_drain` is unchanged in contract and now only ever called with a validated
budget; `systemd_stop_budget()` and `resolve_stop_drain_budget()` are the only places that may
invent one.

`gateway/run_startup.py`: the boot probe now validates before storing (`coerce_stop_budget_s`),
stores `managed-no-budget` state as budget unknown, and emits the warning at boot — the operator
learns before teardown, not from a SIGKILL.

`gateway/run_shutdown.py`: the shutdown snapshot gains `systemd_stop_budget_source` with values
`"live systemd TimeoutStopSec"`, `"assumed systemd stop floor (TimeoutStopSec unreadable)"`,
`"live launchd exit timeout"`, `"none"`. A drain-cap warning therefore can never present an assumed
floor as a measured one.

### 1.3 Negative controls

`tests/gateway/test_stop_budget_probe_failclosed.py` — **158 lines, 51 tests**, all synthetic (a
`_Runner` stand-in, no systemd, no I/O):

| control | cases | asserts |
|---|---|---|
| `test_invalid_budget_never_restores_the_configured_drain` | 10: `None`, `0`, `0.0`, `-5.0`, `nan`, `inf`, `-inf`, `"180s"`, `""`, `"budget9"` | every invalid probe on a **systemd-managed** host yields drain **0**, never the configured 180 |
| `test_invalid_budget_shortens_the_watchdog_leash_below_the_assumed_floor` | same 10 | the watchdog leash is ≤ the assumed floor − dump margin, so forensics land before SIGKILL |
| `test_assumed_floor_is_the_smallest_unit_this_code_can_generate` | 1 | "unknown" can never *extend* a drain: the assumed budget equals the generated-unit floor |
| `test_known_small_budget_still_interrupts_immediately` | 7 | a *known*, valid budget below drain + reserve also yields 0 — fail-closed is not the only path to 0 |
| `test_known_budget_is_capped_and_never_extended` | 4 (`210`, `240`, `300`, `190`) | a known budget caps the drain and is never rounded up |
| `test_unknown_budget_off_systemd_keeps_the_configured_drain` | 1 | no systemd clock ⇒ configured drain preserved |
| `test_duck_typed_runner_without_the_seam_is_untouched` | 1 | a runner lacking the probe seam is treated as unmanaged, not as an error |
| `test_unreadable_budget_is_loud_once` | 1 | the warning is emitted, and only once per process |
| `test_probe_failure_is_visible_in_the_stop_budget_source` | 1 | `systemd_stop_budget_source` names the assumed floor as unreadable, never as measured |
| `test_coerce_stop_budget` | 11 | the validator's full accept/reject matrix |
| `test_systemd_managed_detection` | 1 | management is detected from the real seams (`/proc/1/comm`, unit env), not guessed |
| `test_unreadable_proc_and_no_env_is_not_managed` | 1 | undetectable ⇒ unmanaged, i.e. the conservative reading of the environment, with the drain preserved |
| `test_assumed_cap_is_exactly_the_floor_arithmetic` | 1 | the assumed budget produces drain 0 by arithmetic, not by a special case |
| `test_launchd_stop_still_honours_its_own_budget_first` | 1 | launchd's `ExitTimeOut` still wins on launchd |

The previously reviewed `test_stop_budget_adaptation.py` contained a test asserting the fail-open
behaviour. It was **not deleted and not weakened** — it was renamed to
`test_pure_cap_ignores_an_unknown_budget_but_the_caller_must_fail_closed`, which is exactly what
`resolve_systemd_capped_drain` still does, with a docstring pointing at the new file as the
fail-closed authority. `test_launchd_margin_still_wins_under_launchd` now declares non-systemd
explicitly via `monkeypatch.setattr(rs, "systemd_managed", ...)`, because the suite runs on a
systemd host where the old assumption was silently false.

Suite result: **51 passed** (file run standalone), and **244 passed, 4 skipped** across the seven
targeted suites — see §4.

---

## 2. Fault B — the defect, the model, the retirement condition

*(retained from the reviewed package; unchanged by this correction)*

### 2.1 Observed event (2026-11-03, cutover stop)

| phase | measured |
|---|---|
| pre-drain `mark_resume_pending` + notify | **6.32 s** |
| drain (`restart_drain_timeout`) | **180.01 s**, `timed_out=True`, 1 agent |
| interrupt + settle | 3.14 s |
| kill tool subprocesses + finalize + quiesce | 4.45 s |
| SessionDB close | 0.05 s |
| **total stop** | **193.61 s** |

The drain spent its whole budget and returned `timed_out`, then needed 13.6 s more. Against
`TimeoutStopSec=210` that leaves 16 s of slack, while every remaining phase (settle alone is
budgeted 45 s + a 10 s tail reserve) is unbudgeted inside it. **The drain budget was not a share of
the stop envelope; it was the whole envelope plus whatever teardown cost.**

### 2.2 Root cause

`resolve_systemd_timeout_stop_sec(drain, cron_floor)` sized the unit as `drain + 30 s`. Systemd's
stop clock starts at SIGTERM and covers drain **and** interrupt **and** settle **and** finalize
**and** quiesce **and** SessionDB close. A 30 s headroom is smaller than the 45 s settle budget
alone, so a unit generated by this function could be "correct" and still SIGKILLed mid-settle. The
300 s drop-in hid this for the cutover; it did not fix it.

Second half: nothing clamped the drain against the *live* unit — only launchd's `ExitTimeOut` was
honoured.

### 2.3 Reproduction (deterministic, no restart required) — `harness/fault_b_repro.py`

Worst-case phase budgets are the code's own: notify 15 s, settle 45 s, finalize 15 s, close 10 s.

| case | unit | drain | total | margin | verdict |
|---|---|---|---|---|---|
| pre-fix / shipped | 210 | 180 | 265.00 | −55.00 | **SIGKILL** |
| pre-fix / drop-in | 300 | 180 | 265.00 | +35.00 | fits |
| post-fix / shipped (stale unit) | 210 | 120 | 205.00 | +5.00 | fits |
| post-fix / regenerated | 300 | 180 | 265.00 | +35.00 | fits |

Observed-cost rows (6.32 / 180.01 / 3.14 / 4.45 / 0.05) reproduce the historical 193.6 s stop and
fit in every case; the model is calibrated on the journal, not on an estimate. It does **not**
prove the kernel never sends SIGKILL — that needs a real stop, which is owner-gated (§6).

### 2.4 The envelope correction

- `SYSTEMD_STOP_HEADROOM_S 30.0 → 120.0`, derived from the sum of the post-drain phase budgets
  (85 s worst case) plus margin. Side effect: the generated unit for the shipped 180 s drain is now
  **300 s**, exactly what the drop-in provides, so retiring the drop-in changes no behaviour.
- `SYSTEMD_POST_DRAIN_RESERVE_S = 90.0` — settle 45 + tail 10 + quiesce/close ~7 + margin.
- `resolve_systemd_timeout_stop_sec`: `(60,30) → 180`, `(180,30) → 300`, `(0,60) → 190`, floor 90.

### 2.5 When `95-timeout-stop.conf` can be retired

All four must hold:

1. The fixed code is installed (generator emits 300 s for a 180 s drain / 30 s cron floor).
2. `systemctl show -p TimeoutStopUSec hermes-gateway.service` reports `300000000` **with the
   drop-in removed** — proving the value now comes from the unit, not the override.
3. One owner-approved restart completes the shutdown-alignment gate with no SIGKILL and a stop
   time inside the envelope.
4. Rollback is one file: re-add the drop-in, `daemon-reload`, no restart required.

Until (1) is deployed, removing the drop-in reverts to 210 s, which overruns by 55 s.
**Retirement follows deployment, never precedes it.** The exact reversible procedure is §6.6.

---

## 3. EXT-RESTORE-002-02 — reconciliation of the reported failed file edit

### 3.1 The report

During RESTORE-002 development an attempt to write `gateway/session_recovery.py` was refused:

```
Refusing to overwrite .../gateway/session_recovery.py
```

The file already existed (27,789 chars). The report to the owner described the intended module and
the refusal, and the External CA correctly asked what, if anything, was lost — because a refused
write is not evidence that the *intended* change landed somewhere else.

### 3.2 What was verified, and how

| check | method | result |
|---|---|---|
| Is the intended artefact present? | `git ls-files` + tree listing of the candidate | `gateway/session_resume_plan.py` exists, 263 lines, 11 dedicated tests, both committed |
| Was the pre-existing module damaged? | `git hash-object gateway/session_recovery.py` vs `git rev-parse 5d02e1da:gateway/session_recovery.py` vs the same path in the deployed tree `e05128fe` | **byte-identical in all three** — the refused write mutated nothing |
| Did the working tree diverge from the commit? | `git status --porcelain -uall` before the correction commit; `git diff --stat 5d02e1da..HEAD` | clean before committing; the diff is exactly the files listed in §5, no orphan edits |
| Are there stub or half-written files left behind? | grep for `NotImplementedError`, `TODO`, `pass  # stub`, empty `def` bodies across `gateway/`, `harness/`, `tests/gateway/` | none attributable to this work |
| Do all new modules import and parse? | `harness/restore002_tree_audit.py` — AST parse of every file changed since `5d02e1da`, a docstring-integrity heuristic per module/class/function node, and sha256 comparison of `session_recovery.py` across the worktree, the reviewed head and the deployed tree | pass on all files; `session_recovery.py` identical in all three |
| Is any test evidence missing from the PR? | full `-k 'stop or drain or shutdown or restart'` selection run in **both** the candidate and a detached worktree at the reviewed head, FAILED sets diffed | identical FAILED sets; candidate adds 51 passing tests and no new failures |

### 3.3 Conclusion

**No change and no test evidence is missing.** The refused write was a safety guard doing its job:
the module name collided with an existing 27,789-char file that is still byte-identical to both the
reviewed head and the deployed installation. The intended classifier/planner shipped under a
distinct name, `gateway/session_resume_plan.py`, with its own tests, and is present in the
candidate tree (`git cat-file -e 1cc36452:gateway/session_resume_plan.py` succeeds).

Two consequences worth stating plainly:

- The refusal was **not** a partial write. `write_file` refuses before truncating; the pre-existing
  file's blob hash is unchanged, so nothing was half-applied.
- The rename was not cosmetic damage control. `session_recovery.py` is the gateway's existing
  recovery *executor*; the planner is a read-only *classifier* that must be usable without any
  execution path. Keeping them separate is the safety property, not an accident of naming.

---

## 4. Retest — exact identity, both sides

Command (identical in both trees, `-p no:randomly` fixed order, `PYTHONPATH` cleared, `TMPDIR=/tmp`):

```
env -u PYTHONPATH TMPDIR=/tmp /usr/bin/python3.13 -m pytest \
  tests/gateway/test_stop_budget_probe_failclosed.py tests/gateway/test_stop_budget_adaptation.py \
  tests/gateway/test_cron_drain_floor.py tests/gateway/test_launchd_exit_timeout_drain_cap.py \
  tests/gateway/test_shutdown_forensics.py tests/hermes_cli/test_gateway_service.py \
  tests/gateway/test_session_resume_plan.py -q -p no:randomly
```

| tree | passed | skipped | failed | errors |
|---|---|---|---|---|
| reviewed head `5d02e1da` (7 files, incl. new file absent → 6 files present) | **193** | 4 | **1** | **2** |
| candidate `e8ed5b9b` (EXT-01/02) | **244** | 4 | **1** | **2** |
| candidate `1cc36452` (EXT-03/04/05) | **327** | 4 | **1** | **2** |

Delta: **+51 passing**, and the single failure plus two errors are the *same tests with the same
causes* at both identities (§8). No test was skipped, deleted, xfailed or loosened to reach this
number; the file that changed expectation (`test_stop_budget_adaptation.py`) gained 2 tests.

Collection: `85 tests collected` across the three new/rewritten files
(`test_stop_budget_probe_failclosed.py` 51, `test_stop_budget_adaptation.py` 23,
`test_session_resume_plan.py` 11).

---

## 5. What changed, precisely (`5d02e1da..1cc36452`)

Cumulative, reviewed head to candidate (`19 files changed, 4066 insertions(+), 198 deletions(-)`):

```
docs/restore002/session-inventory-20261104.json    | 137 ++++++
gateway/restart.py                                 | 136 ++++++++++++++-----
gateway/run_shutdown.py                            |  13 +-
gateway/run_startup.py                             |  16 +-
harness/release002_preflight.py                    | 380 +++++++++++++++++++++++
harness/release002_release.sh                      | 312 +++++++++++++++++++++
harness/restore002_session_inventory.py             |  92 ++++++
harness/restore002_tree_audit.py                   | 110 +++++
tests/gateway/test_stop_budget_adaptation.py       |  12 +-
tests/gateway/test_stop_budget_probe_failclosed.py | 158 ++++++++++
gateway/drain_control.py                           |  86 +++++++++
gateway/session_ingress.py                         |  10 ++
harness/hermes-release-002                         | 325 ++++++++++++++++++++
harness/release002_control.py                      | 640 +++++++++++++++++++++++++++
tests/gateway/test_admission_freeze_gate.py        | 122 ++++++
tests/harness/__init__.py                          |   0
tests/harness/test_release002_control.py           | 476 +++++++++++++++++++++
tests/harness/test_release002_launcher.py          | 306 +++++++++++++++++
```


No production file, unit, drop-in, hold, session row or receipt was modified. The live install is
still `e05128fe` / `dc05abd0`, clean.

---

## 6. The single bounded release procedure

Three artefacts, all in the candidate tree, all read-only until explicitly armed. **There is one
documented way to run this release** (§11, §15): the launcher installed outside the installation.
The engine below is an implementation detail that refuses to run itself.

- `harness/hermes-release-002` — 325 lines, the single entry point; `status|stage|plan|apply|
  verify|recover`; installed to `/usr/local/lib/hermes-release/`; verifies its own checksum manifest
  before doing anything.
- `harness/release002_preflight.py` — 380 lines, mutates nothing, exit `0` green / `2` gate failed /
  `3` unevaluable (fail closed, treated as failed).
- `harness/release002_release.sh` — 620 lines, the bounded engine, **dry-run by default**; refuses
  `--apply` without `HERMES_RELEASE_LAUNCH_TOKEN`, `HERMES_RELEASE_APPROVAL=<candidate-sha>`, root,
  and a cgroup outside the gateway's own.

### 6.1 Preflight — prove nothing will be interrupted (gate `quiet_window`)

`release002_preflight.py` asserts, before any mutation:

| gate | requirement |
|---|---|
| `install_identity` | install HEAD/tree = the recorded deployed identity, working tree clean |
| `candidate_identity` | candidate SHA exists, its tree matches the recorded candidate tree, and it descends from the deployed HEAD |
| `unit_identity` | `/etc/systemd/system/hermes-gateway.service` sha256 matches the recorded value (no drift since review) |
| `dropins_present` | `90-recovery-hold.conf` (sha256 match) and `95-timeout-stop.conf` (`TimeoutStopSec=300s`) both present |
| `stop_budget` | live `TimeoutStopUSec` parses (via `parse_systemd_duration`: `300000000`, `300s`, `5min`, `1min 30s`, …) to **≥ 300 s** |
| `recovery_hold` | `HERMES_RECOVERY_HOLD=1` in the unit environment |
| `service_state` | active/running, `MainPID` recorded, `NRestarts=0` |
| `headroom` | `MemAvailable` ≥ floor, disk free ≥ floor |
| `no_active_turns` | `gateway_state.json` reports `active_agents == 0` — the gateway's own count of turns it is still running |
| `quiet_window` | no session with activity inside the window (`recent_session_activity`), and the gateway's process tree is quiet (`gateway_children_quiet`, ≤ 12 children) |
| `session_inventory` | read-only open of `state.db` (`mode=ro`, `query_only`), 498 open rows, fingerprint recorded for post-hoc comparison |

If either cannot be evaluated — state file unreadable, process table blocked — the script exits `3`
and the procedure stops. It never assumes "probably idle".

**Live demonstration.** Run at 10:49 local with four turns in flight, the preflight returned
`verdict=NO_GO`, `failed=["recent_session_activity","no_active_turns"]`, exit `2`:

```
no_active_turns          gateway_state.active_agents=4
recent_session_activity  active within 120s: 4 (20260919_114326_d064228f, 20261022_064037_ca2cae72,
                         20261101_113736_7565f3f9, 20261103_111647_bd71d22f)
```

That is the behaviour the External CA asked to see: the gate refuses to authorise a stop while a
turn is live, including this conversation's own.

### 6.2 One controlled stop/start (gate `shutdown_timing`)

`release002_release.sh --apply --candidate <sha>` from a transient unit, so the runner cannot be
killed by the stop it performs:

```
sudo HERMES_RELEASE_APPROVAL=<sha> systemd-run --unit=hermes-restore-002-release --collect \
  --property=Type=oneshot --property=TimeoutStartSec=900 \
  /home/hermes/work/restore002-fix/harness/release002_release.sh --apply --candidate <sha>
```

Sequence, exactly once, no automatic retry:

1. **Preflight** (`release002_preflight.py`). Any non-zero exit → stop, mutate nothing.
2. **Archive** the live unit, every drop-in and a sha256 manifest, plus the pre-deploy install HEAD
   and a read-only session inventory.
3. **Quiesce**: poll `gateway_state.json` for `active_agents == 0` on **three consecutive samples**
   (5 s apart, up to `QUIESCE_WAIT`). No window → `ABORTED_NOT_QUIET`, exit `3`, nothing mutated.
4. **Deploy**: `git merge --ff-only <candidate>` in the install; assert HEAD = candidate, tree =
   recorded candidate tree, working tree clean. Any mismatch → rollback.
5. **Regenerate the unit** from the deployed code (`gateway install --system`), `daemon-reload`,
   then assert the unit file carries `TimeoutStopSec=300s` **and** that `systemctl show` reports
   `TimeoutStopUSec=5min`. Either check failing → rollback (this is what makes §6.6 safe).
6. **One controlled stop/start**: `timeout 340 systemctl restart hermes-gateway.service`, timed in milliseconds, no retry; then wait for
   `ActiveState=active` with a **new** non-zero MainPID.
7. **Forensics**: journal since the restart, counted `with signal KILL` lines (must be 0), the last
   drain/stop lines, and the shutdown snapshot's `systemd_stop_budget_source`.

**SIGKILL verification is positive evidence, not the absence of a crash**: `restart` rc 0, a stop
duration inside the envelope, the drain/stop lines showing the budget that was used, and a journal
scan proving no `signal KILL` line for the gateway in that window.

On any failure after the deploy step: restore the archived unit and drop-ins, `daemon-reload`,
`git merge`/`checkout` back to the recorded deployed HEAD, one restart, verify identity — and stop.
A second restart is a rollback, not a retry.

### 6.3 Generated unit identity and the effective 300 s

Three assertions, all made by the procedure after the start (steps 5–7):

1. The unit file on disk carries `TimeoutStopSec=300s` — written by the deployed generator, not by
   the drop-in.
2. `systemctl show -p TimeoutStopUSec hermes-gateway.service` → `5min`, i.e. `300000000`.
3. The gateway's own boot alignment line (`_start_log_systemd_timing_alignment`) reports the same
   budget it will use at stop time.

If (1) or (2) fails, the drop-in is the only thing providing the ceiling, the generator is wrong,
and the procedure rolls back rather than proceeding to §6.6.

### 6.4 Both drop-ins stay

`90-recovery-hold.conf` and `95-timeout-stop.conf` remain in place for the whole procedure and
after it. The hold is not a deployment artefact; it is a control the owner releases deliberately.
Keeping `95-` through the first restart also makes the first restart's envelope identical to the
one that carried the cutover — the fix is tested against a known-good ceiling before the ceiling is
removed.

### 6.5 State preserved

- `90-recovery-hold.conf` — untouched, `HERMES_RECOVERY_HOLD=1` throughout.
- `state.db` — opened read-only by every script; no vacuum, no prune, no row write.
- The **eight unresolved session records** — see §7. Not touched, not ended, not resumed.

### 6.6 Retiring `95-timeout-stop.conf` — reversible, only after §6.2 passes

Separate, later, and independently reversible:

1. Confirm §6.3 still holds (`300000000` from the unit).
2. `cp -a /etc/systemd/system/hermes-gateway.service.d/95-timeout-stop.conf <archive>/` and record
   its sha256.
3. `rm` the drop-in, `systemctl daemon-reload`.
4. `systemctl show -p TimeoutStopUSec` **still** `300000000` — this is the whole proof; if it drops
   to `210000000`, the generator is not emitting 300 s and the drop-in must be restored at once.
5. One further owner-approved restart, with the same timing + SIGKILL verification as §6.2.
6. Rollback at any point: restore the archived file, `daemon-reload`, no restart needed to restore
   the ceiling.

Retirement is behaviour-neutral **only** after the fix is deployed; §2.5 gives the four conditions.

---

## 7. The eight unresolved session records

`harness/restore002_session_inventory.py --summary` against the live database (read-only) reports
`open_sessions = 498`, `empty_open = 8`, fingerprint
`c7bca83b3a10ccf8082fbcbbd3e0d4edca8b07e939c36ffee864a286dac29dc5`.

| session id | messages | in index | routing row | bucket |
|---|---|---|---|---|
| `20260521_130704_e2b85331` | 0 | no | no | obsolete |
| `20260627_140722_29907a01` | 0 | no | no | obsolete |
| `local-7d6f5ddea272ed622551035454fde9f467243e7aa1e16822764761032a4ed901` | 0 | yes | yes | obsolete |
| `local-da851133fb90988c974c24459af0641ff1f2094550476ada887b69f88de4f4d5` | 0 | yes | yes | obsolete |
| `local-efa627bea17ff79e9e8267f617e9f10b7eba2a3586e9092420510b8245b44187` | 0 | yes | yes | obsolete |
| `20261101_114213_465a52f1` | 0 | yes | yes (id mismatch) | **needs_human** |
| `20261102_110517_98e494e0` | 0 | yes | yes | obsolete |
| `20261102_170709_5cdda643` | 0 | yes | yes | obsolete |

These are exactly the eight empty open rows; none carries a `resume_pending` marker, so none is
resumable and none will activate on its own. `465a52f1` is the only row whose routing entry names a
different `session_id`; the planner treats it as `needs_human` and will never batch it. All eight
are preserved unmodified — this package neither ends them nor explains them away.

Bucket digests (so the classification can be re-verified without dumping 2 711 ids):

```
active      e85ad131da3b8d91a31d2cbd81ee986c0cfaa27f6aef63411f22407d6b25cedc  (5)
obsolete    515c2035a0eaf3a375fbe8c50ed6ad764e0e7dfabe8952648f270dacdd98e8cd  (492)
needs_human e7945815289e0e5069d13dcbdd7f76328115cde829a21bab40746df258581300  (1)
resumable   e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  (0 — empty digest)
completed   9c88752cb65e0cac3887b7723fd58c2bde578718b6cc4b0e0358fe80f1c6bd42  (2213)
```

Releasing the hold today would resume **zero** sessions: no marker exists, and any 2026-11-03
marker would be stale by an order of magnitude against the 1 h freshness window. The hold remains
in place regardless — that is the owner's decision, not this package's.

### 7.1 The 508 → 498 arithmetic (unchanged, still closes)

| movement | count | evidence |
|---|---|---|
| open at cutover | 508 | RESTORE-001 receipt |
| ended after cutover, started before | 10 | `end_reason='startup_orphan_reap'`, all 21:41:11 local |
| created after cutover | +2 | both `source='cron'` |
| ended after cutover, started after | −2 | `end_reason='cron_complete'` |

`508 − 10 + 2 − 2 = 498`. No unexplained rows.

---

## 8. Adversarial self-review (revised for this correction)

| attack | answer |
|---|---|
| "Fail-closed means drain 0 — you turned a graceful stop into an abrupt one." | Only when systemd management is confirmed **and** the budget is unreadable, i.e. when the alternative is a SIGKILL at an unknown moment. A SIGTERM at a known moment is recoverable via `resume_pending`; a SIGKILL mid-write is not. And the state is reported: boot warning + `systemd_stop_budget_source` in the snapshot. |
| "So a transient `systemctl` hiccup now kills every turn." | A hiccup at boot is warned once and the operator can restart before an engineering turn; the assumed floor is 60 s, and 60 s is what the code considers the safe minimum for any unit. The alternative — pretending the drain still fits — is what Fault B punished. |
| "You moved the policy into a new function to make the old test pass." | The old test asserted the *pure cap*, which is still exactly what the function does; it was renamed to say so and now cross-references the fail-closed tests. Nothing was deleted, and the new file adds 51 controls, including 10 invalid-input cases per contract. |
| "`test_launchd_margin_still_wins_under_launchd` was broken by your change — you monkeypatched it away." | The test asserted launchd behaviour while running on a systemd host, so it was silently testing the wrong branch once `systemd_managed` became load-bearing. It now declares the environment it means to test. |
| "The 30 → 120 headroom is tuning to a target." | Derived from the sum of the phase budgets the code itself sets (85 s worst case) plus margin; 90 was rejected for leaving 5 s. It also lands on 300 s, independently corroborated by a real stop. |
| "The model is not a SIGKILL proof." | Correct, and §2.3 does not claim otherwise. §6.2 is the only SIGKILL proof and it is owner-gated. |
| "The preflight can pass while a turn is running." | It cannot: `quiet_window` requires no recent session activity, no tool subprocess in the gateway cgroup, and a readable `gateway_state.json` showing no active turn; unevaluable ⇒ exit 3 ⇒ stop. |
| "The release script could be killed by the stop it performs." | It runs from `hermes-restore-002-release.service` and refuses to run if its cgroup matches the gateway's. |
| "Two restarts are hidden in the rollback path." | One restart on the success path. The rollback restarts once after a *failure*, and the script then stops; there is no retry loop, and the failure is reported as `ROLLED_BACK` rather than retried. |
| "The planner is dead code." | Deliberate. Activation is the owner's decision and needs identity checks against live state, not a stored plan. Its value is the classification evidence in §7 and a pre-flight for any future release. |
| "Calling 492 rows `obsolete` is a data-loss euphemism." | It means "open row with no resume marker"; nothing is deleted, ended or pruned. |

---

## 9. Reproduction

```bash
cd /home/hermes/work/restore002-fix        # candidate 1cc36452 / 2ec9f471

env -u PYTHONPATH /usr/bin/python3.13 harness/fault_b_repro.py                  # §2.3 table
env -u PYTHONPATH /usr/bin/python3.13 harness/restore002_tree_audit.py          # §3.2 integrity
env -u PYTHONPATH /usr/bin/python3.13 harness/restore002_session_inventory.py --summary   # §7
env -u PYTHONPATH /usr/bin/python3.13 -m pytest \
  tests/gateway/test_stop_budget_probe_failclosed.py tests/gateway/test_stop_budget_adaptation.py \
  tests/gateway/test_cron_drain_floor.py tests/gateway/test_launchd_exit_timeout_drain_cap.py \
  tests/gateway/test_shutdown_forensics.py tests/hermes_cli/test_gateway_service.py \
  tests/gateway/test_session_resume_plan.py -q -p no:randomly                   # §4 table

env -u PYTHONPATH /usr/bin/python3.13 -m gateway.session_resume_plan             # hold active → refused
env -u PYTHONPATH /usr/bin/python3.13 -m gateway.session_resume_plan --assume-hold-released

sudo /home/hermes/venv/bin/python /home/hermes/work/restore002-fix/harness/release002_preflight.py
/usr/local/lib/hermes-release/hermes-release-002 plan --candidate 1cc36452f47cef911afde6da5139a56e9de2da7e   # dry run
```

Base-side comparison worktree: `git worktree add /home/hermes/work/restore002-base 5d02e1da`.

Session SQL (read-only): `sqlite3 'file:/home/hermes/.hermes/state.db?mode=ro'`.

## 10. Known pre-existing failures (identical at both identities)

- `tests/hermes_cli/test_gateway_service.py::TestSystemUnitHermesHome::test_system_unit_remaps_caller_home_ld_library_path_components`
  — `PermissionError: /root/.hermes/tools/facts.json` raised by `tests/home_io_guard.py`; identical at
  `5d02e1da` and `e05128fe`.
- `tests/gateway/test_shutdown_forensics.py` — 2 errors, `blocked os.kill(<pid>, 9) — PID is outside
  the test process subtree` from `tests/conftest.py`; environmental, identical at base.
- `tests/hermes_cli/test_commands.py`, `tests/hermes_cli/test_skills_hub.py` — collection defects
  present in every worktree, unrelated to this change.
- `shellcheck` is not installed on the host; `release002_release.sh` is syntax-checked with `bash -n`
  and exercised dry-run only.

## 11. EXT-RESTORE-002-03 — one authoritative launcher, pinned to verified identities

### 11.1 The finding, and what was wrong before

The corrected candidate shipped `harness/release002_release.sh` inside the tree being modified and
described it as the thing to run. That is three defects at once: the release entry point is part of
what the release replaces, its instructions lived in the repository it mutates, and a second
invocation path (`bash harness/release002_release.sh --apply`) could not be distinguished from the
sanctioned one. A reviewer could not tell which file was authoritative, and a failed deploy could
leave no launcher able to roll it back.

### 11.2 The launcher

`harness/hermes-release-002` (325 lines) is installed **outside** the installation:

```
/usr/local/lib/hermes-release/hermes-release-002
/usr/local/lib/hermes-release/manifest.json            # pinned identities + checksums
/usr/local/lib/hermes-release/engine/<candidate>/      # engine + helpers extracted from the candidate
/var/lib/hermes-release/restore-002/journal.json       # release journal
/var/log/hermes-release-002-<run-id>.log               # supervisor output
```

Commands: `status` (read-only), `stage` (extract + pin engine, write manifest; root), `plan` (dry
run), `apply` (one controlled stop/start from a transient unit; root), `verify` (post-release,
read-only), `recover` (deterministic handling of an interrupted supervisor; root).

### 11.3 What is pinned, and to what

`manifest.json` records the candidate commit **and** tree, the SHA-256 of the launcher itself, of
each engine file, of the generated unit and of both drop-ins, the installation identity and the
release-run id. Every command re-verifies the manifest before it acts, so a launcher that has been
edited, a partially written `stage`, an engine blob that does not match the pinned candidate, or a
candidate that no longer resolves to the pinned tree is refused rather than executed.

`stage` is also the step that makes the candidate *available to the installation*: if the object is
absent it fetches **that one commit** from `$REPO` (`git fetch <repo> <sha>`, no refs, no branch, no
worktree change) and then refuses to pin unless the tree it just obtained equals the pinned tree.

### 11.4 Refusal matrix (all fail closed, nothing mutated)

| condition | exit | message |
|---|---|---|
| launcher is not the copy the manifest pins | 65 | `REFUSED: launcher checksum … does not match the manifest` |
| manifest missing / unreadable | 65 | `REFUSED: no manifest at …` |
| candidate is not a full 40-char SHA | 64 | `… is not a full 40-character commit SHA` |
| candidate absent from the installation, and unfetchable | 65 | `candidate … is not present in <install> (stage it first)` *(see 11.5)* |
| candidate tree in the installation differs from the repo tree | 65 | `candidate tree in <install> differs from <repo> — refusing to pin` |
| installation HEAD/tree not as expected, or worktree dirty | 65 | preflight `install_head` / `install_tree` / `install_clean` NO_GO |
| `apply` without `HERMES_RELEASE_APPROVAL=<candidate>` | 65 | `approval … does not authorise this candidate` |
| engine invoked directly (no launch token) | 65 | `refusing to run outside the launcher` |
| `apply` not root | 65 | `needs root` |
| `apply` from inside the gateway's cgroup | 65 | `refusing to restart the service from inside it` |
| another release supervisor is alive | 65 | `another release supervisor is running` |
| interpreter / control library unusable | 65 | `refusing to run: …` (before any phase) |

`plan` and `status` need no privileges and mutate nothing; `apply`, `verify` and `recover` need root.

### 11.5 A defect this finding exposed in the previous candidate

`git rev-parse <absent-sha>^{tree}` **prints its argument back on stdout and exits 0**, so the
previous gate `CAND_TREE=$(git -C "$INSTALL" rev-parse "$CANDIDATE^{tree}")` reported a candidate as
"present" when it was not. The candidate-availability gate — the exact thing EXT-03 asks for — was
vacuous, and the previously reported `CANDIDATE_PRESENT` for the live installation was an artefact
of it.

Corrected in all three call sites (`release002_release.sh`, `hermes-release-002`,
`release002_control.py::_git`) by requiring a full 40-character SHA and using
`rev-parse --verify --quiet`. The honest current state is that **the candidate is not in the live
installation**, and the engine now says so:

```
$ bash harness/release002_release.sh --plan --candidate 1cc36452… --archive …
rc=64
candidate 1cc36452f47cef911afde6da5139a56e9de2da7e is not present in
/home/hermes/.hermes/hermes-agent (stage it first: hermes-release-002 stage)
```

That is the correct, fail-closed answer, and it is why section 15 step 1 is `stage`.

### 11.6 Conflicting instructions removed

Sections 6 and 15 of this package name **only** the launcher path. The engine's own `--apply`
refuses without `HERMES_RELEASE_LAUNCH_TOKEN`, which only the launcher supplies, so running the
engine directly is not a second launch path — it is a refusal. Process-boundary tests assert that
refusal rather than trusting the comment (`tests/harness/test_release002_launcher.py`).

---

## 12. EXT-RESTORE-002-04 — cutover admission control

### 12.1 What was already covered, and the seam that was not

The existing external drain marker already covers messaging ingress and cron ticks: while
`.drain_request.json` is live, inbound Telegram is refused, cron dispatch is held, and
`gateway_state` publishes `"draining"`. The uncovered seam was **turn admission from non-messaging
producers** — the API server, A2A, local automation and worker reservations all reach
`gateway/session_ingress.py::execute_admission` without touching the messaging gate. A request
arriving through one of those paths could be admitted after the quiescence samples and before the
merge.

### 12.2 The freeze

Rather than invent a second pause mechanism, the cutover reuses the drain marker and closes the
admission seam:

- `gateway/drain_control.py` — `freeze_principal()` / `admission_frozen()` / `freeze_turn_result()`,
  namespaced `release-freeze:<run-id>`, so a release freeze is distinguishable from an operator
  drain and can be journalled and recovered deterministically.
- `admission_frozen()` asks `drain_requested()`, not "does the marker file exist", so the same age
  rule (`DRAIN_REQUEST_MAX_AGE_SECONDS`) and the same live-machine-epoch test decide everywhere: an
  orphan marker left by a killed supervisor expires instead of freezing admission forever.
- `gateway/session_ingress.py` — the freeze check is the **first statement** of
  `execute_admission`, ahead of `busy_turn_started()` and `note_if_unviewed()`. A refused admission
  therefore neither registers as active work nor consumes an admission row, and it returns a
  **settled failed turn** (`freeze_turn_result`) rather than a queue position — no ownerless turn
  can start after deployment.

### 12.3 Freeze before quiescence, and revalidation before mutation

The engine's order is fixed: `freeze_engage` (12.2, drain marker + ESTOP sentinel, then wait for
`gateway_state == draining`) → three consecutive quiescence samples 20 s apart of
`active_agents == 0` **and** live admissions for the current runtime epoch `== 0` → revalidate
install/candidate/unit/drop-in identities **and** re-assert the freeze and quiescence immediately
before the first mutation → only then `git merge --ff-only`.

`release002_control.py::admission_report` answers the question the operator must not be able to
answer optimistically: it splits admissions into **live** and **stale** (a `queued` row abandoned by
a crash is not live work — `STALE_ADMISSION_HOURS = 6 h`, keyed off the target session's
`last_activity_at`), reports the freeze state and the ESTOP sentinel, and exposes `evaluable`. The
CLI exit codes are `0` frozen-and-idle, `2` proven NO_GO, `3` **could not be evaluated** — "probably
idle" is never a verdict.

### 12.4 Restoring admission

`unfreeze` (success) and every failure path (`rollback`, interrupted supervisor, refused
configuration) release the freeze through one function: clear the marker only if it is ours, remove
the ESTOP only if we set it, journal the reason, then confirm `gateway_state` is no longer
`draining` and the sentinel is gone. `decide_recovery()` does the same from the journal when the
supervisor died and left the freeze behind.

### 12.5 Evidence that a request cannot race the deployment

- **Live, real gates** — `plan` against a clone of the live installation that does contain the
  candidate, while Hermes was serving traffic. 22 of 25 gates passed (`install_head`,
  `install_tree`, `install_clean`, `service_active`, `service_pid`, `no_restart_loop`,
  `kill_mode_mixed`, `effective_timeout_is_300s`, both drop-ins, `hold_env_active`, runtime state and
  `code_sha`, `session_inventory`, `no_sigkill_today`, `journal_readable`, memory, disk, DB); the
  three quiescence gates failed for the right reason — `active_agents=3`, `live_admissions=5`,
  `freeze_not_engaged`, `estop_not_engaged` — and the engine answered `DRY RUN RESULT: NO_GO`,
  exit 2, **after** printing the full runbook. Nothing was changed.
- **Admission report** — the same run printed
  `{"ok": false, "evaluable": true, "reasons": ["active_agents=3","live_admissions=5",
  "freeze_not_engaged","estop_not_engaged"]}`: it refuses on live work *and* on the absence of a
  freeze, rather than accepting quiescence as a property of the moment.
- **Gate ordering** — `tests/gateway/test_admission_freeze_gate.py` asserts by AST that the freeze
  check is the first statement of `execute_admission`, so no later refactor can move it behind a
  side effect without failing the suite.
- **Behaviour** — a frozen admission returns a settled failed turn and makes **no** session lookup,
  **no** busy-tag, **no** viewer note and **no** turn; an unfrozen admission still proceeds, so the
  gate cannot pass vacuously.
- **Unreadable state** — a marker that exists but cannot be parsed is reported as
  `present=true, active=false, why="unreadable"`, and an unreadable gateway state forces exit 3.
  Neither can be read as "no freeze, carry on".

---

## 13. EXT-RESTORE-002-05 — supervisor lifecycle

### 13.1 The supervisor outlives the client

`apply` does not run the engine in the caller's shell. It starts a **transient oneshot systemd
unit** (`systemd-run --unit=hermes-release-<run-id> --collect`) with:

| property | value | why |
|---|---|---|
| `RuntimeMaxSec` | **1500 s** | computed, not guessed (13.2) |
| `TimeoutStopSec` | 420 s | one restart (300 s) plus settle |
| `KillMode` | `control-group` | no orphaned children if the unit is stopped |
| `SendSIGKILL` | `yes` | a stuck supervisor cannot hold the service hostage |
| `StandardOutput/Error` | `append:/var/log/hermes-release-002-<run-id>.log` | durable, outside the install |

Interrupting the client (`Ctrl-C`, SSH drop, timeout) does **not** stop the release: the unit keeps
running and `--detach` is available for operators who want the prompt back immediately. A second
`apply` while a supervisor is alive is refused (`guard_no_other_release`), so no orphan can start a
competing release.

### 13.2 The envelope is computed from the work it must cover

`release002_control.py::required_envelope_seconds()` sums quiesce + archive + deploy + restart +
verify + one rollback and adds a safety margin:

```
$ release002_control.py envelope
{"cover": ["quiesce","archive","deploy","restart","verify","rollback"],
 "required_sec": 1290, "safety_sec": 120, "runtime_max_sec": 1500,
 "start_timeout": 90, "stop_timeout": 300}
```

1500 s is the shortest limit that still covers a full rollback after a full quiescence wait — a
limit sized only for the happy path would SIGKILL the supervisor exactly when it is needed most.
The engine also refuses a `QUIESCE_WAIT` larger than its `MAX_QUIESCE=180 s` share, with
`freeze_release` and exit 65, so the envelope cannot be overrun by configuration.

### 13.3 Deterministic handling when the supervisor dies

Every phase writes the journal (`/var/lib/hermes-release/restore-002/journal.json`) before and after
the action it covers. `decide_recovery()` reads the journal and returns exactly one action:

| journal phase | facts | action |
|---|---|---|
| before any mutation | any | `unfreeze` (release the freeze, nothing to undo) |
| `deploy` / `mutated` | install still at the old head | `unfreeze` |
| `deploy` / `mutated` | install at the candidate, service inactive | `restart_then_verify` |
| `rollback` / `mutated` | install at the deployed head, service active | `unfreeze` |
| `rollback` / `mutated` | anything else | `needs_human` — **keep the freeze** |
| unreadable / unknown phase | any | `needs_human` |

`recover` executes that action; it never retries the release. There is no loop, no backoff, no
"try again": an interrupted release ends either verified, rolled back, or parked for a human with
the freeze held. Partial unit configuration is impossible by construction — the unit is regenerated
in one `gateway install --system` call from the archived copy, and every failure path after the
first mutation restores the archive and `daemon-reload`s before it restarts.

---

## 14. Test evidence added by this correction

| file | lines | what it proves |
|---|---|---|
| `tests/harness/test_release002_control.py` | 476 | decision-level adversarial controls: manifest drift/absence/dirty worktree/wrong commit refuse; freeze engaged + nothing executing passes, unreadable state fails closed; envelope covers quiesce+archive+deploy+restart+verify+one rollback; `decide_recovery` resolves every journal state deterministically |
| `tests/harness/test_release002_launcher.py` | 306 | process-boundary controls: both scripts parse; every launcher refusal and every engine refusal returns the documented code and message; `plan` with a GO preflight returns 0, prints the runbook and leaves install, worktree, journal, freeze markers and ESTOP untouched; `plan` with a NO_GO preflight returns 2 and still prints the runbook |
| `tests/gateway/test_admission_freeze_gate.py` | 122 | a frozen admission is answered with a settled failed turn and claims nothing; the gate is the first statement of `execute_admission` (AST); an unfrozen admission still proceeds |

Combined regression (nine suites — stop-budget fail-closed, adaptation, session resume plan, cron
drain floor, launchd cap, shutdown forensics, gateway service, `tests/harness/`, admission freeze):

```
1 failed, 327 passed, 4 skipped, 2 errors in 15.65s
```

The failure and both errors are the same pre-existing ones recorded at `5d02e1da` and at
`e8ed5b9b` (section 10): a `home_io_guard` `PermissionError` in `test_gateway_service.py`, and two
`shutdown_forensics` cases blocked by `os.kill(<pid>, 9) — PID is outside the test process subtree`.
No test that passed at `e8ed5b9b` fails now; 83 tests were added.

---

## 15. The final release command

Exactly one documented path. Nothing below has been run against production; steps 1–3 are the
owner's authorisation, and step 3 is the only mutating step.

```bash
SHA=1cc36452f47cef911afde6da5139a56e9de2da7e
LAUNCH=/usr/local/lib/hermes-release/hermes-release-002

# 0. read-only: what is pinned, what is live, is a freeze still held somewhere?
$LAUNCH status

# 1. install the launcher outside the installation and pin the engine + candidate (root, no service change)
sudo $LAUNCH stage --candidate $SHA

# 2. dry run: manifest + full preflight, prints the ordered plan, mutates nothing
$LAUNCH plan --candidate $SHA          # exit 0 = would proceed, 2 = would refuse, 3 = could not evaluate

# 3. one controlled stop/start under the transient supervisor (root, the only mutating step)
sudo HERMES_RELEASE_APPROVAL=$SHA $LAUNCH apply --candidate $SHA

# 4. post-release verification: identity, unit, both drop-ins, hold, sessions, no SIGKILL
$LAUNCH verify --candidate $SHA

# only if the supervisor was interrupted:
sudo $LAUNCH recover --candidate $SHA
```

What `apply` does, in order: archive the unit and both drop-ins with a SHA-256 manifest → engage the
admission freeze (drain marker + ESTOP, wait for `gateway_state == draining`) → quiesce (three
consecutive 20 s samples of `active_agents == 0` **and** zero live admissions) → revalidate
identities and re-assert the freeze → `git merge --ff-only <candidate>` → regenerate the unit
(`TimeoutStopSec=300 s`) → assert the on-disk unit and the effective `systemctl show` value → **one**
timed `systemctl restart hermes-gateway.service` → scan the journal window for SIGKILL → post-verify → unfreeze → write
`RECEIPT.md` (`RELEASED_VERIFIED` or `ROLLED_BACK`).

`status`, `plan` and `verify` are non-mutating and safe to run at any time, including while Hermes is
serving Telegram and cron traffic.

---

## 16. Rollback conditions

Rollback is automatic on any failure after the first mutation, and it is a **single** restart — not a
retry loop.

| trigger | evidence available | action |
|---|---|---|
| `git merge --ff-only` refused | `$ARCHIVE/merge.txt` | restore archive, `daemon-reload`, reset `--hard e05128fe`, one restart, unfreeze, `ROLLED_BACK` |
| regenerated unit's `TimeoutStopSec` ≠ 300 s, or effective ≠ `5min` | `$ARCHIVE/unit/` + `systemctl show` | same as above — never restart into an unverified envelope |
| the one timed stop is SIGKILLed | journal `Killed process` / `sigkill=1` | restore + reset + one restart + unfreeze, `ROLLED_BACK`; **the correction is not deployed** |
| restart does not come back `active` | `systemctl is-active`, `NRestarts` | restore + reset + one restart + unfreeze, `ROLLED_BACK` |
| post-verify fails (identity, drop-ins, hold, session fingerprint, new SIGKILL) | `$ARCHIVE/post-preflight.json` | restore + reset + one restart + unfreeze, `ROLLED_BACK` |
| supervisor killed by `RuntimeMaxSec` mid-phase | journal phase | `recover` → `decide_recovery()` (13.3); freeze stays held when the state is ambiguous |
| any gate unevaluable before the first mutation | preflight report | stop, nothing mutated, exit 3 |

Standing conditions that do **not** change with this release: `90-recovery-hold.conf` and
`HERMES_RECOVERY_HOLD=1` stay; `95-timeout-stop.conf` stays until 6.6 is separately approved and
6.2 has produced its timing evidence; the eight empty session rows stay untouched; no hold release
is requested (it would resume zero sessions); `state.db` is never written by this procedure.

---

## 18. EXT-06..09 — the final release-path findings, closed

The External CA accepted Fault B and EXT-03/04/05 in principle, then raised four release-path
defects. Each is now a *separate* mechanism with its own negative controls, not a relaxed gate.

### 18.1 EXT-06 — phase-aware verification (two verifiers, not one relaxed one)

`release002_preflight.py` gained `--phase {pre,post,released,any}`. The release now runs **two**
different checks:

- `--phase pre` (before any mutation): pre-release identity, `gateway_state=running`, no
  in-flight turn, no pre-existing freeze.
- `--phase post` / `--phase released` (after the restart): **the frozen cutover state** —
  `gateway_state=draining` with *this run's* `release-freeze:` marker engaged — plus deploy
  artefacts and service state. This is a *stronger* assertion than `running`, not a weaker one.
- A post phase without `--candidate` is a JSON `NO_GO` (`post_phase_requires_candidate`, exit 2),
  so the engine can never mistake empty output for a pass. A sessions store that cannot be read is
  reported as an *unevaluable* gate, never a traceback.

Negative controls (each proves the verifier can still fail): a frozen cutover **verifies** (no
`runtime_state_frozen`/`admission_freeze_held` in `failed`), while a `running` gateway fails
`runtime_state_frozen`, a missing marker fails `admission_freeze_held`, and a foreign marker
(`principal` not under `release-freeze:`) fails it too.

### 18.2 EXT-07 — pre-release and post-release identities pinned separately

`build_manifest` now writes **two** identity sets: `pre_release` (install HEAD/tree, unit and
drop-in checksums at stage time) and `post_release` (candidate HEAD/tree, the regenerated unit's
`TimeoutStopSec`, and the journaled post-unit checksum). `verify_manifest` selects by phase and
reports every gate in order.

Negative controls: pre verification passes on the pre-release installation and fails
(`install_head_post`) after a real swap; post verification passes only after the swap *and* the
regenerated unit at `TimeoutStopSec=300`, failing `unit_timeout_post` at any other value and
`unit_sha256_post` when the journaled checksum is wrong.

### 18.3 EXT-08 — a pre-mutation NO_GO is inert

`decide_failure(phase, mutated=False)` maps every pre-mutation phase (`start`, `stage`, `preflight`,
`archive`, `freeze`, `quiesce`, `revalidate`, `aborted`) to `release_freeze` — no restart, no
rollback, releasing **only** the freeze this run created. `MAX_RESTART_ATTEMPTS = 1`. The engine's
revalidate block routes every drift (HEAD, tree, dirty worktree, unit change, manifest identity,
admission not frozen) to `abort_before_mutation()` (zero deployment changes, zero restarts,
exit 2) instead of `rollback()`.

Negative control, run against a fixture with a `systemctl` shim: `--plan` with drift exits 2 with
`DRY RUN RESULT: NO_GO`, the shim logs **only `show`** (no stop/start/restart), the install HEAD is
unchanged, no freeze marker or journal is written.

### 18.4 EXT-09 — a verified bootstrap, and old-runtime admission protection

`hermes-release-002 bootstrap` installs *this* launcher under `$LIB` byte-verified against its
committed copy, idempotently, and writes a bootstrap manifest pinning the launcher's `sha256`,
path, source commit and `state=boastrap` — refusing an uncommitted/edited launcher (exit 65), a
second different launcher without `--force` (exit 65, previous launcher retained as `*-prev`), and
an invocation already running from the install path.

`release002_oldgate.py` proves the **deployed `e05128fe` runtime** — not the unshipped
`execute_admission` guard — refuses new admissions through every producer during the cutover, via
the drain marker and the ESTOP sentinel, and ships in `ENGINE_FILES` so the proof is re-run at
release time. The static half reports any producer whose gate cannot be located as `uncovered`, and
any unreadable source as `unevaluable` — never assumed covered.

Negative controls: the deployed tree reports `covered`; removing the drain gate from a producer
yields `messaging_inbound/drain_marker` in `uncovered`; renaming a gate definition yields
`uncovered` with a "not defined in the deployed tree" detail; and bootstrap refuses an edited
launcher.

### 18.5 Evidence

`tests/harness/test_release002_release_paths.py` adds 39 negative controls; `tests/harness`,
`tests/gateway/test_admission_freeze_gate.py` and `tests/gateway/test_stop_budget_adaptation.py`
pass **145** tests. Fault B tests are not re-run: their qualification is unchanged and already
accepted.

---

## 19. EXT-RESTORE-002-10 — the released verification phase, corrected

The External CA accepted EXT-06/07/08/09 at code level and named one remaining defect: the
independent post-release verification could not pass. `cmd_verify` ran `ctrl manifest --verify`
with **no phase** (defaulting to `pre`, which demands the old installed HEAD/unit), while the
engine's `--verify-only` path drove `released` — a phase `verify_manifest` did not accept.

### 19.1 One phase contract across launcher, engine, manifest and preflight

- `verify_manifest` accepts `released`, mapping it to the `post_release` identity set (candidate
  HEAD/tree, regenerated unit, both retained drop-ins) with `released`-labelled gates
  (`install_head_released`, `unit_timeout_released`, `unit_sha256_released`,
  `dropin_released:90-recovery-hold.conf`, `dropin_released:95-timeout-stop.conf`).
- The `ctrl manifest --verify --phase` choice list adds `released`; the default stays `pre`
  (conservative — an omitted phase can never quietly become a released check).
- `cmd_verify` now drives `--phase released`; `cmd_plan` and `cmd_apply` pin `--phase pre`
  explicitly, so no call relies on the default.
- The engine `--verify-only` path extracts the journaled regenerated-unit `sha256` and passes it to
  `manifest_verify released`, so `released` verifies the regenerated unit's exact bytes, not only
  its `TimeoutStopSec`.
- `recover` uses `recover-plan`/`decide_recovery` (journal-driven, no phase), and `status`/`stage`
  verify nothing — no other mismatched default remains.

The six released assertions are therefore covered end to end: candidate HEAD/tree (manifest),
regenerated unit identity (manifest timeout + journaled sha), both retained drop-ins (manifest),
operational gateway state and cleared admission freeze (preflight `released`:
`runtime_state_running` + `no_preexisting_freeze`), and intact recovery hold (engine environment
gate).

### 19.2 E2E negative/positive tests (real repo, launcher, unit, drop-ins, manifest)

`tests/harness/test_release002_ext10.py` (11 cases) drives the real `manifest --verify` CLI against
a real temporary Git repository, a real launcher copy, a real unit file and both drop-ins:

- before deployment, `pre` passes and `released` refuses (`install_head_released`);
- after a simulated deployment (install at the candidate, unit regenerated to `TimeoutStopSec=300`),
  `released` passes with every identity gate evaluated;
- wrong HEAD, a changed unit (`TimeoutStopSec=60`), wrong unit bytes (bad journaled sha), a missing
  hold drop-in, and a remaining freeze each make `released` refuse — including a run of the real
  `release002_preflight.py --phase released` against a home that still holds the drain marker.

No production mutation occurs during qualification: the tests clone a throwaway repository and
never touch the installed tree, systemd, the gateway, the drop-ins or the recovery hold.

### 19.3 Evidence

`tests/harness` + `tests/gateway/test_admission_freeze_gate.py` +
`tests/gateway/test_stop_budget_adaptation.py` pass **156** tests (145 prior + 11 new EXT-10).
Unchanged shutdown tests are not re-run.

---

## 20. EXT-RESTORE-002-11 — the journaled unit checksum, made mandatory

### 20.1 The defect

`harness/release002_release.sh --verify-only` recovered the regenerated systemd unit's SHA-256 by
scanning the release journal's free text for a `sha256=` token. Every failure mode of that scan
produced the **same empty string**: no journal, unparseable journal, no `unit` phase, no note on the
receipt, a truncated or upper-case digest. The empty string was then passed to `manifest_verify`,
where `verify_manifest()` compared it against the unit's actual bytes only *if* it was non-empty.

The result was a silent downgrade. With no checksum the exact-byte gate disappeared, and the only
remaining unit proof was `unit_timeout_<phase>` — `TimeoutStopSec` at the computed value. Any number
of different unit files satisfy that: a stray `Restart=no`, a changed `ExecStart`, an edited
`KillMode`. Independent post-release verification could therefore report PASS while the deployed unit
was not the unit the release generated.

### 20.2 The correction (three layers, each fail-closed)

**Layer 1 — the only way to obtain the checksum.** `release002_control.verify_unit_receipt()` accepts
a journal, a candidate and (optionally) a run id and unit path, and returns the recorded digest only
when *all* of the following hold; otherwise it returns `ok: false` with the failing gates named:

| gate | requires |
|---|---|
| `journal` | the journal file exists and parses as an object |
| `release` | it names `RESTORE-002` |
| `journal_candidate` | its recorded candidate equals the one under verification |
| `journal_run_id` | it carries an attempt id at all |
| `run_id` | when the caller names the attempt, the ids are equal |
| `deploy_receipt` | a `deploy` receipt names `head == candidate` — the attempt really deployed it |
| `unit_receipt` | a `unit` receipt **after** that deploy receipt, carrying the same candidate |
| `unit_checksum` | that receipt's `sha256` is exactly 64 lowercase hex characters, nothing else |
| `unit_path` | when the caller names the unit path, the receipt records the same path |

The checksum is a structured field, not a note: `phase_record()` now pins `candidate`, `head`,
`sha256`, `unit_path` and the attempt's `run_id` into the journal, and the engine writes the unit
receipt with `--sha256`/`--unit-path` at generation time. Nothing scrapes text any more.

**Layer 2 — the manifest check cannot run without it.** In `verify_manifest()`, `post` and `released`
compute `supplied` and `observed_sha = sha256_file(unit_path)` and gate on
`SHA256_RE.match(supplied) and observed_sha == supplied`. An absent, empty, short, upper-case or
prefixed digest fails `unit_sha256_<phase>` — it is a failed gate, never a skipped one. A missing unit
file hashes to `None` and fails the same gate. `pre` keeps its original archived-bytes contract and
`any` its structural-only contract; neither was weakened.

**Layer 3 — no path reaches layer 2 without layer 1.** The engine's `--verify-only` calls
`unit-receipt --print sha256` and exits 2 on any refusal; `manifest_verify()` itself refuses a
`post`/`released` call whose checksum argument is empty; the launcher's `verify` recovers the checksum
the same way before its released manifest check and passes it as `--expected-post-unit-sha256`, so
`hermes-release-002 verify` has no order in which it can verify bytes it has not been given. All
three sites name their phase explicitly (the EXT-10 lesson), and a structural test now fails the
suite if any `manifest --verify` invocation appears without `--phase`.

`verify --candidate SHA --run-id ID` binds the receipt to one attempt: the applied run id is printed
by `apply` (`journalctl -u hermes-release-$run_id`), and the journal records it. Wrong-run, stale,
wrong-candidate and pre-deploy receipts are all refused.

### 20.3 Qualification — 42 cases through the real CLI

Every case runs the real `release002_control.py` CLI and, for the composed path, the real
`release002_release.sh --verify-only`, against a throwaway git repository, installation, systemd unit,
drop-ins, manifest and journal under `tmp_path`. No case touches the installed tree, systemd, the
gateway, the drop-ins or the recovery hold.

| control | expected | where |
|---|---|---|
| valid journal + matching unit bytes | **PASS**, `unit_sha256_released` and `unit_timeout_released` both green | `test_released_passes_with_the_journaled_checksum`, `test_verify_only_passes_on_a_complete_release` |
| missing journal | NO_GO, `journal` gate; `--verify-only` exits 2 | `test_missing_journal_refuses`, `test_verify_only_refuses_a_missing_journal` |
| missing `unit`-phase receipt | NO_GO, `unit_receipt` | `test_missing_unit_receipt_refuses`, `test_verify_only_refuses_a_journal_without_a_unit_receipt` |
| unit receipt predating the deploy | NO_GO, `unit_receipt` | `test_unit_receipt_that_predates_the_deploy_is_not_accepted` |
| empty / short / long / non-hex / prefixed / absent checksum | NO_GO, `unit_checksum` (7 shapes) | `test_empty_or_malformed_checksum_refuses` |
| **correct timeout, different unit bytes** | NO_GO on `unit_sha256_released` while `unit_timeout_released` stays green — the exact silent-skip shape | `test_released_refuses_a_matching_timeout_with_different_bytes` |
| bytes changed after a valid receipt | NO_GO, `unit_sha256_released` | `test_verify_only_still_checks_identity_and_bytes` |
| wrong candidate | NO_GO, `journal_candidate` / `unit_receipt` | `test_wrong_candidate_refuses`, `test_receipt_from_another_candidate_refuses` |
| stale journal (deploy head ≠ candidate) | NO_GO, `deploy_receipt` | `test_stale_journal_without_a_matching_deploy_refuses` |
| wrong run id (function and CLI and end-to-end) | NO_GO, `run_id`; `--verify-only` exits 2 | `test_wrong_run_id_refuses`, `test_cli_refuses_a_wrong_run_id`, `test_verify_only_rejects_a_wrong_attempt_id` |
| journal with no attempt id at all | NO_GO, `journal_run_id` | `test_journal_without_a_run_id_refuses` |
| receipt for a different unit path | NO_GO, `unit_path` | `test_wrong_unit_path_refuses` |
| corrupt journal file | NO_GO, nothing printed on stdout | `test_cli_refuses_a_corrupt_journal` |
| unknown `--print` field | argparse refuses — a typo cannot degrade into "printed nothing" | `test_cli_rejects_an_unknown_print_field` |
| manifest deleted before verification | NO_GO, `no release manifest` (previously the whole check was skipped) | `test_verify_only_refuses_without_a_manifest` |
| recovery hold absent | NO_GO, `hold env entries: 0` | `test_verify_only_refuses_when_the_hold_is_absent` |
| identity gates still enforced with a good receipt | wrong HEAD refuses; both retained drop-ins verified | `test_released_*`, `verify_manifest` gates `install_head_released`, `install_tree_released`, `dropin_released:*` |
| no bypass path reintroduced | source-level guards: no `sha256=` scraping, no phase-less `manifest --verify`, launcher `verify` orders `unit-receipt` before the manifest check | `TestNoBypassPath` (4 cases) |
| qualification unit override | refused (rc 64) unless `HERMES_RELEASE_QUALIFY=1`; production always uses `/etc/systemd/system/hermes-gateway.service` | `test_qualify_unit_hook_requires_the_flag` |

### 20.4 Evidence

`tests/harness` + `tests/gateway/test_admission_freeze_gate.py` +
`tests/gateway/test_stop_budget_adaptation.py`: **199 passed** (156 at EXT-10 close + 42 new in
`tests/harness/test_release002_ext11.py` + 1 updated `test_post_phase_passes_after_a_real_swap` and
its new negative control). Unchanged shutdown suites were not re-run.

Candidate `7596026ab684d4b52935388bfc17bcaa948f2387`, tree `7e6042d786509439badf8eba554bd9890f039ab0`.
No production mutation of any kind: no deployment, no staging, no restart, no systemd or drop-in edit,
no merge, `HERMES_RECOVERY_HOLD=1` untouched, no session-database write.

---

## 21. EXT-RESTORE-002-12 — the release manifest's engine inventory, made single-source

### 21.1 The defect (exposed by the first owner-approved attempt)

The 2026-11-04 production attempt reached `bootstrap` (rc 0) and `stage` (rc 0) and then stopped at
PLAN with **rc 65**:

```
REFUSED: staged harness/restore002_session_inventory.py does not match the pinned candidate blob
```

Nothing was mutated and the gateway was not restarted — the refusal was correct. The staged bytes
were also correct: the file's candidate blob sha equalled the staged sha. What disagreed was the
release package's own account of itself. The two lists were not the same list, and they were not
even different lengths:

| at `65a52a3e` | entries |
|---|---|
| launcher `ENGINE_FILES` (what `stage` copied and what `prepare_engine` walked) | `release002_release.sh`, `release002_control.py`, `release002_preflight.py`, `release002_oldgate.py`, **`restore002_session_inventory.py`** |
| control `engine_paths` literal inside `manifest --write` (what got pinned) | `release002_release.sh`, `release002_control.py`, `release002_preflight.py`, `release002_oldgate.py`, **`hermes-release-002`** |

Both lists held five paths, so a count check would have passed. The manifest pinned the candidate
*launcher blob* where the launcher staged the *session inventory*, and `prepare_engine()` — which
demanded a pin for every file it had staged — therefore found no pin for
`harness/restore002_session_inventory.py` and refused. This was a **release-package defect, not an
operational NO_GO**: no runtime condition was wrong.

Blast radius: `prepare_engine()` precedes every mutating path, so `plan`, `apply` and `verify` were
unreachable for any candidate staged since the inventory entered the launcher. CI never exercised
the real launcher end to end, which is why the first real PLAN found it.

The defect could not be closed in flight. A one-line change to the pin list produces a new candidate
tree, which requires a new CA review and a new exact-identity owner approval; hand-editing the
generated manifest would have disabled the very gate that had just refused correctly.

### 21.2 The correction — one inventory, three separately named drifts

Per the authority this is not "add the missing entry". The list now exists once, and every consumer
reads it:

* **One declaration.** `harness/release002_control.py` declares `ENGINE_FILES` (the five files copied
  into the engine directory and executed from it) and `PINNED_ENGINE_FILES` (those five plus the
  candidate launcher blob `harness/hermes-release-002`). `engine_inventory(pinned=...)` is the sole
  accessor; `build_manifest()` defaults to `PINNED_ENGINE_FILES`, so the manifest writer can no longer
  pin a different set from the one that is staged. The launcher's private `ENGINE_FILES` array is
  deleted.
* **Staging derives from the candidate, not from the launcher.** `stage` extracts the candidate's own
  `harness/release002_control.py` (`git cat-file blob`), asks *that* file for its inventory
  (`engine-files --set staged`), and copies exactly those blobs. Paths outside `harness/` and any
  `..` component are refused, and an inventory of fewer than five entries is refused as implausible.
  A launcher can therefore no longer stage a set that the candidate's manifest does not pin.
* **The staged control must prove itself before it is believed.** `engine_audit()` first checks the
  staged `harness/release002_control.py` against its own pinned blob; a control whose bytes drifted
  cannot declare an inventory that legitimises its own drift.
* **Three drifts, three names.** `audit_engine()` compares staged vs pinned vs inventory and fails
  separately on `no_staged_file_missing_from_manifest`, `no_unexpected_manifest_engine_entry`,
  `every_inventory_file_staged`, `no_unexpected_staged_file` and `staged_bytes_match_pins`
  (`__pycache__`/`*.pyc` ignored). `prepare_engine()` runs it before every `plan`/`apply`/`verify` and
  logs the result, so the success is externally observable rather than inferred.
* **Publish only what passed.** `stage` writes the manifest beside the real one and `mv`s it into
  place only after the audit passes, so a manifest describing an unverified engine directory never
  becomes the staged manifest.
* **The launcher's integrity requirement stays separate.** `manifest["launcher"]["sha256"]` describes
  the launcher file actually installed and executed; the candidate launcher *blob* pin in
  `PINNED_ENGINE_FILES` is provenance, not execution integrity.
* **Test-only privilege relaxation is fenced.** `require_stage_root()` permits unprivileged staging
  only with `HERMES_RELEASE_STAGE_UNPRIVILEGED=1` **and** both a non-default `$HERMES_RELEASE_LIB` and
  a non-default `$HERMES_RELEASE_JOURNAL`; live staging into `/usr/local/lib/hermes-release` stays
  root-only.

### 21.3 Negative controls and structural regressions (`tests/harness/test_release002_ext12.py`, 18 cases)

| condition | expected | test |
|---|---|---|
| inventory is complete and includes the session inventory | `set(ENGINE_FILES) == set(PINNED_ENGINE_FILES) - {launcher}`, 5 entries | `test_the_inventory_names_every_staged_file_including_the_inventory_script` |
| launcher keeps no copy of the list | `ENGINE_FILES` absent from the launcher source | `test_the_launcher_no_longer_carries_its_own_copy_of_the_list` |
| CLI and module agree | `engine-files --set staged/pinned` equals the tuples | `test_the_cli_reports_the_same_inventory_the_module_declares` |
| no production caller may pin its own list | no `engine_paths=` call site; manifest writer defaults to `PINNED_ENGINE_FILES` | `test_no_production_caller_can_supply_its_own_engine_list` |
| manifest engine map == inventory | six pins, launcher blob included | `test_the_manifest_pins_every_staged_file_and_the_launcher_blob` |
| **missing pin** (the live defect) | NO_GO `no_staged_file_missing_from_manifest`, rc 2 | `test_a_missing_pin_is_refused` |
| **additional staged file** | NO_GO `no_unexpected_staged_file` | `test_an_extra_staged_file_is_refused` |
| **changed file bytes** | NO_GO `staged_bytes_match_pins` | `test_changed_staged_bytes_are_refused` |
| **stale candidate** (staged for another commit) | NO_GO at `prepare_engine` | `test_plan_refuses_a_stale_candidate` |
| **wrong candidate tree** | NO_GO `candidate_tree drifted` | `test_plan_refuses_a_wrong_candidate_tree` |
| **corrupted manifest** | NO_GO `manifest_unreadable`, nothing trusted | `test_a_corrupted_manifest_is_refused` |
| unexpected manifest entry | NO_GO `no_unexpected_manifest_engine_entry` | `test_an_unexpected_manifest_entry_is_refused` |
| tampered control claiming its own inventory | refused before the inventory is read | `test_a_tampered_control_is_not_trusted_for_its_own_inventory` |
| `__pycache__` / `*.pyc` present | ignored, not drift | `test_pycache_is_not_mistaken_for_a_release_file` |
| inventory entry outside `harness/` | refused, nothing written outside the engine dir | `test_an_inventory_entry_outside_harness_is_refused` |
| candidate the installation cannot see | NO_GO, not a package defect | `test_plan_refuses_a_candidate_the_installation_cannot_see` |
| clean stage | audit green, 9/9 gates | `test_a_correct_staging_audits_clean` |
| **real integration** | bootstrap → stage → manifest verify → PLAN passes `prepare_engine()` and stops only on a genuine operational NO_GO | `test_bootstrap_stage_and_plan_get_past_prepare_engine` |

The integration test is not a mock: it clones the harness into a disposable Git repo, commits a
candidate, bootstraps a real launcher and manifest into a throwaway library, stages with the real
`stage` command, verifies the real manifest, and runs the real `plan`. It asserts the run gets past
`prepare_engine()` (`engine inventory verified`, `manifest verified`, no `REFUSED`) and exits 2 on
`NO_GO: install identity` — an operational verdict, which is the distinction EXT-12 was raised to
draw.

### 21.4 Evidence — the real thing, in isolation

Run against the committed candidate, in `/home/hermes/.cache/hermes-release-002-tests/manual-a5aedab5`
(isolated repo, install, library, journal and `HERMES_HOME`):

```
bootstrap rc=0
stage     rc=0    engine staged: .../engine/d4a5ef0a038b (5 files, inventory from the candidate)
                  engine inventory verified: staged == pinned == candidate inventory, byte-exact
engine-audit      9/9 gates ok, failed: []
manifest --verify --phase pre   27/27 gates ok, failed: []
plan      rc=2    engine inventory verified (staged == pinned == candidate inventory)
                  manifest verified (launcher, candidate, tree, engine blobs, unit, drop-ins, install identity)
                  NO_GO: install identity is 65a52a3e/ec029a6e, expected e05128fe/dc05abd0
```

The PLAN NO_GO is the correct verdict for that fixture: the disposable "installation" is not the
live one. Under the production identities the same run continues into the quiescence gates.

Complete staged-versus-pinned engine manifest for `d4a5ef0a` (16 hex prefixes; full values in
`manifest.json`):

| engine file | staged | pinned blob | candidate blob |
|---|---|---|---|
| `harness/release002_release.sh` | `3fbaf6bf401a50b5` | `3fbaf6bf401a50b5` | `3fbaf6bf401a50b5` |
| `harness/release002_control.py` | `09868c70b915bb66` | `09868c70b915bb66` | `09868c70b915bb66` |
| `harness/release002_preflight.py` | `f97bda141ac254f4` | `f97bda141ac254f4` | `f97bda141ac254f4` |
| `harness/release002_oldgate.py` | `f1cdd09aee26da33` | `f1cdd09aee26da33` | `f1cdd09aee26da33` |
| `harness/restore002_session_inventory.py` | `e66b8f8930636cd9` | `e66b8f8930636cd9` | `e66b8f8930636cd9` |
| `harness/hermes-release-002` (pinned, **not** staged — the installed launcher runs from the library root) | — | `4105d5584c345d2c` | `4105d5584c345d2c` |

Regression totals: **217 passed** — `tests/harness` plus
`tests/gateway/test_admission_freeze_gate.py` and `tests/gateway/test_stop_budget_adaptation.py`
(199 at EXT-11 close + 18 new). `bash -n` on both shell entry points and `py_compile` on the four
Python harness files: clean. Shutdown qualification was not re-run: no shutdown path changed.

Difference from the previously approved candidate `7596026a` / `7e6042d7`, docs excluded — five
files, all release-package or test, no gateway runtime code:

```
harness/hermes-release-002             |  90 +++++++--
harness/release002_control.py          | 135 ++++++++++++-
tests/harness/test_release002_ext10.py |   3 +-
tests/harness/test_release002_ext11.py |   3 +-
tests/harness/test_release002_ext12.py | 340 +++++++++++++++++++++++++++++++++
```

`test_release002_ext10.py`/`_ext11.py` change only their fixture file list (the launcher now stages
the candidate's six-file inventory, so those fixtures must contain the sixth file).

### 21.5 Proposed replacement release command

Unchanged in shape — the corrected package is staged and planned exactly like its predecessor, from
the candidate's own control:

```
sudo /home/hermes/work/restore002-fix/harness/hermes-release-002 bootstrap
sudo /home/hermes/work/restore002-fix/harness/hermes-release-002 stage --candidate d4a5ef0a038ba1f4a9796291a445d09713ea5a28
sudo /usr/local/lib/hermes-release/hermes-release-002 plan --candidate d4a5ef0a038ba1f4a9796291a445d09713ea5a28
```

`plan` must reach the quiescence gates and report `engine inventory verified (staged == pinned ==
candidate inventory)` and `manifest verified (…)` before it evaluates any operational condition. A
`REFUSED:` line is now a package defect; `NO_GO:` is an operational verdict.

### 21.6 Production is unchanged

No production action was taken and none is authorised by this correction. Re-verified after the
change: installation `/home/hermes/.hermes/hermes-agent` at `e05128fe` / tree `dc05abd0`, clean;
`hermes-gateway.service` active/running, PID `743641`, `NRestarts=0`, `TimeoutStopUSec=300s`; unit
sha256 prefix `8b94a1f0` (not regenerated); `90-recovery-hold.conf` and `95-timeout-stop.conf`
byte-identical to the pinned values; `HERMES_RECOVERY_HOLD=1`; journal
`/var/lib/hermes-release/restore-002/journal.json` with zero entries; zero `hermes-release-*` units.
The previously staged launcher (`0450610012…`), `manifest.json` and `engine/7596026ab684/` under
`/usr/local/lib/hermes-release` are **untouched** and remain the superseded candidate's artifacts
until a separately authorised restaging.

Biggie LLM Router: PR #17 merged 2026-11-04T07:54:48Z and complete;
`biggie-llm-router-release.service` PID `1145`, `NRestarts=0`, `/health` 200, `/v1/models` 200 —
not touched by this work.

---

## 17. Release decision requested

1. **Approve section 15 steps 1–3** — `stage` (installs the launcher outside the installation and
   pins the candidate), `plan` (dry run), then one `apply` for candidate
   `d4a5ef0a038ba1f4a9796291a445d09713ea5a28` / tree `7cc9304fb8d11ae6100d002b4b66ad556ab90a5a`,
   retaining both drop-ins and the recovery hold. The superseded candidate `7596026a` / `7e6042d7`
   must not be reused: its release authority was exhausted by the 2026-11-04 `STOPPED_SAFE` attempt
   and does not transfer.
2. **Defer 6.6** (retirement of `95-timeout-stop.conf`) until 6.2 has passed and its timing +
   SIGKILL evidence is published.
3. **No hold release** is requested. Section 7 shows it would resume zero sessions today; the eight
   empty rows stay untouched.
4. **No merge** is requested. PR #9 stays draft until the owner accepts the release evidence.

Any gate that cannot be evaluated stops the procedure before mutation. There is no automatic retry,
no orphaned supervisor, and no unrelated cleanup or updater work in this package.
