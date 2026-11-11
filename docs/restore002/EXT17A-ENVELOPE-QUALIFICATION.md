# EXT-17A — supervisor and trigger envelope qualification

Status: evidence for External CA review. Nothing in this document was installed, started, enabled or
re-armed. The installed trigger is unchanged (root-owned, `0700`, still v1).

## 1. What the envelope has to guarantee

Two independent clocks can end an attempt: the supervisor's `RuntimeMaxSec` (the release budget) and
the quiet-window trigger's `DEADLINE_S` (how long to wait for a quiet host). For the release to be
reviewable, both must be *effective* (they actually stop something), every child must be bounded
with them, neither may retry, and whichever one ends the attempt must leave an authoritative
terminal record rather than a plausible-looking exit.

## 2. Supervisor: measured on this host (ephemeral transient units, `--collect`)

Each probe is a throwaway unit created with `systemd-run --no-block`, carrying the exact property set
the release supervisor uses. No installed unit, drop-in or trigger file was touched.

| probe | properties observed while running | outcome |
| --- | --- | --- |
| `p1-term` | `Type=exec`, `RuntimeMaxUSec=6s`, `TimeoutStopUSec=5s`, `KillMode=control-group`, `SendSIGKILL=yes`, `Restart=no` | body logged `STARTED 07:23:03Z`; the cap delivered SIGTERM at `07:23:10Z`; body's own trap ran and exited 70 |
| `p2-children` | same | body forked `sleep 600`; after the cap **no** `sleep 600` process remained (cgroup kill, not just PID 1) |
| `p3-settle` | same, `RuntimeMaxUSec=12s` | the **shipped settlement library** was sourced into the unit body; the cap's SIGTERM produced `signal TERM received in phase 'quiesce'` → `admission restored` → terminal journal `phase=aborted`, `result=ABORTED_SAFE`, `mutated=false`; drain marker and ESTOP sentinel gone; no NEEDS_HUMAN evidence |

Reading of the three:

- The cap is real only because the unit is `Type=exec`. This host reproduced the opposite earlier in
  EXT-17: `Type=oneshot` + `RuntimeMaxSec=5s` ran a 30 s body to `Result=success` and systemd logged
  that the property has no effect for that type. R-11's change to `Type=exec` is therefore what makes
  the release budget enforceable, and `ExecMainStatus` keeps its one-process/one-exit-status meaning.
- `KillMode=control-group` bounds the engine's children (helper python, `git`, `systemctl`, drain
  pollers), not only the engine. A settlement that itself hangs is covered by `SendSIGKILL=yes` after
  `TimeoutStopSec` — which the supervisor sets to 420 s, longer than the 300 s effective stop budget
  and shorter than the 1500 s release budget.
- The supervisor unit is created with no `Restart=` property at all, and `systemd-run` transient units
  default to `Restart=no`, so the trigger cannot be turned into a retry loop. Measured `NRestarts=0`.
- A supervisor timeout is not an unexplained death: the shipped `on_signal` → `on_exit` path settles
  it, and the journal (not the exit status) becomes the authoritative record.

Note on method: with `--collect` the unit is released as soon as it exits, so `systemctl show` returns
defaults afterwards. Properties are captured **while the unit runs**, and terminal outcomes are read
from the attempt's own artefacts and journal rather than from a post-mortem `Result=` field.

## 3. Installed trigger (v1): the five envelope defects

`/usr/local/lib/hermes-release/qw-launch-apply.sh`, 82 lines, unchanged and out of scope to modify.
Read-only review found:

1. **It re-opened the R-12 trust hole.** The launcher is invoked with
   `GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'`, i.e. git trust for every repository on
   the host, for the whole launcher invocation. R-12 had deliberately narrowed this to the two
   repository paths in `/etc/gitconfig`; v1 widens it again for the process that starts a release.
2. **It reports a fire it did not make.** `attempt-fired.json` is written after the launcher call
   regardless of `rc`, so a launcher that refused (e.g. exit 64) still leaves a record that reads as
   a spent attempt — and the script still `exit 0`.
3. **The retry guard has a hole.** The only guard is the engine's `journal.json`, which the engine
   creates *after* the launcher returns. A crash inside that window leaves a host that looks
   un-armed and re-armable.
4. **The deadline can be overshot and the disarm is silent.** The deadline is checked only at the top
   of the loop, so the last `sleep $INTERVAL` can run past it; and a TERM during the watch exits with
   no record of why the trigger stopped.
5. **The candidate is hardcoded** (`CAND=3c3e24a5…`), so a re-arm would replay a spent identity — the
   exact thing EXT-17A forbids.

## 4. Correction: `harness/trigger/qw-launch-apply-v2.sh` (versioned, not installed)

Same shape, same budgets (`NEED=6`, `INTERVAL=20`, `DEADLINE_S=14400`), no new gate and no bypassed
gate. Differences, one per defect above:

1. Trust is granted for exactly `/home/hermes/.hermes/hermes-agent` and
   `/home/hermes/work/restore002-fix` (`GIT_CONFIG_COUNT=2`), never `'*'`.
2. The attempt record is written **atomically before** the call with `status="pending"`, then
   rewritten to `fired` (with the parsed `engine_run_id`) or `failed` (with `apply_rc`). A refusal or
   a launcher that prints no supervisor line exits **4**, not 0.
3. Arming refuses on either artefact — `journal.json` **or** `attempt-fired.json` — and re-checks both
   immediately before firing (exit 76), plus the existing `hermes-release-*` unit check (exit 77).
4. The inter-sample sleep is clamped to the remaining deadline, and TERM/INT before the fire logs
   `DISARMED by SIG<sig> … no retry` and exits 143.
5. `--candidate` (40 lowercase hex) and `--expect-head` are required arguments; nothing is hardcoded.

Overrides for the state dir, the observation instruments, the launcher and the three budgets are
accepted only with `TRIGGER_QUALIFY=1`, so no environment can quietly shorten a real watch or point a
real trigger at a different database.

## 5. Trigger evidence: green and red

Twelve tests in `tests/harness/test_release002_ext17a_trigger.py` drive v2 in a sandbox with a fake
launcher, a real SQLite session database, a stubbed `systemctl` and a 3-second deadline where the
watch is meant to expire:

- fires exactly once, parses the run id, records `status=fired`, and passes exactly two
  `safe.directory` paths with no `'*'`;
- a refused launch is recorded `status=failed` with `apply_rc` and exits 4;
- a launcher that exits 0 without printing a supervisor line is **not** reported as fired;
- a second arming exits 76 and does not invoke the launcher again;
- a journal without a fired record still refuses;
- deadline expiry exits 3, invokes nothing, writes no attempt record;
- an unreadable gateway-state file never counts as a quiet sample;
- a TERM during the watch exits 143, fires nothing, leaves no record, and logs the disarm;
- invalid candidates (short, non-hex, empty) exit 64 before any observation;
- v1 is still the installed trigger;
- the supervisor's unit arguments still carry `Type=exec`, `RuntimeMaxSec`, `TimeoutStopSec=420`,
  `KillMode=control-group`, `SendSIGKILL=yes`, `--collect`, and no `Restart=`.

Red control: a mutant of v2 with v1's semantics restored (wildcard trust, `exit 0` on refusal, no
pending record) fails **3 of 12** — the fire/record, refusal-propagation and no-run-id tests — while
the remaining nine still pass, so the red is caused by the corrected behaviours and not by the
sandbox.

## 6. Installation procedure (for future owner approval; not executed here)

1. Verify the candidate identity in the repo: the commit carrying this document and
   `harness/trigger/qw-launch-apply-v2.sh` is the only source; record its SHA and the file's sha256.
2. Confirm the installed trigger is still v1 and unchanged: run `sha256sum
   /usr/local/lib/hermes-release/qw-launch-apply.sh` and record the value as the approval baseline.
   No hash is written into this document on purpose: a copied hash is not evidence, and the value must
   be read from the host at the moment of approval.
3. Copy v2 to `/usr/local/lib/hermes-release/qw-launch-apply-v2.sh`, `chown root:root`, `chmod 0700`,
   and leave v1 in place until the owner has reviewed both side by side.
4. Re-run the twelve trigger tests against the installed copy
   (`TRIGGER_UNDER_TEST=/usr/local/lib/hermes-release/qw-launch-apply-v2.sh`) as root, and record the
   output.
5. Switch over only by changing the unit's `ExecStart` to the v2 path — one line, reversible — and
   record the unit's before/after sha256.
6. Arm with the *new* candidate identity and its `--expect-head`; the attempt record and journal are
   the proof of what fired.

Steps 3–6 are owner actions. EXT-17A performed none of them.

## 7. Remaining risks

- The trigger still cannot observe the supervisor's outcome; it deliberately exits after firing. The
  authoritative terminal record is the journal plus `hermes-release-002 verify`, and any process that
  treats the trigger's exit status as the release result would be wrong. This is unchanged from v1
  and is documented here rather than "fixed" by making the trigger wait.
- `attempt-fired.json` is written before the launcher call, so a host killed between the record and
  the call leaves `status="pending"` with no run. That is the safe direction: it blocks a re-arm and
  is visible in the record, whereas the reverse would hide a possible fire.
- The supervisor's `RuntimeMaxSec` comes from `ctrl envelope`; EXT-17A did not change it, and the
  measured 1500 s budget is longer than every inner budget it bounds.
- The transient-unit probes were run as root on the host. They created no persistent artifact and
  were collected on exit, but they are live systemd activity and are disclosed as such.
