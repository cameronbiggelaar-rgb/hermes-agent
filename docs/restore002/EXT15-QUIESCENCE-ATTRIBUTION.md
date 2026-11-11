# EXT-15 — Why quiescence could not be reached, and the safe way to get a maintenance window

Evidence base: PR #9 at `865928f255d84b45f37042ac880bf42e60089d97` / tree
`f2cdd53bdef4b37ac325d3e3879851f74b61a2e3`; authorised candidate
`16a4d39a418f16ebac8eeb27ce7585158ce9e8ef` / tree `3ff43dc4aed41d3cc8bbe8df527aedbd6b77466f`;
installed baseline `e05128febbc559469a5fb835a9235a543dfdfa64` / tree
`dc05abd074c834569cf19ca89a6446d74be5c41d`. Production was not touched: no deployment, no gateway
restart, no freeze, no session write. Every database read used
`file:…?mode=ro` + `PRAGMA query_only=1`.

Authority for this work: *read-only investigation, isolated testing and non-production PR
corrections only*. The release-sequence change in §5 is **prepared, not qualified and not
authorised**; it requires External CA review and a new owner approval before any use.

---

## 1. Bottom line

The driver could not establish quiescence because **the approved sequence asks for quiescence
before the only mechanism that can produce it is engaged.**

`apply` evaluates the operational preflight — whose critical gate `no_active_turns` requires
`gateway_state.active_agents == 0` — and refuses with `exit 2` *before* `freeze_engage`. The freeze
(drain marker + ESTOP) is engaged later, and only then does the engine run its real quiescence probe
(`ct admission`, three consecutive samples 20 s apart). The engine's own dry-run text says exactly
this:

```
# harness/release002_release.sh (plan mode)
# A dry-run runs while the gateway is still serving Telegram and cron work, so the quiescence
# gates legitimately fail here; they are re-evaluated after the freeze.
```

…while apply mode treats that same verdict as terminal:

```
# harness/release002_release.sh (apply mode)
if [ "$PF_RC" -ne 0 ]; then
  log "preflight verdict NO_GO (rc=$PF_RC) — nothing was changed"
  journal aborted "preflight NO_GO rc=$PF_RC" --result ABORTED_SAFE
  exit 2
fi
```

There is a second, independent reason the pre-freeze probe can never succeed:
`release002_control.admission_report()` — the probe the engine polls — adds
`freeze_not_engaged` to its failure reasons whenever no drain marker is present. Before the freeze,
quiescence is therefore *unevaluable by construction*: 0 active agents, 0 admissions, no freeze
still reads "not quiet". This was the shape of all 129 samples in the EXT-14 window
(`"drain_marker_active": false` in every sample, alongside `active_agents` ≥ 1).

So RESTORE-002 did not fail because the host was unusually busy. It failed because the release
sequence has no path from "a gateway serving traffic" to "a frozen, drained gateway": the gate that
demands an empty gateway is evaluated before the freeze that empties it.

## 2. Phase 1 — attribution of the persistent admissions

### 2.1 The three sessions named in the reconciliation receipt

| session | source | chat / thread | title | msgs | API calls | epoch-23 admissions |
|---|---|---|---|---|---|---|
| `20261101_113736_7565f3f9` | telegram | 8849200240 / 280402 | Qualify AI assurance Issue #118 | 154 | 5111 | 61 terminal (32 completed, 29 cancelled) + 2 live |
| `20261103_111647_bd71d22f` | telegram | — | EXT-15 recovery session | — | — | 24 terminal (23 completed, 1 interrupted) + 2 live |
| `20261105_083311_65e056f7` | telegram | — | — | — | — | 22 terminal (completed) + 3 live |

"Live" = `queued`/`started` rows still non-terminal **and** the session demonstrably executing in
`agent.log` at the time of the probe. The counts above are the state at 16:20 local, not at 12:55.

### 2.2 The 12:34–12:55 window

Per-minute attribution from `logs/agent.log` (+`.1`), 1-minute buckets, session ids extracted per
line:

- **`20261101_113736_7565f3f9`: present in 21 of 21 minutes, 12:34 → 12:54 inclusive.**
  API calls `#251` (12:40:02) … `#364` (12:54:35) — a single turn, 3–6 s per call, no reset, no
  `Turn ended` inside the window. This is genuine, continuous model execution.
- `20261105_083311_65e056f7`: present at 12:34, 12:39, 12:40. API `#98` (12:00:03) → `#190`
  (12:34:04), then a **300 s context-compression stall** (`Context compression made no progress for
  300.0s`, 12:34:28 → 12:39:28), then a short background-review turn (`thread=bg-review`, calls
  #2–#6) and `Turn ended: reason=text_response(finish_reason=stop)` at **12:40:05**.
- `20261103_111647_bd71d22f`: no activity in the window.

**Attribution of the final admission (12:34–12:55): `20261101_113736_7565f3f9`.** Evidenced by
continuous execution across every minute of the window, on the session the driver's last samples
counted as the single remaining agent.

**What remains UNKNOWN, and is reported as such:** which *admission sequence number* owned the last
sample at 12:54:59. `session_admissions` stores no timestamp; the epoch floats embedded in some
`request_id` values cover only the completion-driven rows. The engine's own quiescence probe
(`live_admissions`) classifies by `sessions.last_activity_at` within 6 h, so it too cannot name the
owning sequence. Attribution stops at the session, which is what the evidence supports.

### 2.3 Were those three admissions stale?

No. Every non-terminal row at the time belonged to a session that was executing:

- `7565f3f9` was mid-turn for the whole window (§2.2);
- `65e056f7` was mid-turn, partly stalled in compression, until 12:40:05;
- `bd71d22f`'s rows were queued behind its own live turn.

And the lifecycle does terminalise: 107 of the 114 epoch-23 rows across the three sessions are
terminal, with **29 `cancelled`** outcomes on `7565f3f9` alone — the admission layer coalescing
overlapping turns rather than letting them pile up. The "three persistent admissions" were live
work, not leaked rows.

## 3. Phase 2 — is the gate working?

| class | verdict | evidence |
|---|---|---|
| **A. Genuine long-running execution** | **yes, dominant** | one `7565f3f9` turn spanning > 90 min; 114 API calls in the last 16 min alone |
| **B. Continuous / overlapping background activity** | **yes** | completion-driven re-admissions (`request_id` = `["completion","proc_…​",epoch]`), `thread=bg-review` turns, cron pulses inside `active_agents` |
| **C. Non-terminal after execution ends** | **not evidenced** | every non-terminal row mapped to a session executing at that moment; 107/114 rows terminal |
| **D. Incorrect active-agent accounting** | **yes, one instance** | 12:34:25 sample read `1` while `agent.log` shows *two* sessions mid-turn; see below |
| **E. Another cause** | none needed | §1 covers the structural cause |

### 3.1 D, precisely

`active_agents` is not a live counter. `agent_cache._persist_active_agents()` publishes it **"to
`gateway_state.json` at every turn boundary"** — nothing else refreshes it:

```
def _persist_active_agents(self) -> None:
    """Persist the live in-flight agent count to ``gateway_state.json`` at every turn boundary."""
    from gateway.run import _write_runtime_status_quiet
    _write_runtime_status_quiet(active_agents=self._active_work_count())
```

and `_active_work_count()` is the sum of five sources, cron included:

```
self._running_agent_count() + managed_turn_count(self) + self._active_cron_job_count()
+ self._active_api_run_count() + self._active_deferred_agent_worker_count()
```

Consequences for a gate that reads the snapshot:

1. it **lags** a turn that starts after the last boundary — at 12:34:25 the snapshot said `1` while
   two sessions were executing, i.e. it under-reported by one for ~6 minutes;
2. it **inherits cron pulses** — a 1.5 s cron job that begins and ends between two boundaries can
   leave a non-zero value behind until the next turn ends;
3. it can read `0` while a freshly admitted turn is already running (the mirror case of (1)), which
   is the only direction that would matter for safety — and it is why `live_admissions` and
   `recent_session_activity` exist as a second and third opinion.

This is an observability property, not the cause of the stop: in the EXT-14 window the value never
reached 0 while work was running, which is the correct answer.

## 4. Phase 3 — qualifying a maintenance window

### 4.1 Measured activity (2026-11-04 22:04 → 2026-11-05 16:20 local, 1097 minutes)

| metric | value |
|---|---|
| minutes with any session executing | 376 (34 %) |
| minutes with no session executing | 721 (66 %) |
| uninterrupted no-execution runs | 47 |
| runs ≥ 120 s | 31 |
| runs ≥ 180 s | 21 |
| runs ≥ 5 min | 8 |
| runs ≥ 30 min | 3 |
| longest runs | 411 min (00:39 → 07:30), 110 min (09:43), 61 min (14:25) |

On 5 November between 08:00 and 18:00: **30 runs, 15 ≥ 120 s, 11 ≥ 180 s**, but only three ≥ 5 min
(110, 61, 20 min) — the daytime host is busy roughly a third of the time, in bursts, driven by four
concurrent Telegram threads: `7565f3f9` 166 busy minutes, `65e056f7` 119, `ca2cae72` 74,
`bd71d22f` 65, `d064228f` 50, `a6b15035` 31.

### 4.2 Why the observed 43-minute window produced 0 quiet samples

The driver sampled `plan` 129 times over 43 minutes; `active_agents` went 3 → 2 (12:20) → 1 (12:34)
and stayed at 1 for the last 62 samples. The log-derived quiet runs at 12:24 and 12:55 were **not**
gate-quiet: a 2-minute gap between log lines is ordinary for a long tool call or a compression
stall, while the turn — and its admission row — stays open. **Log silence is not absence of an
active turn**, and the gate is right to say so.

### 4.3 Likelihood and sufficiency

- With 15 candidate ≥ 120 s gaps in a 10-hour day, a driver that must *catch* `active_agents == 0`
  at a 20 s sample has a workable chance only during the quiet hours (00:39 → 07:30 is a 411-minute
  window; 09:43 is 110 minutes).
- The 130 s settling period is **not** the limiter. It is longer than the 120 s activity window by
  design, and the engine's in-freeze probe needs 3 × 20 s = 60 s. Neither is what blocked.
- Background traffic **can** prevent quiescence indefinitely: cron fires every 2 minutes, and
  `active_agents` includes it. A single cron pulse landing between two turn boundaries can hold the
  snapshot non-zero until the next boundary.

### 4.4 The design — quiesce-first, using mechanisms that already exist

The engine already contains every mechanism needed. Steps 1–5 of its own dry-run runbook are the
safe sequence; the defect is that the *pre-freeze* preflight gates it:

1. archive unit + drop-ins (reversible), session fingerprint before;
2. **engage the freeze**: drain marker (`principal release-freeze:<run>`) + ESTOP, wait until the
   live gateway reports `gateway_state=draining`;
3. **quiesce inside the freeze**: three consecutive `ct admission` samples 20 s apart — zero active
   agents *and* zero live admissions, with the freeze held;
4. revalidate install/candidate/unit/drop-in identities immediately before the first mutation;
5. one `merge --ff-only`, one timed restart, post-verify, release the freeze.

The change required is exactly one thing: in apply mode, a pre-flight whose **only** failed gates
are the quiescence pair (`no_active_turns`, `recent_session_activity` — the latter already
informational) must not be terminal; it must defer to step 3, which evaluates the same condition
with the freeze held and with a live admission count. Any other critical gate failure still aborts
before mutation, exactly as today.

Why this preserves every property EXT-15 requires:

- **drain/ESTOP semantics** — unchanged: the same `freeze_engage`/`freeze_release`, same marker
  principal, same ESTOP;
- **admission protection** — strengthened in effect: admissions are refused from step 2, not step 5,
  so no new turn can arrive between the quiescence check and the mutation (today's ordering checks
  quiescence *before* refusing new work, a TOCTOU window that this removes);
- **active-turn completion** — nothing is killed: the freeze stops admission, and in-flight turns
  finish; the 60 s probe and `QUIESCE_WAIT` budget bound the wait, and a timeout still aborts
  before mutation (`ABORTED_NOT_QUIET`);
- **existing session records** — untouched; the engine's session inventory is read-only and this
  change adds no writes;
- **scheduled-work integrity** — cron is deferred by the freeze and runs after release; nothing is
  deleted, disabled or skipped.

The change is a **release-sequence change**. It is therefore prepared as a separate candidate and
must pass External CA review and receive a new owner approval before it is used. It is not
implemented on any default path in this PR.

## 5. Phase 4 — closing the driver observability gap

`harness/release002_driver.sh` (new) replaces the EXT-14 driver whose receipt writer existed only
after APPLY. Its contract, enforced by structure rather than by discipline:

- **every terminal result writes `summary.json` + `RECEIPT.md` exactly once** — deadline stop, PLAN
  UNEVALUABLE, guard failure, SIGTERM, apply failure, success;
- **publishes at most once**, and a failed publication never loses the durable receipt (the
  finalised marker bounds republication on a re-run);
- never shortens the quiet window, never aborts a turn, never retries a mutation, never applies
  without PLAN GO;
- each sample records the published runtime snapshot (`active_agents`, `updated_at`) *beside* the
  PLAN verdict — the EXT-14 window could not be attributed after the fact precisely because the
  verdict truncated those facts;
- each sample classifies its blockers: `quiescence` when the only failed gates are the quiescence
  pair, `other` (named) otherwise. Classification is evidence only — it never turns a NO_GO into a
  GO.

Tests (`tests/harness/test_release002_ext15.py`, 18) run the real driver against stub
`git`/`systemctl`/`sudo`/`gh` and a stub launcher: deadline stop, plan-mode GO, apply with a settle
re-check, exactly-one apply, UNEVALUABLE → NEEDS_HUMAN, four guard failures, SIGTERM, at-most-once
publication, and the two blocker-classification cases.

```
tests/harness/test_release002_ext15.py   18 passed
tests/harness (full)                    247 passed in 73.20s
```

## 6. What this does not claim, and what is left

- No production action was taken: no deployment, no restart, no freeze, no session write, no merge.
- The release-sequence change in §4.4 is **prepared only** — no code path in this PR adopts it by
  default, and using it requires a new owner approval plus External CA review.
- The owning admission sequence for the last sample at 12:54:59 stays **UNKNOWN** (§2.2); the
  owning *session* is proven.
- Two pre-existing properties are recorded but not changed here: `active_agents` is a
  turn-boundary snapshot (§3.1), and `live_admissions` classifies staleness by a 6-hour
  `last_activity_at` threshold — which is a time-based rule, and therefore not a basis this
  investigation uses to call any admission stale.
