"""Mattermost ``custom_voice_message`` posts (mattermost-plugin-voice-message 2.0.1) reach the
normal voice/STT path; every other typed post stays ignored. No network: REST and download are mocked."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType
import gateway.run  # noqa: F401  # module-level like test_mattermost.py; _classify_inbound_media imports it lazily
from gateway.run_inbound import GatewayInboundMixin

WEBM = b"\x1a\x45\xdf\xa3 fake webm"


def _adapter(**extra):
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    adapter = MattermostAdapter(PlatformConfig(enabled=True, token="t", extra={"url": "https://mm.example.com", **extra}))
    adapter._bot_user_id, adapter._bot_username = "bot_id", "hermes-bot"
    adapter.handle_message = AsyncMock()
    adapter._api_get = AsyncMock(return_value={"name": "voice.webm", "mime_type": "video/webm"})
    resp = AsyncMock(status=200, read=AsyncMock(return_value=WEBM))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    adapter._session = MagicMock(get=MagicMock(return_value=resp))
    return adapter


def _event(post_id="p1", *, ptype="custom_voice_message", user="user_1", message="", channel_type="D",
           file_ids=("f1",), **post_extra):
    post = {"id": post_id, "user_id": user, "channel_id": "chan_1", "message": message, **post_extra}
    if ptype:
        post["type"] = ptype
    if file_ids is not None:
        post["file_ids"] = list(file_ids) if isinstance(file_ids, tuple) else file_ids
    return {"event": "posted", "data": {"post": json.dumps(post), "channel_type": channel_type, "sender_name": "@alice"}}


async def _run(adapter, event):
    with patch("gateway.platforms.base.cache_audio_from_bytes", side_effect=lambda d, ext=".ogg": f"/tmp/a{ext}"), \
         patch("gateway.platforms.base.cache_document_from_bytes", side_effect=lambda d, n: f"/tmp/{n}"):
        await adapter._handle_ws_event(event)


@pytest.mark.asyncio
async def test_voice_post_reaches_handler_as_audio_for_stt():
    adapter = _adapter()
    await _run(adapter, _event(root_id="root_9"))
    ev = adapter.handle_message.call_args[0][0]
    assert ev.message_type is MessageType.VOICE
    assert ev.media_types == ["audio/webm"]  # Mattermost reports recorder webm as video/webm
    assert ev.source.user_id == "user_1" and ev.source.chat_type == "dm" and ev.source.thread_id == "root_9"
    # the real gateway classifier routes it to STT, not video/document handling
    images, stt, files, videos = GatewayInboundMixin._classify_inbound_media(ev, pending_stt_prepared=False)
    assert (images, stt, files, videos) == ([], ev.media_urls, [], [])
    assert ev.media_urls[0].endswith(".webm")


@pytest.mark.asyncio
async def test_voice_post_real_audio_mime_is_kept():
    adapter = _adapter()
    adapter._api_get.return_value = {"name": "v.ogg", "mime_type": "audio/ogg"}
    await _run(adapter, _event())
    assert adapter.handle_message.call_args[0][0].media_types == ["audio/ogg"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ptype", ["system_join_channel", "custom_other", "custom_voice_message2", "Custom_Voice_Message"])
async def test_other_typed_posts_stay_ignored(ptype):
    adapter = _adapter()
    await _run(adapter, _event(ptype=ptype))
    adapter.handle_message.assert_not_called()
    adapter._api_get.assert_not_called()


@pytest.mark.asyncio
async def test_own_voice_post_and_duplicate_ignored():
    adapter = _adapter()
    await _run(adapter, _event("own", user="bot_id"))
    adapter.handle_message.assert_not_called()
    await _run(adapter, _event("dup"))
    await _run(adapter, _event("dup"))
    assert adapter.handle_message.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("file_ids", [None, [], "f1", {"a": 1}])
async def test_voice_post_without_valid_file_ids_dropped(file_ids):
    adapter = _adapter()
    await _run(adapter, _event(file_ids=file_ids))
    adapter.handle_message.assert_not_called()
    adapter._api_get.assert_not_called()


@pytest.mark.asyncio
async def test_voice_post_with_failed_download_dropped():
    adapter = _adapter()
    adapter._api_get.side_effect = RuntimeError("boom")
    await _run(adapter, _event())
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_voice_post_in_channel_keeps_mention_and_allowlist_gates():
    adapter = _adapter()
    await _run(adapter, _event("c1", channel_type="O"))  # no @mention
    adapter.handle_message.assert_not_called()
    await _run(adapter, _event("c2", channel_type="O", message="@hermes-bot"))
    assert adapter.handle_message.call_args[0][0].message_type is MessageType.VOICE
    free = _adapter(free_response_channels="chan_1")
    await _run(free, _event("c3", channel_type="O"))
    assert free.handle_message.call_args[0][0].message_type is MessageType.VOICE
    blocked = _adapter(free_response_channels="chan_1", allowed_channels="other")
    await _run(blocked, _event("c4", channel_type="O"))
    blocked.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_normal_posts_unchanged():
    adapter = _adapter()
    await _run(adapter, _event("t1", ptype=None, message="hi", file_ids=None))
    assert adapter.handle_message.call_args[0][0].message_type is MessageType.TEXT
    # a plain (untyped) webm upload is NOT rewritten to audio
    await _run(adapter, _event("t2", ptype=None, message="clip"))
    ev = adapter.handle_message.call_args[0][0]
    assert ev.media_types == ["video/webm"]
    adapter._api_get.return_value = {"name": "m.ogg", "mime_type": "audio/ogg"}
    await _run(adapter, _event("t3", ptype=None))
    assert adapter.handle_message.call_args[0][0].message_type is MessageType.VOICE
