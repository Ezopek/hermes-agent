"""A registered ``pre_stop`` hook holds the turn's text until the stop verdict is in.

The hook runs after the model has finished its answer, so a draft it rejects must never have reached the
display or TTS delta callbacks, nor the interim callback: only the accepted answer or the ``Unresolved``
notice is published. Real streaming through a loopback provider and the real plugin manager. The control shows
that without a ``pre_stop`` hook the text still streams.
"""

import pytest

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from run_agent import AIAgent
from tests.fakes.fake_llm_provider import FakeLLMServer, Text

DRAFT = "REJECTED_DRAFT_TEXT"
ANSWER = "ACCEPTED_ANSWER_TEXT"


def _register(monkeypatch, pre_stop=None):
    mgr = PluginManager()
    mgr._discovered = True
    if pre_stop is not None:
        PluginContext(PluginManifest(name="probe", source="user"), mgr).register_hook("pre_stop", pre_stop)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)


def _run(base_url):
    agent = AIAgent(base_url=base_url, api_key="sk-fake-e2e", provider="custom", model="fake-model", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=["todo"])
    shown: list = []
    agent.stream_delta_callback = shown.append
    agent._stream_callback = shown.append  # the TTS delta seam
    agent.interim_assistant_callback = lambda text, **_: shown.append(text)
    return agent.run_conversation("report"), [text for text in shown if text]


def test_text_streams_when_no_pre_stop_hook_is_registered(monkeypatch):
    _register(monkeypatch)
    with FakeLLMServer([Text(ANSWER, chunk_chars=5)]) as srv:
        result, shown = _run(srv.base_url)
    assert result["final_response"] == ANSWER
    assert any(ANSWER.startswith(text) for text in shown)


def test_an_unresolved_draft_never_reaches_the_user(monkeypatch):
    _register(monkeypatch, lambda **_kw: {"action": "unresolved", "message": "not confirmed"})
    with FakeLLMServer([Text(DRAFT, chunk_chars=5)]) as srv:
        result, shown = _run(srv.base_url)
    assert result["completed"] is False
    assert result["final_response"].startswith("Unresolved") and DRAFT not in result["final_response"]
    assert not any(DRAFT[:5] in text for text in shown), shown


def test_a_sent_back_draft_never_reaches_the_user_only_the_accepted_answer(monkeypatch):
    verdicts = iter([{"action": "continue", "message": "check it again"}, None])
    _register(monkeypatch, lambda **_kw: next(verdicts))
    with FakeLLMServer([Text(DRAFT, chunk_chars=5), Text(ANSWER, chunk_chars=5)]) as srv:
        result, shown = _run(srv.base_url)
    assert result["final_response"] == ANSWER
    assert len(srv.main_requests()) == 2
    assert not any(DRAFT[:5] in text for text in shown), shown
    assert result["messages"][-1]["content"] == ANSWER


@pytest.mark.parametrize("verdict", [None, {"action": "unresolved", "message": "no"}], ids=["accepted", "unresolved"])
def test_the_hold_is_per_turn_the_next_turn_without_a_hook_streams_again(monkeypatch, verdict):
    _register(monkeypatch, lambda **_kw: verdict)
    with FakeLLMServer([Text(ANSWER, chunk_chars=5), Text(ANSWER, chunk_chars=5)]) as srv:
        agent = AIAgent(base_url=srv.base_url, api_key="sk-fake-e2e", provider="custom", model="fake-model",
                        quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=["todo"])
        shown: list = []
        agent.stream_delta_callback = shown.append
        agent.run_conversation("first")
        assert not [text for text in shown if text]
        _register(monkeypatch)
        agent.run_conversation("second")
    assert any(ANSWER.startswith(text) for text in shown if text)
