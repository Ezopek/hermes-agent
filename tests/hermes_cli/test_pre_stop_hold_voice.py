"""CLI voice mode with a ``pre_stop`` hook: the held turn streams nothing to TTS, so the accepted answer is spoken whole.

Real streaming through the loopback provider and the real plugin manager; the CLI's own turn-audio wiring
(``_chat_setup_turn_audio``) feeds a stand-in speaker, and ``_chat_render_turn`` makes the speak-or-not decision.
"""

import threading

import pytest

import hermes_cli.plugins as plugins_mod
from tools import tts_tool
from tools import tts_tool_speaker as speaker_mod
from cli import HermesCLI, _ChatTurn
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from run_agent import AIAgent
from tests.fakes.fake_llm_provider import FakeLLMServer, Text

DRAFT = "REJECTED_DRAFT_TEXT"
ANSWER = "ACCEPTED_ANSWER_TEXT"


def _speak(monkeypatch, base_url, script_hook):
    """One voice-mode turn; returns ``(final_response, text streamed to the speaker, texts batch-spoken)``."""
    mgr = PluginManager()
    mgr._discovered = True
    if script_hook is not None:
        PluginContext(PluginManifest(name="probe", source="user"), mgr).register_hook("pre_stop", script_hook)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    streamed: list[str] = []
    batch: list[str] = []

    def stand_in_speaker(text_queue, stop_event, done_event, *a, **k):
        while (delta := text_queue.get()) is not None:
            streamed.append(delta)
        done_event.set()

    monkeypatch.setattr(tts_tool, "check_tts_requirements", lambda: True)
    monkeypatch.setattr(speaker_mod, "stream_tts_to_speaker", stand_in_speaker)
    for name in ("_chat_print_reasoning_box", "_chat_print_response_panel", "_emit_focus_recovery_line", "_ring_bell"):
        monkeypatch.setattr(HermesCLI, name, lambda *a, **k: None)
    cli = HermesCLI.__new__(HermesCLI)
    cli._voice_mode = cli._voice_continuous = False
    cli._voice_tts = True
    cli._voice_tts_done = threading.Event()
    cli._voice_last_tts_text = ""
    cli.show_timestamps = False
    cli.streaming_enabled = False
    cli.timestamp_format = "%H:%M"
    cli._voice_speak_response_async = batch.append
    cli.agent = AIAgent(base_url=base_url, api_key="sk-fake-e2e", provider="custom", model="fake-model",
                        quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=["todo"])
    turn = _ChatTurn()
    cli._chat_setup_turn_audio(turn, "report", False)
    turn.result = cli.agent.run_conversation("report", stream_callback=turn.stream_callback)
    turn.text_queue.put(None)
    turn.tts_thread.join(timeout=10)
    response = cli._chat_render_turn(turn, None, None)
    return response, "".join(streamed), batch


def test_without_a_hook_streaming_tts_speaks_and_nothing_is_batched(monkeypatch):
    with FakeLLMServer([Text(ANSWER, chunk_chars=5)]) as srv:
        response, streamed, batch = _speak(monkeypatch, srv.base_url, None)
    assert response == ANSWER and streamed == ANSWER and batch == []


@pytest.mark.parametrize("verdict", [None, {"action": "unresolved", "message": "not confirmed"}],
                         ids=["accepted", "unresolved"])
def test_a_held_turn_speaks_the_accepted_answer_or_the_notice_once_and_never_the_draft(monkeypatch, verdict):
    verdicts = iter([{"action": "continue", "message": "check it again"}, verdict, verdict])
    with FakeLLMServer([Text(DRAFT, chunk_chars=5), Text(ANSWER, chunk_chars=5), Text(ANSWER, chunk_chars=5)]) as srv:
        response, streamed, batch = _speak(monkeypatch, srv.base_url, lambda **_kw: next(verdicts))
    assert streamed == ""
    assert batch == [response] and DRAFT not in response
    assert response == ANSWER if verdict is None else response.startswith("Unresolved")
