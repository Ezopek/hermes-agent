"""Native libolm/SQLite regressions for durable OTK uploads and uncertain responses.

The API transport below simulates acceptance/response loss; it never sends HTTP.
A batch whose upload outcome is unknown may already be claimed by a peer, so it is
never published again: its private keys stay (sessions peers already started must
decrypt) and the next upload carries fresh key IDs.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import aiohttp
from mautrix.api import HTTPAPI
from mautrix.client import Client
from mautrix.client.state_store import MemoryStateStore
from mautrix.crypto import OlmAccount
from mautrix.crypto.signature import verify_signature_json
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.errors import MatrixResponseError
from mautrix.types import DeviceOTKCount
from mautrix.util.async_db import Database
import pytest
import pytest_asyncio

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter

USER = "@bot:example.org"
DEVICE = "BOT"
PICKLE_KEY = f"{USER}:{DEVICE}"


class Transport:
    """Accept real SDK upload payloads, optionally lose or corrupt the response."""

    def __init__(self, count=50):
        self.count = count
        self.uploads = []
        self.probes = 0
        self.on_upload = None

    async def __call__(self, method, path, content, **kwargs):
        assert str(path).endswith("/keys/upload"), "unexpected API call"
        if not content:
            self.probes += 1
        else:
            self.uploads.append(deepcopy(content))
            if self.on_upload:
                return await self.on_upload(content)
        return {"one_time_key_counts": {"signed_curve25519": self.count}}


def _signed(account):
    # Request signatures without generating additional keys.
    return account.get_one_time_keys(USER, DEVICE, account.max_one_time_keys // 2)


def _decrypts(account, uploaded_key):
    """Real pre-key round trip against ``uploaded_key``; consumes it from ``account``."""
    sender = OlmAccount()
    encrypted = sender.new_outbound_session(account.identity_key, uploaded_key["key"]).encrypt("claimed")
    return account.new_inbound_session(sender.identity_key, encrypted.body).decrypt(encrypted) == "claimed"


async def _assert_retired(db, old_upload, new_upload):
    """``new_upload`` shares no ID with ``old_upload``, whose private keys are still durable."""
    old, new = old_upload["one_time_keys"], new_upload["one_time_keys"]
    assert new and not set(old) & set(new), "an uncertain batch's key IDs were published again"
    for key in old.values():
        assert _decrypts(await _fresh_account(db), key), "private key of an uncertain batch was lost"


async def _fresh_account(db):
    # PgCryptoStore caches account objects; a fresh store must read the actual row.
    return await PgCryptoStore(USER, PICKLE_KEY, db).get_account()


@asynccontextmanager
async def _owner(path, transport):
    """Run the real adapter crypto setup, substituting only remote services."""
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="synthetic", extra={
        "homeserver": "https://matrix.example.org", "user_id": USER,
        "device_id": DEVICE, "encryption": True}))
    adapter._store_dir = Path(path)
    adapter._verify_device_keys_on_server = AsyncMock(return_value=True)
    adapter._verify_or_bootstrap_cross_signing = AsyncMock()
    transport.adapter = adapter
    async with aiohttp.ClientSession() as session:
        api = HTTPAPI(base_url="https://matrix.example.org", token="synthetic",
                      client_session=session)
        api.request = transport
        state = MemoryStateStore()
        client = Client(mxid=USER, device_id=DEVICE, api=api, state_store=state)
        try:
            assert await adapter._connect_setup_e2ee(client, api, state)
            assert isinstance(client.crypto.crypto_store, PgCryptoStore)
            assert client.crypto.crypto_store is not client.state_store
            yield client.crypto
        finally:
            if adapter._crypto_db is not None:
                await adapter._crypto_db.stop()


@pytest_asyncio.fixture
async def native_owner(tmp_path):
    transport = Transport()
    async with _owner(tmp_path / "store", transport) as machine:
        # Startup uploads device keys only because the simulated server has enough OTKs.
        assert machine.account.shared
        assert not _signed(machine.account)
        transport.uploads.clear()
        transport.probes = 0
        machine._last_key_share = time.monotonic() - 61
        yield machine, transport


@pytest.mark.asyncio
async def test_crypto_setup_commits_private_keys_before_initial_upload(tmp_path):
    transport = Transport(count=0)
    durable = []

    async def inspect_upload(content):
        account = await _fresh_account(transport.adapter._crypto_db)
        durable.append(_signed(account))
        return {"one_time_key_counts": {"signed_curve25519": 50}}

    transport.on_upload = inspect_upload
    async with _owner(tmp_path / "store", transport) as machine:
        uploaded = transport.uploads[0]["one_time_keys"]
        assert uploaded and durable == [uploaded], "startup upload preceded durable private keys"
        for key in uploaded.values():
            assert verify_signature_json(key, USER, DEVICE, machine.account.signing_key)
        stored = await _fresh_account(machine.crypto_store.db)
        assert stored.shared and not _signed(stored)


@pytest.mark.asyncio
async def test_preupload_persistence_failure_prevents_upload(native_owner, monkeypatch):
    machine, transport = native_owner
    monkeypatch.setattr(machine.crypto_store.db, "execute", AsyncMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        await machine.share_keys(0)
    assert transport.uploads == [], "public keys escaped before private-key commit"
    assert _signed(machine.account)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancel", "malformed"])
async def test_uncertain_upload_is_never_republished(native_owner, failure):
    machine, transport = native_owner

    async def lose_response(content):
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "malformed":
            return {}  # SDK rejects a response without one_time_key_counts.
        raise ConnectionError("accepted but response lost")

    transport.on_upload = lose_response
    expected_error = {"cancel": asyncio.CancelledError, "malformed": MatrixResponseError,
                      "exception": ConnectionError}[failure]
    with pytest.raises(expected_error):
        await machine.share_keys(0)
    assert _signed(machine.account) == transport.uploads[0]["one_time_keys"]
    stored = await _fresh_account(machine.crypto_store.db)
    assert _signed(stored) == transport.uploads[0]["one_time_keys"], "uncertain upload has no durable private keys"
    # A peer may have claimed a key of the first batch; every later share (the sync handler
    # fires on the count drop that claim causes) must publish only fresh IDs.
    transport.on_upload = None
    machine._last_key_share = time.monotonic() - 61
    await machine.share_keys(49)
    await _assert_retired(machine.crypto_store.db, transport.uploads[0], transport.uploads[1])
    assert not _signed(machine.account)


@pytest.mark.asyncio
async def test_reloaded_unpublished_batch_is_retired_not_republished(native_owner):
    machine, transport = native_owner
    pending = machine.account.get_one_time_keys(USER, DEVICE, 0)
    await machine.crypto_store.put_account(machine.account)
    machine.crypto_store = PgCryptoStore(USER, PICKLE_KEY, machine.crypto_store.db)
    await machine.load()
    await machine.share_keys(0)
    await _assert_retired(machine.crypto_store.db, {"one_time_keys": pending}, transport.uploads[0])


@pytest.mark.asyncio
async def test_initial_upload_failure_leaves_device_and_otks_unpublished(tmp_path):
    transport = Transport(count=0)

    async def lose_response(content):
        raise ConnectionError("accepted but response lost")

    transport.on_upload = lose_response
    async with _owner(tmp_path / "store", transport) as machine:
        assert not machine.account.shared
        stored = await _fresh_account(machine.crypto_store.db)
        assert not stored.shared
        assert _signed(stored) == transport.uploads[0]["one_time_keys"]


async def _exit_at_upload(path, payload_path):
    """A bounded child exits as the transport double accepts a public batch."""
    transport = Transport()
    async with _owner(path, transport) as machine:
        async def accepted_without_response(content):
            Path(payload_path).write_text(json.dumps(content))
            os._exit(73)
        transport.on_upload = accepted_without_response
        machine._last_key_share = time.monotonic() - 61
        await machine.share_keys(0)


@pytest.mark.asyncio
async def test_process_exit_at_upload_reopens_keeps_private_keys_and_publishes_fresh_ids(tmp_path):
    path, payload = tmp_path / "store", tmp_path / "accepted.json"
    script = (
        "import asyncio, sys\n"
        "from tests.plugins.platforms.matrix.test_otk_durability import _exit_at_upload\n"
        "asyncio.run(_exit_at_upload(sys.argv[1], sys.argv[2]))\n"
    )
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(path), str(payload)],
                                     capture_output=True, text=True, timeout=20)
    assert result.returncode == 73, (result.stdout, result.stderr)
    accepted = json.loads(payload.read_text())
    db = Database.create(f"sqlite:///{path / 'crypto.db'}", upgrade_table=PgCryptoStore.upgrade_table)
    await db.start()
    try:
        account = await _fresh_account(db)
        assert _signed(account) == accepted["one_time_keys"], "child exited with uploaded private keys missing on disk"
        # Real Olm pre-key decryption proves the persisted private half is usable.
        sender = OlmAccount()
        public_key = next(iter(accepted["one_time_keys"].values()))["key"]
        outbound = sender.new_outbound_session(account.identity_key, public_key)
        encrypted = outbound.encrypt("durable private OTK")
        inbound = account.new_inbound_session(sender.identity_key, encrypted.body)
        assert inbound.decrypt(encrypted) == "durable private OTK"
    finally:
        await db.stop()
    transport = Transport(count=0)
    async with _owner(path, transport) as reopened:
        await _assert_retired(reopened.crypto_store.db, accepted, transport.uploads[0])


@pytest.mark.asyncio
async def test_success_persists_publication_and_noop_preserves_store(native_owner, monkeypatch):
    machine, transport = native_owner
    put_account = AsyncMock(wraps=machine.crypto_store.put_account)
    monkeypatch.setattr(machine.crypto_store, "put_account", put_account)
    await machine.share_keys(0)
    assert len(transport.uploads) == 1
    assert machine._last_key_share > time.monotonic() - 5
    assert machine.account.shared and not _signed(machine.account)
    stored = await _fresh_account(machine.crypto_store.db)
    assert stored.shared and not _signed(stored)
    assert put_account.await_count == 2, "success must persist before upload and after publication"
    put_account.reset_mock()
    transport.uploads.clear()
    await machine.share_keys(machine.account.max_one_time_keys // 2)
    assert not transport.uploads
    put_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_persistence_failure_retains_recoverable_private_keys(native_owner, monkeypatch):
    machine, transport = native_owner
    original = machine.crypto_store.put_account

    async def fail_publication(account):
        if not account.one_time_keys.get("curve25519"):
            raise OSError("publication commit failed")
        await original(account)

    monkeypatch.setattr(machine.crypto_store, "put_account", fail_publication)
    with pytest.raises(OSError, match="publication commit failed"):
        await machine.share_keys(0)
    assert len(transport.uploads) == 1
    stored = await _fresh_account(machine.crypto_store.db)
    assert _signed(stored) == transport.uploads[0]["one_time_keys"]
    # A reload recovers the private keys; the helper itself never spins/retries.
    machine.account = stored
    monkeypatch.setattr(machine.crypto_store, "put_account", original)
    transport.count = 0
    machine._last_key_share = time.monotonic() - 61
    await machine.share_keys(0)
    await _assert_retired(machine.crypto_store.db, transport.uploads[0], transport.uploads[1])


@pytest.mark.asyncio
async def test_public_sdk_lock_and_count_probe_are_preserved(native_owner):
    machine, transport = native_owner
    transport.count = 50
    await machine.share_keys()  # Missing count always probes.
    assert transport.probes == 1 and not transport.uploads
    machine._last_key_share = time.monotonic()
    await machine.handle_otk_count(DeviceOTKCount(signed_curve25519=0))
    assert transport.probes == 2 and not transport.uploads
    transport.count = 0
    machine._last_key_share = time.monotonic() - 61
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_upload(content):
        entered.set()
        await release.wait()
        return {"one_time_key_counts": {"signed_curve25519": 50}}

    transport.on_upload = hold_upload
    first = asyncio.create_task(machine.share_keys(0))
    await asyncio.wait_for(entered.wait(), 3)
    assert machine._share_keys_lock.locked()
    second = asyncio.create_task(machine.share_keys(50))
    try:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 3)
    finally:
        first.cancel()
        second.cancel()
    assert len(transport.uploads) == 1


@pytest.mark.asyncio
async def test_device_only_upload_control(tmp_path):
    transport = Transport(count=50)
    async with _owner(tmp_path / "store", transport) as machine:
        assert transport.probes == 1
        assert len(transport.uploads) == 1
        assert "device_keys" in transport.uploads[0]
        assert "one_time_keys" not in transport.uploads[0]
        assert machine.account.shared
        assert (await _fresh_account(machine.crypto_store.db)).shared


@pytest.mark.asyncio
@pytest.mark.parametrize("count", ["not-a-count", True, -1, 1.5])
@pytest.mark.parametrize("algorithm", ["signed_curve25519", "curve25519"])
async def test_invalid_upload_counts_retire_batch_for_retry_and_reopen(tmp_path, count, algorithm):
    path = tmp_path / "store"
    transport = Transport()
    async with _owner(path, transport) as machine:
        machine.account.shared = False
        machine._last_key_share = time.monotonic() - 61
        last_share = machine._last_key_share
        transport.uploads.clear()

        async def invalid_response(content):
            return {"one_time_key_counts": {"signed_curve25519": 50, algorithm: count}}

        transport.on_upload = invalid_response
        with pytest.raises(ValueError, match="^Invalid one-time key counts response$"):
            await machine.share_keys(0)
        assert not machine.account.shared
        assert machine._last_key_share == last_share
        pending = transport.uploads[0]["one_time_keys"]
        assert pending and _signed(machine.account) == pending
        stored = await _fresh_account(machine.crypto_store.db)
        assert not stored.shared and _signed(stored) == pending
        original_upload = transport.uploads[0]
    # A new database, store and machine must keep the private keys and publish fresh IDs.
    retry = Transport(count=0)
    async with _owner(path, retry) as reloaded:
        await _assert_retired(reloaded.crypto_store.db, original_upload, retry.uploads[0])
        assert reloaded.account.shared and not _signed(reloaded.account)
        stored = await _fresh_account(reloaded.crypto_store.db)
        assert stored.shared and not _signed(stored)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", ["not-a-count", True, -1, 1.5])
async def test_invalid_probe_counts_leave_pending_batch_until_a_valid_upload_retires_it(native_owner, count):
    machine, transport = native_owner
    machine.account.shared = False
    pending = machine.account.get_one_time_keys(USER, DEVICE, 0)
    await machine.crypto_store.put_account(machine.account)
    last_share = machine._last_key_share
    transport.count = count
    with pytest.raises(ValueError, match="^Invalid one-time key counts response$"):
        await machine.share_keys()
    assert transport.probes == 1 and not transport.uploads
    assert not machine.account.shared and machine._last_key_share == last_share
    assert _signed(machine.account) == pending
    stored = await _fresh_account(machine.crypto_store.db)
    assert not stored.shared and _signed(stored) == pending
    transport.count = 0
    await machine.share_keys()
    await _assert_retired(machine.crypto_store.db, {"one_time_keys": pending}, transport.uploads[0])
    assert machine.account.shared and not _signed(machine.account)


@pytest.mark.asyncio
@pytest.mark.parametrize("counts", [{}, {"signed_curve25519": 0}, {"signed_curve25519": 50}])
async def test_valid_count_maps_allow_probe_upload_and_noop(native_owner, counts):
    machine, transport = native_owner
    machine.account.shared = False

    async def valid_response(method, path, content, **kwargs):
        await transport(method, path, content, **kwargs)
        return {"one_time_key_counts": counts}

    machine.client.api.request = valid_response
    await machine.share_keys()
    assert transport.probes == 1 and len(transport.uploads) == 1
    uploaded = transport.uploads[0]
    assert "device_keys" in uploaded
    assert ("one_time_keys" in uploaded) == (counts.get("signed_curve25519", 0) == 0)
    stored = await _fresh_account(machine.crypto_store.db)
    assert stored.shared and not _signed(stored)
    await machine.share_keys(50)
    assert len(transport.uploads) == 1  # Enough server keys and no pending batch is a no-op.
