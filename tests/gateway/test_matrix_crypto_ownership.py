"""One cached Olm owner for a store/device, through MatrixAdapter.connect()."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter


def make_adapter(store, device="BOT"):
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="synthetic", extra={
        "homeserver": "https://matrix.example.org", "user_id": "@bot:example.org",
        "encryption": True, "device_id": device}))
    adapter._store_dir = Path(store)
    adapter._resolve_store_dir = lambda: adapter._store_dir
    return adapter


def isolate_transport(adapter):
    async def authenticate(client, api):
        client.device_id = adapter._device_id or "BOT"
        return True
    adapter._connect_authenticate = AsyncMock(side_effect=authenticate)
    adapter._connect_setup_e2ee = AsyncMock(return_value=True)
    adapter._connect_initial_sync = AsyncMock()
    adapter._sync_loop = AsyncMock()
    adapter._wire_plugin_handlers = lambda client: None


@pytest.mark.asyncio
async def test_same_pid_second_object_refused_before_auth_and_sync(tmp_path):
    first, second = make_adapter(tmp_path / "store"), make_adapter(tmp_path / "store")
    for adapter in (first, second):
        isolate_transport(adapter)
    try:
        assert await first.connect()
        assert not await second.connect(), "two crypto objects may not own one store"
        second._connect_authenticate.assert_not_awaited()
        second._connect_initial_sync.assert_not_awaited()
        await second.disconnect()  # a refused contender cannot release the first claim
        assert not await second.connect()
        assert await first.connect(is_reconnect=True)
    finally:
        await first.disconnect()
        await second.disconnect()
    assert await second.connect()
    await second.disconnect()


@pytest.mark.asyncio
async def test_same_device_different_stores_still_has_one_owner(tmp_path):
    first, second = make_adapter(tmp_path / "one"), make_adapter(tmp_path / "two")
    for adapter in (first, second):
        isolate_transport(adapter)
    try:
        assert await first.connect()
        assert not await second.connect(), "one device cannot have two cached accounts"
        second._connect_setup_e2ee.assert_not_awaited()
        second._connect_initial_sync.assert_not_awaited()
    finally:
        await first.disconnect()
        await second.disconnect()


@pytest.mark.asyncio
async def test_partial_failure_and_cancellation_release_only_own_claim(tmp_path):
    first, second = make_adapter(tmp_path / "store"), make_adapter(tmp_path / "store")
    for adapter in (first, second):
        isolate_transport(adapter)
    first._connect_setup_e2ee.return_value = False
    assert not await first.connect()
    assert first._client is None
    assert await second.connect()
    await second.disconnect()
    entered = asyncio.Event()
    async def cancelled(*args):
        entered.set()
        await asyncio.Future()
    first._connect_setup_e2ee.side_effect = cancelled
    task = asyncio.create_task(first.connect())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first._client is None
    assert await second.connect()
    await second.disconnect()


@pytest.mark.asyncio
async def test_different_pid_refusal_and_release(tmp_path):
    script = """
import asyncio, sys
from tests.gateway.test_matrix_crypto_ownership import make_adapter, isolate_transport
async def main():
    adapter = make_adapter(sys.argv[1])
    isolate_transport(adapter)
    result = await adapter.connect()
    await adapter.disconnect()
    print("CONNECTED" if result else "REFUSED", flush=True)
asyncio.run(main())
"""
    async def probe():
        from tests.waiting import async_wait_until
        results = []
        def run():
            results.append(subprocess.run(
                [sys.executable, "-c", script, str(tmp_path / "store")],
                capture_output=True, text=True, timeout=10, env=dict(os.environ), check=False))
        thread = threading.Thread(target=run)
        thread.start()
        try:
            await async_wait_until(lambda: results, timeout=12, what="crypto owner process probe")
            result = results[0]
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()
        finally:
            thread.join(timeout=2)
    adapter = make_adapter(tmp_path / "store")
    isolate_transport(adapter)
    try:
        assert await adapter.connect()
        assert await probe() == "REFUSED"
    finally:
        await adapter.disconnect()
    assert await probe() == "CONNECTED"


@pytest.mark.asyncio
async def test_disconnect_cancels_accepted_owner_send_before_releasing_claim(tmp_path):
    from types import SimpleNamespace
    from gateway.platform_send import send_on_owner_loop
    first, second = make_adapter(tmp_path / "store"), make_adapter(tmp_path / "store")
    for adapter in (first, second):
        isolate_transport(adapter)
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def send(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    first.send_platform_message = send
    assert await first.connect()
    task = asyncio.create_task(send_on_owner_loop(SimpleNamespace(), first, {
        "profile_id": str(tmp_path), "platform": "matrix", "chat_id": "!room:example.org",
        "message": "hello", "media_files": [], "thread_id": None, "force_document": False}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not await second.connect()
        await first.disconnect()
        assert stopped.is_set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await second.connect()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await first.disconnect()
        await second.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["db", "http", "both", "cancel_db"])
async def test_cleanup_failure_closes_independent_resource_and_retains_owner(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    first, contender = make_adapter(tmp_path / "store"), make_adapter(tmp_path / "store")
    for adapter in (first, contender):
        isolate_transport(adapter)
    assert await first.connect()
    client = first._client
    session = client.api.session
    actual_close = session.close
    db_error = asyncio.CancelledError() if failure == "cancel_db" else RuntimeError("synthetic DB stop failure")
    http_error = RuntimeError("synthetic HTTP close failure")
    db_fails = failure in ("db", "both", "cancel_db")
    http_fails = failure in ("http", "both")
    stop = AsyncMock(side_effect=db_error if db_fails else None)
    database = first._crypto_db = SimpleNamespace(stop=stop)
    close = AsyncMock(side_effect=http_error if http_fails else actual_close)
    monkeypatch.setattr(session, "close", close)
    try:
        expected = db_error if db_fails else http_error
        with pytest.raises(type(expected)) as raised:
            await first.disconnect()
        assert raised.value is expected
        assert first._crypto_ownership is not None
        assert first._crypto_db is (database if db_fails else None)
        stop.assert_awaited_once()
        close.assert_awaited_once()
        assert session.closed is (not http_fails)
        assert first._closing and not first.is_connected
        assert first._client is (client if http_fails else None)
        assert not await contender.connect()
        contender._connect_authenticate.assert_not_awaited()
        # Retry only resources whose shutdown was not confirmed; no new client/connect.
        stop.side_effect = None
        close.side_effect = actual_close
        await first.disconnect()
        assert first._crypto_ownership is None
        assert first._client is None and first._crypto_db is None
        assert session.closed
        assert stop.await_count == (2 if db_fails else 1)
        assert close.await_count == (2 if http_fails else 1)
        assert await contender.connect()
    finally:
        stop.side_effect = None
        monkeypatch.setattr(session, "close", actual_close)
        await first.disconnect()
        await contender.disconnect()
