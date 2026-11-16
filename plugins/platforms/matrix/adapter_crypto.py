"""Matrix E2EE crypto state-store shim handed to mautrix's OlmMachine."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)


class _CryptoStateStore:
    """StateStore shim for OlmMachine (MemoryStateStore lacks is_encrypted/get_encryption_info/
    find_shared_rooms); falls back to a homeserver state query when the store has no info."""

    def __init__(self, client_state_store: Any, joined_rooms: set, client=None):
        self._ss = client_state_store
        self._joined_rooms = joined_rooms
        self._client = client
        # MemoryStateStore has no set_encryption_info, so cache homeserver answers here.
        self._enc_info_cache: dict = {}

    async def is_encrypted(self, room_id: str) -> bool:
        return (await self.get_encryption_info(room_id)) is not None

    async def get_encryption_info(self, room_id: str):
        info = await self._ss.get_encryption_info(room_id) if hasattr(self._ss, "get_encryption_info") else None
        if info is not None:
            return info
        if room_id in self._enc_info_cache:
            return self._enc_info_cache[room_id]
        if self._client is None:
            return None
        try:
            from mautrix.types import EventType as _ET, RoomEncryptionStateEventContent as _Enc, RoomID as _RID
            raw = await self._client.get_state_event(_RID(room_id), _ET.ROOM_ENCRYPTION)
        except Exception as exc:
            logger.debug("Matrix: homeserver encryption-info query failed for %s: %s", room_id, exc)
            return None
        if not raw:
            return None
        content = raw if isinstance(raw, _Enc) else _Enc.deserialize(
            raw.serialize() if hasattr(raw, "serialize") else raw)
        if hasattr(self._ss, "set_encryption_info"):
            with suppress(Exception):
                await self._ss.set_encryption_info(_RID(room_id), content)
        self._enc_info_cache[room_id] = content
        return content

    async def find_shared_rooms(self, user_id: str) -> list:
        return list(self._joined_rooms)  # all joined rooms: correct for a single-user bot


async def connect_crypto_owner(adapter, *, is_reconnect=False) -> bool:
    import asyncio
    lock = getattr(adapter, "_crypto_lifecycle_lock", None)
    if lock is None:
        lock = adapter._crypto_lifecycle_lock = asyncio.Lock()
    async with lock:
        return await _connect_crypto_owner(adapter, is_reconnect=is_reconnect)


async def _connect_crypto_owner(adapter, *, is_reconnect=False) -> bool:
    """Reserve crypto before constructing a client or dispatching initial-sync membership."""
    from plugins.platforms.matrix import adapter as matrix
    from plugins.platforms.matrix.adapter_crypto_ownership import CryptoOwnership
    if adapter._client is not None or getattr(adapter, "_crypto_ownership", None) is not None:
        await _disconnect_crypto_owner(adapter)
    adapter._device_id_unverified = False
    if not adapter._homeserver:
        logger.error("Matrix: homeserver URL not configured")
        return False
    claim = CryptoOwnership()
    try:
        store = adapter._resolve_store_dir()
        if adapter._encryption:
            claim.acquire(store / "crypto.db.owner")
        adapter._crypto_ownership = claim
        result = await _connect_client(adapter, matrix)
        if not result:
            await _disconnect_crypto_owner(adapter)
        return result
    except BaseException as exc:
        await _disconnect_crypto_owner(adapter)
        if not isinstance(exc, Exception):
            raise
        logger.error("Matrix: connect refused or failed: %s", exc)
        return False


async def _connect_client(adapter, matrix) -> bool:
    import asyncio
    import time
    from mautrix.api import HTTPAPI
    from mautrix.client import Client, InternalEventType
    from mautrix.client.dispatcher import MembershipEventDispatcher
    from mautrix.client.state_store import MemoryStateStore, MemorySyncStore
    adapter._resolve_store_dir().mkdir(parents=True, exist_ok=True)
    api = HTTPAPI(base_url=adapter._homeserver, token=adapter._access_token or "",
                  client_session=matrix._create_matrix_session(adapter._proxy_url))
    state_store = MemoryStateStore()
    client = Client(mxid=matrix.UserID(adapter._user_id or ""), device_id=adapter._device_id or None,
                    api=api, state_store=state_store, sync_store=MemorySyncStore())
    adapter._client = client
    adapter._platform_send_loop = asyncio.get_running_loop()
    if not await adapter._connect_authenticate(client, api):
        return False
    if adapter._encryption:
        adapter._crypto_ownership.acquire_device(str(client.mxid), str(client.device_id or ""))
        if not await adapter._connect_setup_e2ee(client, api, state_store):
            return False
    client.add_dispatcher(MembershipEventDispatcher)
    client.add_event_handler(matrix.EventType.ROOM_MESSAGE, adapter._on_room_message, wait_sync=True)
    client.add_event_handler(matrix.EventType.REACTION, adapter._on_reaction, wait_sync=True)
    client.add_event_handler(InternalEventType.INVITE, adapter._on_invite, wait_sync=True)
    if getattr(client, "crypto", None) is not None:
        from plugins.platforms.matrix.adapter_crypto_ingress import install_crypto_ingress
        install_crypto_ingress(adapter, client)
    adapter._startup_ts = time.time()
    adapter._reset_clock_skew_detector()
    adapter._closing = False
    await adapter._connect_initial_sync(client)
    if adapter._encryption and getattr(client, "crypto", None):
        try:
            await client.crypto.share_keys()
        except Exception as exc:  # health: allow BLE001 -- preserve advisory startup key-upload behavior
            logger.warning("Matrix: initial key share failed: %s", exc)
    adapter._sync_task = asyncio.create_task(adapter._sync_loop())
    adapter._mark_connected()
    adapter._wire_plugin_handlers(client)
    return True


async def disconnect_crypto_owner(adapter) -> None:
    import asyncio
    lock = getattr(adapter, "_crypto_lifecycle_lock", None)
    if lock is None:
        lock = adapter._crypto_lifecycle_lock = asyncio.Lock()
    async with lock:
        await _disconnect_crypto_owner(adapter)


async def _disconnect_crypto_owner(adapter) -> None:
    """Quiesce every adapter writer before releasing this object's lifetime reservation."""
    import asyncio
    adapter._closing = True
    adapter._mark_disconnected()
    sync_task = adapter._sync_task
    if sync_task and not sync_task.done():
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
    ingress = getattr(adapter, "_crypto_ingress", None)
    if ingress is not None:
        await ingress.close()
        adapter._crypto_ingress = None
    for tasks in (adapter._invite_join_tasks.values(), adapter._reaction_redaction_tasks):
        pending = list(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    adapter._invite_join_tasks.clear()
    adapter._reaction_redaction_tasks.clear()
    sends = list(getattr(adapter, "_platform_send_tasks", ()))
    for task in sends:
        task.cancel()
    if sends:
        await asyncio.gather(*sends, return_exceptions=True)
    await _close_crypto_resources(adapter)
    claim = getattr(adapter, "_crypto_ownership", None)
    if claim is not None:
        claim.release()
        adapter._crypto_ownership = None
    logger.info("Matrix: disconnected")


async def _close_crypto_resources(adapter) -> None:
    """Close independent resources, retaining failed handles and ownership for retry."""
    failures = []
    if adapter._crypto_db is not None:
        try:
            await adapter._crypto_db.stop()
        except BaseException as exc:  # health: allow BLE001 -- retain failure/cancellation while closing independent resource
            failures.append(exc)
        else:
            adapter._crypto_db = None
    if adapter._client is not None:
        try:
            await adapter._client.api.session.close()
        except BaseException as exc:  # health: allow BLE001 -- retain failure/cancellation while closing independent resource
            failures.append(exc)
        else:
            adapter._client = None
    if failures:
        for extra in failures[1:]:
            failures[0].add_note(f"Matrix HTTP session cleanup also failed: {type(extra).__name__}")
        raise failures[0]
