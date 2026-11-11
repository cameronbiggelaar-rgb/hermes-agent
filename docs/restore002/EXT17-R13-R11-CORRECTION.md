# RESTORE-002 / EXT-17 — R-13 and R-11 correction, reconciled against production `3652a370`

**Disposition: `RESTORE-002_CORRECTED_CANDIDATE_READY_FOR_EXTERNAL_CA_REVIEW`**

**Authority executed:** code investigation, isolated correction, isolated testing, adversarial
self-review, draft PR evidence publication. **No production mutation of any kind was performed:**
no APPLY, no second attempt, no re-arm of `restore002-qw-launch`, no gateway restart, no systemd unit
change, no session or database write, no bootstrap/stage into `/usr/local/lib/hermes-release`, no
PR #9 undraft or merge, no recovery-hold or drop-in change, no credential or governance change.

The RESTORE-002 RECON-01 outcome (`RESTORE-002_ABORTED_SAFE_VERIFICATION_READY`) stands as published
in `#issuecomment-6788026921` and is not revisited here. The former release authority is spent; this
document claims none of it.

---

## 1. Identities recorded under EXT-17 item 1

Read from `cameronbiggelaar-rgb/chief-architect-loop` at branch `docs/external-ca-operating-contract`,
which is where the contract lives (it is not on that repository's default branch).

| Artefact | Identity |
|---|---|
| Branch tip | `069364d07a53ee526c70370acea6d62eb319b380` — *docs: direct agents to operating contract* |
| `governance/EXTERNAL_CA_OPERATING_CONTRACT.md` | blob `36b0a084e5be585b21a8d9f06801df8d84bb46f8`, 4 801 bytes, sha256 `ab02df4af92f139a0aa4ee4a26c9c3a6e3bf1f67e289986ab7ad987a43101427`, `Version: 1.0` |
| Contract at the pinned revision `4d421e2bddb8cfc31f33ce82ff41abcaad115fff` | same 4 801 bytes, same sha256 — **byte-identical** (`cmp` clean), so the contract text has not moved since the CA pinned it |
| `AGENTS.md` | blob `f9e5703d1847c16e1ce45f3fc49e65b4cf66d37c`, 1 138 bytes, sha256 `7113a62c413e82b4c2fbd66830bc9c5fa88847929794c862dacef3b5586703c9` |

`AGENTS.md` states its own scope: it governs agents working in `chief-architect-loop`; HEI and this
repository adopt equivalents through normal review. It is not itself a permission to merge, deploy,
mutate production, restart/thaw a controller, publish a governed decision or activate autonomy. The
contract's role split is the one used here: implementation, tests, commits, pushes and PR updates are
Hermes's ordinary work; privileged steps require the human owner's explicit bounded approval, which
EXT-17 does not grant for any production action.

## 2. Reconciled candidate

| | Commit | Tree |
|---|---|---|
| Production baseline (installed, untouched) | `3652a3702dae8d2917b91b6949da580efd3ef496` | `3bfc1c842bf76e8a95356f4f0f3a91e75767992c` |
| Replayed qualified delta (mechanical, no edits) | `7cda0a9dfc0cc54d651761ab7baaf6f8b80b78c2` | — |
| **Corrected candidate (this document)** | **`895c4091b1014f46584eb22bdca33fc2af5046c5`** | **`5a22a70a0291db54dd82df9a089238c9c34b711d`** |

Branch `restore002/ext17-r13-r11`. `3652a370` is an ancestor of the candidate and the distance is
**2 commits**, so the release's single fast-forward merge is still structurally available. Working
tree clean.

File digests of the corrected package (candidate tree):

```
f4a603ca17660d2b8e21e56c582349769734ea6cba56168adbeba684fe7652c0  harness/release002_preflight.py
78e9d5cb7bdad7ede1f5ae5ce2260471f4e36689e90662f6153833070994ffc1  harness/release002_release.sh
fc750eb14ca708b17d29ef3764b02ebb5d7fae54f29791a37e6b59b0f6642188  harness/hermes-release-002
ec0a9528e6ca210ec5ca7b52f985150292b4d5e84e7f327407b6c89830fb7ba5  tests/harness/test_release002_ext17.py
```

### Why the candidate is built this way

`e05128fe` (the RESTORE-001 installation and the previous candidate's baseline) **is not an ancestor**
of the installed tree: production's `main` was reset to upstream `origin/main` on 2026-11-08 and four
local commits were replayed onto it, while the RESTORE-002 harness lineage was not. Rebasing the old
branch was therefore impossible without rewriting production history — prohibited. The candidate is
instead the qualified release delta, `git diff --binary e05128fe 3c3e24a5`, applied to `3652a370` with
`git apply -3` (commit `7cda0a9`, no hand edits), with the R-13/R-11 corrections on top.

### Reconciliation findings against the 2026-11-09 upgrade

* **The engine is absent from production.** `harness/release002_preflight.py`, `release002_release.sh`,
  `release002_control.py` and `tests/harness/` do not exist at `3652a370`. The candidate re-adds them:
  of the 42 files it touches relative to production, **35 are pure additions** and only **6 are
  modifications** (the six that carry the 25 deleted lines below).
* **Total deletions against production: 25 lines, every one accounted for** — 11 in
  `gateway/restart.py`, 4 in `gateway/run_shutdown.py`, 1 in `gateway/run_startup.py`, 6 in
  `tests/gateway/test_cron_drain_floor.py`, 1 in `tests/gateway/test_shutdown_forensics.py`, 2 in
  `tests/hermes_cli/test_gateway_service.py`.
* **`gateway/restart.py` is additive on the stop path.** Production carries
  `resolve_systemd_timeout_stop_sec()` (unit generation) and its forensics consumer, but the
  attribute the *stop* path would read, `runner._systemd_timeout_stop_sec`, **appears zero times in
  the whole production tree** (`git grep -E '\b_systemd_timeout_stop_sec\b' 3652a370 -- gateway/`
  → no hits), so the probe value never reaches a live stop and no cap applies. The candidate sets it once at boot (`gateway/run_startup.py:1053`) and adds
  `systemd_stop_budget`, `resolve_stop_drain_budget`, the fail-closed `managed-no-budget` floor and
  the two caps. No duplicate definitions; the attribute is written in exactly one place.
* **The 30 s → 120 s headroom change is a real behaviour change and is disclosed.** For the shipped
  180 s drain it generates `TimeoutStopSec=300`, which is what the installed unit already runs
  (`TimeoutStopUSec=5min`), so unit regeneration does not change the live stop timeout. The two
  production tests that assert the old numbers were updated with the new arithmetic (not weakened:
  90 + 60 + 30 → 60 + 10 + 120, and 60 → 120).
* **The launchd warning in `gateway/run_shutdown.py` is not lost.** It is generalised to
  launchd-or-systemd while keeping `_launchd_exit_timeout_s` and the launchd branch; macOS behaviour
  gains a systemd branch, it does not lose one.
* **No replayed-commit change was silently reverted.** The removals above are exactly the lines that
  the delta rewrites (`_stop_requested_by_signal` early-returns became signal-gated caps; the
  launchd-only warning string became the dual-source one; the one-line import became a three-name
  import). Nothing from *carry the operator recovery hold into the release tree* or *read
  TimeoutStopSec from the unit that is actually loaded* is dropped.

## 3. R-13 — the quiet interval must measure work, not liveness

**Defect.** `quiet_window_elapsed` was `now − gateway_state.updated_at ≥ 120 s`. `updated_at` is the
running gateway's liveness heartbeat: the gateway rewrites it every few seconds whether or not any
work exists. RECON-01 recorded it 2 s old *inside attempt 3's own freeze*, on all nine samples across
180 s, with `active_agents == 0`, `live_admissions == 0` and no cron work in flight. The gate was
unsatisfiable by construction on a healthy host; attempt 3 aborted `ABORTED_SAFE` for that reason
alone, and the same candidate could never have reached mutation.

**Correction.** Two questions, two instruments, no substitution:

* `gateway_heartbeat_fresh` — `heartbeat_age ≤ 180 s`, where the age is
  `now − gateway_state.updated_at`. Freshness is now only a liveness cross-check: it proves the
  counters read in the same sample are current, and it still fails closed if the state file is
  unreadable or stale.
* `quiet_window_elapsed` — dated from **work**, not from liveness. `session_state()` now reports
  `newest_activity_epoch`, `newest_activity_session` and `newest_activity_age_s`: the newest
  `sessions.last_activity_at` across **every** session, open or ended. The interval is measured from
  the later of that instant and the freeze epoch, which can only understate idleness, and is
  evaluable when the store's last write predates the window — the ordinary case.

`last_activity_at` is genuine work activity, not a heartbeat: `hermes_state_sessions.touch_session_activity`
advances it monotonically (`… WHERE last_activity_at IS NULL OR last_activity_at < ?`) from
`AIAgent._touch_activity` during a turn, on API/tool/compaction events, and
`clear_session_activity_labels` keeps it at turn end. A row with `last_activity_at IS NULL` falls back
to `started_at`, which is older, i.e. conservative.

**Fail-closed behaviour preserved.** An unreadable store, or a state with neither a work stamp nor a
freeze epoch, yields `None` — unevaluable, never "assume idle". A future-dated stamp yields a negative
interval and fails the gate. The raw epoch is preferred over the derived age so a slow sample cannot
inflate the interval.

**Limitation, stated plainly.** The work clock sees *recorded* activity. `_touch_activity` is
rate-limited, and a single long provider call could in principle exceed the window without writing;
the in-freeze gate set still requires `active_agents == 0` **and** `live_admissions == 0` **and**
`cron_inflight_zero` in the same sample, which is what actually holds admission shut. The quiet
interval is corroboration of quiescence, not its sole proof.

## 4. R-11 — the envelope was inert, and a signal abandoned the attempt

**Defect, measured on this host (systemd 257, disposable `ext17-*` transient units, nothing else
touched):**

| Unit type | `RuntimeMaxSec=5s`, body `sleep 30` | Observed |
|---|---|---|
| `Type=oneshot` | property accepted and reported as `RuntimeMaxUSec=5s` while loaded | ran the full **30 s**, `Result=success` — **not enforced** |
| `Type=exec` | same property | terminated at **6 s**, `Result=timeout`, `ExecMainStatus=15` (SIGTERM) — enforced |

The RESTORE-002 trigger unit had already logged systemd's own words at load:
`RuntimeMaxSec= has no effect in combination with Type=oneshot. Ignoring.` The supervisor unit was
created the same way, so the advertised 1 500 s envelope bounded nothing; only the engine's internal
per-phase budgets did.

**Second half of the defect.** Making the cap effective turns it into a SIGTERM, and the engine used
to answer a signal by setting `PHASE=interrupted` and exiting 70 with `interrupted` in `on_exit`'s
early-return list — leaving the drain marker and ESTOP **engaged with no journal settlement**, i.e. a
worse state than a hang, and one that would make the next attempt read a stale freeze.

**Correction.** Two changes, both in the release package:

* `harness/hermes-release-002` creates the supervisor with `--property=Type=exec` (keeping
  `RuntimeMaxSec`, `TimeoutStopSec=420`, `KillMode=control-group`, `SendSIGKILL=yes`). `Type=exec`
  preserves one-process-run-to-exit semantics and makes the cap land.
* `harness/release002_release.sh` gains `on_signal()`: a signal in an already-settled phase
  (`rollback|done|recover|aborted`) exits without touching state; anywhere else it marks the phase
  `interrupted` and falls through to the same settlement as any other failure — `rollback` if the
  install was proven changed, otherwise release **only this run's** freeze and journal
  `ABORTED_SAFE` before any mutation. `interrupted` is removed from `on_exit`'s early-return list.

**Not corrected here, by scope.** The four-hour *trigger* unit is created by
`/usr/local/lib/hermes-release/qw-launch-apply.sh`, which is outside the repository and outside this
authority; it carries the same inert `RuntimeMaxSec`. Any future arming must add `Type=exec` there
too. No budget was weakened: 180 s quiescence, 45 s drain ack, 240 s startup, 60 s unfreeze, 300 s
effective stop and the 4 h deadline are all unchanged — the envelope number is now real rather than
smaller.

## 5. Tests

New file `tests/harness/test_release002_ext17.py`, 16 tests. Behavioural where the code is
behavioural (both gate helpers, the session work clock through a stubbed pinned planner); pinned-source
where the artefact *is* source (a systemd property, a bash trap) — a stub cannot prove what systemd
does with a unit, which is why §4 carries the measured table instead.

* **RED, at the uncorrected delta `7cda0a9`:** `15 failed, 1 passed`. The single pass is the
  "budgets must not shrink" guard, which is a non-regression assertion and is expected to pass both
  before and after.
* **GREEN, at the corrected candidate:** `tests/harness` → **338 passed, 0 failed** (all 16 new tests
  included), `/usr/bin/python3.13`, `-p no:hypothesispytest`. The run's process exit code is 1 because
  the pytest **session teardown** (`tests/conftest.py` → `import psutil` at exit) finds no `psutil`
  for the system interpreter; it happens after the summary line and is not a test failure.
* Gateway stop-path subset (`test_stop_budget_adaptation`, `test_stop_budget_probe_failclosed`,
  `test_cron_drain_floor`, `test_shutdown_forensics`, `test_admission_freeze_gate`,
  `test_session_resume_plan`) → **127 passed, 1 skipped, 2 errors**. Both errors are the live-system
  guard in `test_shutdown_forensics.py` (`TestArgvFreePersistence`, `TestSpawnAsyncDiagnostic`) and are
  **identical at production `3652a370`** — caused by `psutil` being absent for the system interpreter,
  not by this change.
* `tests/hermes_cli/test_gateway_service.py` → **112 passed, 3 skipped, 1 failed**
  (`test_system_unit_remaps_caller_home_ld_library_path_components`, `PermissionError`), **identical at
  production `3652a370`** — the test needs a system path it cannot write as this user.

Neither pre-existing failure is repaired here: both reproduce unchanged on the untouched production
tree, and neither touches R-13 or R-11.

## 6. Adversarial self-review — what a reviewer should attack

1. *Did the 3-way apply revert any post-baseline work?* No: all 25 deleted lines are enumerated in §2
   and each is a line the delta itself rewrites.
2. *Does the new quiet clock let a stop through while a turn is running?* It cannot on its own —
   `active_agents`, `live_admissions` and `cron_inflight_zero` remain conjunctive and critical in the
   same sample; the window is corroboration. The rate-limited `_touch_activity` case is disclosed in §3.
3. *Can the gate pass on a host whose session store predates the window?* Yes, by design: the freeze
   epoch becomes a floor, so the release waits at least the window after the freeze regardless of
   store age. That is the intended semantic, and it is the one attempt 3 could never satisfy.
4. *Does `Type=exec` change supervisor exit semantics?* It reports the same `ExecMainStatus`, with one
   difference that matters: with `--collect` the unit is unloaded promptly, so post-mortem inspection
   must read the archived log and the journal rather than `systemctl show` after completion.
5. *Is the interrupted settlement safe under `set -e`?* `on_exit` runs under `set -e`, so a failing
   settlement command could truncate the rest of the settlement. This pre-dates EXT-17 and is
   **not** changed here; flagged as a residual item for the CA in §7 rather than silently widened.
6. *Are the identity constants right?* `DEFAULT_INSTALL_*`, `EXPECT_HEAD`, `EXPECT_TREE` and the
   `restore002_tree_audit` constant now carry `3652a370…` / `3bfc1c84…`, verified by
   `git rev-parse 3652a370^{tree}`. The manifest still pins them at packaging time, and a mismatch
   remains a hard stop.
7. *Does anything else consume the gate name or the old detail text?* Only `harness/release002_driver.sh`
   lists `quiet_window_elapsed` among four gate names; it is the superseded driver, is not on the
   launcher path, and reads the JSON verdict rather than the stderr detail.

## 7. Open items for the External CA

1. **Deployment is not possible from this candidate as published.** The engine refuses when the
   candidate commit is absent from the install repository; publishing to `fork` does not put it
   there, and fetching it into `/home/hermes/.hermes/hermes-agent` is a production-repository write.
   A future authority must name (a) the fetch of the candidate ref into the install repository and
   (b) a fresh `bootstrap`/`stage` into `/usr/local/lib/hermes-release` for the new candidate tree.
   Neither was done.
2. **The `install_head` gate of the previous release authority is stale** — it expects `e05128fe`.
   It is superseded by `3652a370` here; the old package document keeps its historical text, and this
   document supersedes those specific identities.
3. **`on_exit` settlement is not `set -e`-hardened** (§6.5). A settlement command that fails mid-way
   can truncate the settlement; making that robust is its own change with its own review.
4. **The trigger unit remains `Type=oneshot`** (§4), outside the repository.
5. **`restore002-qw-launch` remains spent and must not be re-armed** with either candidate; the
   journal, the 4 h deadline and the attempt identity from `20261107T222858Z` are untouched.

## 8. What was *not* done

No APPLY. No second attempt. No re-arm. No gateway restart. No systemd unit created, edited or
reloaded outside the disposable `ext17-*` probe units (all collected; `systemctl list-units 'ext17-*'`
returns nothing). No write to `/usr/local/lib/hermes-release` or `/var/lib/hermes-release`. No
session, database or journal write. No bootstrap or stage. PR #9 remains **draft and unmerged**. No
recovery-hold or drop-in change. No credential or governance change. Production remains
`3652a370` / `3bfc1c84`.
