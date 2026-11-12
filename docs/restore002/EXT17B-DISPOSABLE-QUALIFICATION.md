# EXT-17B — disposable isolated qualification of the RESTORE-002 release path

**Mission:** `RESTORE-002 / EXT-17B FINAL INTEGRATION QUALIFICATION`
**Authority:** isolated qualification, code correction where necessary, adversarial self-review,
commits, pushes and draft-PR evidence publication only.
**Explicitly not done:** production deployment, production staging, APPLY against production, live
gateway restart, trigger installation or activation, systemd modification on the host, session or
database write, recovery-hold removal, PR merge, Stage C activation.

**Disposition:** see §14. Nothing in this document is a release approval.

---

## 1. What had to be proven, and what was actually run

The eight outcomes the mission names were proven by driving the **assembled** path — the installed
launcher `hermes-release-002`, its staged engine, the transient supervisor unit, the engine itself,
the release journal and the post-release verification — inside a disposable installation and a
disposable systemd environment:

```
ext17b_build.py          builds one throwaway installation + fixtures
ext17b_run.sh            enters a private user+mount namespace, then runs a command in it
ext17b_ns.sh             mounts the private systemd unit dir, release lib, journal dir and log dir
ext17b_guard.py          refuses (exit 64) unless the environment really is the disposable one
ext17b_systemd.py        systemctl / systemd-run / journalctl substitute (per namespace)
ext17b_gateway.py        gateway substitute that publishes gateway_state.json and can be drained
ext17b_case.sh           one adversarial scenario, driven end to end inside the namespace
ext17b_matrix.py         builds, drives and reads back every scenario; emits one JSON record each
```

No test sourced a function and called it directly. Every result below is the exit status of the
launcher or of the supervisor it started, plus artifacts read back from the case directory on the
host *after* the namespace was gone.

## 2. Identities

| What | Commit | Tree |
| --- | --- | --- |
| Candidate under review (EXT-17A) | `9601cc8a8ec1fc7fb9d534c653cf66e916b50c20` | `c9861d5712d1c6a59fd1ed60e8e85437707950e5` |
| EXT-17B engine candidate (every case but `trigger`) | `a41242328a6319b39078288ca3e66b3dcab99c4b` | `11ba4a37ba8166946b73c88329b2cfc37c9611b4` |
| EXT-17B final head (`trigger` case, and this document) | `663c11ddd279d0b8d397a681b13518a48abfd457` | `5df0c9b4ac622b48f87cf016724cb2ac69892bb2` |
| Production baseline (untouched) | `3652a3702dae8d2917b91b6949da580efd3ef496` | `3bfc1c842bf76e8a95356f4f0f3a91e75767992c` |

Every scenario record carries the candidate it was executed against, read back from the journal, so
no record can be attributed to a different identity than the one it names.

## 3. Isolation: what makes production unreachable

The launcher and the engine deliberately name production paths (`/etc/systemd/system`,
`/usr/local/lib/hermes-release`, `/var/lib/hermes-release`, `/var/log`). Isolation is therefore not a
matter of asking them nicely; it is a matter of those names resolving somewhere else.

`ext17b_run.sh` creates `unshare -r -m --propagation private`. Inside it `ext17b_ns.sh`:

* mounts a **fresh tmpfs** over `/etc/systemd/system` — the unit the release regenerates exists only
  for the lifetime of the namespace, so a host unit can neither be read nor overwritten;
* bind-mounts the case's own `lib/`, `var/` and `logs/` over `/usr/local/lib/hermes-release`,
  `/var/lib/hermes-release` and `/var/log`, so the staged engine, the journal, the archive and the
  supervisor log survive the namespace and can be inspected afterwards;
* writes a per-run random **sentinel** last into all four mounts.

The guard is the enforcement point, not the mounts. It refuses with exit 64 unless **all** hold:

1. euid is 0 and the uid map maps uid 0 **from a non-zero host uid** (a user namespace, not the
   host's own identity, and not a `0→0` map);
2. every release path carries this run's sentinel — a value generated per run and written inside the
   new mount, so it cannot exist on the host, be guessed, or be replayed;
3. `systemctl` resolves to the qualification substitute;
4. the seeded unit names **this scenario's** `HERMES_HOME`, read from the private unit path
   (`release_paths[0]`), so a host unit can never satisfy the condition.

Negative evidence, taken on the host (not inside a namespace):

```
$ python3.13 harness/qualify/ext17b_guard.py --root <case-root>
EXT17B GUARD REFUSED: EXT17B_SENTINEL is not set; this is not a qualified disposable run
HOST_GUARD_RC=64
$ EXT17B_SENTINEL=<anything> python3.13 harness/qualify/ext17b_guard.py --root <case-root>
EXT17B GUARD REFUSED: euid is 1001, not namespace root
RC=64
```

`tests/harness/test_release002_ext17b_guard.py` (154 lines) asserts each of the four conditions and
the four refusal paths individually, including "uid 0 mapped from uid 0 is refused" and "a host unit
in a host unit directory never satisfies the last condition", and finishes by running the real CLI
on the host and requiring exit 64.

### 3.1 The one production code change isolation required

`release002_preflight.py` gate 5 read `HERMES_RECOVERY_HOLD` out of `/proc/<pid>/environ`. Inside a
user namespace the release process is a **sibling** of the gateway, not its parent: Yama blocks the
read and namespace root holds no `CAP_SYS_PTRACE` over the initial namespace. The gate therefore
could not be evaluated at all, and the scenario refused every time for the wrong reason.

The minimum correction was a qualification-only path (`substitute_hold(pid)`), which returns a
verdict **only** when all three hold: `EXT17B_SENTINEL` is set, the state file declares itself a
`substitute`, and the state file's pid is the pid being asked about. None of those can hold on the
host. When any is missing the function degrades to the production behaviour (`unevaluable`), and
`tests/harness/test_release002_ext17b_substitute.py` pins both halves.

## 4. Substitutes, and why they are not shortcuts

`ext17b_systemd.py` implements the verbs the release uses — `daemon-reload`, `start`, `stop`,
`restart`, `show` (including the `-p <Name> --value` form the release uses most), `is-active`,
`is-enabled`, `enable`, `disable`, `list-units`, plus `systemd-run --wait --pipe` and `journalctl`.
It derives the properties the release reads (`ActiveState`, `MainPID`, `TimeoutStopUSec`,
`Environment`) from its own state, and it enforces the unit's own stop budget and `RuntimeMaxSec`
rather than a shortened test value:

* a `hang` gateway that ignores `TERM` is killed with `SIGKILL` at the unit's `TimeoutStopUSec`
  (300 s) — the release's 300 s stop budget is the real one, not a test value;
* a `SIGKILL` is reported the way systemd reports it (`Main process exited, code=killed,
  status=9/KILL`) so the release's "SIGKILL lines after restart" forensic gate sees what it would
  see on the host.

`ext17b_gateway.py` publishes `gateway_state.json` with `pid`, `code_sha`, `gateway_state`,
`active_agents`, `open_sessions`, `recovery_hold` and `substitute: true`, honours the drain marker
and the ESTOP sentinel, and can be built to hang on `TERM` for the stop-budget cases.

## 5. The disposable installation

* **Git**: one shared bare mirror (`base.git`) per scratch root, and each case gets a `--shared`
  clone of it, so the installation has a real git identity (`HEAD`, `HEAD^{tree}`, `status`) without
  copying the object store seven times. Identity is re-derived from the clone, never inherited.
* **Interpreter**: a real `venv` created with `--without-pip`, with an `ext17b-production-deps.pth`
  pointing at the production `purelib`, then probed (`import ruamel.yaml, psutil, yaml`). A symlinked
  interpreter was rejected by the release's own runtime-coverage gate as **foreign evidence**, which
  is the gate doing its job; the `.pth` keeps dependencies resolvable while the interpreter itself
  is inside the installation under audit.
* **Session fixture**: a scenario-local `state.db` — never `/home/hermes/.hermes/state.db` — so the
  release's session inventory is real and untouched production sessions are not read.
* **Unit**: a seeded `hermes-gateway.service` plus the two operator drop-ins
  (`90-recovery-hold.conf`, `95-timeout-stop.conf`) written into the private unit directory; the
  release regenerates the unit there and the archived copy lets the rollback restore it exactly.
* **Faults**: `--dirty`, `--foreign-freeze`, `--active-agents N`, `--stop-mode hang|sigkill`,
  `--fail-start`, `--fail-start-on N`, `--fail-start-after N`. The faults are defects in the
  *fixture*, never weakened budgets.

## 6. What the assembled path did

Each case runs the same driver inside the namespace:

```
ext17b_case.sh  →  systemctl daemon-reload && systemctl start hermes-gateway.service
                   hermes-release-002 stage  --candidate <candidate>
                   hermes-release-002 plan|apply|recover ...
```

`stage` extracts the engine from the candidate and writes the manifest; `plan`/`apply` are the
installed launcher, which refuses unless it still matches the manifest checksum, the candidate is
staged and the installation is clean and on the expected identity; `apply` starts the supervisor as
a transient unit and returns; the supervisor runs the engine phases
`identity → preflight → manifest → archive → freeze → quiesce → deploy → unit → restart → verify →
unfreeze → receipt`, journalling each step.

Case results are in §7. Every cell below is a value read from `journal.json`, `RECEIPT.md`,
`preflight-before.json`, `rollback.txt`, `NEEDS_HUMAN.txt` or the installation's git identity on the
host after the namespace exited.

## 7. Case matrix

| Case | What it proves | Exit statuses | Journal | Receipt | Installation after | Leftover |
| --- | --- | --- | --- | --- | --- | --- |
| `dirty` | a dirty installation is refused before anything is read or written | plan 2, apply 2 | none written | none | baseline `3652a370`, still dirty (the fixture's own edit) | 0 |
| `foreign-freeze` | another attempt's drain marker is never adopted, cleared or overwritten | plan 2, apply 2 | `ABORTED_SAFE`, `mutated=false` | none | baseline, clean | 0 |
| `busy` | a host that never presents a quiet window aborts before mutation | plan 0, apply 3 | `ABORTED_SAFE`, `mutated=false` | `ABORTED_NOT_QUIET` | baseline, clean | 0 |
| `rollback` | a deployed unit that cannot come up is rolled back and **verified** | apply 1 | `ROLLED_BACK`, `mutated=true`, `restart_attempts=1` | `ROLLED_BACK` | baseline, clean, unit sha == archived sha | 0 |
| `needs-human` | an unverifiable rollback is NEEDS_HUMAN with the journal left open, then completed from the journal | apply 1, recover 0 | result unset, `phase=unfreeze`, `NEEDS_HUMAN-<run>.json` beside it | none | baseline, clean | 0 |
| `interrupt` | TERM during the restart settles the run, and recovery refuses to race it | apply-detach 0, TERM 0, recover-while-running **65**, recover 0 | `ROLLED_BACK`; final `recover` phase `decision=none reason=already settled as ROLLED_BACK` | `ROLLED_BACK` | baseline, clean | 0 |
| `timeout` | a gateway that never stops settles at the 300 s budget and rolls back | apply 1 | `ROLLED_BACK`; `restart rc=0 elapsed_ms=300209` | `ROLLED_BACK` | baseline, clean, unit restored | 0 |
| `trigger` | trigger v2 hands off exactly once into the qualified supervisor | wrong-candidate 4, wrong-again 76, trigger 0, trigger-again 76 | `COMPLETE`, `mutated=true`, `restart_attempts=1` | `RELEASED_VERIFIED` | **deployed** `663c11dd`, clean | 1 → 0 after the collector's own reap |

The refusals name themselves, which is what makes them evidence rather than absence of evidence:

```
dirty           failed gates: ["install_clean"]                    detail: "1 changed path(s)"
foreign-freeze  failed gates: ["no_preexisting_freeze"]            detail: "principal=release-freeze:foreign-attempt owned_by_this_attempt=True age_s=4"
busy            engine log: "no quiescent window within 180s — aborting before any mutation"
timeout         rollback.txt: head/tree back at 3652a370/3bfc1c84, unit_sha256 == archived, active_state=active,
                restart_attempts=2, settlement_failures=none
trigger         fired:   {candidate a4124232…, status fired, apply_rc 0, engine_run_id 20261112T030837Z-…, quiet_samples 3, v2}
                refused: {candidate 9601cc8a…, status failed, apply_rc 65, engine_run_id "", quiet_samples 3, v2}
```

Two rows deserve a note:

* **`dirty` writes no journal at all.** That is the correct outcome: the refusal happens at `plan`,
  before the release has an attempt to record. The evidence is the gate name and the fact that the
  installation is exactly as the fixture left it.
* **`needs-human` leaves the journal without a `result`.** That is deliberate: `result` is the
  *terminal* disposition and this attempt never reached one. The disposition lives in
  `NEEDS_HUMAN-<run_id>.json` (`"disposition": "RESTORE-002_NEEDS_HUMAN"`) with the reason spelled
  out:

  ```
  rollback did not restore the deployed identity (head=3652a370… tree=3bfc1c84… active=failed
  unit=52770600…/52770600…; rollback restart returned 1)
  ```

  The run then proved recovery: `recover` bound itself to the journalled attempt, decided
  `unfreeze` ("install is still at the deployed identity; nothing applied"), restored admission, and
  — correctly — did **not** claim `COMPLETE`.

Successful APPLY, end to end (the `trigger` case's `RECEIPT.md`, quoted, not summarised):

```
- status: **RELEASED_VERIFIED**
- run id: `20261112T030837Z-a41242328a63`
- candidate: `a41242328a6319b39078288ca3e66b3dcab99c4b` (tree `11ba4a37ba8166946b73c88329b2cfc37c9611b4`)
- install before: `3652a3702dae8d2917b91b6949da580efd3ef496` (tree `3bfc1c842bf76e8a95356f4f0f3a91e75767992c`)
- effective TimeoutStopUSec: 300000000
- restart attempts: 1
restart_rc: 0
restart_elapsed_ms: 168
old_pid: 1785983   new_pid: 1788488
sigkill_lines: 0
hold_env_in_new_process: 1
session_inventory: same
--- stop/drain lines ---
qualification-host systemd[1]: Stopping hermes-gateway...
qualification-host systemd[1]: Stopped hermes-gateway.service.
```

Note what that receipt is *not*: it is not a claim that the run was quiet because the fixture is
quiet. The 45 s drain ack, the 3 × 20 s quiescence samples, the identity revalidation before
mutation, the 300 s stop budget and the post-release preflight/manifest/hold/session checks all ran
inside the engine and are journalled phase by phase.

## 8. Trigger v2 handoff

Trigger v2 (`harness/trigger/qw-launch-apply-v2.sh`) is **not installed**; the installed trigger is
still v1 (82 lines, untouched). The case runs v2 from the repository with its qualification hook
(`TRIGGER_QUALIFY=1`) and a case-local state directory, against the same installed launcher the other
cases use.

Verified, in the assembled path:

| Requirement | Evidence |
| --- | --- |
| single use | a second arming refuses with 76: `REFUSE: attempt-fired record already present at …/armed/attempt-fired.json` |
| exact candidate binding | `--candidate 9601cc8a…` while the installation is staged for another candidate → the launcher refuses (65); the trigger records `status=failed`, `apply_rc=65` and exits 4 |
| refusal propagation | the same run: `REFUSE: the launcher did not start a supervisor (rc=65) — no retry from this trigger` |
| no automatic retry | the refused attempt's own record blocks a second arming (76); the fired attempt's record blocks a second arming (76) |
| handoff | `arm v2 … quiet=3/3` → `recording the attempt and starting the single supervisor` → launcher prints `supervisor started detached: hermes-release-20261112T030837Z-…service` → `apply rc=0 engine_run_id=…` → `attempt-fired.json written (status=fired) — launcher exiting, no retry, no second APPLY` |
| effective deadline and child termination | driven against a stub launcher that never returns: the trigger exits 5, records `status=failed`/`apply_rc=124`, logs `DEADLINE: …`, and the child is gone from `/proc` (see D-5) |

The quiet-window sampling interval is compressed in the case (1 s instead of 20 s) so that the matrix
fits a bounded run; the gate arithmetic is unchanged (`3 consecutive zero-activity samples`), and
the trigger's own record stores the interval it used (`sample_interval_s: 1`), so the compression is
visible in the evidence rather than inferred from it.

## 9. Defects found and corrected during qualification

Three production defects were found by driving the assembled path, each with a red control, and each
committed separately so the correction is reviewable on its own:

**D-1 (EXT-17B qualification commit `aff012fef8`).** `apply` in the namespace returned 1 with a
`NEEDS_HUMAN` file and no explanation. Cause: the *systemd substitute* lacked `enable`/`is-enabled`
(`hermes gateway install --system` enables the unit) and mis-parsed `systemctl show <unit> -p Name`.
Fixed in the substitute, not in the release.

**D-2 (`755005177f`).** Recovery re-verified a deployed release by calling
`verify_post_release "$PHASE"`, and in recovery mode `PHASE` is `recover` — a *mode* name, not one
of the preflight's phases (`pre`, `infreeze`, `revalidate`, `post`, `released`, `any`). `preflight()`
maps an unknown phase to its usage exit 64, `verify_post_release` reads any non-zero exit as
`NO_GO`, so **every** recovery of an already-deployed release ended in `NEEDS_HUMAN` with the freeze
engaged and no operator action able to clear it. Recovery happens after the restart, so the state to
prove is the post-release one; the phase is now the literal `post`.
`tests/harness/test_release002_ext17b_recovery_phase.py` pins the phase set and the rule that
`verify_post_release` is never handed a variable.

**D-3 (`d01af9e541`).** `freeze_release()` set `PHASE=unfreeze` and never restored it. The exit trap
only recognises exits in phases it knows are settled, so a verified rollback — which exits non-zero
by contract after journalling `ROLLED_BACK` and writing the receipt — arrived at the trap as
"unhandled failure in phase unfreeze". Releasing the freeze is work *inside* a phase, so the phase is
now restored on every return path. Red control: with the restore removed the driver reports
`PHASE_AFTER=unfreeze`.

**D-4 (`ae6bf710b9`).** The same trap still triaged a settled attempt, because a verified rollback
journals its outcome under phase `report`. The trap now returns early when the journal already
carries a result **for this run** (`result` non-empty and `run_id == $RUN_ID`). This cannot suppress
triage of a real failure: `journal_settle` is the last thing every settling path does, so a run that
fails earlier has no result to read. Red control: with the check removed the trap overwrote the
proven `ROLLED_BACK` with `ABORTED_SAFE` and filed `NEEDS_HUMAN` evidence beside it.

**D-5 (`a41242328a`).** Recovery ran while the supervisor that owned the journal was still inside its
300 s restart. It read "candidate is deployed and the service is active" as
`complete_verification`, journalled `recovered by completing verification` and restored admission;
five minutes later the supervisor finished, rolled back, and journalled `ROLLED_BACK` over the top.
Two contradictory terminal outcomes for one attempt. `recover` now refuses (exit 65) while another
release engine is running or while a `hermes-release-*` unit is active, using the same process-age
test the APPLY guard uses so `pgrep` cannot match itself. In the re-run the assembled path shows
`recover-while-running 65`, then `recover 0` with the journal recording
`decision=none reason=already settled as ROLLED_BACK`.

**D-6 (`bc1b5549e8`).** Trigger v2 called the launcher with no bound at all. Against a launcher that
never returns, the attempt deadline expired with the trigger still blocked, the record stuck at
`pending`, and no way to tell whether a supervisor had been started; the prompt only came back
because the harness had wrapped the whole thing in `timeout`. The call is now bounded by what is
left of the attempt deadline (`timeout --kill-after=5`, floor 10 s): a launcher still running at the
deadline is recorded `status=failed`/`apply_rc=124` with a `DEADLINE` line, the trigger exits 5, and
the child is gone from `/proc`.

Two further defects were in the qualification harness itself and are recorded because they changed
what the evidence means: a dirty-worktree fixture that dirtied a file only `stage` creates (so the
case never dirtied anything), and a scenario driver that was **edited while bash was executing it**,
which corrupted a run; the affected records were discarded and every case was re-run against a
frozen driver.

## 10. Process hygiene

`ext17b_matrix.py` counts, from the host, the fixture gateway processes started from the case's own
interpreter, kills them, and counts again. Every case in §7 reports `leftover = 0`. The gateway
substitute is also stopped by the driver's `EXIT` trap so that a case that ends in a refusal leaves
nothing behind.

`tests/conftest.py` disables session process reaping on this host because `psutil` is only declared
for Python ≥ 3.14 and is not importable here; the notice is emitted (`[conftest] psutil unavailable:
session process reaping skipped`) and **no test is skipped or disabled**. The reaping coverage this
would have provided is replaced, for this mission, by the per-case host-side count above.

## 11. Suites and lint

* `tests/harness` — **413 passed, 0 failed, 0 skipped, exit 0** in 172.73 s, run alone on the host
  with nothing else executing (`env -u PYTHONPATH TMPDIR=<scratch>/px python3.13 -m pytest tests/harness
  -q -p no:hypothesispytest`). No test was disabled, deselected or quarantined to reach this number.
* New coverage added by this mission: 12 guard tests (each of the four isolation properties and each
  refusal path, plus the real CLI exiting 64 on the host), 6 preflight-substitute tests, 3
  recovery-phase tests, 5 recovery-exclusivity tests, 2 settlement tests (phase restoration, and
  "a settled attempt is not triaged again"), 1 trigger test (deadline and child termination), plus
  the scenario driver itself.
* The suite is sensitive to its environment in one honest way: two runs taken while the matrix was
  executing reported failures in `tests/harness/test_release002_ext16a.py`, because the engine's
  single-instance guard correctly saw the scenario engines and refused (`pids: …`). That is the
  guard working; the numbers above are from a run with no scenario in flight, and the contamination
  was discarded rather than explained away.
* `ruff` over every Python file this mission added or changed: clean. The repository's `harness/`
  tree has 36 pre-existing ruff findings in files this mission did not touch (verified identical
  against `HEAD`); they are untouched and not suppressed.
* `bash -n` over the engine, launcher, trigger v2 and all four qualification shell scripts: clean.
* Process hygiene: after the final suite run, `pgrep -f 'ext17b|release002_release|install/venv'`
  returns nothing, and every case record reports `leftover → 0` (see §7).

## 12. Residual risks, and what is not proven

* The fixture's `gateway_state.json` is a file contract, not a real gateway process tree. The
  substitute reproduces the fields the release reads and the signals it sends, but a real gateway's
  shutdown ordering (drain ack, in-flight turns) is only modelled, not executed.
* `substitute_hold()` has unit coverage and scenario coverage, but no production run: on the host the
  production `/proc/<pid>/environ` branch is still the only branch that can be taken.
* The 45 s drain-ack budget and the 3 × 20 s quiescence samples are enforced by the engine and were
  observed in the scenario logs, but the trigger case compresses the *sample interval* (1 s) to keep
  the matrix inside a bounded run; the gate arithmetic (3 consecutive samples inside 180 s) is
  unchanged, and the production default interval is not altered by this work.
* Host-level conditions (memory, disk, cron claims, admission epochs) are fixture values. A
  production run meets the real ones; that is what the bounded production proposal in §13 is for.
* `state.db` FTS5 corruption on this host is untouched: repairing it needs a live-database write,
  which is outside this authority.

## 13. Bounded production release proposal

What follows is the sequence this mission *proposes*, with the identities it would use. It is not
authorised, scheduled, or armed by this document.

1. **Identity.** Candidate `663c11ddd279d0b8d397a681b13518a48abfd457` / tree
   `5df0c9b4ac622b48f87cf016724cb2ac69892bb2`, installed over production
   `3652a3702dae8d2917b91b6949da580efd3ef496` / `3bfc1c842bf76e8a95356f4f0f3a91e75767992c`. Any
   mismatch at run time is a hard stop, not a re-derivation.
2. **Preconditions, checked by the launcher, not by a human.** launcher checksum still matches the
   pinned manifest; candidate staged with its engine inventory; installation clean and on the
   expected identity; no other release engine and no `hermes-release-*` unit.
3. **One attempt, one arming.** The trigger fires once; `attempt-fired.json` is written `pending`
   before the call and `fired` only after the launcher printed the supervisor unit. No retry, no
   second APPLY, ever, from the same armed state.
4. **The engine's own sequence, unchanged:** identity → preflight → manifest → archive → freeze
   (drain marker + ESTOP owned by this attempt) → 45 s drain ack → 3 × 20 s zero-activity samples
   inside the 180 s window → revalidate identities → one fast-forward merge → regenerate the unit →
   **one** restart under a 300 s effective `TimeoutStopUSec` → forensic check (zero SIGKILL lines) →
   post-release preflight/manifest/hold/session verification → release **only** this attempt's
   controls → receipt → terminal journal settlement.
5. **Stop conditions, all of them pre-mutation or bounded:** not quiet in 180 s → `ABORTED_SAFE`
   with `mutated=false`; identity drift, dirty worktree or foreign freeze → refusal with nothing
   written; restart failure or SIGKILL → verified rollback to the archived identity
   (`ROLLED_BACK`); unverifiable rollback → `NEEDS_HUMAN` with the freeze held and evidence beside
   the journal; recovery only after the supervisor is gone, and only from the journal.
6. **Budgets.** 45 s drain ack; 90 s quiescence window with 3 × 20 s samples; 240 s startup wait;
   60 s unfreeze; 300 s stop; supervisor `RuntimeMaxSec=1500 s` sized from the computed envelope;
   trigger attempt deadline 4 h. None of these is widened by the qualification work.
7. **Rollback.** Exactly one rollback restart (the second restart overall). A third is a human
   decision. The rollback target comes from the journal, never from a human-supplied hash.
8. **After the run.** The receipt and journal are read back; `RECEIPT.md` must say
   `RELEASED_VERIFIED`, `ABORTED_*` or `ROLLED_BACK`; a `NEEDS_HUMAN` file of any kind stops the
   process there and the freeze stays engaged.
9. **What would have to change for this proposal to be superseded:** any change to the engine bytes,
   the manifest pins, the unit template, the drop-ins, or the budgets. Each of those invalidates the
   evidence in this document, because every case is bound to the identities in §2.

## 14. Disposition

**`EXT-17B_READY_FOR_EXTERNAL_CA_REVIEW`**

The assembled launcher → supervisor → engine → journal → verification path was exercised end to end
in a disposable installation and a disposable systemd environment, and all eight required outcomes
have a record: successful APPLY (`COMPLETE` / `RELEASED_VERIFIED`), pre-mutation abort
(`ABORTED_NOT_QUIET`, `mutated=false`), post-mutation rollback (`ROLLED_BACK`, identity and unit
restored and verified), interrupted restart (settled, not abandoned; recovery refused while the
supervisor ran and `decision=none` once it had), timeout settlement (300 s budget, then verified
rollback), dirty-worktree refusal (`install_clean`), foreign-freeze refusal
(`no_preexisting_freeze`), and recovery from a durable journal. Trigger v2's handoff, single use,
candidate binding, refusal propagation, deadline and child termination are all evidenced.

Four production defects were found by doing this and every one of them was fixed with a red control
and its own commit: the preflight phase recovery could never evaluate (D-2), the phase name the
freeze release left behind (D-3), the trap re-triaging a settled attempt and overwriting its result
(D-4), and recovery racing a live supervisor (D-5); plus the trigger's unbounded launcher call (D-6)
in the versioned, not-yet-installed v2. The last two were
*observed in the assembled path*, not inferred from reading the code — which is the reason this
qualification was worth running.

Stopping here. Nothing was deployed, staged into production, applied against production, restarted,
installed, activated, merged or activated at Stage C, no session or database was written, no freeze
or hold was removed, and PR #9 remains draft and unmerged.
