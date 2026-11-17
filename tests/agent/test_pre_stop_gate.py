"""Behaviour of the generic ``pre_stop`` hook in the real conversation loop.

``pre_verify`` only fires after code edits. ``pre_stop`` fires on every text stop in every task
domain, through the real ``PluginManager`` dispatch, and may keep the turn going or declare it
unresolved. An exhausted continuation, an exhausted iteration budget, or a failed/timed-out hook
must never publish the rejected candidate as the answer.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager
from run_agent import AIAgent


def _response(content):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(
            session_id="pre-stop-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=8,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    instance._handle_max_iterations = MagicMock(return_value="replacement summary")
    return instance


def _register(monkeypatch, **hooks):
    """Real manager, real dispatch: callbacks go in the way a plugin's ``register`` would."""
    mgr = PluginManager()
    mgr._discovered = True
    for name, callbacks in hooks.items():
        mgr._hooks[name] = list(callbacks)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return mgr


def _script(agent, *texts):
    """Model stub: one text answer per call; returns the call counter."""
    answers = iter(texts)
    calls = []

    def model_call(_api_kwargs):
        calls.append(1)
        return _response(next(answers))

    agent._interruptible_api_call = model_call
    return calls


def test_no_registered_hook_makes_no_extra_model_call(agent, monkeypatch):
    _register(monkeypatch)
    calls = _script(agent, "research summary")

    result = agent.run_conversation("summarise the topic")

    assert len(calls) == 1
    assert result["final_response"] == "research summary"
    assert result["completed"] is True


def test_read_only_turn_is_kept_going_until_the_hook_passes(agent, monkeypatch):
    seen = []

    def gate(**kw):
        seen.append(kw)
        if kw["attempt"] == 0:
            return {"action": "continue", "message": "record the disposition first"}
        return None

    _register(monkeypatch, pre_stop=[gate])
    calls = _script(agent, "draft one", "draft two")

    result = agent.run_conversation("summarise the topic")

    assert len(calls) == 2
    assert [kw["attempt"] for kw in seen] == [0, 1]
    assert seen[0]["final_response"] == "draft one"
    assert seen[0]["session_id"] == "pre-stop-test"
    assert result["final_response"] == "draft two"
    assert result["completed"] is True
    assert not any(m.get("_pre_verify_synthetic") for m in result["messages"])


def test_continuation_cap_ends_unresolved_and_never_publishes_the_rejected_candidate(agent, monkeypatch):
    def gate(**_kw):
        return {"action": "continue", "message": "result r1 has no recorded disposition"}

    _register(monkeypatch, pre_stop=[gate])
    calls = _script(agent, "candidate one", "candidate two", "candidate three")

    with patch("agent.verify_hooks.max_verify_nudges", return_value=2):
        result = agent.run_conversation("summarise the topic")

    assert len(calls) == 3
    assert result["final_response"].startswith("Unresolved")
    assert "result r1 has no recorded disposition" in result["final_response"]
    assert "candidate three" not in result["final_response"]
    assert result["completed"] is False
    last = result["messages"][-1]
    assert last["role"] == "assistant" and last["content"] == result["final_response"]


def test_iteration_budget_exhaustion_does_not_promote_the_rejected_candidate(agent, monkeypatch):
    agent.max_iterations = 1
    agent.iteration_budget.max_total = 1

    def gate(**_kw):
        return {"action": "continue", "message": "disposition missing"}

    _register(monkeypatch, pre_stop=[gate])
    _script(agent, "premature success report")

    result = agent.run_conversation("summarise the topic")

    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["final_response"].startswith("Unresolved")
    assert "disposition missing" in result["final_response"]
    assert "premature success report" not in result["final_response"]
    assert result["completed"] is False


def test_a_later_pass_clears_the_earlier_rejection(agent, monkeypatch):
    verdicts = iter([{"action": "continue", "message": "not yet"}, None])
    _register(monkeypatch, pre_stop=[lambda **_kw: next(verdicts)])
    _script(agent, "early draft", "final answer")

    result = agent.run_conversation("summarise the topic")

    assert result["final_response"] == "final answer"
    assert result["completed"] is True


def test_raising_hook_is_reported_unresolved_not_passed(agent, monkeypatch):
    def broken(**_kw):
        raise RuntimeError("synthetic-secret-marker-7731")

    _register(monkeypatch, pre_stop=[broken])
    calls = _script(agent, "looks fine")

    result = agent.run_conversation("summarise the topic")

    assert len(calls) == 1
    assert result["final_response"].startswith("Unresolved")
    assert "broken" in result["final_response"]
    assert "synthetic-secret-marker-7731" not in result["final_response"]
    assert "looks fine" not in result["final_response"]
    assert result["completed"] is False


def test_timed_out_hook_is_reported_unresolved_not_passed(agent, monkeypatch):
    release = threading.Event()

    def hung(**_kw):
        release.wait(timeout=10.0)

    _register(monkeypatch, pre_stop=[hung])
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.2)
    _script(agent, "looks fine")
    try:
        result = agent.run_conversation("summarise the topic")
    finally:
        release.set()

    assert result["final_response"].startswith("Unresolved")
    assert "looks fine" not in result["final_response"]
    assert result["completed"] is False


def test_hook_may_declare_the_turn_unresolved_without_another_model_call(agent, monkeypatch):
    def gate(**_kw):
        return {"action": "unresolved", "message": "validator refused: dependency missing"}

    _register(monkeypatch, pre_stop=[gate])
    calls = _script(agent, "all done")

    result = agent.run_conversation("summarise the topic")

    assert len(calls) == 1
    assert result["final_response"].startswith("Unresolved")
    assert "dependency missing" in result["final_response"]
    assert result["completed"] is False


def test_interrupt_wins_over_the_hook(agent, monkeypatch):
    fired = []
    _register(monkeypatch, pre_stop=[lambda **kw: fired.append(kw) or {"action": "continue", "message": "x"}])

    def model_call(_api_kwargs):
        agent._interrupt_requested = True
        return _response("partial answer")

    agent._interruptible_api_call = model_call

    agent.run_conversation("summarise the topic")

    assert fired == []


def test_pre_verify_stays_edit_only_and_runs_before_pre_stop(agent, monkeypatch):
    order = []

    def verify(**kw):
        order.append("pre_verify")

    def stop(**kw):
        order.append("pre_stop")

    _register(monkeypatch, pre_verify=[verify], pre_stop=[stop])
    _script(agent, "read only answer")
    agent.run_conversation("summarise the topic")
    assert order == ["pre_stop"]

    order.clear()
    _script(agent, "edited answer")
    real_call = agent._interruptible_api_call

    def edited(api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return real_call(api_kwargs)

    agent._interruptible_api_call = edited
    agent.run_conversation("edit the file")
    assert order == ["pre_verify", "pre_stop"]


# ── boundaries found by independent review ─────────────────────────────────────────────────────


def test_unreadable_hook_registry_is_unresolved_not_a_pass(agent, monkeypatch):
    _register(monkeypatch)
    from agent.turn_stop_gates import _pre_stop_verdict

    with patch("hermes_cli.lifecycle.has_hook", side_effect=RuntimeError("synthetic-secret-marker-5512")):
        action, message = _pre_stop_verdict(agent, "unchecked candidate")

    assert action == "unresolved"
    assert "synthetic-secret-marker-5512" not in message


def test_registry_failure_in_the_loop_never_publishes_the_candidate(agent, monkeypatch):
    _register(monkeypatch)
    _script(agent, "unchecked candidate")

    from hermes_cli.lifecycle import has_hook

    def registry_down_for_pre_stop(name, *args, **kwargs):
        # Only the stop gate's registry read fails; other lifecycle checks in the loop keep working.
        if name == "pre_stop":
            raise RuntimeError("registry down")
        return has_hook(name, *args, **kwargs)

    with patch("hermes_cli.lifecycle.has_hook", side_effect=registry_down_for_pre_stop):
        result = agent.run_conversation("summarise the topic")

    assert result["final_response"].startswith("Unresolved")
    assert "unchecked candidate" not in result["final_response"]
    assert result["completed"] is False


def test_absent_hook_registry_state_stays_a_no_op(agent, monkeypatch):
    _register(monkeypatch)
    from agent.turn_stop_gates import _pre_stop_verdict

    assert _pre_stop_verdict(agent, "draft") == ("pass", "")


def test_unresolved_still_runs_the_kanban_terminal_guard(agent, monkeypatch):
    _register(monkeypatch, pre_stop=[lambda **_kw: {"action": "unresolved", "message": "dependency needs a handoff"}])
    calls = _script(agent, "not done", "still unresolved", "still unresolved", "still unresolved")
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)

    with patch("agent.kanban_stop.owned_kanban_task", return_value="synthetic_owned_card"):
        result = agent.run_conversation("report the result")

    assert getattr(agent, "_kanban_stop_nudges", 0) == 2  # bounded by the guard's own cap
    assert len(calls) == 3  # two nudged retries, then the unresolved verdict: no unbounded loop
    assert result["completed"] is False
    assert result["final_response"].startswith("Unresolved")
    assert "dependency needs a handoff" in result["final_response"]


def test_unresolved_without_kanban_worker_does_not_nudge(agent, monkeypatch):
    _register(monkeypatch, pre_stop=[lambda **_kw: {"action": "unresolved", "message": "m"}])
    calls = _script(agent, "x", "y")
    agent.run_conversation("report the result")
    assert len(calls) == 1
    assert getattr(agent, "_kanban_stop_nudges", 0) == 0


def _trusted_reasoning(agent, text="Synthetic trusted route answer"):
    agent._custom_providers = [{"name": "test-local", "base_url": "https://example.invalid/v1",
                                "capabilities": {"answer_in_reasoning": True}}]
    response = _response(None)
    response.choices[0].message.reasoning = text
    response.choices[0].message.reasoning_content = None
    response.choices[0].message.reasoning_details = None
    agent._interruptible_api_call = lambda _kw: response


def test_trusted_reasoning_promotion_is_unchanged_without_a_gate(agent, monkeypatch):
    _register(monkeypatch)
    _trusted_reasoning(agent)

    result = agent.run_conversation("answer the question")

    assert result["completed"] is True
    assert result["final_response"] == "Synthetic trusted route answer"
    assert not result["messages"][-1].get("content")
    assert result["messages"][-1]["api_content"] == "Synthetic trusted route answer"


def test_unresolved_notice_replacing_promoted_reasoning_is_ordinary_content(agent, monkeypatch):
    _register(monkeypatch, pre_stop=[lambda **_kw: {"action": "unresolved", "message": "source result not consumed"}])
    _trusted_reasoning(agent, "private chain of thought marker-8841")

    result = agent.run_conversation("answer the question")

    last = result["messages"][-1]
    assert result["completed"] is False
    assert result["final_response"].startswith("Unresolved")
    assert last["content"] == result["final_response"]
    assert "marker-8841" not in str(last.get("content"))
    assert "api_content" not in last  # the rejected candidate is not replayed as the answer


def test_unresolved_notice_is_what_the_session_db_stores(agent, tmp_path, monkeypatch):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "stop.db")
    try:
        agent._session_db = db
        db.create_session(session_id=agent.session_id, source="test", model="test/model")
        _register(monkeypatch, pre_stop=[lambda **_kw: {"action": "unresolved", "message": "stored truthfully"}])
        _script(agent, "premature success")

        result = agent.run_conversation("summarise the topic")

        rows = [m for m in db.get_messages(agent.session_id) if m["role"] == "assistant"]
        assert rows[-1]["content"] == result["final_response"]
        assert rows[-1]["content"].startswith("Unresolved")
    finally:
        db.close()
