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
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

DRAFT = "REJECTED_DRAFT_TEXT"
ANSWER = "ACCEPTED_ANSWER_TEXT"


def _register(monkeypatch, pre_stop=None, **observers):
    mgr = PluginManager()
    mgr._discovered = True
    ctx = PluginContext(PluginManifest(name="probe", source="user"), mgr)
    if pre_stop is not None:
        ctx.register_hook("pre_stop", pre_stop)
    for name, callback in observers.items():
        ctx.register_hook(name, callback)
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


def test_stream_observers_never_see_a_held_draft(monkeypatch):
    """``on_stream_delta`` / ``on_stream_end`` / ``on_interim_message`` feed TTS and dashboards: held like the display."""
    from agent.plugin_stream_hooks import shutdown_plugin_stream_hook_dispatcher

    seen: list = []
    observers = {name: (lambda name=name, **kw: seen.append((name, kw))) for name in
                 ("on_stream_delta", "on_stream_end", "on_interim_message")}
    verdicts = iter([{"action": "continue", "message": "check it again"}, None])
    _register(monkeypatch, lambda **_kw: next(verdicts), **observers)
    script = [ToolCall("todo", {}, text="TOOL_PROSE"), Text(DRAFT, chunk_chars=5), Text(ANSWER, chunk_chars=5)]
    with FakeLLMServer(script) as srv:
        result, _shown = _run(srv.base_url)
    shutdown_plugin_stream_hook_dispatcher()  # joins the observer threads after they drained their queues
    assert result["final_response"] == ANSWER
    leaked = [(name, kw) for name, kw in seen if any(word in str(kw) for word in (DRAFT[:5], "TOOL_PROSE"))]
    assert not leaked, leaked


def test_stream_observers_still_see_the_text_without_a_pre_stop_hook(monkeypatch):
    from agent.plugin_stream_hooks import shutdown_plugin_stream_hook_dispatcher

    seen: list = []
    _register(monkeypatch, on_stream_delta=lambda **kw: seen.append(kw))
    with FakeLLMServer([Text(ANSWER, chunk_chars=5)]) as srv:
        _run(srv.base_url)
    shutdown_plugin_stream_hook_dispatcher()
    assert "".join(kw["delta"] for kw in seen if kw["kind"] == "text") == ANSWER


PROSE = "OPTION_A is cheap and OPTION_B is fast"


def _clarify_run(base_url, surface):
    """One turn: the model explains its options in prose, calls ``clarify``, then answers. ``surface`` is how a
    front end receives text: the gateway/TUI interim callback, the gateway's clarify-context sink, or (CLI) deltas."""
    agent = AIAgent(base_url=base_url, api_key="sk-fake-e2e", provider="custom", model="fake-model", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=["clarify"])
    events: list = []
    agent.clarify_callback = lambda questions: events.append(("CLARIFY", questions[0]["question"])) or {
        "answers": {"q0": "A"}, "outcome": "answered"}
    agent.stream_delta_callback = lambda text: events.append(("DELTA", text)) if text else None
    sink = lambda text, **_: events.append(("PROSE", text))
    if surface == "interim":
        agent.interim_assistant_callback = sink
    elif surface == "clarify_context":
        agent.clarify_context_callback = sink
    return agent.run_conversation("which one?"), events


@pytest.mark.parametrize("hooked", [False, True], ids=["control_no_hook", "pre_stop_hook"])
@pytest.mark.parametrize("surface", ["interim", "clarify_context", "cli"])
def test_the_prose_before_a_clarify_call_reaches_the_user_with_the_prompt(monkeypatch, surface, hooked):
    _register(monkeypatch, (lambda **_kw: None) if hooked else None)
    script = [ToolCall("clarify", {"questions": [{"question": "Which one?", "choices": ["A", "B"]}]}, text=PROSE),
              Text(ANSWER, chunk_chars=5)]
    with FakeLLMServer(script) as srv:
        result, events = _clarify_run(srv.base_url, surface)
    assert result["final_response"] == ANSWER
    kinds = [kind for kind, _ in events]
    shown = "".join(text for kind, text in events if kind != "CLARIFY")
    assert PROSE in shown, events
    prose_at = next(i for i, (kind, text) in enumerate(events) if kind != "CLARIFY" and PROSE.startswith(text[:5]))
    assert prose_at < kinds.index("CLARIFY")  # the explanation is on screen before the prompt asks
    if hooked:
        assert shown.count(PROSE) == 1 and ANSWER[:5] not in shown  # once, and the answer still waits for the verdict


def test_a_hold_stream_transform_still_sees_all_text_first_so_clarify_prose_stays_held(monkeypatch):
    mgr = PluginManager()
    mgr._discovered = True
    PluginContext(PluginManifest(name="probe", source="user"), mgr).register_hook(
        "transform_llm_output", lambda **_kw: None, hold_stream=True)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    script = [ToolCall("clarify", {"questions": [{"question": "Which one?", "choices": ["A", "B"]}]}, text=PROSE),
              Text(ANSWER, chunk_chars=5)]
    with FakeLLMServer(script) as srv:
        _result, events = _clarify_run(srv.base_url, "interim")
    assert PROSE not in "".join(text for kind, text in events if kind != "CLARIFY"), events


@pytest.mark.parametrize("hooked", [False, True], ids=["control_no_hook", "pre_stop_hook"])
@pytest.mark.parametrize("progress_callback", [False, True], ids=["no_progress_callback", "classic_cli_wiring"])
def test_the_prose_before_a_clarify_call_prints_in_a_cli_without_streaming(monkeypatch, hooked, progress_callback):
    """The classic CLI with streaming and voice off has no delta, interim or clarify-context callback.
    ``classic_cli_wiring`` is what ``cli_agent_setup_mixin`` builds (quiet, ``platform="cli"``, a tool-progress callback,
    status lines through ``_print_fn``): without a hook it prints no tool-call prose, so the control expects none.
    The ``no_progress_callback`` embedder prints it as a ``💬`` line. Under a ``pre_stop`` hold the prose is released
    to both, once, before the prompt."""
    _register(monkeypatch, (lambda **_kw: None) if hooked else None)
    script = [ToolCall("clarify", {"questions": [{"question": "Which one?", "choices": ["A", "B"]}]}, text=PROSE),
              Text(ANSWER, chunk_chars=5)]
    printed: list = []
    with FakeLLMServer(script) as srv:
        agent = AIAgent(base_url=srv.base_url, api_key="sk-fake-e2e", provider="custom", model="fake-model",
                        quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=["clarify"],
                        platform="cli", tool_progress_callback=(lambda *_a, **_kw: None) if progress_callback else None)
        agent._print_fn = lambda *args, **_kw: printed.append(" ".join(map(str, args)))
        agent.clarify_callback = lambda questions: printed.append("CLARIFY") or {"answers": {"q0": "A"}, "outcome": "answered"}
        result = agent.run_conversation("which one?")
    assert result["final_response"] == ANSWER
    prose_lines = [i for i, line in enumerate(printed) if PROSE in line]
    assert len(prose_lines) == (0 if progress_callback and not hooked else 1), printed
    if prose_lines:
        assert prose_lines[0] < printed.index("CLARIFY")


def _observed_turn(monkeypatch, verdicts, script):
    """Run one turn with ``pre_stop`` returning ``verdicts`` in order; returns the result, the text deltas and the end events."""
    from agent.plugin_stream_hooks import shutdown_plugin_stream_hook_dispatcher

    deltas: list = []
    ends: list = []
    verdicts = iter(verdicts)
    _register(monkeypatch, lambda **_kw: next(verdicts), on_stream_delta=lambda **kw: deltas.append(kw),
              on_stream_end=lambda **kw: ends.append(kw))
    with FakeLLMServer(script) as srv:
        result, _shown = _run(srv.base_url)
    shutdown_plugin_stream_hook_dispatcher()
    return result, [kw for kw in deltas if kw["kind"] == "text"], ends


def test_stream_observers_get_the_accepted_answer_once_and_never_the_draft(monkeypatch):
    result, deltas, ends = _observed_turn(
        monkeypatch, [{"action": "continue", "message": "check it again"}, None],
        [Text(DRAFT, chunk_chars=5), Text(ANSWER, chunk_chars=5)])
    assert result["final_response"] == ANSWER
    assert "".join(kw["delta"] for kw in deltas) == ANSWER
    assert [kw["final_text"] for kw in ends if kw["final_text"]] == [ANSWER]
    assert not any(DRAFT[:5] in str(kw) for kw in deltas + ends)


def test_stream_observers_get_the_unresolved_notice_and_never_the_draft(monkeypatch):
    result, deltas, ends = _observed_turn(
        monkeypatch, [{"action": "unresolved", "message": "not confirmed"}], [Text(DRAFT, chunk_chars=5)])
    notice = result["final_response"]
    assert notice.startswith("Unresolved") and DRAFT not in notice
    assert "".join(kw["delta"] for kw in deltas) == notice
    assert [kw["final_text"] for kw in ends if kw["final_text"]] == [notice]
    assert not any(DRAFT[:5] in str(kw) for kw in deltas + ends)


def test_the_observer_end_event_still_carries_the_streamed_text_without_a_pre_stop_hook(monkeypatch):
    from agent.plugin_stream_hooks import shutdown_plugin_stream_hook_dispatcher

    ends: list = []
    _register(monkeypatch, on_stream_end=lambda **kw: ends.append(kw))
    with FakeLLMServer([Text(ANSWER, chunk_chars=5)]) as srv:
        _run(srv.base_url)
    shutdown_plugin_stream_hook_dispatcher()
    assert [kw["final_text"] for kw in ends] == [ANSWER]
