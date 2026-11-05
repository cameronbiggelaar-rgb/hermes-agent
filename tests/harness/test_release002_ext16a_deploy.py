"""EXT-16A: the deploy/unit phase must survive the production unit layout.

Three defects found by the isolated APPLY run, none reachable by the pre-freeze dry run:

D-3  ``receipt()`` and the quiescence loop redirect into ``$ARCHIVE`` before anything creates the
     directory, so a first run with a fresh run id loses its admission samples to
     "No such file or directory" and the receipt step exits non-zero.
D-4  the unit check demanded ``TimeoutStopSec=300`` *in the regenerated unit*. The stock unit ships
     210 and the operator drop-in ``95-timeout-stop.conf`` raises the effective value to 300s, so
     every correctly regenerated unit was rolled back one step after a successful merge.
D-5  the launcher derived an archive path for the release but never passed it into the transient
     supervisor unit, so the engine fell back to the default archive while the caller and the later
     ``verify`` invocation looked somewhere else.

    env -u PYTHONPATH /usr/bin/python3 -m pytest tests/harness/test_release002_ext16a_deploy.py -q
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "harness" / "release002_release.sh"
LAUNCHER = ROOT / "harness" / "hermes-release-002"


def engine() -> str:
    return ENGINE.read_text()


def test_archive_is_created_before_first_use() -> None:
    text = engine()
    mkdir_at = text.index('mkdir -p "$ARCHIVE"')
    first_use = text.index('>"$ARCHIVE/admission-')
    assert mkdir_at < first_use
    assert text.index('ARCHIVE=${ARCHIVE:-') < mkdir_at


def test_launcher_passes_the_archive_into_the_supervisor_unit() -> None:
    text = LAUNCHER.read_text()
    run = text[text.index("local args=("):]
    assert '--setenv=ARCHIVE="$archive"' in run
    # The engine reads $ARCHIVE; the prefixed name keeps the intent visible in the unit's env.
    assert '--setenv=HERMES_RELEASE_ARCHIVE="$archive"' in run


def test_effective_timeout_is_the_contract_not_the_unit_number() -> None:
    text = engine()
    block = text[text.index('UNIT_TIMEOUT=$(awk'):text.index('journal unit "regenerated unit')]
    assert 'EFFECTIVE=${EFFECTIVE#TimeoutStopUSec=}' in block
    assert 'effective TimeoutStopUSec' in block
    assert 'generated unit does not carry TimeoutStopSec=300s' not in block
    assert "5min" in block and "300000000" in block


def test_unit_number_mismatch_is_reported_not_fatal() -> None:
    text = engine()
    block = text[text.index('case "$UNIT_TIMEOUT" in'):text.index('POST_UNIT_SHA=$(sha256sum')]
    assert "rollback" not in block, "a unit/drop-in split must not roll back"
    assert "drop-in" in block


def test_quiescence_log_reads_the_sample_it_wrote() -> None:
    """D-7: the counter moves before the log line, so the old form opened a file nobody wrote yet."""
    text = engine()
    loop = text[text.index("while [ $SECONDS -lt $deadline ]"):text.index("if [ \"$quiet\" -lt 3 ]")]
    assert "idx=$quiet" in loop
    assert '>"$ARCHIVE/admission-$idx.json"' in loop
    assert '<"$ARCHIVE/admission-$idx.json"' in loop
    assert '<"$ARCHIVE/admission-$quiet.json"' not in loop
