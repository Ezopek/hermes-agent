"""A ``pre_stop`` hold must not make the gateway skip the whole-file voice reply.

The prose beside a ``clarify`` call is released to the user while the turn's text is held, so on a voice
chat it reaches streaming TTS through the interim callback and the stream turns audible. The turn's final
answer is held and never streams: the runner must still speak it whole, so the turn must not be marked as
already spoken by streaming TTS. Real agent, real loopback provider, real ``TurnRunner`` and streaming-TTS consumer.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import StreamingConfig
from gateway.run_turn import GatewayTurnMixin
from gateway.stream_consumer import StreamConsumerConfig
from gateway.run_turn_runner import TurnRunner
from gateway.streaming_tts_consumer import StreamingTTSConsumer
from gateway.turn_context import TurnContext
from run_agent import AIAgent
from tests.agent.test_pre_stop_holds_stream import ANSWER, PROSE, _register
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.gateway.test_streaming_tts_consumer import FakeStreamer, FakeVoiceAdapter


def _voice_turn(monkeypatch, base_url, loop):
    """The gateway's wiring for one voice-input turn: delta + interim callbacks teeing into streaming TTS."""
    spoken: list[str] = []

    class Streamer(FakeStreamer):
        def stream(self, text):
            spoken.append(text)
            yield b"\x01\x00" * 480

    adapter = FakeVoiceAdapter()
    adapter.marked = []
    adapter._mark_streaming_tts_completed_turn = lambda *key: adapter.marked.append(key)
    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda cfg: Streamer())
    monkeypatch.setattr("gateway.run_turn_runner.stream_held", lambda: False)
    tts = StreamingTTSConsumer(adapter, "voice", {}, loop)
    ctx = TurnContext(
        streaming_tts_consumer_holder=[tts], user_config={}, resolve_display_setting=lambda *args: True,
        interim_assistant_messages_enabled=True, session_key="voice-session", run_generation=7,
        source=SimpleNamespace(platform=SimpleNamespace(value="realtime"), chat_id="voice"),
        _run_still_current=lambda: True,
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig()), _delivery_adapter_for=lambda source: adapter,
        _build_stream_consumer_config=lambda *args, **kwargs: (StreamConsumerConfig(), None),
    )
    _, delta, interim, _ = TurnRunner(runner, ctx)._setup_stream_consumer("realtime")
    agent = AIAgent(base_url=base_url, api_key="sk-fake-e2e", provider="custom", model="fake-model", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, enabled_toolsets=["clarify"])
    agent.clarify_callback = lambda questions: {"answers": {"q0": "A"}, "outcome": "answered"}
    agent.stream_delta_callback, agent.interim_assistant_callback = delta, interim
    ctx.agent_holder[0] = agent
    return agent, ctx, tts, adapter, spoken


@pytest.mark.parametrize("hooked", [False, True], ids=["control_no_hook", "pre_stop_hook"])
def test_a_held_turn_leaves_its_final_answer_to_the_whole_file_voice_reply(monkeypatch, hooked):
    _register(monkeypatch, (lambda **_kw: None) if hooked else None)
    script = [ToolCall("clarify", {"questions": [{"question": "Which one?", "choices": ["A", "B"]}]}, text=PROSE),
              Text(ANSWER, chunk_chars=5)]

    async def run():
        with FakeLLMServer(script) as srv:
            agent, ctx, tts, adapter, spoken = _voice_turn(monkeypatch, srv.base_url, asyncio.get_running_loop())
            tts.start()
            result = await asyncio.to_thread(agent.run_conversation, "which one?")
            await GatewayTurnMixin._run_agent_finalize_streaming_tts(SimpleNamespace(), ctx, adapter)
        return result, adapter, spoken

    result, adapter, spoken = asyncio.run(run())
    assert result["final_response"] == ANSWER
    # the speech stream strips markdown, underscores included
    assert spoken[:1] == [PROSE.replace("_", "")]  # the prose is spoken as streaming speech
    if hooked:
        assert adapter.marked == []  # the held answer never streamed: the whole-file reply must still speak it
        assert spoken == [PROSE.replace("_", "")]
    else:
        assert adapter.marked == [("voice-session", 7)]  # control: streaming TTS spoke the reply, whole-file is skipped
