# EXT-17C — release readiness closure

Mission: `RESTORE-002 / EXT-17C RELEASE READINESS CLOSURE`. Authority: read-only production checks,
isolated corrections, testing, commits and draft-PR evidence. No production staging, deployment,
systemd change, restart, trigger activation or merge. `EXT-17B` integration qualification is accepted
and is not re-litigated here; this document closes the five release-readiness concerns that remained
open after it.

All production statements below were produced by read-only commands on the host. The installed
trigger's digest was recomputed on the host at the time of this document; no digest is copied from an
earlier document or receipt.

---

## 1. Trigger admission freshness (`immutable=1` vs a live WAL)

### 1.1 The instrument under test

The quiet-window gate is the only thing standing between "arm the single attempt" and "spend it on a
busy host". Before this mission it read admission state twice per sample as:

```
sqlite3 "file:$DB?immutable=1" "<count query>"
```

`immutable=1` is a promise to SQLite that the database file will not change, so SQLite skips locking
and — decisively — never opens or consults the `-wal`. The gateway store runs `journal_mode=wal`
(recomputed on the host: `wal`, §4). A commit that has not yet been checkpointed lives only in the
`-wal`, so it is invisible to an `immutable=1` reader.

The consequence is not a rare race. The window samples every 20 s for up to 180 s and needs three
consecutive zeroes; every sample taken between checkpoints sees the pre-admission state, so a host
that has just admitted a session looks quiet for as long as the WAL stays uncheckpointed.

### 1.2 Probe — a committed admission that is not checkpointed

`wal_probe2.py` builds a store with the production DDL, sets `journal_mode=wal`, disables
autocheckpoint, holds the writer connection open, and commits one live admission **without ever
checkpointing**. It then reads with both instruments. `out/wal_probe2.log`, SQLite 3.46.1:

| step | store state | `immutable=1` | `mode=ro` |
| --- | --- | --- | --- |
| S1 | baseline, 0 admissions, checkpointed | `0` | `0` |
| S2 | live admission committed, `-wal` = 12,392 B, writer open | `0` **stale** | `1` |
| S3 | three-sample window with the trigger's own loop | `quiet 3/3`, `would_fire=true` | `quiet 0/3`, samples `1,1,1` |
| S4 | epoch advanced 9→10, two queued rows still at epoch 9 | live `0`, epoch `9` **both stale** | live `1`, epoch `10` |
| S5 | main file copied without its hot `-wal` | `0` | `0` (agrees, and both are wrong) |
| S6 | hot `-wal`, `-shm` absent and not creatable by the reader | `0` | `1` |

Three findings, each of which shapes the correction:

1. **The stale read is real and it reaches the decision**: S3 shows the pre-mission instrument
   reaching `quiet 3/3` — i.e. firing — over a store with a live, committed, uncheckpointed admission.
2. **`immutable=1` also misreads the epoch** (S4), which matters because the count is bound to it: a
   window keyed on a stale epoch silently discards the admissions that belong to the current one.
3. **Agreement between instruments is not truth** (S5): against a store copied without its `-wal`,
   `mode=ro` also returns `0`. A store that cannot be shown to be the live one must not be read as
   quiet, which is why unreadable or ambiguous state is a refusal in §1.5 rather than a fallback.

### 1.3 End-to-end proof inside the qualification namespace

`harness/qualify/ext17b_case.sh wal-live` drives the assembled path: it stages the candidate, forces
the fixture store into `journal_mode=wal`, commits one live admission with the writer held open, waits
until frames exist in the `-wal` (8,272 B — the case fails if they do not), and arms the corrected
trigger with `need=2 interval=1s deadline=8s`:

```
ARM v2.1 ... need=2 interval=1s deadline_s=8
admission observation mode=ro epoch=1 live=1 ahead=0 stale_recent=0 stale_expired=0 undated=0 wal_bytes=0
sample agents=0 admissions=busy epoch=1 live=1 ... wal_bytes=8272 quiet=0/2     (eight samples, 07:06:05→07:06:12)
DEADLINE: no quiet window within 8s — disarmed without applying (attempt unspent)   → exit 3
```

No `attempt-fired.json` was written (`armed-despite-busy=0`). The same row is then marked terminal,
and on the next arming the same store reads `quiet 2/2` and the handoff happens exactly once —
`apply rc=0`, `attempt-fired.json`, journal `COMPLETE`, receipt `RELEASED_VERIFIED`. So the gate
refuses over live work **and** still fires over a genuinely quiet store; it is not a blanket refusal.

### 1.4 Correction

`harness/trigger/qw-launch-apply-v2.sh` (v2.1) replaces both `immutable=1` reads with one `mode=ro`
observation, which participates in WAL indexing. `git diff 663c11dd..HEAD -- harness/trigger`:
**70 insertions, 7 deletions**, 12,626 bytes; `grep -c 'immutable=1'` = 1 (a comment explaining why it
must not be used), `grep -c 'mode=ro'` = 4.

The observation returns six numbers, not one, so that ambiguity has somewhere to go:

| column | meaning | qualifies quiet only if |
| --- | --- | --- |
| `epoch` | `runtime_epoch.epoch` for the singleton | `>= 1` |
| `live` | non-terminal admissions owned by the current epoch | `= 0` |
| `ahead` | non-terminal admissions owned by an epoch *newer* than the current one | `= 0` |
| `stale_recent` | non-terminal admissions from an older epoch whose session activity is within 6 h | `= 0` |
| `stale_expired` | non-terminal admissions from an older epoch, session quiet > 6 h | ignored (reported) |
| `undated` | non-terminal admissions that cannot be dated at all | `= 0` |

### 1.5 Stale epochs and unreadable stores

A row left behind by an older runtime epoch is not automatically dead: the runtime that owned it may
still be draining, and counting it would block every future window; writing it off blindly would
qualify over a live session. The trigger therefore applies the engine's own stale-admission horizon
(`STALE_ADMISSION_HOURS = 6`, `release002_control.py`): older-epoch rows younger than that block
quiet, older ones are reported as `stale_expired` and do not. Rows with no joinable session activity
are `undated` and block. Rows from a *newer* epoch than `runtime_epoch` are `ahead` and block — that
state means the observed epoch is itself stale.

Unreadable is never zero:

* at arm, an observation that fails (unopenable, unparseable, non-numeric, absent epoch) refuses with
  `exit 64` and arms nothing — a single attempt must not be spent on an unread instrument;
* inside the window, any such sample resets the counter (`quiet=0`), exactly like a non-zero count;
* every sample is logged with its mode, epoch and all six counts, so a later reader can tell *why* a
  window did or did not qualify.

### 1.6 Tests

* `tests/harness/test_release002_ext17c_admission_freshness.py` — 278 lines, **9 passed / 19.36 s**.
  First test is the red control: the old `immutable=1` read against the same WAL returns the stale
  count. The rest pin the corrected observation, the `admissions=busy` refusal, the arm-time
  `exit 64`, and the stale/ahead/undated classifications.
* `tests/harness/test_release002_ext17a_trigger.py` — the sandbox store now carries the production
  relations (`sessions(id, last_activity_at)`, `runtime_epoch(singleton, epoch)`,
  `session_admissions(admission_id, owner_epoch, status, target_session_id)`) with a current-epoch
  terminal row; **13 passed / 23.04 s**.
* `harness/qualify/ext17b_case.sh wal-live` — the end-to-end case above, recorded by the matrix.

### 1.7 What this does *not* claim

The store read at any single instant can agree between both instruments (the production sample in
§4 shows `immutable=4, mode=ro=4`, epoch 24 both). The defect is not "always wrong"; it is
"silently wrong for as long as the WAL is uncheckpointed", which is precisely when an admission has
just happened. A spot check cannot clear an instrument; the WAL test can, and does.

---

## 2. One final executable identity

Final candidate:

| | |
| --- | --- |
| commit | `62359402a6e35fb14e01462fe9bf233de982c9aa` |
| tree | `622ff89cf77be93d2fe8834ba2863ad546b11f08` |
| parent | `16c82ddabc296bcb3388256b3d034f0ea431ae34` (trigger correction) |
| branch | `restore002/ext17-r13-r11` |
| fast-forward from EXT-17B final head `663c11dd` | yes (`merge-base --is-ancestor`) |
| working tree | clean (0 changed paths) |

Every file the release path executes, compared byte-for-byte against the EXT-17B qualified
integration candidate `a41242328a6319b39078288ca3e66b3dcab99c4b`, computed from the trees
(`out/identity.txt`):

| release-path file | candidate `a4124232` | final `62359402` | |
| --- | --- | --- | --- |
| `harness/hermes-release-002` | `f00828df64369564…` | `f00828df64369564…` | same |
| `harness/release002_control.py` | `413c1b9a1b285b27…` | `413c1b9a1b285b27…` | same |
| `harness/release002_oldgate.py` | `1b17ec849cce8036…` | `1b17ec849cce8036…` | same |
| `harness/release002_preflight.py` | `0743827d6e7ad741…` | `0743827d6e7ad741…` | same |
| `harness/release002_release.sh` | `4edf7e117751aa98…` | `4edf7e117751aa98…` | same |
| `harness/trigger/INSTALL-v2.md` | `c2addc848611ad31…` | `c2addc848611ad31…` | same |
| `harness/trigger/qw-launch-apply-v2.sh` | `2b4044b46e9e1e14…` | `569ff5a26a516a3e…` | **changed by this mission** |

Six of seven are byte-identical to the qualified candidate. The only release-path change is the
trigger named in §1, and it is not carried forward on the strength of the old qualification: the two
scenarios it governs — `trigger` and `wal-live` — are re-run against this commit in §3, together with
the full nine-case matrix.

What changed between `663c11dd` (EXT-17B) and this commit is exactly:

```
A docs/restore002/EXT17B-DISPOSABLE-QUALIFICATION.md   (EXT-17B evidence, documentation only)
M harness/qualify/ext17b_case.sh                       (qualification tooling)
M harness/qualify/ext17b_matrix.py                     (qualification tooling)
M harness/trigger/qw-launch-apply-v2.sh                (the §1 correction — the only executable change)
M tests/harness/test_release002_ext17a_trigger.py      (fixture widened to the production schema)
A tests/harness/test_release002_ext17c_admission_freshness.py
```

No engine, launcher, supervisor, control-plane or preflight byte differs from the integration
candidate that EXT-17B qualified. The documentation commit that carries this file is a **documentation
head**, not the executable identity: it is a child of `62359402` that touches `docs/` only, and the
candidate in the tables above stays `62359402a6e35fb14e01462fe9bf233de982c9aa` /
`622ff89cf77be93d2fe8834ba2863ad546b11f08` throughout.

---

## 3. Reproducibility

### 3.1 Full harness

`/usr/bin/python3.13 -m pytest tests/harness -p no:hypothesispytest -q`, `TMPDIR=/tmp`:

```
422 passed in 193.75s (0:03:13)   exit 0      (final candidate, out/full11.txt)
422 passed in 192.61s            exit 0      (trigger commit 16c82dd, out/full10.txt)
```

413 pre-mission tests plus the 9 new §1 tests, no regressions, run twice — once at the trigger
correction and again at the final candidate, so the qualifying number belongs to the commit named in
§2 rather than to its parent. Environment note, disclosed rather than hidden: the same command through
the *gateway venv* interpreter (Python 3.11.15) fails one test —
`test_release002_ext14.py::TestInterpreterResolution::test_probe_is_evaluable_without_an_installed_venv`
— because the probe resolves the venv's own base interpreter under the real hermes home and
`tests/home_io_guard.py` refuses that I/O. It is an interpreter-version artifact of the invocation,
not a property of the tree: the repository's interpreter is 3.13 and the suite is authoritative there.

### 3.2 Nine-case qualification matrix

All nine cases at candidate `62359402a6e35fb14e01462fe9bf233de982c9aa`, exit 0
(`out/matrix-full17c.log`, records in `out/matrix.jsonl`). Every case is a fresh disposable root; the
install HEAD column is what the namespace was left holding when the case ended.

| case | drives | journal | mutated | restart attempts | install head | leftover / after kill |
| --- | --- | --- | --- | --- | --- | --- |
| `dirty` | install tree dirty at plan → refused | none created | no | – | `3652a370` | 0 / 0 |
| `foreign-freeze` | pre-existing foreign freeze | `aborted` / `ABORTED_SAFE` | no | – | `3652a370` | 0 / 0 |
| `busy` | live agents through the whole window | `aborted` / `ABORTED_SAFE` | no | – | `3652a370` | 0 / 0 |
| `rollback` | restart fails on the 2nd start → rollback | `unfreeze` / `ROLLED_BACK` | yes | 1 | `3652a370` | 0 / 0 |
| `needs-human` | rollback cannot be verified | `unfreeze`, result unset | yes | 1 | `3652a370` | 0 / 0 |
| `interrupt` | TERM during restart, then recover | `recover` / `ROLLED_BACK` | yes | 1 | `3652a370` | 0 / 0 |
| `timeout` | restart never returns | `unfreeze` / `ROLLED_BACK` | yes | 1 | `3652a370` | 0 / 0 |
| `trigger` | full handoff, then three re-arm attempts | `report` / `COMPLETE` | yes | 1 | `62359402` | 1 / 0 |
| `wal-live` | §1: live admission in a hot `-wal` | `report` / `COMPLETE` | yes | 1 | `62359402` | 1 / 0 |

`receipt`: `RELEASED_VERIFIED` for the two success cases, `ROLLED_BACK` for `rollback`, `interrupt`,
`timeout`, `ABORTED_NOT_QUIET` for `busy`, none for the three that never reached the report phase.
`recovery_hold=true` and `unit TimeoutStopSec=210` in every case; `preflight.failed=[]` everywhere
except `foreign-freeze` (`no_preexisting_freeze`), which is the case's own precondition.

The `leftover = 1` on the two success cases is the fixture gateway the release deliberately leaves
running — a successful APPLY ends with the service up. It is inside the namespace, owned by the case,
and reaped by the collector: `leftover_after_kill = 0` for all nine, and after the matrix the host
carries no `release002_release`, `hermes-release-002`, `ext17b_case` or `ext17b_systemd` process.

Behaviour the matrix pins, beyond the outcome column:

| case | evidence |
| --- | --- |
| `dirty` | `apply` rc 2, plan rc 2, preflight detail `1 changed path(s)`, **no journal created** — refusal is pre-mutation, not a journal entry |
| `foreign-freeze` | `preflight verdict NO_GO (rc=2) in the current state — nothing was changed` |
| `busy` | `no quiescent window within 180s — aborting before any mutation`, `mutated=false` |
| `rollback` | head/tree/unit digest restored to `3652a370` / `3bfc1c84` and verified |
| `needs-human` | `NEEDS_HUMAN.txt`: rollback did not restore the deployed identity (`active=failed`, rollback restart rc 1) — no `ROLLED_BACK` claim is made |
| `interrupt` | `recover-while-running` refused with **65** (`a release engine is still running (pids: …)`), then `recover` rc 0 → `ROLLED_BACK` |
| `timeout` | `systemctl restart rc=0 after 300190ms`, then `SIGKILL lines after restart: 1` → rollback → `ROLLED_BACK` |
| `trigger` | one fire; second arming **76**; wrong candidate **4**; wrong arming **76**; no retry |
| `wal-live` | `-wal` = 8,272 B; eight samples `admissions=busy … quiet=0/2`; `DEADLINE … disarmed without applying (attempt unspent)` exit **3**, no `attempt-fires`; then quiet `2/2` on the marked-terminal store → apply rc 0, `COMPLETE`, `RELEASED_VERIFIED` |

Note on `wal-live` step values: `trigger-wal-live=3` is the deadline exit code and `wal-bytes=8272`
is the byte count, not a failure counter — both are recorded numerically so a reviewer can see the
precondition was real rather than inferred.

---

## 4. Production preflight — read only

Run at `2026-11-12T06:50:24Z`. Read-only by construction: `git` queries, `systemctl` status queries,
`stat`/`sha256sum`, and `sqlite3 file:…?mode=ro` (plus `PRAGMA query_only=1` where the schema was
dumped). Nothing was staged, written, restarted or reloaded.

| check | value |
| --- | --- |
| installed HEAD | `3652a3702dae8d2917b91b6949da580efd3ef496` |
| installed tree | `3bfc1c842bf76e8a95356f4f0f3a91e75767992c` |
| branch / describe | `main` / `v0.23.0+canary.20261108T064411Z-93-g3652a3702d` |
| working tree | clean, 0 changed paths |
| gateway unit | `active` / `enabled`, `SubState=running`, `MainPID=3691490`, `NRestarts=1`, entered 2026-11-09 01:10:16 AEDT |
| gateway state file | `gateway_state=running`, telegram connected, `active_agents=2`, mtime 2026-11-12 17:50:23 AEDT |
| recovery hold | drop-in `90-recovery-hold.conf` present (`ba4a1c11…`, 47 B); no `/etc/hermes-release/recovery-hold` |
| drop-ins | `90-recovery-hold.conf`, `95-timeout-stop.conf` (`bb1844fd…`, 250 B), `memory-limit.conf` (`1d59fa70…`, 37 B) |
| journal `/var/lib/hermes-release/restore-002/journal.json` | `run_id=20261107T222858Z-3c3e24a50bfc`, `phase=aborted`, `result=ABORTED_SAFE`, `mutated=false`, candidate `3c3e24a50bfc…`, sha256 `149a701c…` |
| installed trigger | `/usr/local/lib/hermes-release/qw-launch-apply.sh`, 3,592 B, mode `0700 root`, mtime 2026-11-07 07:42:10 |
| installed trigger sha256 (recomputed here) | `cfd2fcc16c50761e1c352043345627dc85be1c8a454507601708b8d08fe60d19` |
| installed trigger `immutable=1` reads | **2** — the installed v1 carries the §1 defect |
| trigger v2 installed | no (`qw-launch-apply-v2.sh` absent) |
| attempt record | `attempt-fired.json` present (280 B) from the 2026-11-07 attempt — the single attempt is recorded as spent |
| competing release activity | 0 `hermes-release-*` units, 0 launcher/supervisor processes, `restore002-qw-launch.timer` `inactive`/`not-found`, lock file 0 B (unlocked) |
| production store | `journal_mode=wal`, `-wal` = 56,431,672 B, epoch 24, live admissions 4, stale-epoch non-terminal 6, 4,036 rows total |
| capacity | `/` 61 G free (68 % used), inodes 21 % used; `state.db` 13,667,123,200 B |

Two consequences for the release proposal, both stated plainly rather than smoothed over:

1. **The installed trigger is the defective version.** Any production attempt armed by v1 could
   qualify over live work. The proposal therefore installs v2.1 and the operator must record the
   recomputed digest of *that* file before arming; `INSTALL-v2.md` step 1 (baseline digest) is
   performed at approval time, on the host, not copied from here.
2. **The host is not quiet now** (`active_agents=2`) and the single attempt is already recorded. The
   proposal does not assume either condition away: it requires a fresh attempt record, and the
   trigger itself will refuse or disarm rather than fire over a busy host.

---

## 5. Release proposal

### 5.1 Candidate and manifest inputs

| input | value |
| --- | --- |
| candidate commit | `62359402a6e35fb14e01462fe9bf233de982c9aa` |
| candidate tree | `622ff89cf77be93d2fe8834ba2863ad546b11f08` |
| trigger | `harness/trigger/qw-launch-apply-v2.sh`, 12,626 B, sha256 `569ff5a26a516a3eca9c3f358d3e9d2c15dba7c9fa328f2c6fe71c582490d9eb` |
| trigger install procedure | `harness/trigger/INSTALL-v2.md` (`c2addc848611ad3157335687d9373bafcea831f3e7f2b0174e637175511a183e`) |
| expected installed HEAD at approval | `3652a3702dae8d2917b91b6949da580efd3ef496` |
| expected installed tree | `3bfc1c842bf76e8a95356f4f0f3a91e75767992c` |

Pinned engine inventory, sha256 of each file as read from the candidate tree:

```
f00828df6436956486cabe3886f66c6f3aac475a6bb37fae8cd1d2767f01d356  harness/hermes-release-002
413c1b9a1b285b2724dfe72256d8ca2a3d569ab66d9f3c2fdcbf7ad9599a0090  harness/release002_control.py
1b17ec849cce80360eb75ad49daa6095bb0447e76de32f897c4b48cf71fec0d4  harness/release002_oldgate.py
0743827d6e7ad741c3a394054f989d29b5f87a485dffd7bf906faaeccc18cda1  harness/release002_preflight.py
4edf7e117751aa98c27dc9942b424c101c452f6d0fd74902c6e4670f29444b2a  harness/release002_release.sh
44391930344c10b5293b500094d5b9230655111bbba942f5998d613b76a4a201  harness/restore002_session_inventory.py
569ff5a26a516a3eca9c3f358d3e9d2c15dba7c9fa328f2c6fe71c582490d9eb  harness/trigger/qw-launch-apply-v2.sh
```

Every digest in this table is derived from the tree, not from a previous document; the *installed*
digests are recomputed on the host at approval time (§4).

### 5.2 Approved scope

In scope: one attempt of the release path against `hermes-gateway.service` on the host — stage the
candidate, preflight, archive, freeze, quiescence window, fast-forward merge, unit regeneration, one
restart, post-release verification, admission restoration, journal settlement.

Out of scope, unchanged from §13 of the governing authority: PR #9 merge or undrafting; Stage C
activation; recovery-hold removal; protective drop-in removal; a second APPLY; manual session repair
or deletion; credential or governance changes; unrelated service changes; expanding the release
scope; bypassing any identity, freeze or recovery refusal. Note the asymmetry the proposal relies on:
the release path may *engage* its own attempt-scoped freeze and must *release only controls it owns*
— the standing recovery hold and the two protective drop-ins are not attempt-owned and stay.

### 5.3 One-attempt procedure

Preconditions, all verified immediately before arming: installed HEAD/tree equal to §5.1; working
tree clean; gateway `active`; no `hermes-release-*` unit or supervisor; no `attempt-fired.json` for
this attempt; recovery hold present; installed trigger digest equal to §5.1; `active_agents` observed
by the trigger, not assumed.

1. Record the installed v1 digest (approval baseline) and install v2.1 per `INSTALL-v2.md`; recompute
   its digest on the host and record it.
2. `plan --candidate <candidate>` — dry run must return `GO`.
3. `stage --candidate <candidate>` — must report `staged == pinned == candidate inventory`.
4. `preflight` — identity, cleanliness, unit, drop-ins, session inventory, hold, estop.
5. Arm the trigger with the deadline inside the approved window. Quiet = three consecutive samples
   with `agents=0` **and** `admissions=quiet` (six columns, §1.4).
6. On quiet: one `hermes-release-002 apply` (handoff). `attempt-fired.json` is written by the
   launcher itself; the trigger never retries and never issues a second APPLY.
7. Engine: drain ack (45 s budget), freeze, three zero-activity samples 20 s apart inside 180 s,
   identity revalidation, one fast-forward merge, unit regeneration, one restart (240 s startup
   budget; 300 s effective stop, `RuntimeMaxSec=1500 s`).
8. `verify --candidate <candidate>`; post-release verification of head, tree, unit digest, service.
9. Release only attempt-owned controls; confirm the standing hold and drop-ins are untouched.
10. Journal settlement through the qualified path (`ct journal-state`), never forced.

### 5.4 Stop conditions

Any of the following stops the attempt; none of them is retried:

* installed HEAD/tree or a manifest digest differs from §5.1 at any revalidation point;
* the store cannot be read, or an admission observation is ambiguous (`undated`, `ahead`,
  `stale_recent`) — the window resets, and at arm the trigger exits 64 without arming;
* the window does not qualify inside 180 s — the engine aborts pre-mutation (`ABORTED_NOT_QUIET`,
  `mutated=false`) and the attempt is unspent; a *fired* attempt that cannot be verified follows the
  rollback path instead;
* the launcher exceeds the trigger deadline — `timeout --kill-after=5` kills it, `apply_rc=124`,
  exit 5, no retry;
* a `hermes-release-*` unit already exists (exit 77), a journal already exists (exit 76), an attempt
  record already exists (exit 76);
* verification fails or a rollback cannot be verified — `NEEDS_HUMAN`, no further mutation.

### 5.5 Rollback

After any mutation, the only permitted path is the qualified rollback: restore the recorded
head/tree/unit digest, restart once, verify, and settle the journal as `ROLLED_BACK`. A rollback that
cannot be verified is `NEEDS_HUMAN` — the engine must not attempt a repair loop. EXT-17B exercised
both paths (`rollback`, `needs-human`) inside the namespace; §3 re-exercises them against this commit.

### 5.6 Operational verification criteria

The attempt is complete only when all of the following are separately observable: journal
`phase=report`, `result=COMPLETE`; deployed head/tree equal to the candidate; unit digest equal to the
regenerated unit; gateway `active` with a post-restart `MainPID`; `RECEIPT.md` present with
`RELEASED_VERIFIED`; `restart_attempts=1`; no `SIGKILL` lines in the engine log; standing hold and
drop-ins byte-identical to §4; no orphan `release002_*` process; admission state restored and
reported. Completion is the reviewable operational outcome, not a zero exit code.

---

## 6. Residual risk and disclosure

* **The §1 defect is intermittent by nature.** A spot sample can agree between instruments; the
  claim rests on the deterministic WAL test (§1.2), the end-to-end case (§1.3) and the red control in
  the test suite (§1.6), not on a live coincidence.
* **The installed trigger is v1 and carries the defect.** The proposal replaces it before arming and
  requires the host-recomputed digest to match §5.1. Between now and approval, no attempt should be
  armed by the installed trigger.
* **The attempt record from 2026-11-07 is present.** The proposal requires a fresh attempt; the
  trigger's `exit 76` on an existing record is correct behaviour, not something to work around.
* **`state.db` carries a pre-existing FTS5 index defect** (`malformed inverted index for FTS5 table
  main.messages_fts`). Repairing it is a live-database write, forbidden by this mission and out of
  release scope. The trigger's reads do not touch `messages_fts`.
* **The 6 h stale-admission horizon is a judgement, not a measurement.** It mirrors
  `STALE_ADMISSION_HOURS` in `release002_control.py`; if the engine's constant changes, the trigger's
  `ADM_STALE_HORIZON_S` must change with it. The mismatch would block windows (safe direction), never
  qualify one.
* **Suite interpreter.** §3.1's 422-pass result is on the repository interpreter (3.13); the gateway
  venv's 3.11 produces one environment-only failure. Disclosed so a reviewer can reproduce both.

## 7. Disposition

`EXT-17C_READY_FOR_RELEASE_AUTHORITY_REVIEW` when §3's matrix and suite are green at the commit named
in §2, no orphan processes remain, and the receipt on PR #9 carries these results unaltered.
