# Installing the corrected quiet-window trigger (v2)

`qw-launch-apply-v2.sh` in this directory is a **versioned correction, not an installation**. It is not
installed by EXT-17A and must not be installed without the owner's explicit approval.

The authoritative procedure, the defects it fixes, the measurements and the remaining risks are in
[`docs/restore002/EXT17A-ENVELOPE-QUALIFICATION.md`](../../docs/restore002/EXT17A-ENVELOPE-QUALIFICATION.md),
§3–§7. Read it before touching `/usr/local/lib/hermes-release/`.

Summary of the owner's steps (nothing here is a command to run blindly):

1. Record the installed v1 `sha256` from the host as the approval baseline.
2. Review v1 and v2 side by side; v2 must differ in exactly the five documented behaviours.
3. Copy v2 to `/usr/local/lib/hermes-release/qw-launch-apply-v2.sh`, `root:root`, mode `0700`; leave v1
   in place.
4. Run the twelve trigger tests against the installed copy as root:
   `TRIGGER_UNDER_TEST=/usr/local/lib/hermes-release/qw-launch-apply-v2.sh pytest
   tests/harness/test_release002_ext17a_trigger.py`.
5. Switch the unit's `ExecStart` to the v2 path — one line, reversible — and record the unit's
   before/after sha256.
6. Arm with the candidate identity that is actually being released, and keep the resulting
   `attempt-fired.json` and journal as the record of what fired.

If any step is skipped, the trigger must stay at v1: a trigger that reports a fire it did not make is
worse than one that is merely old.
