"""Matrix public standalone delivery must use the existing owner, on its event loop."""
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.matrix.adapter import MatrixAdapter, _standalone_send
from gateway.outbound.senders import _send_matrix_via_adapter


@pytest.fixture(autouse=True)
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def live_owner(monkeypatch):
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="synthetic", extra={
        "homeserver": "https://matrix.example.org", "user_id": "@bot:example.org", "encryption": False}))
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.MATRIX: adapter}
    runner._profile_adapters = {}
    from hermes_cli.profiles import get_active_profile_name
    runner._primary_profile_name = get_active_profile_name()
    runner._gateway_loop = asyncio.get_running_loop()
    runner._draining = False
    from gateway.session_authorities import SessionAuthorities
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    runner.session_authorities = SessionAuthorities(home)
    authority = SimpleNamespace(profile_id=str(home), runner=runner)
    runner.session_authorities.add(home, authority)
    import gateway.run
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
    return runner, adapter


@pytest.mark.asyncio
async def test_public_registered_sender_routes_text_media_and_chunks(monkeypatch, tmp_path):
    import plugins.platforms.matrix.adapter as module
    monkeypatch.setattr("aiohttp.ClientSession", Mock(side_effect=AssertionError("raw HTTP forbidden")))
    _runner, adapter = live_owner(monkeypatch)
    wire = []
    async def send_event(room, event_type, content):
        wire.append((str(room), content))
        return f"$event{len(wire)}"
    adapter._client = SimpleNamespace(crypto=None, send_message_event=send_event)
    adapter.max_message_length = 100
    adapter.send_image_file = AsyncMock(return_value=SimpleNamespace(success=True, message_id="$image"))
    image = tmp_path / "picture.png"
    image.write_bytes(b"synthetic image")
    connect = AsyncMock(side_effect=AssertionError("must not connect another SDK client"))
    monkeypatch.setattr(MatrixAdapter, "connect", connect)
    registered = {}
    module.register(SimpleNamespace(register_platform=lambda **kw: registered.update(kw)))
    result = await registered["standalone_sender_fn"](
        PlatformConfig(), "!room:example.org", "hello " * 800,
        media_files=[(str(image), False)], thread_id="$thread")
    assert result.get("success"), result
    assert len(wire) > 1
    assert all(event[1]["m.relates_to"]["event_id"] == "$thread" for event in wire)
    adapter.send_image_file.assert_awaited_once_with("!room:example.org", str(image), metadata={"thread_id": "$thread"})
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_loop_sender_uses_owner_loop(monkeypatch):
    runner, adapter = live_owner(monkeypatch)
    loops = []
    async def send_event(*args):
        loops.append(asyncio.get_running_loop())
        return "$sent"
    adapter._client = SimpleNamespace(crypto=None, send_message_event=send_event)
    def worker():
        async def run():
            from tests.waiting import async_wait_until
            task = asyncio.create_task(_send_matrix_via_adapter(PlatformConfig(), "!room:example.org", "hello"))
            await async_wait_until(task.done, timeout=10, what="worker received owner result")
            return task.result()
        return asyncio.run(run())
    from tests.waiting import async_wait_until
    results = []
    thread = threading.Thread(target=lambda: results.append(worker()))
    thread.start()
    try:
        await async_wait_until(lambda: not thread.is_alive(), timeout=10, what="owner send completed")
        result = results[0]
    finally:
        thread.join(timeout=2)
    assert result.get("success"), result
    assert loops == [runner._gateway_loop]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["timeout", None, {"success": True, "message_id": {"invalid": True}}])
async def test_no_owner_or_ambiguous_ipc_never_connects_or_retries(monkeypatch, reply):
    import gateway.run
    from gateway import session_hosted_transport
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: None)
    async def exchange(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr("asyncio.to_thread", exchange)
    request = Mock(side_effect=TimeoutError("reply lost")) if reply == "timeout" else Mock(return_value=reply)
    monkeypatch.setattr(session_hosted_transport, "owner_request", request)
    connect = AsyncMock(return_value=False)
    monkeypatch.setattr(MatrixAdapter, "connect", connect)
    result = await _send_matrix_via_adapter(PlatformConfig(), "!room:example.org", "hello")
    assert "error" in result
    assert "owner" in result["error"].lower() or "unknown" in result["error"].lower()
    connect.assert_not_awaited()
    assert request.call_count == 1


@pytest.mark.asyncio
async def test_unserved_profile_cannot_borrow_default_matrix(monkeypatch, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    runner, adapter = live_owner(monkeypatch)
    runner._profile_adapters["unserved"] = {}
    adapter.send = AsyncMock()
    connect = AsyncMock(return_value=False)
    monkeypatch.setattr(MatrixAdapter, "connect", connect)
    home = tmp_path / "profiles" / "unserved"
    home.mkdir(parents=True)
    token = set_hermes_home_override(str(home))
    try:
        result = await _send_matrix_via_adapter(PlatformConfig(), "!room:example.org", "hello")
    finally:
        reset_hermes_home_override(token)
    assert "error" in result
    adapter.send.assert_not_awaited()
    connect.assert_not_awaited()


def native_sdk_owner(monkeypatch, *, event_id=None, private=False):
    """Synthetic owner with real SDK send/transaction construction and no HTTP exchange."""
    from mautrix.api import HTTPAPI
    from mautrix.client import Client
    from mautrix.client.state_store import MemoryStateStore
    runner, adapter = live_owner(monkeypatch)
    api = HTTPAPI("https://matrix.example.org", token="synthetic-owner-token",
                  client_session=SimpleNamespace(close=AsyncMock()))
    wire = []
    async def send(method, url, content, query, headers):
        import json
        wire.append({"method": str(method), "path": url.raw_path, "content": json.loads(content),
                     "headers": headers, "loop": asyncio.get_running_loop()})
        return {"event_id": event_id or f"$event{len(wire)}"}, SimpleNamespace(status=200)
    api._send = AsyncMock(side_effect=send)
    adapter._client = Client(mxid="@bot:example.org", device_id="SYNTHETIC", api=api,
                             state_store=MemoryStateStore())
    if private:
        from gateway.control_socket import GatewayControlServer
        from gateway.platform_send import register_platform_send
        from gateway.session_hosted_transport import owner_request
        from hermes_constants import get_hermes_home
        from tests.gateway.test_platform_send import private_exchange
        home = get_hermes_home().resolve()
        server = GatewayControlServer(home)
        register_platform_send(runner, server, runner._gateway_loop)
        monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: None)
        async def exchange(fn, request_home, verb, params, *, timeout):
            # Only the local IPC transport is synthetic; the private verb and consumer are real.
            assert fn is owner_request and Path(request_home).resolve() == home
            assert verb == "platform-send" and timeout > 0
            response = await private_exchange(server, params, subject="synthetic-authenticated-peer")
            assert response["ok"], response
            return response["result"]
        monkeypatch.setattr(asyncio, "to_thread", exchange)
    return runner, adapter, wire


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_reply", [False, True])
async def test_unicode_wire_chunks_preserve_media_thread_and_stop_on_unknown(monkeypatch, tmp_path, lost_reply):
    """Escaped text larger than one IPC request must keep content and avoid replaying an accepted prefix."""
    runner, adapter, wire = native_sdk_owner(monkeypatch, private=True)
    document = tmp_path / "document.pdf"
    document.write_bytes(b"synthetic document")
    adapter.send_document = AsyncMock(return_value=SimpleNamespace(success=True, message_id="$document"))
    exchange = asyncio.to_thread
    attempts = []
    async def reply(fn, *args, **kwargs):
        result = await exchange(fn, *args, **kwargs)
        attempts.append(result)
        if lost_reply and len(attempts) == 2:
            raise TimeoutError("synthetic accepted operation lost its reply")
        return result
    monkeypatch.setattr(asyncio, "to_thread", reply)
    text = "界" * 15_000
    result = await _standalone_send(PlatformConfig(), "!room:example.org", text,
                                    media_files=[(str(document), False)], thread_id="$thread")
    # Existing Matrix chunks append transport indicators; compare the original text separately.
    sent = "".join(event["content"]["body"].partition(" (")[0] for event in wire)
    assert all(event["loop"] is runner._gateway_loop for event in wire)
    assert all(event["content"]["m.relates_to"]["event_id"] == "$thread" for event in wire)
    if lost_reply:
        assert "unknown" in result["error"].lower()
        assert len(attempts) == 2 and len(wire) == 2
        assert text.startswith(sent) and len(sent) < len(text)
        adapter.send_document.assert_not_awaited()
    else:
        assert result["success"] and result["message_id"] == "$document"
        assert len(attempts) > 1 and sent == text
        adapter.send_document.assert_awaited_once_with(
            "!room:example.org", str(document), metadata={"thread_id": "$thread"})


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["text", "media"])
@pytest.mark.parametrize("failure_type", [TimeoutError, RuntimeError])
async def test_accepted_sdk_response_loss_is_unknown_and_stops_delivery(monkeypatch, tmp_path, stage, failure_type):
    """Real adapter methods must preserve uncertainty through the public owner route."""
    from mautrix.types import ContentURI
    _runner, adapter = live_owner(monkeypatch)
    accepted = []
    async def accepted_then_lost(*args, **kwargs):
        accepted.append(args)
        raise failure_type("synthetic-private-exception-data")
    upload = AsyncMock(return_value=ContentURI("mxc://example.org/synthetic"))
    keys = AsyncMock()
    adapter._client = SimpleNamespace(crypto=SimpleNamespace(share_keys=keys),
        send_message_event=accepted_then_lost, upload_media=upload)
    adapter._encryption = False
    adapter.max_message_length = 100
    connect = AsyncMock(side_effect=AssertionError("second client forbidden"))
    monkeypatch.setattr(MatrixAdapter, "connect", connect)
    document = tmp_path / "document.pdf"
    document.write_bytes(b"synthetic document")
    result = await _send_matrix_via_adapter(PlatformConfig(), "!room:example.org",
        "hello " * 200 if stage == "text" else "", [(str(document), False)] * 2, "$thread")
    assert len(accepted) == 1
    assert accepted[0][2]["m.relates_to"]["event_id"] == "$thread"
    assert upload.await_count == (0 if stage == "text" else 1)
    connect.assert_not_awaited()
    keys.assert_not_awaited()
    error = result.get("error", "").lower()
    assert "unknown" in error and "do not retry automatically" in error, result
    assert "synthetic-private-exception-data" not in str(result)


@pytest.mark.asyncio
async def test_owner_media_validation_failure_remains_known(monkeypatch, tmp_path):
    _runner, adapter, wire = native_sdk_owner(monkeypatch)
    missing = tmp_path / "missing.pdf"
    result = await _send_matrix_via_adapter(PlatformConfig(), "!room:example.org", "", [(str(missing), False)])
    assert "file not found" in result["error"].lower() and "unknown" not in result["error"].lower()
    large = tmp_path / "large.pdf"
    large.write_bytes(b"synthetic document")
    adapter._max_media_bytes = 1
    result = await _send_matrix_via_adapter(PlatformConfig(), "!room:example.org", "", [(str(large), False)])
    assert "exceeds Matrix limit" in result["error"] and "unknown" not in result["error"].lower()
    assert not wire


@pytest.mark.asyncio
async def test_unknown_sdk_outcome_is_final_for_base_delivery(monkeypatch):
    _runner, adapter = live_owner(monkeypatch)
    send = AsyncMock(side_effect=TimeoutError("synthetic-private-exception-data"))
    adapter._client = SimpleNamespace(crypto=None, send_message_event=send)
    result = await adapter._send_with_retry("!room:example.org", "hello", base_delay=0)
    assert not result.success and "unknown" in result.error.lower()
    assert "do not retry automatically" in result.error.lower()
    assert send.await_count == 1
