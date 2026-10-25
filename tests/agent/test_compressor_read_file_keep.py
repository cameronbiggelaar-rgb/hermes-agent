"""A demoted ``read_file`` must not collapse to a bare announcement (2026-10-26).

The compaction path demoted every ``read_file`` body larger than ``_PRUNE_MIN_CHARS`` into
``"[read_file] read <path> from line N (N chars)"``. The model could not see the content it
had just paid for, so it re-read the same path with identical arguments, and the no-progress
guard halted the run at the 12th identical read.

The newest few ``read_file`` bodies now survive compaction with their content; older ones
keep the cheap announcement so the extra context cost stays bounded.
"""

import json
from unittest.mock import patch

from agent.context_compressor import (
    ContextCompressor,
    _READ_FILE_KEEP_COUNT,
    _READ_FILE_KEEP_MAX_CHARS,
    _read_file_keep_indices,
    _tool_calls_by_id,
)


def _compressor(**kwargs):
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
            **kwargs,
        )
        _ = c.context_length
        return c


def _read_messages(reads, tail_pad=6):
    """A transcript whose read_file results sit BEFORE the protected tail.

    ``reads`` is a list of ``(path, body)``; every read is an assistant tool_call followed by
    its tool result, so the compressor can map the row back to the call.
    """
    messages = [{"role": "user", "content": "please read"}]
    for i, (path, body) in enumerate(reads):
        call_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": path, "offset": 1})},
            }],
        })
        messages.append({"role": "tool", "tool_call_id": call_id, "content": body})
    messages.extend({"role": "user", "content": f"filler {i}"} for i in range(tail_pad))
    return messages


def _tool_row(messages, call_id):
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
            return msg
    raise AssertionError(f"tool row for {call_id} vanished")


class TestReadFileKeepsItsContent:
    def test_newest_read_file_body_keeps_its_content(self):
        body = "LINE-0\n" + ("x" * 600)
        messages = _read_messages([("/tmp/probe24.txt", body)])

        pruned, _ = _compressor()._prune_old_tool_results(messages, protect_tail_count=2)

        content = _tool_row(pruned, "call_0")["content"]
        assert "LINE-0" in content, "the read_file body was replaced by a bare announcement"

    def test_kept_body_is_stable_across_a_second_prune_pass(self):
        body = "LINE-0\n" + ("x" * 600)
        compressor = _compressor()

        pruned, _ = compressor._prune_old_tool_results(_read_messages([("/tmp/probe24.txt", body)]), protect_tail_count=2)
        pruned_again, _ = compressor._prune_old_tool_results(pruned, protect_tail_count=2)

        assert "LINE-0" in _tool_row(pruned_again, "call_0")["content"]

    def test_large_read_file_body_keeps_its_head_and_names_the_recovery(self):
        body = "HEAD-MARKER\n" + ("z" * (_READ_FILE_KEEP_MAX_CHARS * 2))
        messages = _read_messages([("/tmp/big.txt", body)])

        pruned, _ = _compressor()._prune_old_tool_results(messages, protect_tail_count=2)

        content = _tool_row(pruned, "call_0")["content"]
        assert content.startswith("HEAD-MARKER")
        assert len(content) <= _READ_FILE_KEEP_MAX_CHARS + 300
        assert "re-read with a different offset/limit" in content

    def test_read_file_bodies_beyond_the_keep_count_still_collapse(self):
        # Distinct bodies: identical ones are collapsed by the dedup pass, not by the demote pass.
        reads = [(f"/tmp/f{i}.txt", f"body-{i} " + "w" * 600) for i in range(_READ_FILE_KEEP_COUNT + 2)]
        messages = _read_messages(reads)

        pruned, _ = _compressor()._prune_old_tool_results(messages, protect_tail_count=2)

        collapsed = [
            msg for msg in pruned
            if msg.get("role") == "tool" and msg["content"].startswith("[read_file] read")
        ]
        assert len(collapsed) >= 2, "older read_file bodies must still collapse to the cheap announcement"

    def test_failed_read_file_still_reports_the_failure(self):
        failure = json.dumps({"error": "File not found: gone.py " + "why " * 60})
        messages = _read_messages([("/tmp/gone.py", failure)])

        pruned, _ = _compressor()._prune_old_tool_results(messages, protect_tail_count=2)

        content = _tool_row(pruned, "call_0")["content"]
        assert "FAILED" in content
        assert "File not found" in content

    def test_pressure_pass_keeps_the_kept_read_body(self):
        """Pass 4 (the pressure demotion that fired in the incident) must not strip a kept read.

        The read sits BEFORE the oversized bash body, so the pressure loop reaches it first:
        without the keep set the read body is demoted to the announcement, and the model has to
        re-read the same path.
        """
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{
                "id": "call_read", "type": "function",
                "function": {"name": "read_file",
                             "arguments": json.dumps({"path": "/tmp/probe24.txt", "offset": 1})},
            }]},
            {"role": "tool", "tool_call_id": "call_read", "content": "READMARK\n" + ("r" * 1200)},
            {"role": "assistant", "tool_calls": [{
                "id": "call_bash", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "cat big.log"})},
            }]},
            {"role": "tool", "tool_call_id": "call_bash", "content": "B" * 40000},
        ]
        messages.extend({"role": "user", "content": f"filler {i}"} for i in range(4))
        compressor = _compressor()
        call_id_to_tool = _tool_calls_by_id(messages)
        keep_reads = _read_file_keep_indices(messages, call_id_to_tool)

        demoted = compressor._pressure_demote_tail(
            messages, 0, 2000, call_id_to_tool, 200, range(0), keep_reads,
        )

        assert demoted >= 1, "the oversized bash body should have been demoted"
        assert "READMARK" in _tool_row(messages, "call_read")["content"], (
            "the pressure pass stripped the kept read_file body"
        )


class TestReadFileTwinKeepsItsBody:
    """The row that survives compaction in ``state.db`` must carry the body.

    ``archive_and_compact()`` does not materialise an announcement: it re-inserts the
    caller's in-memory dicts as fresh active rows.  The 2026-10-26 incident wrote 89-char
    announcement twins because the demotion had already replaced the body upstream, so the
    unit-level seam alone does not prove what the model actually reads back.
    """

    def test_persisted_twin_row_keeps_the_read_body(self):
        import tempfile
        from pathlib import Path

        from hermes_state import SessionDB

        body = "LINE-0\n" + ("x" * 600)
        messages = _read_messages([("/tmp/probe24.txt", body)])

        pruned, _ = _compressor()._prune_old_tool_results(messages, protect_tail_count=2)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "t.db")
            db.ensure_session("s1", source="test")
            db.append_message("s1", "user", "seed")
            db.archive_and_compact("s1", pruned)

            rows = db.get_messages("s1")

        kept = [row for row in rows if "LINE-0" in (row.get("content") or "")]
        assert kept, "the persisted row lost the read_file body"
        announced = [row for row in rows if (row.get("content") or "").startswith("[read_file] read")]
        assert not announced, "the persisted row is the bare announcement, not the body"
