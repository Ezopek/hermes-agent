"""A webhook turn is autonomous: a healthy bare silence marker stays silent end to end.

Chain under test, with no live gateway: the event the webhook adapter builds -> the gateway's
result shaping -> ``WebhookAdapter.send``. The human-turn fallback and every unhealthy result
keep their visible notice.
"""

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.platforms.webhook import WebhookAdapter
from tests.gateway.test_gateway_silence_tokens import _event as _telegram_event
from tests.gateway.test_gateway_silence_tokens import _runner
from tests.gateway.test_webhook_adapter import _make_adapter

_WARNING = "silence marker rejected on a user turn"


def _webhook_event(adapter: WebhookAdapter):
    """The event ``_admit_agent_run`` builds for one delivery (no ``reply_expected``, not internal)."""
    source = adapter._admission_source("synthetic-route", None, "delivery-1")
    event = MessageEvent(text="synthetic payload", message_type=MessageType.TEXT, source=source,
                         raw_message={}, message_id="delivery-1", timestamp=datetime.fromtimestamp(0, UTC))
    return event, source


def _result(final_response, **extra):
    return {"final_response": final_response, "failed": False, "api_calls": 1, "messages": [
        {"role": "user", "content": "synthetic payload"},
        {"role": "assistant", "content": final_response}], **extra}


async def _turn(runner, event, source, result):
    runner._run_agent = AsyncMock(return_value=result)
    return await runner._handle_message_with_agent(event, source, "lane", 1)


async def _deliver_through_webhook(monkeypatch, adapter, source, response):
    """What the base adapter does with a non-empty response: hand it to ``send``."""
    monkeypatch.setattr("gateway.platforms.webhook_delivery.retained_destination",
                        lambda _adapter, _chat_id: {"deliver": "log"})
    adapter._deliver_to = AsyncMock()
    if response:
        await adapter.send(source.chat_id, response)
    return adapter._deliver_to


@pytest.mark.asyncio
async def test_healthy_bare_marker_on_a_webhook_turn_delivers_nothing(monkeypatch, tmp_path, caplog):
    adapter = _make_adapter()
    event, source = _webhook_event(adapter)
    runner = _runner(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING):
        response = await _turn(runner, event, source, _result("NO_REPLY"))
    delivered = await _deliver_through_webhook(monkeypatch, adapter, source, response)
    assert not delivered.called, f"delivered {delivered.call_args!r}"
    assert not response
    assert _WARNING not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("final, extra", [
    ("", {"failed": True, "error": "provider error"}),
    ("NO_REPLY", {"partial": True}), ("NO_REPLY", {"interrupted": True}),
    ("NO_REPLY", {"completed": False}), ("NO_REPLY", {"error": "provider error"}),
])
async def test_unhealthy_webhook_result_is_never_silent(monkeypatch, tmp_path, final, extra):
    adapter = _make_adapter()
    event, source = _webhook_event(adapter)
    runner = _runner(monkeypatch, tmp_path)
    response = await _turn(runner, event, source, _result(final, **extra))
    delivered = await _deliver_through_webhook(monkeypatch, adapter, source, response)
    assert delivered.called, "an unhealthy webhook turn must reach the operator"


@pytest.mark.asyncio
async def test_empty_webhook_result_keeps_its_notice(monkeypatch, tmp_path):
    adapter = _make_adapter()
    event, source = _webhook_event(adapter)
    runner = _runner(monkeypatch, tmp_path)
    response = await _turn(runner, event, source, _result("(empty)"))
    delivered = await _deliver_through_webhook(monkeypatch, adapter, source, response)
    assert delivered.called


@pytest.mark.asyncio
@pytest.mark.parametrize("final, expected", [
    ("Recorded the change; the review is yours.", "Recorded the change; the review is yours."),
    # A decision or outage notice that ends with a marker line is still the model's message.
    ("Decision needed: the service goes down at noon.\n\nNO_REPLY",
     "Decision needed: the service goes down at noon."),
])
async def test_meaningful_webhook_response_is_delivered(monkeypatch, tmp_path, final, expected):
    adapter = _make_adapter()
    event, source = _webhook_event(adapter)
    runner = _runner(monkeypatch, tmp_path)
    response = await _turn(runner, event, source, _result(final))
    delivered = await _deliver_through_webhook(monkeypatch, adapter, source, response)
    delivered.assert_awaited_once()
    assert delivered.await_args.args[2] == expected


@pytest.mark.asyncio
async def test_explicit_platform_opt_out_keeps_the_webhook_fallback(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig
    adapter = _make_adapter()
    event, source = _webhook_event(adapter)
    runner = _runner(monkeypatch, tmp_path)
    runner.config = GatewayConfig.from_dict({"platforms": {"webhook": {"allow_silence": False}}})
    response = await _turn(runner, event, source, _result("NO_REPLY"))
    assert "silence marker" in response


@pytest.mark.asyncio
async def test_human_dm_marker_still_gets_the_visible_fallback(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    event = _telegram_event()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42")
    event.source = source
    response = await _turn(runner, event, source, _result("NO_REPLY"))
    assert "silence marker" in response


@pytest.mark.asyncio
async def test_queued_chain_ending_in_a_healthy_marker_stays_silent(monkeypatch, tmp_path):
    from types import SimpleNamespace
    adapter = _make_adapter()
    _, source = _webhook_event(adapter)
    runner = _runner(monkeypatch, tmp_path)
    result = _result("NO_REPLY")
    runner._deliver_queued_first_response = AsyncMock(return_value=True)
    ctx = SimpleNamespace(session_key="lane", source=source, stream_consumer_holder=[None],
                          mute_notification_reply=False, persist_user_display_kind=None,
                          reply_expected=None, _status_thread_metadata=None,
                          event_message_id=None, inbound_message_id="42", run_generation=1)
    await runner._run_agent_deliver_first_response(ctx, None, result, result, None)
    assert not runner._deliver_queued_first_response.called


@pytest.mark.asyncio
async def test_crash_left_healthy_marker_is_not_resurrected_as_the_warning(monkeypatch, tmp_path):
    import time
    from tests.gateway.test_active_turn_recovery import _close_store_db, _db_runner
    adapter = _make_adapter()
    _, source = _webhook_event(adapter)
    runner, store = _db_runner(tmp_path)
    result = _result("NO_REPLY")
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    store.append_to_transcript(entry.session_id, {"role": "user", "content": "synthetic payload"})
    store.append_to_transcript(entry.session_id, {"role": "assistant", "content": "NO_REPLY"})
    try:
        _, intentional, _ = await runner._hmwa_shape_agent_response(
            result, source, [], entry, entry.session_key, entry.session_key, 1,
            entry.session_id, "webhook", time.time())
        assert intentional
        await runner._record_room_silence_decision(entry.session_key, result, intentional)
        assert await runner._recover_unclean_sessions() == (0, 0)
    finally:
        _close_store_db(store)
