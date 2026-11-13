# `harness/reconcile` — operator tooling for a fenced admission reconciliation

These four files are the **exact artifacts executed** for mission `RESTORE-002-ADM-RECON-01`, kept
verbatim so the published evidence can be re-derived. They are operator tooling, not release code:

- they are **not collected by pytest** (`pyproject.toml` sets `testpaths = ["tests"]`, so files here
  are outside the release gate);
- they require read access to a live Hermes store and, for `--apply`, a verified backup;
- nothing in the release path imports them.

Design and evidence: `docs/restore002/ADM-RECON-01-RECONCILIATION-DESIGN.md`.

## Files

| File | Role |
|---|---|
| `adm_recon.py` | The reconciliation tool. Dry-run by default; `--apply` is the only write path. Fenced terminal transition (`status='terminal'`, `outcome='interrupted'`) mirroring `hermes_state_runtime.resolve_unknown_session_input`: one `BEGIN IMMEDIATE`, CAS on `(admission_id, status='unknown', generation, owner_epoch)` with row-count assertions, worker retirement, `sessions.runtime_revision` bump, post-write verification in a fresh connection. Never opens `SessionDB`. |
| `make_small_fixture.py` | Builds a purpose-made fixture: real DDL of the eight touched tables, the three target rows verbatim, plus terminal decoys. Needs read access to the production store. |
| `test_adm_recon_fixture.py` | Adversarial suite over disposable copies of that fixture (49 checks): every refusal path, the exactly-six-updates assertion, idempotency, fail-closed on new non-terminal work. |
| `equivalence_test.py` | Runs the tool and the runtime's own path on two copies of the same store and compares every row image the transition may touch (31 checks). |

## Usage

```bash
# dry run — no --apply, no --backup needed
/usr/bin/python3.13 adm_recon.py --db /path/to/store.db \
    --trigger-script harness/trigger/qw-launch-apply-v2.sh --out out/dry

# apply, only with a verified backup and the epoch the operator was authorised for
/usr/bin/python3.13 adm_recon.py --db /path/to/store.db \
    --trigger-script harness/trigger/qw-launch-apply-v2.sh --out out/apply \
    --apply --backup /path/to/backup.db --backup-sha256 <sha256> --expect-epoch <epoch>
```

Exit codes: `0` committed or already terminal; `2` dry-run refusal; `64` precondition, guard, backup
or epoch refusal; `65` post-write verification failed; `70` internal error.

Take the backup with `VACUUM INTO` from a `mode=ro` connection, not `.backup`: under continuous
gateway writes `.backup` restarts indefinitely (observed: 559 GB read for a 13.6 GB store).

## Host paths in these files

They are evidence anchors, not portability bugs: `make_small_fixture.py` reads
`/home/hermes/.hermes/state.db`, and both `equivalence_test.py` and `test_adm_recon_fixture.py` pin
`TRIGGER` to `/home/hermes/work/restore002-fix/harness/trigger/qw-launch-apply-v2.sh`.
`equivalence_test.py` also puts `/home/hermes/.hermes/hermes-agent` on `sys.path` for Lane A
(`HERMES_HOME` is defaulted, not overridden). Re-running elsewhere means changing those constants
and nothing else.
