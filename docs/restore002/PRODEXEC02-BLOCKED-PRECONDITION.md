# PRODEXEC-02 — BLOCKED PRECONDITION

**Mission:** `RESTORE-002-PRODEXEC-02`
**Disposition:** `PRODEXEC-02_BLOCKED_PRECONDITION`
**Recorded:** 2026-11-12T10:20Z
**Production mutated:** no. No staging, no install, no arming, no restart, no database write.

---

## 1. Summary

Every §3 identity, ancestry, baseline, service and evidence-preservation prerequisite was established.
One prerequisite could not be: **the qualified trigger v2.1 can never report a quiet window against this
production store**, because three unresolved admissions from epoch 1 (2026-10-18) are permanently
undatable and the qualified trigger classifies undatable stale-epoch rows as blocking.

The blocking state is not host activity. It does not drain, it is not a quiet-window timing problem, and
waiting cannot resolve it. Each remedy that would clear it is a boundary this mission explicitly forbids.
Arming the single attempt would therefore spend it on a guaranteed deadline disarm.

Per §3 — "If any prerequisite cannot be established, stop before mutation and report
`PRODEXEC-02_BLOCKED_PRECONDITION`" — and §4 — "Do not widen timeouts, weaken gates or manufacture a
quiet window" — the release stopped before mutation.

---

## 2. Identities (all verified, all matching)

| Item | Expected | Observed | Result |
|---|---|---|---|
| Candidate commit | `62359402a6e35fb14e01462fe9bf233de982c9aa` | present in tree, ancestor of docs head | match |
| Candidate tree | `622ff89cf77be93d2fe8834ba2863ad546b11f08` | `git rev-parse 62359402^{tree}` | match |
| Installed baseline | `3652a3702dae8d2917b91b6949da580efd3ef496` | `main` HEAD, clean | match |
| Baseline tree | `3bfc1c842bf76e8a95356f4f0f3a91e75767992c` | `git rev-parse 3652a370^{tree}` | match |
| Documentation head | `c45aa824fcc6412820a6100ac3985623ec2e7115` | present, not used as candidate | match |
| Docs-head tree | (not pinned) | `9ffa187c9b72b5dc71e618b5b3e312e438e548e1` | informational |

Manifest digests recomputed at the candidate (`git show 62359402:<path> | sha256sum`):

```
f00828df6436956486cabe3886f66c6f3aac475a6bb37fae8cd1d2767f01d356  harness/hermes-release-002
413c1b9a1b285b2724dfe72256d8ca2a3d569ab66d9f3c2fdcbf7ad9599a0090  harness/release002_control.py
1b17ec849cce80360eb75ad49daa6095bb0447e76de32f897c4b48cf71fec0d4  harness/release002_oldgate.py
0743827d6e7ad741c3a394054f989d29b5f87a485dffd7bf906faaeccc18cda1  harness/release002_preflight.py
4edf7e117751aa98c27dc9942b424c101c452f6d0fd74902c6e4670f29444b2a  harness/release002_release.sh
44391930344c10b5293b500094d5b9230655111bbba942f5998d613b76a4a201  harness/restore002_session_inventory.py
569ff5a26a516a3eca9c3f358d3e9d2c15dba7c9fa328f2c6fe71c582490d9eb  harness/trigger/qw-launch-apply-v2.sh
```

Qualified trigger v2.1: 12,626 B, `569ff5a2…`, one textual `immutable=1` occurrence (the comment that
forbids it) and four `mode=ro` reads. Not installed.

---

## 3. §3 prerequisite matrix

| # | Prerequisite | Result | Evidence |
|---|---|---|---|
| 1 | External CA Operating Contract retrieved and identified | **met** | v1.0, `governance/EXTERNAL_CA_OPERATING_CONTRACT.md`, sha256 `ab02df4af92f139a0aa4ee4a26c9c3a6e3bf1f67e289986ab7ad987a43101427`; retrieved at branch head `069364d0` (one commit ahead of the pinned `4d421e2b`), contract file byte-identical at both refs (empty diff, same digest); matches `attempt-id.json` `governance_commit`/`governance_sha256` binding |
| 2 | Candidate SHA, tree, manifest digests, ancestry | **met** | §2 |
| 3 | Installed baseline HEAD/tree, clean worktree | **met** | `3652a370` / `3bfc1c84`, `dirty=0`, branch `main`, `v0.23.0+canary.20261108T064411Z-93-g3652a3702d` |
| 4 | Gateway health, service identity, protective controls | **met** | `active`/`running`, MainPID `3691490`, `NRestarts=1`, `TimeoutStopUSec=5min`; three drop-ins present (`90-recovery-hold.conf` `ba4a1c11…`, `95-timeout-stop.conf` `bb1844fd…`, `memory-limit.conf` `1d59fa70…`) |
| 5 | No competing release process, supervisor or trigger | **met** | `grep -rl qw-launch /etc/systemd/system/` → 0 files; no release/restore unit or timer; no competing process other than this session's own read-only preflight; historical launcher lock present, unheld. (`biggie-llm-router-release.service` is running and unrelated: separate product, separate checkout.) |
| 6 | Historical journal `ABORTED_SAFE`, `mutated=false` | **met** | `journal.json` sha256 `149a701c916c9ef4e6a33f8608121ce3c6a8d93fd104ea3f66f629f40d55d143`, `result=ABORTED_SAFE`, `mutated=false`, `run_id=20261107T222858Z-3c3e24a50bfc` |
| 7 | Historical journal and attempt record preserved with original identities/checksums | **met** | journal `149a701c…`; `attempt-id.json` `d265846b…`; `attempt-fired.json` `1eef7049…`; `RESUME-NOTE.md`, `qw-launcher.log` untouched (mtimes 2026-11-08, read-only copies taken) |
| 8 | New attempt has its own distinct identity and durable state | **not established** | no new attempt was created; creating one is only meaningful if it can fire (§4) |
| 9 | Trigger sampling reconciled against engine quiescence | **met, with a divergence finding** | §4.1 |
| 10 | Trigger v2.1 uses WAL-aware admission observation, digest matches | **met as an artifact, fails as a gate** | `569ff5a2…` matches the manifest; `mode=ro` observation bound to `runtime_epoch`; classification is **permanently non-quiet** — §4.2 |

---

## 4. Blocking finding

### 4.1 Reconciliation (§3.9)

| Quantity | Trigger v2.1 | Engine (`release002_control.py`) |
|---|---|---|
| Window | `NEED=6` × `INTERVAL=20 s` = 120 s | `QUIESCE_MAX_SECONDS = 180` — three consecutive zero samples, 60 s apart |
| Deadline | `DEADLINE_S = 14400` (4 h) | bounded by freeze/drain budgets |
| Stale horizon | `ADM_STALE_HORIZON_S = 21600` (6 h) | `STALE_ADMISSION_HOURS = 6.0` |
| Admission scope | current epoch **plus** ahead, stale-recent, undated | current epoch only |

The trigger window (120 s) is shorter than the engine's quiescence window (180 s), so a trigger fire does
not guarantee the engine's gate: the engine re-samples independently and aborts safely, as it did
historically (`quiet samples=0`, `not quiet within 180s`, `ABORTED_SAFE`). The trigger's admission scope is
a strict superset of the engine's — fail-closed, no weakening in either direction.

### 4.2 The gate cannot pass

The qualified trigger reaches `quiet` only when `live = ahead = stale_recent = undated = 0`
(`qw-launch-apply-v2.sh:175-181`). The qualified SQL (1,311 chars, extracted verbatim to
`admission-sql.txt`, executed exactly as `observe_admissions` executes it) returns, on three consecutive
read-only samples:

```
24|4|0|3|3|0        # epoch|live|ahead|stale_recent|stale_expired|undated
24|4|0|3|3|0
24|4|0|3|3|0
```

`live` fluctuates with real agent turns (3–5 observed). `stale_recent = 3` does not. Those three rows:

| seq | status | owner_epoch | admission_id | session | source | started_at | last_activity_at |
|---|---|---|---|---|---|---|---|
| 101 | `unknown` | 1 | `40a8ac83611e428696d5fee98e4bc599` | `local-7d6f5dde…4ed901` | oneshot | 2026-10-18T23:24:12Z | **NULL** |
| 102 | `unknown` | 1 | `faf7f3066aca401d8b5c309c5d2f397d` | `local-da851133…e4f4d5` | oneshot | 2026-10-18T23:24:14Z | **NULL** |
| 103 | `unknown` | 1 | `e7aa5bc9b3204511846ad644a3279063` | `local-efa627be…5b44187` | oneshot | 2026-10-18T23:24:19Z | **NULL** |

The SQL's `stale_recent` bucket counts a stale-epoch row when `s.last_activity_at is null OR` activity is
within 6 h. A row whose session never recorded activity therefore falls in `stale_recent` **permanently** —
it can never age into `stale_expired`, which requires a non-NULL `last_activity_at` older than 6 h.

This is the intended fail-closed rule from EXT-17C §1: an undatable admission cannot be declared provably
dead, so it must refuse quiet rather than be written off. The rule is correct. This store violates it.

### 4.3 Permanence

- The three rows were written in epoch 1; the store is now at epoch 24 — 22 distinct `owner_epoch` values,
  3,256 admissions reached `terminal` after epoch 1, max `seq` 4050. The store cycles normally around them.
- `hermes_state_runtime.py:463` converts old-epoch `started` rows to `unknown` at rollover. It never
  converts `unknown` to `terminal`.
- `gateway/session_settlement_recovery.py` settles an `unknown` row only for the **live owner** of that
  generation, guarded by `current['owner_epoch'] == authority.epoch` — inapplicable to epoch-1 rows.
- `hermes_state_runtime.py` states the rule directly: "Nothing moves while the old FIFO holds an `unknown`
  row (the operator's to resume)." `unknown` is operator-resolved by design.
- Consequence: `stale_recent ≥ 3` for as long as those rows exist, through any number of epoch increments
  and any gateway restart. Each quiet sample would be `quiet=0/6`; at `DEADLINE_S` the trigger disarms with
  `attempt unspent`.

---

## 5. Why no permitted remedy exists inside this mission

| Remedy | Effect | Boundary that forbids it |
|---|---|---|
| Mark the three rows `terminal` / delete them | clears `stale_recent` | §2 "Manual session/database repair or deletion"; §3 "Do not remove historical evidence merely to make the new trigger arm" |
| Patch the trigger to exempt `oneshot`/undated rows | clears `stale_recent` | §4 "Install the exact qualified trigger v2.1 and verify its checksum"; "Do not widen timeouts, weaken gates" |
| Reset or rebuild the state database | clears the rows | §2 "Manual session/database repair or deletion"; production overwrite/reset is prohibited at all times |
| Wait for the rows to drain | none — no code path drains them | §4 "Do not … manufacture a quiet window" |
| Arm anyway and let the deadline disarm | spends the single attempt on a guaranteed no-fire | §4 "Arm one attempt"; §3 stop-before-mutation rule; EXT-17B/17C doctrine that a case which cannot exhibit its own precondition must fail |

---

## 6. Production state at stop (unchanged)

```
installed trigger   /usr/local/lib/hermes-release/qw-launch-apply.sh
                    sha256 cfd2fcc16c50761e1c352043345627dc85be1c8a454507601708b8d08fe60d19
                    3592 B  mode 700 root:root  mtime 2026-11-07 07:42:10 +1100   (v1, still active)
journal.json        149a701c916c9ef4e6a33f8608121ce3c6a8d93fd104ea3f66f629f40d55d143
attempt-id.json     d265846bc1b2f1292f7ee19ff35f4a59c4e891fccdb3d53fecd56b00acff04f3
attempt-fired.json  1eef7049c69f51d8a8c953058b96119354fadf962e6df4b83fd7b8753d36804a
gateway             ActiveState=active  MainPID=3691490  NRestarts=1
recovery hold       drop-in 90-recovery-hold.conf present (ba4a1c11…)
drop-ins            95-timeout-stop.conf (bb1844fd…), memory-limit.conf (1d59fa70…)
work tree           c45aa824…, dirty=0
```

No staging was performed: `/usr/local/lib/hermes-release/manifest.json` is still the historical one (sha256
`6681d5dcdc3f6c6a9431547e2b75efbb8cd46940f67122613f624ac0d19326dd`, 2,528 B, mtime 2026-11-07 06:57), and
`engine/` is unchanged. Nothing was armed. No `apply`, no
restart, no systemd change, no database write of any kind: every database access in this mission was
`mode=ro` with `PRAGMA query_only=1`.

---

## 7. What would unblock the release (owner decision, outside this mission's authority)

1. **Authorised reconciliation of the three orphaned `unknown` admissions** (mark `terminal` with a recorded
   reason, preserving the rows and their identities) — a database write requiring explicit authority,
   preferred because it resolves the store's actual inconsistency without touching the qualified instrument.
2. **Authorised revision of the trigger's stale-epoch rule** (for example: an undatable row blocks until an
   operator attests it) — changes the qualified artifact, so it requires requalification and a new digest;
   the current digest `569ff5a2…` could not be installed under §4.
3. **Authorised amendment of the mission** to permit arming with a known-permanent `stale_recent` (not
   recommended: it spends the single attempt on a no-fire and cannot produce an operational outcome).

Option 1 is the only one that unblocks without weakening the gate that EXT-17C established.

---

## 8. References

- `harness/trigger/qw-launch-apply-v2.sh` — qualified trigger v2.1, `569ff5a2…`, 12,626 B (SQL at 137-158,
  quiet predicate at 175-181).
- `harness/release002_control.py` — `QUIESCE_MAX_SECONDS=180` (74), `STALE_ADMISSION_HOURS=6.0` (716),
  `live_admissions` (719).
- `hermes_state_runtime.py` — epoch rollover `started→unknown` (463); `unknown` is operator-resolved.
- `gateway/session_settlement_recovery.py` — owner-epoch-guarded settlement of `unknown`.
- Evidence: `out/preflight3.log` (§3 checks), `out/admission-sql.txt` (extracted SQL), `out/classify.log`
  (per-row bucket classification), `out/blocker-evidence.log`, `out/blocker-evidence2.log` (three-sample
  observation), `out/journal-historical.json`, `out/attempt-id.json`, `out/attempt-fired.json`.
