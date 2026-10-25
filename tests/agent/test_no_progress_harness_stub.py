"""The no-progress guardrail must not count harness redundancy stubs as progress loss.

``read_file``/``skill_view`` return a harness-generated "nothing changed" payload on a
repeated identical read. There are two shapes and only ONE carries the
``guardrail_refusal`` marker:

  * soft "unchanged" stub (``tools/file_tools.py::_dedup_stub_or_block``, ``hits == 1``)
    -> ``{"status": "unchanged", "dedup": true, "content_returned": false}``
  * hard refusal (``_dedup_stub_or_block``, ``hits >= 2``)
    -> ``tool_error(..., guardrail_refusal=True)``

Both are the harness deciding a redundant call itself, NOT the task failing to make
progress. ``classify_tool_failure`` already treats the hard refusal as "not failed", so
it falls through to the SUCCESS path in ``after_call`` and lands on the idempotent
no-progress counter. The stub bodies are byte-identical by construction, so on a long
investigation — where intervening tool calls pop only their own signature, and
compaction re-arms the read while leaving the guardrail counter running — five of them
on one ``(tool, args)`` signature trip ``idempotent_no_progress_block`` and the turn dies
with "I kept running read_file N times without making progress" over calls that never
failed.

Invariants pinned here:
  * harness payloads are skipped by (and reset) the no-progress counter, whether the
    caller passes ``failed`` explicitly or lets the classifier decide;
  * a genuine unchanged-CONTENT loop still halts at the configured threshold.
"""

import json

import pytest

from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController

_ARGS = {"path": "/some/file.txt", "offset": 1, "limit": None}

# Emitted by tools/file_tools.py::_dedup_stub_or_block on the first repeat (hits == 1).
_SOFT_DEDUP_STUB = json.dumps({
    "status": "unchanged",
    "message": "This file has not changed since you last read it.",
    "path": "/some/file.txt",
    "dedup": True,
    "content_returned": False,
})

# Emitted by _dedup_stub_or_block once hits >= 2; carries the refusal marker.
_HARD_REFUSAL = json.dumps({
    "error": "BLOCKED: You have called read_file on this exact region 3 times ...",
    "path": "/some/file.txt",
    "already_read": 3,
    "guardrail_refusal": True,
})

# Same soft marker, different tool (tools/skills_tool_dedup.py).
_SKILL_DEDUP_STUB = json.dumps({
    "success": True,
    "status": "unchanged",
    "name": "some-skill",
    "dedup": True,
    "content_returned": False,
})

_STUBS = [
    ("read_file", _SOFT_DEDUP_STUB, "soft-dedup-stub"),
    ("read_file", _HARD_REFUSAL, "hard-refusal"),
    ("skill_view", _SKILL_DEDUP_STUB, "skill-dedup-stub"),
]


def _armed_controller() -> ToolCallGuardrailController:
    """Hard stops ON and the idempotent no-progress block armed at 5, as they are for
    unattended platforms (gateway/Telegram/cron)."""
    return ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            no_progress_warn_after=2,
            no_progress_block_after=5,
        )
    )


@pytest.mark.parametrize(("tool_name", "stub", "case"), _STUBS, ids=[c for _, _, c in _STUBS])
def test_harness_redundancy_stubs_never_trip_the_no_progress_block(tool_name, stub, case):
    controller = _armed_controller()

    for _ in range(6):
        assert controller.before_call(tool_name, _ARGS).action == "allow"
        decision = controller.after_call(tool_name, _ARGS, stub, failed=False)
        assert decision.action in ("allow", "warn"), decision
        assert decision.code != "idempotent_no_progress_warning", decision

    assert controller.halt_decision is None
    assert controller.before_call(tool_name, _ARGS).action == "allow"


@pytest.mark.parametrize(("tool_name", "stub", "case"), _STUBS, ids=[c for _, _, c in _STUBS])
def test_harness_redundancy_stubs_are_skipped_when_failure_classification_is_automatic(tool_name, stub, case):
    """``failed=None`` makes the guardrail classify the payload itself; the exemption must
    not depend on the caller labelling the result."""
    controller = _armed_controller()

    for _ in range(6):
        assert controller.before_call(tool_name, _ARGS).action == "allow"
        decision = controller.after_call(tool_name, _ARGS, stub)
        assert decision.code != "idempotent_no_progress_warning", decision
        assert decision.code != "idempotent_no_progress_block", decision

    assert controller.halt_decision is None
    assert controller.before_call(tool_name, _ARGS).action == "allow"


def test_a_stub_does_not_inherit_or_feed_the_real_content_count():
    """A harness stub short-circuits the call, so it must not carry the count of a real
    content read into the next one (and must not add to it)."""
    controller = _armed_controller()
    content = json.dumps({"success": True, "content": "the same lines\n"})

    for _ in range(4):
        controller.before_call("read_file", _ARGS)
        controller.after_call("read_file", _ARGS, content, failed=False)

    # The stub is skipped entirely: the counter for this signature is cleared.
    controller.before_call("read_file", _ARGS)
    decision = controller.after_call("read_file", _ARGS, _SOFT_DEDUP_STUB, failed=False)
    assert decision.count == 0, decision

    # A real read afterwards starts from scratch instead of continuing the old streak.
    controller.before_call("read_file", _ARGS)
    decision = controller.after_call("read_file", _ARGS, content, failed=False)
    assert decision.count == 1, decision


def test_genuine_unchanged_content_still_halts_as_no_progress():
    """The exemption is keyed on the harness marker, not on "nothing changed": a read that
    genuinely returns the same CONTENT must still halt, or the guardrail that stops a real
    loop is gone."""
    controller = _armed_controller()
    same_content = json.dumps({"success": True, "content": "the same lines\n"})

    for _ in range(5):
        assert controller.before_call("read_file", _ARGS).action == "allow"
        controller.after_call("read_file", _ARGS, same_content, failed=False)

    blocked = controller.before_call("read_file", _ARGS)
    assert blocked.action == "block"
    assert blocked.code == "idempotent_no_progress_block"
    assert blocked.count == 5
