# ADM-RECON-01 — Reconciliation design for three orphaned epoch-1 admissions

**Mission:** `RESTORE-002-ADM-RECON-01` — read-only production investigation, isolated
reproduction, design, tests and draft-PR evidence publication.
**Disposition:** `ADM-RECON-01_READY_FOR_EXTERNAL_CA_REVIEW`
**Production state:** **unchanged.** No live database write, gateway restart, release staging,
trigger installation, APPLY, PR merge or change to protective controls was performed.
**Governing contract:** External Chief Architect Operating Contract **v1.0-ca1**, sha256
`f5d9607fa73ab0eb52fd3acb37e623fa69ac7e6aea63cb264e06780e547ffdd2`, commit `37d87ec`
(`governance/EXTERNAL_CA_OPERATING_CONTRACT.md`, `chief-architect-loop`). Revision identified by
retrieval, not by memory: the file is an amended copy of v1.0 (blob `36b0a084e5be585b21a8d9f06801df8d84bb46f8`)
carrying exactly one change, `GOV-CONTRACT-001`, which qualifies the AGENTS.md requirement for a
repository that has no such file (5 insertions, 2 deletions). The pinned copy relied on during
PRODEXEC-02 (`ab02df4a…`) is the pre-amendment v1.0; the amendment does not alter evidence,
identity or authority rules for this mission.

---

## 1. Verified identities

| Item | Value | How verified |
|---|---|---|
| Production store | `/home/hermes/.hermes/state.db` | `mode=ro` open |
| Size / geometry | 13,681,672,192 B; 3,340,252 pages × 4,096 B; freelist 410 | `pragma page_count/page_size/freelist_count` |
| Journal mode | WAL, `-wal` 56,431,672 B at observation | file listing |
| `runtime_epoch.epoch` | **24** | `select epoch from runtime_epoch where singleton=1` |
| Admissions total | 4,111 | `count(*)` |
| Deployment candidate | `62359402a6e35fb14e01462fe9bf233de982c9aa` / tree `622ff89cf77be93d2fe8834ba2863ad546b11f08` | unchanged, **not deployed** |
| Installed baseline | `3652a3702dae8d2917b91b6949da580efd3ef496` / tree `3bfc1c842bf76e8a95356f4f0f3a91e75767992c` | unchanged |
| PR #9 | `draft=true`, `state=OPEN`, `mergedAt=null`, head `restore002/drain-envelope` | `gh pr view` |

Observation caveat, disclosed: the interpreter's linked SQLite is 3.46.1 and warns that it is
vulnerable to the WAL-reset corruption bug (fixed in 3.50.7+/3.51.3+). That warning is why the
qualified trigger opens the database `mode=ro` bound to `runtime_epoch` rather than `immutable=1`,
and it is why no step below relies on an in-place VACUUM or on downgrading the journal mode.

## 2. The three admissions, exactly

`session_admissions` has **no** `created_at` column (established twice). The rows:

| field | 101 | 102 | 103 |
|---|---|---|---|
| `admission_id` | `40a8ac83611e428696d5fee98e4bc599` | `faf7f3066aca401d8b5c309c5d2f397d` | `e7aa5bc9b3204511846ad644a3279063` |
| `target_session_id` | `local-7d6f5dde…a4ed901` | `local-da851133…de4f4d5` | `local-efa627be…45b44187` |
| `seq` | 101 | 102 | 103 |
| `intent` | `queue` | `queue` | `queue` |
| `status` / `outcome` | `unknown` / `NULL` | `unknown` / `NULL` | `unknown` / `NULL` |
| `owner_epoch` / `generation` | `1` / `1` | `1` / `1` | `1` / `1` |
| `principal_id` | `uid:1001` | `uid:1001` | `uid:1001` |
| `request_id` | `eeb2871c64ad4ee3a9faaa30f99d6104` | `9f73f7475b4245a0b4800a101613c5f3` | `3e55430447184d94b65d3b06c44c3b3f` |
| `payload_json` | 54 B, digest `1c663b76fbc45aa9…` | 54 B, `cbcf1fe64ff7909c…` | 54 B, `a824361f51077b7c…` |
| `lineage_json` | `["local-7d6f5dde…"]` | `["local-da851133…"]` | `["local-efa627be…"]` |

Their sessions, all `source='oneshot'`, `parent_session_id=NULL`:

| field | value |
|---|---|
| `started_at` | 1792365852.956436 / 1792365854.443022 / 1792365859.534121 = **2026-10-18T23:24:12Z / :14Z / :19Z** |
| `last_activity_at` | `NULL` (all three) |
| `ended_at` / `end_reason` | `NULL` / `NULL` (all three) |
| `message_count` | **0** (all three) |
| `runtime_generation` / `runtime_revision` | `1` / `1` (all three) |

Payload bodies are not reproduced here — 54 bytes each, and their digests are the durable
identity for any later comparison.

## 3. Why they are stuck

An admission moves `queued → started → terminal`. `unknown` is the runtime's explicit fence for
"an input was admitted and its turn's outcome is genuinely undetermined". The evidence chain that
leaves these three permanently non-terminal:

1. **Fenced at epoch rollover, not failed.** `hermes_state_runtime.py:440-480` converts a
   long-since-abandoned `started` row (`hermes_state_runtime.py:463` is inside that block) to
   `unknown` when the gateway recovers. The row keeps `owner_epoch=1`; the store is now at epoch
   24.
2. **Settlement recovery cannot claim them.** `gateway/session_settlement_recovery.py` settles only
   rows whose `owner_epoch` matches the *live* owner's epoch for that generation; epoch 1 is not any
   live owner, so it is out of reach by construction.
3. **The unattended settler does not apply.** `gateway/session_unattended_unknown.py` settles turns
   whose submitter is an automation principal. These submissions carry `principal_id='uid:1001'` —
   a person. The policy is deliberate: a *person's* lost turn waits for that person's
   acknowledgement, and is never auto-resolved.
4. **Age cannot retire them.** The qualified trigger's bucket rules put a row in `stale_expired`
   only when it has a dated activity older than the engine's 6 h horizon (`21600 s`). These rows
   have `last_activity_at NULL` and no `created_at` column, so they are `undated` / `stale_recent`
   forever. Nothing in the schema can age an undated row out.
5. **Therefore the quiet predicate is unreachable.** The qualified trigger v2.1 requires
   `live = ahead = stale_recent = undated = 0` before it will fire. Measured with the trigger's own
   `ADMISSION_SQL` executed verbatim under `mode=ro`, the sample at the PRODEXEC-02 attempt was
   `24|4|0|3|3|0` (epoch|live|ahead|stale_recent|stale_expired|undated): `stale_recent` is pinned at
   **3** by exactly these rows.

Re-checked for this mission: the store currently holds **5** `unknown` admissions — the three
epoch-1 rows above, plus `e2268524…` (epoch 15, `messaging` telegram thread 56459, dated
`last_activity_at` 1793653745) and `e4e2e120…` (epoch 18, `messaging` telegram thread 281922,
started 1793663225). Those two are **dated** and are handled by the ordinary aging rules; they are
not part of this mission and must not be touched. This matters for §7: reconciling the three
removes the *structural* blocker, it does not by itself guarantee a quiet sample — the trigger
re-checks every attempt regardless.

## 4. Residual work: is it safe to terminalise?

Every place recoverable work could hide, checked directly against production (`mode=ro`,
`PRAGMA query_only=1`, each probe ending in a negative-control write expected to fail):

| Location | Finding |
|---|---|
| `messages` for the three sessions | **0 rows.** `resolve_unknown_session_input`'s transcript closer is a proven no-op (§5). |
| `worker_executions` | exactly one row per session, `execution_id='admission-worker:<admission_id>'`, `kind='compute'`, **`status='terminal'`**, `owner_epoch=1`, `generation=1` — already retired; retirement will be a value-identical UPDATE. |
| `worker_receipts` | **0** rows matching the three execution IDs. |
| `state_meta` | **0** keys mentioning any of the three admission IDs (no `RESULT_PREFIX` captured result). |
| `session_turn_leases` | **0** for the three conversations (4 leases exist store-wide, all other sessions). |
| `delivery_obligations`, `compression_locks`, `session_model_usage`, `hosted_room_remote_runs` | **0** rows for all three sessions. |
| Worker outboxes `/home/hermes/.hermes/worker-outboxes/admission-worker-<id>/` | present for all three; `pending.json` contains `"pending": []` — **no queued outbound payload**. Scope files carry a secret which is not reproduced here and must not be redacted in place. |
| Worker liveness | PIDs recorded in those scopes (3383983 / 3384057 / 3384203) are **absent from `/proc`**, and the host has been up only since 2026-11-03 08:07:51 — long after those processes would have started. Absence is therefore not an age inference: a process from 2026-10-18 cannot be running on this boot. |

Conclusion for the outcome value: nothing was produced and nothing is owed, so the honest outcome
is `interrupted` — exactly what the runtime itself writes for an operator acknowledgement of a lost
turn. `completed` would assert a result that does not exist; `failed` would assert an error this
turn never produced; `cancelled` means "withdrawn before it ran", which is false — it ran and its
outcome was lost.

## 5. The sanctioned transition, verbatim

`hermes_state_runtime.resolve_unknown_session_input` is the runtime's own operator
acknowledgement. Its body, quoted exactly (docstring abridged, code unchanged):

```python
def resolve_unknown_session_input(db, *, epoch: int, admission_id: str, generation: int,
                                  _terminal_write=None, _captured=None) -> dict:
    """Explicit operator acknowledgement; resolves uncertainty, never requeues it.

    ``_terminal_write(conn, row)`` is the owner's same-transaction effect (the discarded turn's
    transcript boundary): it must not commit, may run again on SQLite retry, and raising rolls
    the whole resolution back, so the FIFO can never advance past an unclosed lost turn.
    ``_captured=(outcome, stored_result)`` is the exact result this owner captured for the turn
    before its settlement write failed ..."""
    from hermes_state_terminal import RESULT_PREFIX
    def write(conn):
        _epoch(conn, epoch)
        row = _admission(conn, admission_id)
        if (type(generation) is int and row['generation'] == generation and row['status'] == 'terminal'
                and (row['outcome'] == 'interrupted' or conn.execute(
                    'SELECT 1 FROM state_meta WHERE key=?', (RESULT_PREFIX + admission_id,)).fetchone())):
            return _row(row)
        if type(generation) is not int or row['status'] != 'unknown' or row['generation'] != generation:
            raise RuntimeStoreError('stale_generation')
        outcome = 'interrupted'
        ...
        if _terminal_write is not None:
            _terminal_write(conn, row)
        _retire_admission_workers(conn, row, row['owner_epoch'])
        conn.execute("UPDATE session_admissions SET status='terminal',outcome=? WHERE admission_id=?",
                     (outcome, admission_id))
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?', (row['target_session_id'],))
        return _row(_admission(conn, admission_id))
    return db._execute_write(write)
```

Four properties that make this safe for epoch-1 rows, each read from the code rather than assumed:

- **The fence is `epoch` + `generation` + `status`, not `owner_epoch`.** `_epoch(conn, epoch)` only
  requires the caller's epoch to equal the store's current `runtime_epoch` (24). `owner_epoch=1` on
  the row is used solely for worker retirement scope and the optional captured result. So the
  runtime path *can* terminalise these rows today.
- **Terminal, interrupted, same generation is idempotent** — it returns the row and writes nothing.
- **One transaction.** Every statement runs inside `db._execute_write(write)`; a failure rolls back
  the whole resolution, so no partial state is reachable.
- **Worker retirement is value-identical here.** `_retire_admission_workers` selects
  `status!='terminal'` rows first (none, so no receipt compaction), then runs
  `UPDATE worker_executions SET status='terminal' WHERE session_id IN (target, lineage[-1]) AND generation=? AND owner_epoch=?`.
  Both scope IDs are the same session; exactly one row matches and it is already `terminal`.

And the transcript closer, `gateway/session_results.close_discarded_turn`, quoted where it decides
our case:

```python
    if notice is None and (tail is None or not (
            tail[0] == 'user' or (tail[0] == 'assistant' and tail[1] not in (None, '', '[]')))):
        return False
```

`notice is None` is what `SessionAuthority.resolve_unknown_as_owner` passes whenever no result was
captured, and `_captured=None` here (no `state_meta` result key, and `pending_results` is in-memory
and cannot survive 22 restarts). With `message_count = 0` the `tail` is `NULL`, so the closer
returns `False` **without writing a message row**. That is the crucial equality: on these three
rows the runtime's `_terminal_write` is a no-op, so a fenced offline CAS that performs the same
statements has exactly the runtime's database effect.

## 6. Two execution lanes

### 6.1 Lane A — operator acknowledgement (preferred)

The shipped, supported path:

```
hermes sessions discard <session-id> --admission <admission-id> --yes
```

which issues RPC `prompt.resolve_unknown{session_id, admission_id, execution_generation}` →
`session_controls.resolve_unknown` → `SessionAuthority.resolve_unknown(actor, ref, admission_id, generation)`:

1. `authorize(actor, ref, 'session:control')` — operator authority; fails closed.
2. `receipt(actor, ref, admission_id)` — the pre-write obligation stamp.
3. `resolve_unknown_as_owner(...)` → `tracked_write(partial(resolve_unknown_session_input, …,
   _captured=prepared, _terminal_write=close_discarded_turn(...notice=None)), then=_resolved_unknown)`.

Advantages: no new code, exact runtime semantics, and `then=_resolved_unknown` performs the
in-process follow-up (dropping `pending_results`, publishing/collecting as the authority requires)
that an offline tool cannot.

Failure modes, stated plainly because they change how a failure may be handled:

- The write commits **before** `_resolved_unknown` runs. A non-zero CLI exit, a lost socket or a
  gateway restart during the call may therefore sit *after* a successful commit. The correct
  response is to **re-read the row**, never to retry blind — and because terminal/interrupted is
  idempotent, a re-run of the same command is safe when the row is still `unknown`.
- `ref` resolution: the authority path is written for live sessions and this session ended 26 days
  ago. Whether the control socket accepts the ref for a session no longer loaded is **not**
  established by this mission; it is step 1 of §7 and must be observed, not assumed. If the RPC
  refuses, nothing is written (authorization and ref resolution precede the transaction) and the
  operator falls back to Lane B deliberately.
- The CLI does not create the backup. §7 requires the verified backup to exist **first**.

### 6.2 Lane B — fenced offline CAS (`adm_recon.py`, in this package)

A single-purpose operator tool, dry-run by default, whose only write path is `--apply`, for **one
explicitly selected admission per invocation**. It mirrors §5 statement-for-statement:

1. Lexical guards with no database handle: exactly one `--admission` for `--apply`, membership of the
   roster, and present/hex `--expect-epoch`, `--backup`, `--backup-sha256`.
2. Read-only snapshot under `mode=ro` + `PRAGMA query_only=1`, taken inside one deferred read
   transaction so every fact the guards and the delta proof use comes from a single consistent view.
3. `already_terminal(snap)` → prints `already_terminal`, writes `already-terminal.json`, exits **0**
   (idempotent, re-runnable, and no backup is demanded for a no-op).
4. Backup currency, in this order: file exists (`backup_missing`), is not the target itself
   (`backup_is_target`), digest **recomputed** and equal (`backup_digest_mismatch`), opens and passes
   `integrity_check` on the four guarded tables (`backup_integrity_failed`), its `runtime_epoch`
   equals `--expect-epoch` (`backup_epoch_mismatch`), and the selected admission's
   `(status, owner_epoch, generation)` equals the live pre-state (`backup_not_current`). All exit
   **64**, all before a writable connection exists.
5. `BEGIN IMMEDIATE`; re-assert `runtime_epoch.epoch == --expect-epoch`; the same worker-retirement
   UPDATE as §5 with its own row-count assertion;
   `UPDATE session_admissions SET status='terminal', outcome='interrupted'
   WHERE admission_id=? AND status='unknown' AND generation=? AND owner_epoch=?` with
   `changes() == 1`; `UPDATE sessions SET runtime_revision=runtime_revision+1`; `COMMIT`.
6. Post-write proof in a fresh read-only connection: the selected row is `('terminal','interrupted')`
   with all other columns identical, its session's `runtime_revision` is exactly `+1` with all other
   columns identical, and every other admission row, session row, worker row, lease, receipt,
   `state_meta` row, table row count, message count/high-water mark, non-terminal set and schema
   fingerprint is unchanged. Any deviation, or an incomplete verifier → exit **65**. §6.4 G3.
7. Its row-count guard excludes derived FTS5 shadow tables (`*_fts*_data/_idx/_content/_docsize/_config`):
   their content is a pure function of `messages`, which *is* counted, the tool never writes a
   message row, and counting them on this store costs minutes of I/O — and would fail outright on the
   one part of the store already known to be malformed. This was not a theoretical concern: an
   earlier revision counted them, and a full-copy run had to be abandoned because the guard
   dominated the runtime.
8. It deliberately never opens `SessionDB` — so it cannot run `_open_writer_conn`'s schema
   maintenance against a store whose FTS5 index is known malformed.

Exit codes: `0` committed **and** delta-verified / already terminal; `2` dry-run: preconditions hold,
nothing written; `64` input, guard, backup or epoch refusal (nothing written, no write connection);
`65` the write may have committed and the exact allowed delta was not proven; `70` internal or
database error.

### 6.3 Equivalence, executed (not asserted)

`equivalence_test.py` runs both lanes against two copies of the same store and compares every row
image the transition may touch — the three `session_admissions`, their `sessions`, their
`worker_executions`, their `messages`, their `session_turn_leases`, every `state_meta` key naming
them, the `runtime_epoch`, the global status and terminal-outcome histograms, the non-terminal ID
set, the full `sqlite_master` schema, and per-table row counts — plus `PRAGMA integrity_check` on
`session_admissions`, `sessions`, `worker_executions` and `state_meta` in both copies.

- **Purpose-built fixture** (`fixture/small.db`: real DDL of the eight tables, the three target rows
  verbatim, four terminal decoys, decoy sessions/workers/messages/leases/state_meta):
  `38/38 checks, differences: []`, tool-only and `--mixed`. Adversarial suite
  `test_adm_recon_fixture.py` (**guard-v2**): **153/153 checks**; red/green negative controls
  `red_controls.py`: **32/32**. (Disclosed fixture artefact only: `SessionDB`'s open runs schema
  migrations on a partial store, so the runtime copy legitimately gains tables and seeds
  `state_meta`; the tool copy gains nothing.)
- **Full copy of production** (`VACUUM INTO` from `mode=ro`, 13,648,543,744 B, sha256
  `345a25b21c32c2f9f33cc3c00a0f7fd9a2bbf32f3a43cf26c55f918880304a00`, `VACUUM_RC=0`), guard-v2:
  **`40/40 checks, differences: []`** in `--mixed` (Lane A for admission 101, then the tool once for
  102 and once for 103) and **`42/42 checks, differences: []`** tool-only. Lane B exited `0` with
  `disposition=committed_verified` and exactly two writes per application; the audit trigger recorded
  no write outside `session_admissions` and `sessions` and none to `state_meta`; the three targets
  came out `terminal/interrupted` in both copies; `runtime_revision` +1 and `messages` unchanged in
  both; `integrity_check` `ok` on the four tables on both sides. Runtime epoch 24, pre-run histogram
  `queued 2 / started 4 / terminal 4100 / unknown 5`.

**`state_meta`, disclosed rather than silenced.** The first strict full-copy run failed
`equiv/rowcounts` with `{'state_meta': (4926, 4925)}`. The delta is the runtime's own open-time
bookkeeping, not the transition: on a fresh copy a plain `SessionDB` open with **no discard at all**
produces it (`legacy_refusal_repair_pending` and `legacy_refusal_repair_session_cursor` removed,
`legacy_refusal_repair` added — the one-time legacy safety-refusal repair, which finishes on a quiet
copy but can only defer against concurrent writers on the live WAL store), and the
`message_uid_backfill_id` cursor advances on open. Two runs on the same bytes showed two different
outcomes of that pass (`4925` and `4926` on the runtime copy), which is the proof that it is not
attributable to the admission writes. `state_meta` is therefore compared by content: row counts are
strict for every other table, the tool copy must show no `state_meta` delta in the tool-only run and
nothing outside the named marker keys in `--mixed`, and any other key difference fails the run. The
failing pre-fix log is kept as `eq_real_tool_pre-state-meta-fix.log`.

What the equivalence establishes: on identical data, Lane B produces **byte-identical rows** to the
runtime's own resolution, and Lane A produces exactly six updates (three admissions, three session
revision bumps) with no insert, no delete and no other table touched.

What it does not establish: that the control socket accepts a ref for a session that is not loaded
(§6.1) — that is observable only by attempting Lane A, which is a production write.

### 6.4 Execution guards (external-CA corrections)

The first package was accepted in design and rejected in execution: it could apply the whole roster
in one invocation, it treated the epoch and the backup digest as advisory, and its post-write check
proved "no unexpected change" without proving the *specific* allowed change. Those three findings are
closed here. All of §6.2's compare-and-set, backup, integrity and recovery protections remain.

**G1 — exact selection.** `--apply` requires exactly one `--admission`, which must be one of
`ROSTER`'s three identities. Two `--admission` flags, none, or an id outside the roster refuse before
any database is opened (`apply_requires_single_admission`, `admission_not_in_roster`). Dry-run still
accepts one or more (default: the whole roster) because it writes nothing. The canary is one named
admission; the operator names each subsequent identity explicitly.

**G2 — mandatory, independently verified inputs.** Under `--apply`, `--expect-epoch`, `--backup` and
`--backup-sha256` are mandatory. Each is validated in two layers: a lexical layer with no database
handle at all (missing, non-hex, wrong length, and `argparse`'s own parse failures converted into the
tool's own refusal exit code), then a data layer before any writable connection exists — the digest is
**recomputed from the file**, never trusted from the argument (`backup_digest_mismatch`); the backup's
`integrity_check` on the four guarded tables must be `ok`; its `runtime_epoch` must equal
`--expect-epoch`; and the selected admission's `(status, owner_epoch, generation)` in the backup must
equal the live pre-state (`backup_not_current`). The live epoch must also equal `--expect-epoch`
(`epoch_not_expected`).

**G3 — the delta is proven, not assumed.** After `COMMIT`, a fresh `mode=ro` connection re-derives the
same image it captured pre-write and the two are compared. Success requires, positively: the selected
admission is `('terminal','interrupted')` with every other column byte-identical; its session's
`runtime_revision` is exactly `+1` with every other column identical; every other admission row, every
other session row, every worker row, lease, receipt and `state_meta` row, every table row count, the
total message count, the `max(id)` high-water mark, the per-session message count, the non-terminal set
and the schema fingerprint are all unchanged. Any deviation — or a verifier that cannot complete — is
`committed_verification_failed` / `committed_verification_incomplete` and exit **65**. A committed
write is therefore never reported as success on incomplete proof.

**Preserved.** One `BEGIN IMMEDIATE`; CAS on `(admission_id, status='unknown', owner_epoch,
generation)` with `rowcount==1` assertions for both tables; the worker-retirement assertion; epoch
re-read inside the transaction (`epoch_moved`); pre-write guards for ownership, intent, generation,
session source/transcript/end-reason/lease, live admissions on the session, captured results and
foreign or live worker rows; `integrity_check` on the backup; and the dry-run default.

**New refusals introduced by the corrections:** `apply_requires_single_admission`,
`admission_not_in_roster`, `expect_epoch_required`, `expect_epoch_malformed`, `backup_required`,
`backup_missing`, `backup_is_target`, `backup_sha256_required`, `backup_sha256_malformed`,
`backup_digest_mismatch`, `backup_unreadable`, `backup_integrity_failed`, `backup_epoch_mismatch`,
`backup_not_current`, `epoch_not_expected`, `unknown_arguments`, `unexpected_outcome_on_unknown`.

**Red controls.** `red_controls.py` takes the frozen pre-correction tool
(`pre_guard/adm_recon_ea7e1dc.py`, sha256 `0bce8e4d499c54004272f82995cee369c68e9911dcdab285fa8fb64e376a693a`) and the corrected tool,
runs each CA-named defect against both on byte-identical copies of the fixture, and measures writes
through an audit trigger rather than trusting either tool's own report. A control passes only when the
old behaviour exhibits the defect (a real multi-row apply, or a committed write accepted with no
epoch/backup proof, or a deviation reported as success) **and** the corrected behaviour refuses (64) or
returns a non-success disposition (65) with the delta unproven.

**One-at-a-time qualification.** The sequence is qualified in two shapes, both compared against the
runtime's own all-three path: (a) tool-only, three separate invocations naming `40a8ac83…`, then
`faf7f306…`, then `e7aa5bc9…`; (b) mixed, exactly as the CA asked — Lane A succeeds for the first
admission, and only then is Lane B used for the second, and again for the third. Both must produce a
final row image identical to the all-Lane-A image. Results in §6.3.

## 7. Recommended production procedure

Only after a separate, explicit human authorisation naming this scope. Nothing below is authorised
by this document.

**Stage 0 — preflight, read-only.**
1. Identity: `runtime_epoch.epoch` is the same as the authorised run, and each of the three rows is
   still `status='unknown' AND generation=<as recorded>`. A change is a hard stop, not a rebase.
2. Verified backup: `VACUUM INTO` from a `mode=ro` connection to a path outside `/tmp`, then
   `sha256sum`, `PRAGMA integrity_check` on the tables above, and `pragma page_count` recorded.
   `.backup` must not be used here: under the gateway's continuous writes it never converges
   (observed: abandoned at 6,700,236,800 of 13,648,543,744 bytes after the allowance, with a partial
   file that would not open).
3. Disk space: at least 2× the store size free at the backup location.
4. Confirm PR #9 still draft/unmerged, the recovery hold and the three protective drop-ins present,
   and that no release attempt is armed.

**Stage 1 — one admission (canary), Lane A.**
Run `hermes sessions discard <session> --admission <id> --yes` for admission 101 only. Then read
back: `status='terminal'`, `outcome='interrupted'`, `sessions.runtime_revision` = previous + 1, the
`messages` count for that session still 0, `worker_executions.status` still `terminal`, and no other
admission changed status. Any failure to confirm → stop, do not proceed to 102/103, and re-read
before deciding.

**Stage 2 — remaining two.** Same command, same readback, one at a time — a separate invocation per
admission, each naming its own id. Never one invocation for more than one admission.

Lane B equivalents, when Lane A is used for the canary and Lane B for the rest (§6.4). One
invocation per admission — never a loop, never two ids:

```bash
B="python3.13 harness/reconcile/adm_recon.py --db <store> \
   --trigger-script harness/trigger/qw-launch-apply-v2.sh --expect-epoch <epoch> \
   --backup <stage0-backup> --backup-sha256 <stage0-sha256>"
$B --out out/102 --admission faf7f3066aca401d8b5c309c5d2f397d --apply   # only after 101 is confirmed
$B --out out/103 --admission e7aa5bc9b3204511846ad644a3279063 --apply   # only after 102 is confirmed
```

The `101/102/103` labels are the store's own `session_admissions.label` values, not a sequence
invented here: `101 = 40a8ac83…`, `102 = faf7f306…`, `103 = e7aa5bc9…`. Re-read them under
`mode=ro` before relying on this paragraph.

Exit `0` is the only success; `65` means the write may have committed and the delta was not proven —
stop and re-read before doing anything else.

**Stage 3 — re-observe.** Execute the qualified trigger's `ADMISSION_SQL` verbatim under `mode=ro`
and confirm `stale_recent` dropped by 3 and no `undated` row was created. Do not interpret an
unchanged `live`/`ahead` figure as failure: those track live traffic.

**Stage 4 — stop and hand back.** Producing a quiet window is PRODEXEC-02's business, not this
mission's. Do not arm, stage or release.

**Fallback.** If Lane A cannot accept the ref, run Lane B per §6.2 with the Stage 0 backup, the
`--expect-epoch` and `--backup-sha256` from Stage 0, first as a dry run, then `--apply` for one
admission, verify, then the other two.

**Rollback.** A backup taken at Stage 0 is restorable, but restoring it discards every write made
after it — so rollback is a *last* resort, and forward completion is preferred because the
transition is idempotent and value-identical for workers. Before any restore, stop the gateway and
account for every write since the backup, or the "restored" store will resurrect settled work.

**Stop conditions** (any one ends the run with a receipt): epoch changed; any target no longer
`unknown` at the recorded generation; backup missing, unreadable or digest-mismatched; a write
touches any row outside the six; post-write verification fails; the gateway restarts during a
stage; any ambiguity about which rows are affected.

**Concurrency.** Lane A requires the gateway running (it is an RPC) and is safe against concurrent
settlement because the transition is one write transaction with a `status='unknown'` predicate.
Lane B must not run while Lane A is in flight for the same rows. Neither may run during a release
attempt, inside a quiet window, or with the trigger armed.

## 8. Adversarial self-review

| Attack | Result |
|---|---|
| Row already terminal when the tool starts | `already_terminal` → exit 0, no write (idempotent). |
| Epoch rolls over between snapshot and write | `--expect-epoch` + `_epoch`-equivalent check refuse → exit 64. |
| Generation drift on a target row | CAS predicate `generation=?` matches 0 rows → row-count assertion fails → rollback, exit 64. |
| Someone resolves the row concurrently | `status='unknown'` predicate matches 0 rows → rollback, no partial state. |
| Process dies mid-transaction | Single `BEGIN IMMEDIATE`; SQLite rolls back; nothing visible. |
| `--apply` run with no/stale backup | `backup_missing` / `backup_digest_mismatch` → exit 64 before any write. |
| Tool pointed at the wrong database | Identity preconditions (epoch + three exact admission IDs with expected generation) fail closed. |
| Unrelated live admissions (`queued`, `started`, fresh `unknown`) present | Out of scope by ID; the tool asserts only the three targets changed and refuses if any new non-terminal admission appears. |
| Tool alters schema or FTS | It never opens `SessionDB`; `sqlite_master` compared before/after (unchanged) and FTS shadow tables are untouched because no message row is written. The row-count guard skips the derived shadow tables for that reason (§6.2.6). |
| Backup restores an already-corrupt FTS index | Known: the store's `messages_fts` inverted index is already malformed. The backup propagates it; this mission does not repair it and no step depends on FTS. |
| Disk exhaustion during backup or copy | Stage 0 requires 2× free space; `VACUUM INTO` fails closed rather than producing a truncated file. |
| Treating "no rows for 26 days" as proof of inactivity | Rejected as a method: inactivity is established by absent PIDs *and* a boot time later than those processes, empty outbox queues, zero messages, terminal workers and zero obligations — not by age. |
| Inventing a status | Rejected: only the runtime's own pair (`terminal`, `interrupted`) is written, through the runtime's own code path or a statement-for-statement copy. |

## 9. What this does not do

- It does not repair `messages_fts` (`malformed inverted index for FTS5 table main.messages_fts`).
  That is a live-store write and out of scope here.
- It does not remove the recovery hold, the protective drop-ins, or touch PR #9.
- It does not guarantee a quiet window: two other `unknown` admissions exist, both dated, and the
  trigger re-checks the sample on every attempt.
- It does not prove Lane A end-to-end. Lane A is a production write; only its code path, its CLI
  surface and its equality with Lane B are established here.
- It does not decide the outcome for the *other* unknown rows, including one created today on this
  very Telegram thread.

## 10. Decision requested

One bounded human authorisation covering: Stage 0 preflight and backup; Stage 1–2 acknowledgement of
admissions `40a8ac83…`, `faf7f306…`, `e7aa5bc9…` via `hermes sessions discard`; Stage 3 read-only
re-observation; with Lane B (`adm_recon.py --apply`) as the declared fallback and no release
activity of any kind. All four stop conditions and the stop-at-failure rule are inherent to the
package.

## 11. Package contents

Committed with this document so the evidence is re-derivable, not merely asserted. These are the
exact bytes that produced the numbers in §6.3; they are operator tooling outside the release gate
(`testpaths = ["tests"]`), and nothing in the release path imports them.

| Path | sha256 | Role |
|---|---|---|
| `harness/reconcile/adm_recon.py` | `118d0bf6…1bb71035` | Lane B tool, guard-v2: dry-run default, `--apply` requires exactly one roster admission plus mandatory `--expect-epoch`/`--backup`/`--backup-sha256` (digest recomputed from the file), fenced CAS, post-write delta proof, exit `0/2/64/65/70`. |
| `harness/reconcile/pre_guard/adm_recon_ea7e1dc.py` | `0bce8e4d…376a693a` | The frozen bytes of commit `ea7e1dc`, kept only so the red controls execute the original behaviour. Never run against anything but a disposable copy. |
| `harness/reconcile/red_controls.py` | `fda88e81…988dbdb26` | Red/green negative controls: frozen pre-guard vs corrected tool on byte-identical copies with an audit trigger, 32 checks. |
| `harness/reconcile/equivalence_test.py` | `d6c5684b…44e718a2c` | Lane A vs Lane B on two copies of one store, 38 row-image checks, `--mixed` for the one-at-a-time sequence, `--sequential` for a 13.6 GB copy. |
| `harness/reconcile/test_adm_recon_fixture.py` | `e635c57c…bcda1ccc3` | Adversarial suite on disposable fixture copies, 153 checks. |
| `harness/reconcile/make_small_fixture.py` | `5b443b1c…0c88a4475` | Builds the fixture from the real DDL. |
| `harness/reconcile/README.md` | `a69a9b50…682c5da5e` | Usage, exit codes, backup rule, host-path anchors. |

Reproduction (needs read access to the production store; no writes):

```bash
cd harness/reconcile
/usr/bin/python3.13 make_small_fixture.py            # fixture/small.db
/usr/bin/python3.13 test_adm_recon_fixture.py        # 153/153
/usr/bin/python3.13 red_controls.py                  # 32/32
/usr/bin/python3.13 equivalence_test.py fixture/small.db \
    --workdir fixture/equiv-small --allow-runtime-migrations   # 38/38
```

The full-copy run additionally needs a `VACUUM INTO` copy of the store (§7 Stage 0) and
`--sequential --pristine <copy>`; it rewrites both copies, so it must never point at the live file.
