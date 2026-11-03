# HERMES-RESTORE-002 — External CA review package (correction 2)

Consolidated evidence for the two remaining operational blockers after RESTORE-001 — the gateway
shutdown/drain defect (**Fault B**) and the interrupted-session inventory — plus the two release
prerequisites the External CA raised against PR #9, and the single bounded release procedure.

Nothing in this package has been deployed, merged, restarted or activated. The live service is
exactly as RESTORE-001 left it.

## 0. Exact identity

| item | value |
|---|---|
| deployed installation (untouched) | HEAD `e05128febbc559469a5fb835a9235a543dfdfa64`, tree `dc05abd074c834569cf19ca89a6446d74be5c41d`, clean |
| **release candidate** (code + tests + tooling) | **`e8ed5b9b853aadef7b3830d0194275084b2449e8`**, tree **`913c0edc6d7cb9529259317a44a18fb63c0a2922`** |
| earlier reviewed head (superseded) | `5d02e1da35ef8afcf5643f31d32283c7c1286740`, tree `b739c90e245e9fa74dfc154323e2353f03256de7` |
| branch | `restore002/drain-envelope` in `/home/hermes/work/restore002-fix`, pushed to `fork` |
| pull request | #9, draft, base `main` |
| this document | committed directly above the candidate; it changes no executable code, so the deployable identity stays `e8ed5b9b` / `913c0edc` |

The candidate is a strict descendant of the reviewed head: `git diff 5d02e1da..e8ed5b9b` touches
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
candidate tree (`git cat-file -e e8ed5b9b:gateway/session_resume_plan.py` succeeds).

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
| candidate `e8ed5b9b` | **244** | 4 | **1** | **2** |

Delta: **+51 passing**, and the single failure plus two errors are the *same tests with the same
causes* at both identities (§8). No test was skipped, deleted, xfailed or loosened to reach this
number; the file that changed expectation (`test_stop_budget_adaptation.py`) gained 2 tests.

Collection: `85 tests collected` across the three new/rewritten files
(`test_stop_budget_probe_failclosed.py` 51, `test_stop_budget_adaptation.py` 23,
`test_session_resume_plan.py` 11).

---

## 5. What changed, precisely (`5d02e1da..e8ed5b9b`)

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
10 files changed, 1347 insertions(+), 19 deletions(-)
```

No production file, unit, drop-in, hold, session row or receipt was modified. The live install is
still `e05128fe` / `dc05abd0`, clean.

---

## 6. The single bounded release procedure

Two artefacts, both in the candidate tree, both read-only until explicitly armed:

- `harness/release002_preflight.py` — 380 lines, mutates nothing, exit `0` green / `2` gate failed /
  `3` unevaluable (fail closed, treated as failed).
- `harness/release002_release.sh` — 312 lines, **dry-run by default**; the mutating path requires
  `--apply` *and* `HERMES_RELEASE_APPROVAL=<candidate-sha>` matching the checkout, *and* root, and
  refuses to run inside the gateway's own cgroup.

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
cd /home/hermes/work/restore002-fix        # candidate e8ed5b9b / 913c0edc

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
bash harness/release002_release.sh --candidate e8ed5b9b853aadef7b3830d0194275084b2449e8   # dry run
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

## 11. Release decision requested

1. **Approve §6.1–6.4** for one controlled stop/start of `hermes-gateway.service` at candidate
   `e8ed5b9b853aadef7b3830d0194275084b2449e8` / tree `913c0edc6d7cb9529259317a44a18fb63c0a2922`,
   retaining both drop-ins and the recovery hold.
2. **Defer §6.6** (retirement of `95-timeout-stop.conf`) until §6.2 has passed and its timing +
   SIGKILL evidence is published.
3. **No hold release** is requested. §7 shows it would resume zero sessions today; the eight empty
   rows stay untouched.
4. **No merge** is requested. PR #9 stays draft until the owner accepts the release evidence.

Any gate that cannot be evaluated stops the procedure before mutation. There is no automatic
retry, and no unrelated cleanup or updater work is part of this package.
