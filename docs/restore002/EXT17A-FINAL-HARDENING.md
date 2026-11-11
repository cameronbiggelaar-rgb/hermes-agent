# EXT-17A — final release hardening: correction and qualification evidence

Disposition: **RESTORE-002_CORRECTED_CANDIDATE_READY_FOR_EXTERNAL_CA_REVIEW**
Mission: `RESTORE-002 / EXT-17A FINAL RELEASE HARDENING`
Authority used: isolated code corrections, tests, adversarial review, commits, branch publication,
draft-PR evidence. **No** production deployment, APPLY, staging, gateway restart, live systemd
change, trigger activation, session mutation, recovery-hold removal or PR merge.

This document is written against the code, not against intentions: every claim below names the file,
the test and the exit code that supports it.

## 1. Identities

| role | commit | tree |
| --- | --- | --- |
| production install (baseline, untouched) | `3652a3702dae8d2917b91b6949da580efd3ef496` | `3bfc1c842bf76e8a95356f4f0f3a91e75767992c` |
| EXT-17 corrected code (previous candidate, **not** reusable) | `895c4091b1014f46584eb22bdca33fc2af5046c5` | — |
| EXT-17 documentation head (previous candidate, **not** reusable) | `f5ba3cca8ec792f891b370236a7e4ca55ec556ea` | `f5f280faa0bf21a1a94b37d72a496088014b5c20` |
| **EXT-17A executable candidate** | `9601cc8a8ec1fc7fb9d534c653cf66e916b50c20` | `c9861d5712d1c6a59fd1ed60e8e85437707950e5` |

`3652a370` is an ancestor of the new candidate (`git merge-base --is-ancestor` → yes), so the branch
fast-forwards from production. `895c4091` remains in history but **its qualification does not carry
forward**: EXT-17A changes executable behaviour (settlement, recovery decisions, trigger), so the new
SHA is the only identity any future APPLY may name.

## 2. What changed, and why

| file | change | reason |
| --- | --- | --- |
| `harness/release002_release.sh` | 985 → 1350 lines: settlement library (`settle_run`, `journal_settle`, `journal_require_attempt`, `needs_human`, `settle_fail`), verified `freeze_release`, verified bounded `rollback`, rewritten `on_signal`/`on_exit`, `abort_before_mutation`, `verify_post_release`, journal-bound `mode: recover`, qualified `HERMES_RELEASE_LOCK` | §3 |
| `harness/release002_control.py` | complete `PHASES` / `PRE_MUTATION_PHASES`; `recover-plan` gains `install_clean` and `journalled_deployed`; unfreeze-at-deployed now refuses a dirty worktree | §3.4, §3.5 |
| `harness/trigger/qw-launch-apply-v2.sh` | **new**, 190 lines — versioned trigger correction, not installed | §4 |
| `tests/harness/test_release002_ext17a_settlement.py` | **new**, 24 adversarial scenarios over the shipped region | §3.7 |
| `tests/harness/test_release002_ext17a_trigger.py` | **new**, 12 trigger-envelope tests | §4 |
| `tests/harness/test_release002_ext17a_recovery.py` | **new**, 10 recovery-decision tests | §3.5 |
| `tests/harness/test_release002_ext17.py` | signal test re-anchored to `SIGNAL_SEEN`, `PHASE=interrupted` removed | §3.2 |
| `tests/harness/test_release002_ext16a_journal.py` | `test_engine_failure_decision_sees_the_journalled_mutation` tracks the settled `abort_before_mutation` shape | §3.6 |
| `tests/conftest.py` | guarded `import psutil` in the session reaper (stderr notice, returns `[]`) | §5.3 |
| `docs/restore002/EXT17A-ENVELOPE-QUALIFICATION.md` | **new** — supervisor/trigger measurements, defects, install procedure | §4 |

Nothing else was touched: `gateway/`, the unit, drop-ins, `/etc/gitconfig`, the installed trigger and
`/usr/local/lib/hermes-release/` are byte-identical to production.

## 3. Interruption settlement

### 3.1 No settlement step can skip the rest

Every recovery step is executed through `settle_run <description> <command…>`, which runs the command,
records any failure in `SETTLE_FAILURES` and **returns 0** (`settle_fail` warns and appends). `set -e`
therefore never ends the settlement early, and the failure is not swallowed either: it is a fact that
later gates read. A settlement that has any entry in `SETTLE_FAILURES` cannot report success — the
freeze/rollback verification paths treat it as unproven and go to a human.

### 3.2 A signal is not a settlement

`on_signal()` sets `SIGNAL_SEEN=$sig` and **leaves `PHASE` alone**; `on_exit()` settles whatever phase
the attempt actually reached. The previous code renamed the phase to `interrupted`, which discarded
what the attempt had done — a signal during `deploy` looked exactly like a signal during `quiesce`, and
`on_exit` then took the pre-mutation path for a possibly-mutated install. Repeated TERMs are
idempotent: the second one finds `SETTLING=1` and does not restart settlement.

### 3.3 Mutation uncertainty fails closed

`journal_settle` reads both the in-memory `JOURNAL_MUTATED` and the journal's own `"mutated": true`
before it writes `--mutated`, so a lost in-memory flag cannot make a mutated attempt look clean. The
recovery decision (`decide_recovery`) takes mutation from the journal history (`mutation_state`), then
from the phase list, and only then from the sticky flag — and any disagreement between "the record
says mutated" and "the install is where I expected" produces `needs_human`, never `unfreeze`.

### 3.4 Post-mutation failure rolls back and verifies the result

`rollback()` restores the deployed identity, re-installs the archived unit and restarts **once**, then
checks head, tree, service state, regenerated unit sha256 and its own failure list. Only when all of
those hold does it write `ROLLED_BACK`; otherwise `needs_human` with the measured values. The rollback
target is the identity the attempt journaled *before* it touched anything (see 3.5), so it cannot roll
the release into itself.

### 3.5 Pre-mutation failure releases only this attempt's freeze

`freeze_release()` deletes the drain marker and ESTOP sentinel only if they carry this attempt's
principal (`drain_is_ours` / `estop_is_ours`), then re-reads them: the marker absent **and** the live
gateway no longer draining **and** no foreign marker present. Only then does it log `admission
restored`. A foreign or stale marker is not deleted; it is human work. `journal_require_attempt()`
refuses to change admission state at all without a durable record of this attempt.

`abort_before_mutation()` releases this run's freeze and never restarts, and `abort_before_mutation`'s
test asserts exactly that (`restarts-- 0`, no `systemctl restart` in the command log).

### 3.6 Recovery is idempotent and bound to the attempt

`mode: recover` no longer asks "is there anything to do?" — it reads `ct journal-state --path
$JOURNAL` and derives `J_RUN`/`J_CAND`, so recovery acts for the attempt the journal records and for
that attempt's principal only. An unreadable journal is `needs_human`, not "nothing to recover".

`recover-plan` now derives the rollback target from the journal itself: the `deploy-intent` record's
`head` (the identity the attempt was deploying *from*). Previously the deployed identity came only from
a CLI flag, so a restarted recovery process could lose the rollback target; now it cannot, and a
journal with no such record stays unknown (`needs_human`) rather than guessed.

Two decision gates were added because the tests found them missing:

- **dirty worktree at the deployed identity → `needs_human`.** HEAD matching the deployed identity
  used to mean "nothing applied, unfreeze". Matching HEAD with modified bytes means something wrote to
  the worktree, and only a human can say what. The new `install_clean` fact is now a condition of the
  unfreeze, not decoration (it was exactly decoration before the test caught it — see §6).
- **a deploy-intent record does not prove "nothing changed"** — it proves the attempt *intended* to
  fast-forward. The release is only unfrozen when the install is provably at the deployed identity
  **and** clean; if the candidate is checked out instead, the decision is `rollback`.

### 3.7 The settlement tests attack the shipped code

`tests/harness/test_release002_ext17a_settlement.py` extracts the region between the
`# ===== EXT-17A: interruption settlement library (BEGIN) =====` / `(END)` markers **from
`harness/release002_release.sh` at test time**, sources it into a bash driver and replaces every
external command (`systemctl`, `git`, `install`, `sha256sum`, `timeout`, `sleep`, the helper python and
`ct`) with per-scenario stubs. Consequences: a drifted copy cannot pass, and moving the region fails
loudly instead of silently testing nothing.

The 24 scenarios include: freeze engaged and verified; clear-drain failure; clear-ESTOP failure;
abort with admission restored; rollback verified; rollback with no archived unit sha (must not claim
success); exit-path settlement before mutation; exit-path settlement after mutation (driven by the
journal record with `MUTATED=1; JOURNAL_MUTATED=1; PHASE=deploy`, not by an in-memory flag); a failing
settlement step not aborting later steps; and a foreign drain marker never being deleted.

## 4. Supervisor and trigger envelope

Full measurements, the five v1 trigger defects and the owner-approval installation procedure are in
`docs/restore002/EXT17A-ENVELOPE-QUALIFICATION.md`. Summary of the executed evidence:

- **Supervisor** (`harness/hermes-release-002`, already `Type=exec` from EXT-17): measured live on
  ephemeral `--collect` transient units — `RuntimeMaxSec=6s` delivered SIGTERM at the cap and the
  body's trap ran; a `sleep 600` child did not survive the cap (`KillMode=control-group`); the real
  settlement region, sourced into such a unit, produced `phase=aborted / result=ABORTED_SAFE /
  mutated=false` with both markers gone and no NEEDS_HUMAN; `NRestarts=0` and no `Restart=` property,
  so no retry is possible.
- **Type=oneshot vs Type=exec** was re-measured in EXT-17 and is the reason R-11 exists: under
  `oneshot`, `RuntimeMaxSec` is ignored (`Result=success` after a 30 s body with a 5 s cap).
- **Trigger**: v1 is unchanged and still installed. Its five defects (wildcard `safe.directory` trust
  that re-opens R-12, `attempt-fired.json` written even when nothing started, unconditional `exit 0`,
  journal-only retry guard, hardcoded candidate) are fixed in `harness/trigger/qw-launch-apply-v2.sh`,
  which is versioned in the repo and **not installed**.
- **Trigger tests**: 12 green. Red control: a mutant with v1's semantics restored fails 3 of 12.

## 5. Qualification run

### 5.1 Result

```
env -u PYTHONPATH TMPDIR=/tmp/px /usr/bin/python3.13 -m pytest tests/harness -p no:hypothesispytest -q
384 passed in 156.15s (0:02:36)
RC=0
```

`RC=0` is the process exit code, measured with `echo "RC=$?"` appended to the same file the suite wrote
— not inferred from the summary line. No test is skipped, xfailed, deselected or disabled.

Composition: 322 pre-existing `tests/harness` tests + 16 EXT-17 (`test_release002_ext17.py`) + 24
settlement + 12 trigger + 10 recovery = 384.

### 5.2 Regression reproduction

- **R-13** (quiescence): `test_release002_ext17.py` separates `gateway_heartbeat_fresh` from
  `quiet_window_elapsed` and fails on the pre-EXT-17 tree, which asked the second question with the
  first instrument.
- **R-11** (envelope): red/green controls in EXT-17 plus the `Type=oneshot` vs `Type=exec` measurement;
  `test_supervisor_unit_keeps_a_real_envelope` now also guards the unit's property set.
- **EXT-17A**: red control at `f5ba3cca` — the pre-correction candidate — with the three new test files
  copied in: **17 failed, 5 passed** (trigger 10/12 failed because the file does not exist there;
  recovery 7/10 failed on the new gates and journal-derived target; the 3 that pass assert behaviour
  that already existed, such as corruption → `needs_human`).

### 5.3 The one change that touches tests, disclosed

`tests/conftest.py` wraps the session reaper's `import psutil` in `try/except ModuleNotFoundError`,
writes `[conftest] psutil unavailable: session process reaping skipped` to stderr and returns `[]`.

Why: `pyproject.toml` declares `psutil` only for `python_version >= "3.14"` (it is a `grep` with no
match below that), while the suite supports 3.11–3.13 and is run here on `/usr/bin/python3.13`. The
reaper runs in `pytest_unconfigure` **after** the summary line, so an otherwise all-pass run exited 1
with a `ModuleNotFoundError` traceback — the process exit code was meaningless. This is
test-infrastructure, not a product assertion: no test is disabled, the notice is printed on every run
that takes this path, and the guarded path is exactly the one that has nothing to reap.

It is disclosed rather than silent: the External CA can require the dependency instead (pin `psutil` for
3.11+, or run the suite on 3.14), in which case the guard becomes dead code and can be removed. What
EXT-17A did *not* do is turn off the reaper's real work when psutil *is* available.

## 6. Adversarial findings and fixes (this mission)

| # | finding | found by | fix |
| --- | --- | --- | --- |
| 1 | `install_clean` was computed but never consulted: a dirty worktree at the deployed identity still decided `unfreeze` | `test_a_dirty_worktree…` failing | gate the unfreeze on `install_clean is True`; dirty → `needs_human` |
| 2 | the rollback target existed only as a CLI flag; a restarted recovery lost it and paged a human for a recoverable attempt | `test_candidate_deployed…` returning `needs_human` | `journalled_deployed()` reads the `deploy-intent` head; CLI still wins when given |
| 3 | `on_signal` renamed the phase to `interrupted`, erasing what the attempt had done | reading `on_exit`'s branches | `SIGNAL_SEEN`, phase preserved (test re-anchored) |
| 4 | the trigger recorded `attempt-fired.json` for a launch that refused, and exited 0 | v1 review + `test_a_refused_launch…` | `pending` → `fired`/`failed`, exit 4 |
| 5 | a launcher exiting 0 without starting a unit was still recorded `fired` | `test_a_launcher_that_reports_no_run_id…` | run id accepted only when `rc=0` |
| 6 | v1 injected `safe.directory='*'` into the launcher environment | v1 review | exactly two explicit repo paths |
| 7 | the trigger's inter-sample sleep could overshoot the deadline and its TERM exit was silent | v1 review + `test_signal_before_the_fire…` | sleep clamped to the remaining budget; `DISARMED by SIG<sig>` |
| 8 | `PHASES` was missing phases the engine can die in, so a real attempt could be read as "corrupt" | `test_every_phase_the_engine_can_record_is_accepted` | complete phase list, guarded by the test |
| 9 | psutil teardown made an all-pass suite exit 1 | the full-suite run | guarded import + notice (§5.3) |
| 10 | `test_engine_failure_decision_sees_the_journalled_mutation` anchored the pre-settlement literal `echo --mutated` | full-suite run after the rewrite | re-anchored to `mutated_flag="--mutated"` / `$mutated_flag` |

Findings 1 and 2 are the substantive ones: both were "the code looked careful and the decision was
wrong", and both were found by a test rather than by review.

## 7. Remaining risks (for the External CA)

1. **The full APPLY path cannot be executed in isolation by design.** `harness/hermes-release-002`
   refuses to run from anywhere except its installed path (`manifest` pins the launcher), and the
   preflight binds to the live install and gateway. What *was* executed: the settlement region inside a
   real supervisor-timeout unit, 24 fault-injection scenarios against that region, 10 recovery
   decisions against a real git repository through the shipped CLI, 12 trigger scenarios against the
   versioned correction. A first real run remains the only test of the assembled path, and that is what
   the bounded proposal below is for.
2. **The trigger cannot observe the supervisor's outcome** — it fires and exits. The journal is the
   authoritative record; treating the trigger's exit status as a result would be wrong (documented, not
   changed).
3. **`attempt-fired.json` is written before the launcher call.** A host killed between the record and
   the call leaves `status="pending"` with no run: it blocks a re-arm and is visible. That is the safe
   direction, and it is deliberate.
4. **Trigger v2 is uninstalled and unexercised on the host.** It has 12 green sandbox tests and a
   documented install procedure; the owner installs it, not this mission.
5. **The conftest guard** (§5.3) is test-infrastructure and is disclosed as such.
6. **`state.db` FTS5 corruption** (`malformed inverted index for main.messages_fts`) is still present
   in the live Hermes state database. Repairing it is a live-DB write outside this authority; it has no
   bearing on the release path, and `session_search` is the only degraded reader.
7. **The four-hour trigger window is spent.** `restore002-qw-launch` fired `2026-11-07T22:28:58Z` and
   its deadline expired `2026-11-08T00:57:45Z`; EXT-17A did not re-arm it and did not change it.

## 8. Bounded production release proposal (not executed)

Identity to release: `9601cc8a8ec1fc7fb9d534c653cf66e916b50c20` / tree `c9861d5712d1c6a59fd1ed60e8e85437707950e5`. The documentation commit that carries this file is a
descendant and is **not** the executable identity: a release names the code commit and its tree, so that
reviewing evidence and running a release cannot be confused.

Sequence, unchanged in shape from the approved P-2 gate set, one APPLY only:

1. **Fetch**: `git fetch` the candidate from `cameronbiggelaar-rgb/hermes-agent` into the install's
   remote refs; do not check anything out.
2. **Bootstrap/stage**: run `hermes-release-002 stage` so the engine and manifest are in
   `/usr/local/lib/hermes-release/` and the manifest pins the candidate commit, tree and launcher
   sha256. Nothing is deployed.
3. **PLAN**: `hermes-release-002 plan --candidate <commit>` must print `DRY RUN RESULT: GO` against
   production `3652a370` / `3bfc1c84…`. Any identity, unit, drop-in, recovery-hold or admission
   disagreement is a hard stop.
4. **APPLY (once)**: the 13-step sequence with its existing budgets — 45 s drain ack, 3 × 20 s
   zero-activity samples inside 180 s, identity revalidation immediately before mutation, one
   fast-forward merge, unit regen, one restart, verification, release of only attempt-owned controls,
   admission restoration, journal settlement. No second APPLY; after mutation only the qualified
   rollback path; ambiguous recovery is `NEEDS_HUMAN`.
5. **Verification**: `hermes-release-002 verify` must reach `RESTORE-002_COMPLETE_VERIFICATION_READY`
   with `HERMES_RECOVERY_HOLD=1` still in the unit environment and `TimeoutStopUSec=5min`.
6. **Recovery**: if and only if the attempt fails after mutation, `hermes-release-002 recover` uses the
   bounded rollback; if any settlement step is unproven, the freeze stays engaged and the disposition
   is `RESTORE-002_NEEDS_HUMAN` with `NEEDS_HUMAN-<run_id>.json` beside the journal.

Invariants that must hold before and after: PR #9 stays draft and unmerged; Stage C not activated;
recovery hold not removed; protective drop-ins not removed; no manual session repair or deletion; no
credential or governance change; no unrelated service change; no release-scope expansion; no bypass of
an identity, freeze or recovery refusal.

## 9. Disposition

`RESTORE-002_CORRECTED_CANDIDATE_READY_FOR_EXTERNAL_CA_REVIEW` — with the candidate identity in §8.1,
`384 passed / RC=0`, red controls for every new gate, no production mutation, and the trigger
correction staged for owner approval rather than installed.

### 8.1 Final identity

- code commit: `9601cc8a8ec1fc7fb9d534c653cf66e916b50c20` (tree `c9861d5712d1c6a59fd1ed60e8e85437707950e5`), parent `f5ba3cca8ec792f891b370236a7e4ca55ec556ea`
- production baseline: `3652a3702dae8d2917b91b6949da580efd3ef496` (ancestor of the candidate)
- branch: `restore002/ext17-r13-r11` on `cameronbiggelaar-rgb/hermes-agent`
