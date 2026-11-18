"""A sync response with more encrypted events than the ingress bound loses none of them.

The SDK runs every ``wait_sync`` handler of one sync at once and stores the sync cursor before
it does, so an event the ingress refuses is never delivered again.
"""

import asyncio
from types import SimpleNamespace

import pytest

from plugins.platforms.matrix.adapter_crypto_ingress import _MAX_OVERFLOW, _MAX_PENDING, CryptoIngress

ROOM = "!room:example.org"


class _Adapter:
    _processed_events_set = set()
    _ignored_user_patterns = []
    _startup_ts = 0

    def _is_self_sender(self, sender):
        return False

    def _is_system_or_bridge_sender(self, sender):
        return False

    async def _is_allowed_matrix_room_event(self, room_id):
        return True


class _Client:
    """Decryption blocks on ``release``, like a busy crypto store; every key is already present."""

    def __init__(self):
        self.release = asyncio.Event()
        self.dispatched = []
        self.crypto = SimpleNamespace(decrypt_megolm_event=self._decrypt)

    async def _decrypt(self, event):
        await self.release.wait()
        return event

    def dispatch_event(self, event, source):
        self.dispatched.append(event.event_id)
        return []


def _event(n):
    return SimpleNamespace(room_id=ROOM, event_id=f"$e{n}", sender="@alice:example.org", timestamp=0,
                           source={}, content=SimpleNamespace(ciphertext="x", session_id="s"))


async def _drain(ingress):
    for _ in range(100):
        if not ingress.pending and not ingress.overflow:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("ingress did not drain")


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_events_past_the_slots_are_parked_without_holding_the_sync():
    client = _Client()
    ingress = CryptoIngress(_Adapter(), client)
    total = 2 * _MAX_PENDING + 1
    handlers = [asyncio.create_task(ingress.handle(_event(n))) for n in range(total)]

    await _settle()
    assert len(ingress.pending) == _MAX_PENDING  # the bound still holds
    assert all(handler.done() for handler in handlers)  # /sync is free to deliver keys meanwhile

    client.release.set()
    await _drain(ingress)

    assert sorted(client.dispatched) == sorted(f"$e{n}" for n in range(total))
    assert ingress.diagnostics() == {"pending": 0, "crypto_tasks": 0, "terminal_failures": {},
                                     "parked": 0, "deferred": total - _MAX_PENDING}


@pytest.mark.asyncio
async def test_a_full_overflow_holds_the_sync_until_a_slot_frees():
    client = _Client()
    ingress = CryptoIngress(_Adapter(), client)
    total = _MAX_PENDING + _MAX_OVERFLOW + 5
    handlers = [asyncio.create_task(ingress.handle(_event(n))) for n in range(total)]

    await _settle()
    assert len(ingress.pending) == _MAX_PENDING and len(ingress.overflow) == _MAX_OVERFLOW
    assert [handler.done() for handler in handlers].count(False) == 5

    client.release.set()
    await asyncio.wait_for(asyncio.gather(*handlers), 5)
    await _drain(ingress)

    assert sorted(client.dispatched) == sorted(f"$e{n}" for n in range(total))
    assert ingress.diagnostics()["terminal_failures"] == {}


@pytest.mark.asyncio
async def test_closing_reports_parked_and_waiting_events_as_disconnected():
    client = _Client()
    ingress = CryptoIngress(_Adapter(), client)
    total = _MAX_PENDING + _MAX_OVERFLOW + 5
    handlers = [asyncio.create_task(ingress.handle(_event(n))) for n in range(total)]
    await _settle()

    await ingress.close()
    await asyncio.wait_for(asyncio.gather(*handlers), 5)

    assert ingress.pending == {} and not ingress.overflow and client.dispatched == []
    assert ingress.failures["disconnected"] == total
