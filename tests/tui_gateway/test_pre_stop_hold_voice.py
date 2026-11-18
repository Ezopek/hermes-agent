"""A turn held for a ``pre_stop`` verdict streams nothing of its answer to the TUI voice queue, so the answer
(accepted text or ``Unresolved`` notice) must be spoken whole by the fallback, like a turn with no queue."""

import threading
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("held", [False, True], ids=["control_streamed_turn", "pre_stop_held_turn"])
def test_the_tui_speaks_a_held_answer_whole(monkeypatch, held):
    from hermes_cli import voice
    from tui_gateway import server

    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    monkeypatch.setattr(server, "_arm_barge_listener_if_enabled", lambda: None)
    spoken, done = [], threading.Event()
    monkeypatch.setattr(voice, "speak_text", lambda text, stop_event=None: (spoken.append(text), done.set()))
    st = SimpleNamespace(tts_queue=object(), agent=SimpleNamespace(_hold_stream=held))
    server._after_complete_turn("sid", {}, st, "the accepted answer")
    if held:
        assert done.wait(10) and spoken == ["the accepted answer"]
    else:  # the queue already spoke what streamed: nothing is spoken twice
        assert not done.wait(1) and spoken == []
