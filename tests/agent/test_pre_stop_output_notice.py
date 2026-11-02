"""The stop-selected ``Unresolved`` notice is not rewritten by ``transform_llm_output`` on any finalization path.

Mocked-provider representations through the real manager, transform cache and finalizer; the stored-row checks look at the
returned transcript, not at a separately observed database or chat surface. The positive controls show ordinary transforms
of an approved answer are untouched, including on the turn after an unresolved one.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from run_agent import AIAgent

REJECTED = "REJECTED_DRAFT"


def _response(content):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], model="test/model", usage=None)


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(session_id="notice-test", api_key="k", base_url="https://example.invalid/v1", provider="openai-compat",
                           model="test/model", max_iterations=6, quiet_mode=True, skip_context_files=True, skip_memory=True)
    instance._cached_system_prompt = "p"
    instance._session_db = None
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_k: None
    instance._save_trajectory = lambda *_a, **_k: None
    instance._handle_max_iterations = MagicMock(return_value="summary")
    return instance


def _register(monkeypatch, *, held=False, **hooks):
    mgr = PluginManager()
    mgr._discovered = True
    ctx = PluginContext(PluginManifest(name="probe", source="user"), mgr)
    for name, callbacks in hooks.items():
        for cb in callbacks:
            if name == "transform_llm_output" and held:
                ctx.register_hook(name, cb, hold_stream=True)
            else:
                ctx.register_hook(name, cb)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return mgr


def _script(agent, *texts):
    answers = iter(texts)
    agent._interruptible_api_call = lambda _kw: _response(next(answers))


UNRESOLVE = lambda **_kw: {"action": "unresolved", "message": "unconsumed native result"}


def _assert_protected(result):
    assert result["completed"] is False
    assert result["final_response"].startswith("Unresolved"), repr(result["final_response"])
    assert REJECTED not in result["final_response"]
    assert result["messages"][-1]["content"] == result["final_response"]
    assert REJECTED not in str(result["messages"][-1].get("content"))


def test_an_ordinary_transform_of_an_approved_answer_still_works(agent, monkeypatch):
    seen = []
    _register(monkeypatch, transform_llm_output=[lambda response_text, **_kw: seen.append(response_text) or "ordinary transformed answer"])
    _script(agent, "ordinary draft")
    result = agent.run_conversation("report")
    assert result["completed"] is True
    assert result["final_response"] == "ordinary transformed answer"
    assert seen == ["ordinary draft"]


def test_unresolved_notice_is_not_replaced_by_the_rejected_draft(agent, monkeypatch):
    seen = []
    _register(monkeypatch, pre_stop=[UNRESOLVE],
              transform_llm_output=[lambda response_text, **_kw: seen.append(response_text) or REJECTED])
    _script(agent, REJECTED)
    result = agent.run_conversation("report")
    _assert_protected(result)
    assert seen == []  # the stop-selected notice never reaches the rewriter


def test_budget_exhaustion_notice_is_protected_through_finalize_turn(agent, monkeypatch):
    agent.max_iterations = 1
    agent.iteration_budget.max_total = 1
    posted = []
    _register(monkeypatch, pre_stop=[lambda **_kw: {"action": "continue", "message": "disposition missing"}],
              transform_llm_output=[lambda response_text, **_kw: REJECTED],
              post_llm_call=[lambda assistant_response, **_kw: posted.append(assistant_response)])
    _script(agent, "premature success report")
    result = agent.run_conversation("report")
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["completed"] is False
    assert result["final_response"].startswith("Unresolved")
    assert REJECTED not in result["final_response"]
    assert all(REJECTED not in p for p in posted)


def test_failed_hook_notice_is_protected_too(agent, monkeypatch):
    def broken(**_kw):
        raise RuntimeError("synthetic")

    _register(monkeypatch, pre_stop=[broken], transform_llm_output=[lambda response_text, **_kw: REJECTED])
    _script(agent, REJECTED)
    _assert_protected(agent.run_conversation("report"))


def test_held_stream_turn_publishes_the_notice_not_the_held_rewrite(agent, monkeypatch):
    shown = []
    _register(monkeypatch, held=True, pre_stop=[UNRESOLVE], transform_llm_output=[lambda response_text, **_kw: REJECTED])
    agent.stream_delta_callback = shown.append
    _script(agent, REJECTED)
    result = agent.run_conversation("report")
    _assert_protected(result)
    assert all(REJECTED not in str(t) for t in shown)


def test_the_next_turn_after_recovery_transforms_normally(agent, monkeypatch):
    verdicts = iter([{"action": "unresolved", "message": "not yet"}, None])
    _register(monkeypatch, pre_stop=[lambda **_kw: next(verdicts)],
              transform_llm_output=[lambda response_text, **_kw: "TRANSFORMED:" + response_text])
    _script(agent, "first draft", "second answer")
    first = agent.run_conversation("report")
    assert first["final_response"].startswith("Unresolved") and first["completed"] is False
    second = agent.run_conversation("report again")
    assert second["completed"] is True
    assert second["final_response"] == "TRANSFORMED:second answer"


def test_a_footer_added_after_the_notice_does_not_reopen_the_transform(agent, monkeypatch):
    seen = []
    _register(monkeypatch, pre_stop=[UNRESOLVE],
              transform_llm_output=[lambda response_text, **_kw: seen.append(response_text) or REJECTED])
    _script(agent, "draft")
    result = agent.run_conversation("report")
    assert result["final_response"].startswith("Unresolved")
    assert seen == []
