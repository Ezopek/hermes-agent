"""Mattermost posts pass native admission and run a turn, with or without attachments.

Contract:
V1 A plain Mattermost text post (no attachment) is admitted and runs a turn.
V2 A Mattermost post with an attachment is admitted, runs a turn and keeps its media.
V3 A custom_voice_message post from an allowed user runs a turn with its audio (real download path);
  from a user outside MATTERMOST_ALLOWED_USERS it runs none.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageType
from gateway.platforms.base import SendResult, cache_image_from_bytes_async
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from tests.gateway.test_mattermost_voice_post import _WebmResponse
from tests.waiting import async_wait_until

SENDER = 'user_123'
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


def _posted_event(post_id, message, file_ids=()):
    post = {'id': post_id, 'user_id': SENDER, 'channel_id': 'dm_chan', 'message': message,
            'file_ids': list(file_ids)}
    return {'event': 'posted', 'data': {'post': json.dumps(post), 'channel_type': 'D', 'sender_name': '@alice'}}


@pytest.mark.asyncio
async def test_real_mattermost_posts_run_a_turn_through_native_admission(tmp_path, monkeypatch):
    """Contract: V1, V2"""
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    monkeypatch.setenv('MATTERMOST_ALLOWED_USERS', SENDER)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = MattermostAdapter(PlatformConfig(enabled=True, token='test-token',
                                               extra={'url': 'https://mm.example.com'}))
    adapter._bot_user_id, adapter._bot_username = 'bot_user_id', 'hermes-bot'
    adapter.gateway_runner, runner.adapters = runner, {Platform.MATTERMOST: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='mm')
    runner._wire_adapter_handlers(adapter)
    image_path = await cache_image_from_bytes_async(PNG_BYTES, '.png')
    sent, turns = [], []

    async def download_attachments(file_ids, **_):
        if not file_ids:
            return [], []
        return [image_path], ['image/png']

    async def send(chat_id, content, **_):
        sent.append(content)
        return SendResult(success=True, message_id='s')

    async def turn(event, *_args, **_kwargs):
        turns.append((event.text, len(event.media_urls), list(event.media_types)))
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    monkeypatch.setattr(adapter, '_download_attachments', download_attachments)
    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        await adapter._handle_ws_event(_posted_event('p1', 'hello'))
        await async_wait_until(lambda: len(sent) >= 1, timeout=20, what='reply to a plain text post')
        await adapter._handle_ws_event(_posted_event('p2', 'look at this', file_ids=['f1']))
        await async_wait_until(lambda: len(sent) >= 2, timeout=20, what='reply to a post with an image')
        assert sent == ['ok', 'ok'], sent
        assert turns == [('hello', 0, []), ('look at this', 1, ['image/png'])], turns
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('user_id, admitted', [(SENDER, True), ('intruder', False)])
async def test_custom_voice_post_honors_user_allowlist_through_native_admission(
        user_id, admitted, tmp_path, monkeypatch):
    """Contract: V3 — only the REST edge is faked; type filter, download, audio cache and admission are real."""
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    monkeypatch.setenv('MATTERMOST_ALLOWED_USERS', SENDER)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = MattermostAdapter(PlatformConfig(enabled=True, token='test-token',
                                               extra={'url': 'https://mm.example.com'}))
    adapter._bot_user_id, adapter._bot_username = 'bot_user_id', 'hermes-bot'
    adapter.gateway_runner, runner.adapters = runner, {Platform.MATTERMOST: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='mm')
    runner._wire_adapter_handlers(adapter)
    sent, turns, decisions = [], [], []
    real_authorized = runner._is_user_authorized_for_source

    def authorized(source, *args, **kwargs):  # record the real admission decision, never replace it
        decisions.append((source.user_id, real_authorized(source, *args, **kwargs)))
        return decisions[-1][1]

    monkeypatch.setattr(runner, '_is_user_authorized_for_source', authorized)
    adapter._session = MagicMock(get=MagicMock(return_value=_WebmResponse(b'\x1a\x45\xdf\xa3 fake webm')))
    monkeypatch.setattr(adapter, '_api_get', AsyncMock(return_value={'name': 'voice.webm', 'mime_type': 'video/webm'}))

    async def send(chat_id, content, **_):
        sent.append(content)
        return SendResult(success=True, message_id='s')

    async def turn(event, *_args, **_kwargs):
        turns.append((event.message_type, event.source.user_id, event.media_types, event.media_urls))
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    post = {'id': 'v1', 'user_id': user_id, 'channel_id': 'dm_chan', 'message': '',
            'type': 'custom_voice_message', 'file_ids': ['f1']}
    event = {'event': 'posted', 'data': {'post': json.dumps(post), 'channel_type': 'D', 'sender_name': '@alice'}}
    try:
        await adapter._handle_ws_event(event)
        await async_wait_until(lambda: decisions, timeout=20, what='authorization decision for the voice post')
        assert decisions[0] == (user_id, admitted), decisions
        if admitted:
            await async_wait_until(lambda: turns, timeout=20, what='turn for an allowed voice post')
            (mtype, uid, media_types, media_urls), = turns
            assert (mtype, uid, media_types) == (MessageType.VOICE, SENDER, ['audio/webm'])
            assert len(media_urls) == 1 and media_urls[0].endswith('.webm')
        else:
            assert turns == []
    finally:
        authority.db.close()
