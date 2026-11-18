"""A turn held for a ``pre_stop`` verdict streamed nothing of its answer to the TUI voice queue, so the settled
answer (accepted text or ``Unresolved`` notice) is put on that same queue: Stop and voice-off drive it, and it is
spoken once. A turn with no queue still uses the whole-reply fallback."""

import queue
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def speaker(monkeypatch):
    from hermes_cli import voice
    from tui_gateway import server

    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    monkeypatch.setattr(server, "_arm_barge_listener_if_enabled", lambda: None)
    spoken, done = [], threading.Event()
    monkeypatch.setattr(voice, "speak_text", lambda text, stop_event=None: (spoken.append(text), done.set()))
    return spoken, done


@pytest.mark.parametrize("held", [False, True], ids=["control_streamed_turn", "pre_stop_held_turn"])
def test_the_tui_queues_a_held_answer_on_the_turn_voice_queue(speaker, held):
    from tui_gateway import server

    spoken, done = speaker
    voice_queue = queue.Queue()
    st = SimpleNamespace(tts_queue=voice_queue, agent=SimpleNamespace(_hold_stream=held))
    server._after_complete_turn("sid", {}, st, "the accepted answer")
    assert voice_queue.qsize() == (1 if held else 0)  # a streamed turn's queue already has what was spoken
    assert not done.wait(0.5) and spoken == []  # never a second, separate speaker beside the queue
    if held:
        assert voice_queue.get_nowait() == "the accepted answer"


def test_a_turn_without_a_voice_queue_still_speaks_the_answer_whole(speaker):
    from tui_gateway import server

    spoken, done = speaker
    server._after_complete_turn("sid", {}, SimpleNamespace(tts_queue=None, agent=SimpleNamespace(_hold_stream=True)), "the answer")
    assert done.wait(10) and spoken == ["the answer"]
