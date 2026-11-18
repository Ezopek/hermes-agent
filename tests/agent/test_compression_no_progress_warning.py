"""A compression attempt that leaves the transcript unchanged must tell the user, not only the log.

Real ``AIAgent._compress_context`` + real ``ContextCompressor`` + real ``SessionDB``; the compressor
returns an equal copy of its input (an engine that made no progress). The caller must keep the session,
rows and native context untouched, arm the structural backoff, report an aborted attempt, and surface a
``warn`` status so a session left over its trigger is never silent.
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB

SESSION = "SESSION_NO_PROGRESS"


@pytest.fixture
def db(tmp_path: Path):
    database = SessionDB(db_path=tmp_path / "state.db")
    database.create_session(SESSION, source="cli")
    for i in range(10):
        database.append_message(SESSION, "user", f"question {i} " + "x" * 200)
        database.append_message(SESSION, "assistant", f"answer {i} " + "y" * 200)
    try:
        yield database
    finally:
        database.close()


def _agent(db: SessionDB, statuses: list):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model", platform="cli",
            quiet_mode=True, session_db=db, session_id=SESSION, skip_context_files=True, skip_memory=True,
            status_callback=lambda *args, **_kwargs: statuses.append(args),
        )
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/model", threshold_percent=0.85, protect_first_n=2, protect_last_n=2, quiet_mode=True,
        )
        _ = compressor.context_length
    compressor.bind_session_state(db, SESSION)
    agent.context_compressor = compressor
    agent.compression_in_place = False
    return agent


def test_no_progress_rejection_warns_and_leaves_the_session_untouched(db):
    statuses: list = []
    payloads: list[dict] = []
    agent = _agent(db, statuses)
    original = db.get_messages_as_conversation(SESSION)
    snapshot = copy.deepcopy(original)
    rows_before = db.get_messages(SESSION)

    with patch.object(agent.context_compressor, "compress", side_effect=lambda msgs, *a, **k: copy.deepcopy(msgs)), \
            patch("agent.conversation_compression_telemetry._log_attempt_record", side_effect=payloads.append):
        returned, _ = agent._compress_context(original, "sys", approx_tokens=120_000)

    assert json.dumps(returned, sort_keys=True) == json.dumps(snapshot, sort_keys=True)
    assert agent.session_id == SESSION
    assert db.find_live_compression_child(SESSION) is None
    assert db.get_messages(SESSION) == rows_before

    assert agent.context_compressor._structural_no_op_backoff_until > time.monotonic()
    assert [(p["commit_status"], p["failure_class"]) for p in payloads] == [("aborted", "no_progress")]

    # Other warnings (e.g. no summary model configured in this fixture) are not under test.
    warnings = [args[1] for args in statuses if args and args[0] == "warn" and "no progress" in args[1]]
    assert len(warnings) == 1, statuses
    assert "unchanged" in warnings[0]
    # Nothing was lost and nothing is wrong with the session: no retry/reset advice.
    assert "/new" not in warnings[0] and "/compress" not in warnings[0]
