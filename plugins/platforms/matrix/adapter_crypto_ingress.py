"""Bounded encrypted-event admission while ordinary key import catches up.

Only SessionNotFound is recoverable. Keep ciphertext off the sync reader, then
use the SDK's replay/integrity checks and the adapter's ordinary admission path.
No room-key requests are sent to untrusted or third-party devices.
"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict, deque
import logging
import time

logger = logging.getLogger(__name__)
_MAX_PENDING = 64
_MAX_OVERFLOW = 256  # parked events past _MAX_PENDING; beyond it handle() holds the sync
_TTL_SECONDS = 30.0
_MAX_SEEN = 1000
_SEEN_TTL_SECONDS = 300.0
_POLL_SECONDS = 0.25


class CryptoIngress:
    def __init__(self, adapter, client):
        self.adapter = adapter
        self.client = client
        self.pending = {}
        self.crypto_tasks = Counter()
        self.seen = OrderedDict()
        self.failures = Counter()
        self.overflow = deque()  # events parked while every slot is busy
        self.deferred = 0  # events that had to be parked; not a failure, nothing is dropped
        self.closed = False

    def track_crypto(self, operation, *, ignore_closed=False):
        async def tracked(*args, **kwargs):
            if self.closed:
                if ignore_closed:
                    return None
                raise RuntimeError("Matrix crypto owner is closed")
            task = asyncio.current_task()
            self.crypto_tasks[task] += 1
            try:
                return await operation(*args, **kwargs)
            finally:
                self.crypto_tasks[task] -= 1
                if not self.crypto_tasks[task]:
                    del self.crypto_tasks[task]
        return tracked

    def track_sdk(self):
        machine = self.client.crypto
        # The SDK dispatches crypto callbacks without wait_sync and can spawn a
        # session-unwedge task from those callbacks. Both must stop before unlock.
        for event_type, handlers in list(self.client.event_handlers.items()):
            for handler, props in list(handlers.items()):
                if getattr(handler, "__self__", None) is machine:
                    self.client.remove_event_handler(event_type, handler)
                    self.client.add_event_handler(event_type, self.track_crypto(handler, ignore_closed=True),
                                                  wait_sync=props.wait_sync, sync_stream=props.sync_stream)
        for name in ("_unwedge_session", "share_keys", "encrypt_megolm_event", "share_group_session", "decrypt_megolm_event"):
            setattr(machine, name, self.track_crypto(getattr(machine, name)))

    def _remember(self, key):
        now = time.monotonic()
        while self.seen and next(iter(self.seen.values())) <= now:
            self.seen.popitem(last=False)
        if key in self.seen:
            return False
        self.seen[key] = now + _SEEN_TTL_SECONDS
        while len(self.seen) > _MAX_SEEN:
            self.seen.popitem(last=False)
        return True

    def _failure(self, event, reason, detail=""):
        self.failures[reason] += 1
        logger.warning("Matrix: encrypted event %s in %s terminal failure (%s)%s",
                       event.event_id, event.room_id, reason, f": {detail}" if detail else "")

    async def handle(self, event):
        key = (event.room_id, event.event_id)
        if self.closed or key in self.pending or event.event_id in self.adapter._processed_events_set:
            return
        if not self._remember(key):
            return
        ciphertext = getattr(event.content, "ciphertext", None)
        if not event.event_id or not isinstance(ciphertext, str) or len(ciphertext) > 128 * 1024:
            self._failure(event, "invalid_event")
            return
        # The SDK runs every handler of one sync at once and stores the cursor first, so a
        # refused event is gone for good. Past the slots, park it (keys for the busy slots may
        # arrive in the next sync, so the sync must keep running); only a full overflow holds
        # this wait_sync handler, and with it the next /sync, until a slot frees.
        if len(self.pending) >= _MAX_PENDING:
            self.deferred += 1
            logger.warning("Matrix: encrypted event %s in %s deferred (%d pending, %d parked)",
                           event.event_id, event.room_id, len(self.pending), len(self.overflow))
            while len(self.overflow) >= _MAX_OVERFLOW and not self.closed:
                await asyncio.wait(set(self.pending.values()), return_when=asyncio.FIRST_COMPLETED)
            if self.closed:
                self._failure(event, "disconnected")
                return
            self.overflow.append(event)
            return
        self._start(event)

    def _start(self, event):
        key = (event.room_id, event.event_id)
        self.pending[key] = asyncio.create_task(self._process(event, event.source))
        self.pending[key].add_done_callback(lambda done: self._finished(key))

    def _finished(self, key):
        self.pending.pop(key, None)
        while self.overflow and len(self.pending) < _MAX_PENDING and not self.closed:
            self._start(self.overflow.popleft())

    async def _wait_for_key(self, event):
        # SDK 0.21 wait_for_session retains timed-out futures indefinitely. A bounded
        # local-store poll also covers imports that finished before a waiter was armed.
        store = self.client.crypto.crypto_store
        while not await store.has_group_session(event.room_id, event.content.session_id):
            await asyncio.sleep(_POLL_SECONDS)

    async def _process(self, event, source):
        from mautrix.errors import DecryptionError, SessionNotFound
        from plugins.platforms.matrix.adapter import _matrix_event_timestamp_seconds, _STARTUP_GRACE_SECONDS
        adapter = self.adapter
        phase = "decrypt"
        try:
            async with asyncio.timeout(_TTL_SECONDS):
                sender = str(event.sender)
                timestamp = _matrix_event_timestamp_seconds(event)
                if (adapter._is_self_sender(sender) or adapter._is_system_or_bridge_sender(sender)
                        or any(pattern.search(sender) for pattern in adapter._ignored_user_patterns)
                        or (timestamp and timestamp < adapter._startup_ts - _STARTUP_GRACE_SECONDS)
                        or not await adapter._is_allowed_matrix_room_event(str(event.room_id))):
                    self._failure(event, "admission_refused")
                    return
                try:
                    decrypted = await self.client.crypto.decrypt_megolm_event(event)
                except SessionNotFound:
                    phase = "missing_session"
                    await self._wait_for_key(event)
                    phase = "decrypt"
                    decrypted = await self.client.crypto.decrypt_megolm_event(event)
                if any(getattr(decrypted, field) != getattr(event, field)
                       for field in ("room_id", "event_id", "sender", "timestamp")):
                    self._failure(event, "envelope_mismatch")
                    return
            event = decrypted  # Release ciphertext before ordinary admission/download awaits.
            phase = "admission"
            tasks = self.client.dispatch_event(decrypted, source)
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    self._failure(event, "admission_failed")
        except asyncio.CancelledError:
            self._failure(event, "disconnected")
            raise
        except TimeoutError:
            self._failure(event, f"{phase}_expired")
        except DecryptionError as exc:
            detail = type(exc).__name__
            if exc.__cause__ is not None:
                detail += f" ({exc.__cause__})"
            self._failure(event, "decrypt_refused", detail)
        except Exception as exc:  # health: allow BLE001 -- report terminal failure without logging ciphertext or exception data
            self._failure(event, "processing_failed", type(exc).__name__)

    async def close(self):
        self.closed = True
        tasks = list(set(self.pending.values()) | set(self.crypto_tasks))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event in self.overflow:
            self._failure(event, "disconnected")
        self.overflow.clear()
        self.pending.clear()
        self.crypto_tasks.clear()
        self.seen.clear()

    def diagnostics(self):
        return {"pending": len(self.pending), "crypto_tasks": len(self.crypto_tasks), "terminal_failures": dict(self.failures),
                "parked": len(self.overflow), "deferred": self.deferred}


def install_crypto_ingress(adapter, client):
    from mautrix.client.encryption_manager import DecryptionDispatcher
    from mautrix.types import EventType
    client.remove_dispatcher(DecryptionDispatcher)
    ingress = CryptoIngress(adapter, client)
    ingress.track_sdk()
    client.add_event_handler(EventType.ROOM_ENCRYPTED, ingress.handle, wait_sync=True)
    adapter._crypto_ingress = ingress
