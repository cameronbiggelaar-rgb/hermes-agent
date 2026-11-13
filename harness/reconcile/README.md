# `harness/reconcile` — operator tooling for a fenced admission reconciliation

These files are the **exact artifacts executed** for mission `RESTORE-002-ADM-RECON-01`, kept
verbatim so the published evidence can be re-derived. They are operator tooling, not release code:

- they are **not collected by pytest** (`pyproject.toml` sets `testpaths = ["tests"]`, so files here
  are outside the release gate);
- they require read access to a live Hermes store and, for `--apply`, a verified backup;
- nothing in the release path imports them.

Design and evidence: `docs/restore002/ADM-RECON-01-RECONCILIATION-DESIGN.md`.

## Files

| File | Role |
|---|---|
| `adm_recon.py` | The reconciliation tool. Dry-run by default; `--apply` is the only write path, and it acts on **exactly one** named admission. Fenced terminal transition (`status='terminal'`, `outcome='interrupted'`) mirroring `hermes_state_runtime.resolve_unknown_session_input`: one `BEGIN IMMEDIATE`, CAS on `(admission_id, status='unknown', generation, owner_epoch)` with row-count assertions, worker retirement, `sessions.runtime_revision` bump, then a post-write delta proof in a fresh read-only connection. Never opens `SessionDB`. |
| `pre_guard/adm_recon_ea7e1dc.py` | The frozen pre-correction tool (commit `ea7e1dc`), kept byte-identical as the negative-control baseline. Not for use. |
| `make_small_fixture.py` | Builds a purpose-made fixture: real DDL of the eight touched tables, the three target rows verbatim, plus terminal decoys. Needs read access to the production store. |
| `test_adm_recon_fixture.py` | Adversarial suite over disposable copies of that fixture (153 checks): explicit-selection refusals, the one-admission canary, the one-at-a-time sequence, every missing/malformed/stale input, injected post-write deviations, and the preserved CAS/ownership/transcript/worker guards. |
| `red_controls.py` | Negative controls (32 checks): each CA-named defect is executed against the frozen pre-correction tool and must be *observed*, then against the corrected tool and must be refused (64) or reported unproven (65). Uses an audit trigger so "did it write" is measured, not self-reported. |
| `equivalence_test.py` | Runs the tool and the runtime's own path on copies of the same store and compares every row image the transition may touch (38 checks on the small fixture, 40 `--mixed` / 42 tool-only on the 13.6 GB production copy). `--mixed` runs Lane A for the first admission and the tool for the rest on one copy; `--sequential` diffs one big copy with bounded disk. |

## Usage

```bash
# dry run — no --apply, no --backup needed
/usr/bin/python3.13 adm_recon.py --db /path/to/store.db \
    --trigger-script harness/trigger/qw-launch-apply-v2.sh --out out/dry

# apply — exactly one admission, with the authorised epoch and an independently hashed backup
/usr/bin/python3.13 adm_recon.py --db /path/to/store.db \
    --trigger-script harness/trigger/qw-launch-apply-v2.sh --out out/apply \
    --admission <admission-id> --expect-epoch <epoch> \
    --backup /path/to/backup.db --backup-sha256 <sha256> --apply

# then, separately, the next identity (never a multi-row apply in one invocation)
/usr/bin/python3.13 adm_recon.py ... --admission <next-admission-id> --apply
```

`--apply` refuses unless it was given exactly one `--admission` from the roster, `--expect-epoch`,
`--backup` and `--backup-sha256`; the digest is recomputed from the file and the backup's own epoch
and integrity are checked. Every refusal happens before a writable connection is opened.

Exit codes: `0` committed **and** delta-verified, or already terminal (no-op); `2` dry-run: preconditions
hold, nothing written; `64` input/guard/backup/epoch refusal, nothing written; `65` the write may have
committed but the exact allowed delta was **not** proven — treat as not done and investigate; `70`
internal or database error.

Take the backup with `VACUUM INTO` from a `mode=ro` connection, not `.backup`: under continuous
gateway writes `.backup` never converges (observed: two attempts, one abandoned at 6,700,236,800 of
13,648,543,744 bytes after the full allowance, leaving an unopenable partial file).

## Host paths in these files

They are evidence anchors, not portability bugs: `make_small_fixture.py` reads
`/home/hermes/.hermes/state.db`, and both `equivalence_test.py` and `test_adm_recon_fixture.py` pin
`TRIGGER` to `/home/hermes/work/restore002-fix/harness/trigger/qw-launch-apply-v2.sh`.
`equivalence_test.py` also puts `/home/hermes/.hermes/hermes-agent` on `sys.path` for Lane A
(`HERMES_HOME` is defaulted, not overridden). `test_adm_recon_fixture.py` and `red_controls.py` work
inside `fixture/` and write nothing outside it. Re-running elsewhere means changing those constants
and nothing else.

## Qualification order

```bash
/usr/bin/python3.13 test_adm_recon_fixture.py        # adversarial suite, disposable fixture
/usr/bin/python3.13 red_controls.py                  # pre-correction tool vs corrected tool
/usr/bin/python3.13 equivalence_test.py fixture/small.db --allow-runtime-migrations
/usr/bin/python3.13 equivalence_test.py fixture/small.db --mixed --allow-runtime-migrations
/usr/bin/python3.13 equivalence_test.py <store-copy>.db --sequential --pristine <store-copy>.db
```
