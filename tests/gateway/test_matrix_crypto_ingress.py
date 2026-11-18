"""Native libolm decryption through the public Matrix connect/sync/admission wiring."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import olm
import pytest
import pytest_asyncio
from mautrix.crypto import OlmMachine
from mautrix.crypto.sessions import InboundGroupSession
from mautrix.crypto.store.memory import MemoryCryptoStore
from mautrix.types import EventType

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter
from tests.waiting import async_wait_until

ROOM = "!room:example.org"
SENDER = "@sender:example.org"


@pytest_asyncio.fixture
async def native_owner(tmp_path, monkeypatch):
    from mautrix.crypto.store import asyncpg
    from mautrix.util.async_db import Database
    import plugins.platforms.matrix.adapter as module
    class Store(MemoryCryptoStore):
        upgrade_table = None
        def __init__(self, account_id, pickle_key, db):
            super().__init__(account_id, pickle_key)
        async def has_group_session(self, room_id, session_id):
            return (room_id, session_id) in self._inbound_sessions
    monkeypatch.setattr(asyncpg, "PgCryptoStore", Store)
    monkeypatch.setattr(Database, "create", lambda *a, **kw: SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))
    monkeypatch.setattr(module, "_check_e2ee_deps", lambda: True)
    monkeypatch.setattr(OlmMachine, "share_keys", AsyncMock())
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="synthetic", extra={
        "homeserver": "https://matrix.example.org", "user_id": "@bot:example.org",
        "device_id": "BOT", "encryption": True, "require_mention": False}))
    adapter._store_dir = tmp_path / "crypto"
    adapter._resolve_store_dir = lambda: adapter._store_dir
    async def authenticate(client, api):
        client.sync = AsyncMock(return_value={"next_batch": "initial"})
        return True
    adapter._connect_authenticate = authenticate
    adapter._verify_device_keys_on_server = AsyncMock(return_value=True)
    adapter._verify_or_bootstrap_cross_signing = AsyncMock()
    adapter._migrate_legacy_crypto_pickle = AsyncMock(return_value=True)
    adapter._reset_crypto_store_if_device_changed = AsyncMock(return_value=False)
    adapter._refresh_dm_cache = AsyncMock()
    adapter._sync_loop = AsyncMock()
    adapter._wire_plugin_handlers = lambda client: None
    adapter._is_dm_room = AsyncMock(return_value=True)
    adapter._resolve_room_identity = AsyncMock(return_value=SimpleNamespace(
        display_name="Synthetic room", room_topic=None, server_name="example.org"))
    adapter._get_display_name = AsyncMock(return_value="Sender")
    adapter._background_read_receipt = lambda *a: None
    adapter._text_batch_delay_seconds = 0
    admitted = []
    adapter.handle_message = AsyncMock(side_effect=lambda event: admitted.append(event))
    assert await adapter.connect()
    machine = adapter._client.crypto
    machine.get_or_fetch_device_by_key = AsyncMock(return_value=None)
    attempted = asyncio.Event()
    original = machine.crypto_store.get_group_session
    async def get_session(*args):
        result = await original(*args)
        if result is None:
            attempted.set()
        return result
    machine.crypto_store.get_group_session = get_session
    try:
        yield adapter, machine, admitted, attempted
    finally:
        await adapter.disconnect()


def encrypted(outbound, event_id="$old", sender=SENDER, timestamp=None):
    return {"type": "m.room.encrypted", "event_id": event_id, "room_id": ROOM,
            "sender": sender, "origin_server_ts": timestamp or int(time.time() * 1000),
            "content": {"algorithm": "m.megolm.v1.aes-sha2", "session_id": outbound.id,
                        "sender_key": "synthetic-sender-key", "device_id": "SENDER",
                        "ciphertext": outbound.encrypt(json.dumps({"room_id": ROOM,
                            "type": "m.room.message", "content": {"msgtype": "m.text", "body": "delayed"}}))}}


def sync(event=None, key=False):
    return {"next_batch": "next", "rooms": {"join": {ROOM: {"timeline": {"events": [event] if event else []}}}},
            "to_device": {"events": [{"type": "com.example.synthetic-key", "sender": SENDER, "content": {}}] if key else []}}


async def import_key(machine, key):
    inbound = InboundGroupSession(key, "synthetic-signing-key", "synthetic-sender-key", ROOM)
    await machine.crypto_store.put_group_session(ROOM, "synthetic-sender-key", inbound.id, inbound)
    machine._mark_session_received(inbound.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_sync", [True, False])
async def test_delayed_key_replays_original_once_without_blocking_sync(native_owner, same_sync):
    adapter, machine, admitted, attempted = native_owner
    outbound = olm.OutboundGroupSession()
    key = outbound.session_key
    event = encrypted(outbound)
    observed = []
    async def observe(decrypted):
        observed.append((decrypted.event_id, decrypted.sender, decrypted.timestamp, decrypted.source))
    adapter._client.add_event_handler(EventType.ROOM_MESSAGE, observe, wait_sync=True)
    async def key_handler(evt):
        await asyncio.wait_for(attempted.wait(), 3)
        await import_key(machine, key)
    adapter._client.add_event_handler(EventType.find("com.example.synthetic-key").with_class(EventType.Class.TO_DEVICE), key_handler, wait_sync=True)
    await asyncio.wait_for(adapter._absorb_sync(adapter._client, sync(event, key=same_sync)), 3)
    await asyncio.wait_for(attempted.wait(), 3)
    if not same_sync:
        await asyncio.wait_for(adapter._absorb_sync(adapter._client, sync(key=True)), 3)
    await async_wait_until(lambda: admitted, timeout=3, what="original encrypted message admitted")
    assert len(admitted) == 1
    assert admitted[0].message_id == "$old"
    assert admitted[0].source.chat_id == ROOM
    assert admitted[0].source.user_id == SENDER
    assert admitted[0].text == "delayed"
    from mautrix.client.syncer import SyncStream
    assert observed == [("$old", SENDER, event["origin_server_ts"], SyncStream.JOINED_ROOM | SyncStream.TIMELINE)]
    await adapter._absorb_sync(adapter._client, sync(event))
    assert len(admitted) == 1


@pytest.mark.asyncio
async def test_later_index_key_is_terminal_not_false_recovery(native_owner, caplog):
    adapter, machine, admitted, attempted = native_owner
    outbound = olm.OutboundGroupSession()
    event = encrypted(outbound)
    later_key = outbound.session_key  # index 1 cannot decrypt the retained index 0
    await adapter._absorb_sync(adapter._client, sync(event))
    await asyncio.wait_for(attempted.wait(), 3)
    await import_key(machine, later_key)
    await async_wait_until(lambda: any("UNKNOWN_MESSAGE_INDEX" in r.message for r in caplog.records),
                           timeout=3, what="irrecoverable original event reported")
    assert not admitted
    await adapter._absorb_sync(adapter._client, sync(encrypted(outbound, "$new")))
    await async_wait_until(lambda: admitted, timeout=3, what="new event admitted")
    assert [event.message_id for event in admitted] == ["$new"]


@pytest.mark.asyncio
async def test_expiry_capacity_duplicates_and_disconnect_are_bounded(native_owner, monkeypatch, caplog):
    import plugins.platforms.matrix.adapter_crypto_ingress as ingress_module
    monkeypatch.setattr(ingress_module, "_TTL_SECONDS", 0.05)
    monkeypatch.setattr(ingress_module, "_MAX_PENDING", 2)
    adapter, _machine, admitted, _attempted = native_owner
    ingress = adapter._crypto_ingress
    outbound = olm.OutboundGroupSession()
    events = [encrypted(outbound, f"$missing{i}") for i in range(3)]
    data = sync()
    data["rooms"]["join"][ROOM]["timeline"]["events"] = events + [events[0]]
    await adapter._absorb_sync(adapter._client, data)
    assert len(ingress.pending) <= 2
    await async_wait_until(lambda: not ingress.pending, timeout=3, what="missing sessions expired")
    assert ingress.failures["capacity"] == 1
    assert ingress.failures["missing_session_expired"] == 2
    assert not admitted
    assert any("$missing0" in record.message for record in caplog.records)
    monkeypatch.setattr(ingress_module, "_TTL_SECONDS", 30)
    await adapter._absorb_sync(adapter._client, sync(encrypted(outbound, "$disconnect")))
    await async_wait_until(lambda: ingress.pending, what="event retained")
    await adapter.disconnect()
    assert ingress.closed and not ingress.pending and not ingress.seen
    assert ingress.failures["disconnected"] == 1
    assert adapter._client is None


@pytest.mark.asyncio
async def test_replay_and_integrity_errors_are_terminal_without_key_requests(native_owner):
    adapter, machine, admitted, _attempted = native_owner
    machine.request_room_key = AsyncMock(side_effect=AssertionError("must not request arbitrary keys"))
    machine.decrypt_megolm_event = AsyncMock(wraps=machine.decrypt_megolm_event)
    outbound = olm.OutboundGroupSession()
    key = outbound.session_key
    first = encrypted(outbound)
    await import_key(machine, key)
    await adapter._absorb_sync(adapter._client, sync(first))
    await async_wait_until(lambda: admitted, what="first event admitted")
    await adapter._absorb_sync(adapter._client, sync(first))
    assert machine.decrypt_megolm_event.await_count == 1
    # Reuse one native ciphertext/index with a different event ID: SDK replay guard must refuse it.
    replay = {**first, "event_id": "$replay"}
    await adapter._absorb_sync(adapter._client, sync(replay))
    await async_wait_until(lambda: adapter._crypto_ingress.failures["decrypt_refused"] == 1,
                           what="native replay refusal")
    corrupt = encrypted(outbound, "$corrupt")
    corrupt["content"]["ciphertext"] = "invalid-ciphertext"
    await adapter._absorb_sync(adapter._client, sync(corrupt))
    await async_wait_until(lambda: adapter._crypto_ingress.failures["decrypt_refused"] == 2,
                           what="native integrity refusal")
    assert machine.decrypt_megolm_event.await_count == 3
    assert len(admitted) == 1
    machine.request_room_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["room", "startup", "ignored"])
async def test_recovery_keeps_ordinary_security_and_startup_gates(native_owner, gate):
    import re
    adapter, machine, admitted, _attempted = native_owner
    machine.decrypt_megolm_event = AsyncMock(wraps=machine.decrypt_megolm_event)
    machine.request_room_key = AsyncMock()
    if gate == "room":
        adapter._allowed_room_ids = {"!permitted:example.org"}
        adapter._is_dm_room.return_value = False
    elif gate == "startup":
        adapter._startup_ts = time.time() + 500
    else:
        adapter._ignored_user_patterns = [re.compile("^@sender:")]
    event = encrypted(olm.OutboundGroupSession())
    await adapter._absorb_sync(adapter._client, sync(event))
    await async_wait_until(lambda: adapter._crypto_ingress.failures["admission_refused"] == 1,
                           what="security refusal")
    assert not admitted
    machine.decrypt_megolm_event.assert_not_awaited()
    machine.request_room_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_quiesces_sdk_background_crypto_before_owner_release(native_owner):
    from mautrix.client import InternalEventType
    from mautrix.types import DeviceLists
    adapter, machine, _admitted, _attempted = native_owner
    client = adapter._client
    entered, stopped = asyncio.Event(), asyncio.Event()
    running = []
    async def fetch(*args, **kwargs):
        running.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    machine._fetch_keys = AsyncMock(side_effect=fetch)
    data = sync()
    data["device_lists"] = {"changed": [SENDER], "left": []}
    try:
        await adapter._absorb_sync(client, data)
        await asyncio.wait_for(entered.wait(), 3)
        await adapter.disconnect()
        assert stopped.is_set(), "SDK background crypto must stop before the ownership claim is released"
        late = client.dispatch_manual_event(InternalEventType.DEVICE_LISTS,
            DeviceLists(changed=[SENDER], left=[]), force_synchronous=True)
        await asyncio.gather(*late)
        assert machine._fetch_keys.await_count == 1
        with pytest.raises(RuntimeError, match="owner.*closed"):
            await machine.encrypt_megolm_event(ROOM, EventType.ROOM_MESSAGE, {})
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
async def test_decrypted_media_uses_ordinary_admission_lifetime(native_owner, monkeypatch, tmp_path, disconnect):
    """A slow valid media download must outlive key recovery, but stop on disconnect."""
    import plugins.platforms.matrix.adapter_crypto_ingress as ingress_module
    from gateway.platforms import base
    monkeypatch.setattr(ingress_module, "_TTL_SECONDS", 0.05)
    adapter, machine, admitted, _attempted = native_owner
    ingress = adapter._crypto_ingress
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def download(_client, url, _max_bytes):
        started.set()
        try:
            await release.wait()
            return b"synthetic media bytes"
        except asyncio.CancelledError:
            cancelled.set()
            raise
    monkeypatch.setattr("plugins.platforms.matrix.adapter.download_mxc_capped", AsyncMock(side_effect=download))
    cached = str(tmp_path / "document.bin")
    monkeypatch.setattr(base, "cache_document_from_bytes_async", AsyncMock(return_value=cached))
    outbound = olm.OutboundGroupSession()
    await import_key(machine, outbound.session_key)
    event = encrypted(outbound, "$media")
    event["content"]["ciphertext"] = outbound.encrypt(json.dumps({
        "room_id": ROOM, "type": "m.room.message", "content": {
            "msgtype": "m.file", "body": "document.bin", "url": "mxc://example.org/file",
            "info": {"mimetype": "application/octet-stream", "size": 21}}}))
    await adapter._absorb_sync(adapter._client, sync(event))
    await asyncio.wait_for(started.wait(), 3)
    assert len(ingress.pending) == 1
    if disconnect:
        await adapter.disconnect()
        assert cancelled.is_set()
        assert not ingress.pending and not admitted
        return
    timer = asyncio.get_running_loop().call_later(0.1, release.set)
    try:
        await async_wait_until(lambda: admitted or cancelled.is_set(), timeout=3, what="media admission completed")
        assert not cancelled.is_set(), "key-recovery deadline cancelled valid decrypted media admission"
        assert len(admitted) == 1
        assert admitted[0].message_id == "$media"
        assert admitted[0].media_urls == [cached]
        assert not ingress.failures
        await async_wait_until(lambda: not ingress.pending, what="admitted media released")
    finally:
        timer.cancel()
