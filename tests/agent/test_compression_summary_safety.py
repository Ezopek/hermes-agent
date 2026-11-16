"""``compression.abort_on_summary_failure=true`` preserves the transcript on EVERY summary-less path.

Real ``AIAgent._compress_context`` + real ``ContextCompressor`` + real ``SessionDB`` with in_place=False
rotation; only the external summary provider transport (``call_llm``) is replaced. Covers the benched
summary model (fallback streak >= 2), its bounded probe, ordinary feasibility skips, default-policy
compatibility and the streak/deadline hand-off to a rotated child.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB

PARENT = "PARENT_SAFETY"
_PROBE_KEY = "_fallback_probe_at"
_TOOL_OUTPUT = "full original tool output line\n" * 400


@pytest.fixture
def db(tmp_path: Path):
    database = SessionDB(db_path=tmp_path / "state.db")
    database.create_session(PARENT, source="cli")
    for i in range(8):
        database.append_message(PARENT, "user", f"question {i} " + "x" * 300)
        database.append_message(PARENT, "assistant", f"answer {i} " + "y" * 300)
    database.append_message(
        PARENT, "assistant", None,
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}],
    )
    database.append_message(PARENT, "tool", _TOOL_OUTPUT, tool_call_id="call_1")
    for i in range(4):
        database.append_message(PARENT, "user", f"recent question {i}")
        database.append_message(PARENT, "assistant", f"recent answer {i}")
    try:
        yield database
    finally:
        database.close()


def _compressor(db: SessionDB, session_id: str, *, abort: bool) -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/model", threshold_percent=0.85, protect_first_n=2, protect_last_n=2,
            quiet_mode=True, abort_on_summary_failure=abort,
        )
        _ = compressor.context_length
    compressor.bind_session_state(db, session_id)
    return compressor


def _agent(db: SessionDB, *, abort: bool):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model", platform="cli",
            quiet_mode=True, session_db=db, session_id=PARENT, skip_context_files=True, skip_memory=True,
        )
    agent.context_compressor = _compressor(db, PARENT, abort=abort)
    agent.compression_in_place = False
    return agent


def _healthy_summary() -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "## Goal\nsemantic summary of the middle"
    response.choices[0].finish_reason = "stop"
    return response


def _compact(agent, db: SessionDB):
    """The supported caller seam. Returns ``(returned, original, telemetry payloads)``."""
    original = db.get_messages_as_conversation(PARENT)
    payloads: list[dict] = []
    with patch("agent.conversation_compression_telemetry._log_attempt_record", side_effect=payloads.append):
        returned, _ = agent._compress_context(original, "sys", approx_tokens=120_000)
    return returned, original, payloads


def _assert_preserved(agent, db: SessionDB, returned, original, rows_before, payloads) -> None:
    assert json.dumps(returned, sort_keys=True) == json.dumps(original, sort_keys=True)
    assert _TOOL_OUTPUT in [m.get("content") for m in returned]
    assert agent.session_id == PARENT
    assert db.find_live_compression_child(PARENT) is None
    assert db.get_messages(PARENT) == rows_before
    assert not any(p.get("commit_status") == "committed" for p in payloads)
    assert not any(p.get("fallback_used") for p in payloads)


def _bench(db: SessionDB, *, streak: int = 2, probe_at: float = 0.0, ineffective: int = 0) -> None:
    db.set_compression_fallback_streak(PARENT, streak)
    if probe_at:
        db.patch_session_model_config(PARENT, {_PROBE_KEY: probe_at})
    if ineffective:
        db.set_compression_ineffective_count(PARENT, ineffective)


@pytest.mark.parametrize("probe_at", [0.0, "future"], ids=["unarmed_deadline", "active_deadline"])
def test_abort_policy_preserves_transcript_while_summary_model_is_benched(db, probe_at):
    _bench(db, probe_at=time.time() + 3600 if probe_at == "future" else 0.0)
    agent = _agent(db, abort=True)
    rows_before = db.get_messages(PARENT)

    with patch("agent.context_compressor.call_llm", return_value=_healthy_summary()) as transport:
        returned, original, payloads = _compact(agent, db)

    transport.assert_not_called()
    _assert_preserved(agent, db, returned, original, rows_before, payloads)
    assert agent.context_compressor._last_compress_aborted
    assert [p["failure_class"] for p in payloads] == ["summary_model_benched"]
    assert db.get_compression_fallback_streak(PARENT) == 2


def test_default_policy_still_degrades_to_deterministic_fallback_when_benched(db):
    _bench(db)
    agent = _agent(db, abort=False)

    with patch("agent.context_compressor.call_llm", return_value=_healthy_summary()) as transport:
        returned, original, payloads = _compact(agent, db)

    transport.assert_not_called()
    child = agent.session_id
    assert child != PARENT
    assert len(returned) < len(original)
    assert [p["method"] for p in payloads] == ["deterministic_fallback"]
    # The skipped-summary commit must not zero the bench on the rotated child, or a rebuilt
    # compressor (restart, gateway cache eviction) would pay for the failing summary model again.
    assert db.get_compression_fallback_streak(child) == 2
    rebuilt = _compressor(db, child, abort=False)
    assert rebuilt._fallback_compression_streak == 2
    assert rebuilt._fallback_probe_at > time.time()


def test_expired_deadline_allows_one_real_summary_that_settles_state(db):
    _bench(db, probe_at=time.time() - 1)
    agent = _agent(db, abort=True)

    with patch("agent.context_compressor.call_llm", return_value=_healthy_summary()) as transport:
        _returned, _original, payloads = _compact(agent, db)

    transport.assert_called_once()
    child = agent.session_id
    assert child != PARENT
    assert [p["method"] for p in payloads] == ["llm_summary"]
    assert [p["fallback_used"] for p in payloads] == [False]
    assert db.get_compression_fallback_streak(child) == 0
    assert not db.get_session_model_config_value(child, _PROBE_KEY, 0.0)


def test_probe_failure_preserves_transcript_without_a_second_paid_attempt(db):
    _bench(db, probe_at=time.time() - 1)
    agent = _agent(db, abort=True)
    rows_before = db.get_messages(PARENT)

    with patch("agent.context_compressor.call_llm", side_effect=RuntimeError("provider down")) as transport:
        returned, original, payloads = _compact(agent, db)
        calls_after_probe = transport.call_count
        again, _, payloads_again = _compact(agent, db)
        calls_after_retry = transport.call_count

    assert calls_after_probe >= 1
    assert calls_after_retry == calls_after_probe  # failure cooldown/bench: no second paid attempt in the window
    _assert_preserved(agent, db, returned, original, rows_before, payloads + payloads_again)
    assert json.dumps(again, sort_keys=True) == json.dumps(original, sort_keys=True)
    assert db.get_compression_fallback_streak(PARENT) == 2


def test_abort_policy_preserves_transcript_when_no_provider_is_available(db):
    agent = _agent(db, abort=True)
    rows_before = db.get_messages(PARENT)

    with patch("agent.context_compressor.call_llm", side_effect=RuntimeError("no provider")):
        returned, original, payloads = _compact(agent, db)

    _assert_preserved(agent, db, returned, original, rows_before, payloads)


@pytest.mark.parametrize("abort", [True, False], ids=["abort_policy", "default_policy"])
def test_ordinary_feasibility_skip_follows_the_abort_policy(db, abort):
    # A prior ineffectiveness strike plus a middle below 10% of the threshold selects the pre-LLM skip.
    _bench(db, streak=0, ineffective=1)
    agent = _agent(db, abort=abort)
    agent.context_compressor._threshold_tokens = 1_000_000  # tiny middle relative to the threshold
    rows_before = db.get_messages(PARENT)

    with patch("agent.context_compressor.call_llm", return_value=_healthy_summary()) as transport:
        returned, original, payloads = _compact(agent, db)

    transport.assert_not_called()
    if abort:
        _assert_preserved(agent, db, returned, original, rows_before, payloads)
    else:
        assert agent.session_id != PARENT
        assert [p["failure_class"] for p in payloads] == ["feasibility_skip"]
