"""The native bootstrap exposes delivery only through OS-authenticated private control."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.control_socket import GatewayControlServer
from gateway.run_bootstrap import _start_gateway_start_control_socket


@pytest.mark.asyncio
async def test_default_bootstrap_registers_private_send_only(monkeypatch):
    monkeypatch.setattr(GatewayControlServer, "start", AsyncMock(return_value=True))
    runner = SimpleNamespace(session_runtime_descriptor={}, session_ticket_store=None)
    server = await _start_gateway_start_control_socket(runner)
    assert server is not None
    try:
        assert "platform-send" in server.private_handlers
        assert "platform-send" not in server._handlers
        raw = json.dumps({"protocol": 1, "id": 1, "verb": "platform-send", "params": {}}).encode()
        response = json.loads(server.handle_request_line(raw))
        assert response["ok"] is False
        assert "authenticated" in response["error"]
    finally:
        await server.stop()


@pytest.fixture
def owner(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session_authorities import SessionAuthorities
    from plugins.platforms.matrix.adapter import MatrixAdapter
    from gateway import status
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(status, "_get_process_hermes_home", lambda: home)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._draining = False
    runner._primary_profile_name = "custom"
    runner._profile_adapters = {}
    runner.session_authorities = SessionAuthorities(home)
    runner.session_authorities.add(home, SimpleNamespace(profile_id=str(home), runner=runner))
    runner.session_ticket_store = None
    runner.session_runtime_descriptor = {
        "runtime_protocol": 1, "instance_id": "synthetic-owner", "authority_epoch": 1,
        "state": "ready", "api_origin": "http://127.0.0.1:12345",
        "capabilities": ["session-authority-v1"],
        "served_profiles": [{"profile_id": str(home), "home": str(home)}]}
    adapter = MatrixAdapter(PlatformConfig(enabled=True, extra={"encryption": False}))
    wire = []
    async def send_event(room, event_type, content):
        wire.append((str(room), content, asyncio.get_running_loop()))
        return f"$event{len(wire)}"
    adapter._client = SimpleNamespace(crypto=None, send_message_event=send_event)
    runner.adapters = {Platform.MATRIX: adapter}
    return home, runner, adapter, wire


def params(home, **changes):
    return {"profile_id": str(home), "platform": "matrix", "chat_id": "!room:example.org",
            "message": "hello", "media_files": [], "thread_id": None, "force_document": False, **changes}


async def private_exchange(server, body, subject="uid:synthetic"):
    import threading
    from tests.waiting import async_wait_until
    replies = []
    raw = json.dumps({"protocol": 1, "id": 1, "verb": "platform-send", "params": body}).encode()
    thread = threading.Thread(target=lambda: replies.append(json.loads(server.handle_request_line(raw, subject))))
    thread.start()
    try:
        await async_wait_until(lambda: replies, what="private owner result")
        return replies[0]
    finally:
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_authenticated_default_wiring_delivers_and_refuses_wrong_profile(owner, monkeypatch):
    home, runner, _adapter, wire = owner
    runner._gateway_loop = asyncio.get_running_loop()
    monkeypatch.setattr(GatewayControlServer, "start", AsyncMock(return_value=True))
    server = await _start_gateway_start_control_socket(runner)
    try:
        accepted = await private_exchange(server, params(home))
        assert accepted["result"] == {"success": True, "platform": "matrix", "chat_id": "!room:example.org", "message_id": "$event1"}
        assert wire[0][2] is runner._gateway_loop
        refused = await private_exchange(server, params(home / "unserved"))
        assert "does not serve" in refused["result"]["error"]
        assert len(wire) == 1
        denied = await private_exchange(server, params(home), subject=None)
        assert denied["ok"] is False
        assert len(wire) == 1
        malformed = await private_exchange(server, params(home, method="disconnect"))
        assert malformed["ok"] is False
        oversized = await private_exchange(server, params(home, message="x" * 32769))
        assert oversized["ok"] is False
        assert len(wire) == 1
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_served_profile_without_adapter_and_same_named_custom_home_refused(owner, monkeypatch):
    home, runner, _adapter, wire = owner
    from gateway.config import Platform
    from gateway.platform_send import send_via_owner
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    import gateway.run
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
    runner._gateway_loop = asyncio.get_running_loop()
    unserved = home.parent / "another-custom-home"
    unserved.mkdir()
    token = set_hermes_home_override(str(unserved))
    try:
        result = await send_via_owner(Platform.MATRIX, "!room:example.org", "do not cross homes")
    finally:
        reset_hermes_home_override(token)
    assert "does not serve" in result["error"]
    secondary = home / "profiles" / "empty"
    secondary.mkdir(parents=True)
    runner.session_authorities.add(secondary, SimpleNamespace(profile_id=str(secondary), runner=runner), name="empty")
    runner._profile_adapters["empty"] = {}
    monkeypatch.setattr(GatewayControlServer, "start", AsyncMock(return_value=True))
    server = await _start_gateway_start_control_socket(runner)
    try:
        result = await private_exchange(server, params(secondary))
        assert "no adapter" in result["result"]["error"]
        assert not wire
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_accepted_timeout_does_not_resend_or_connect(owner, monkeypatch):
    from gateway import platform_send
    home, runner, adapter, wire = owner
    monkeypatch.setattr(platform_send, "_SEND_TIMEOUT", 0.05)
    entered = asyncio.Event()
    async def accepted_without_reply(*args):
        wire.append("accepted")
        entered.set()
        await asyncio.Future()
    adapter._client.send_message_event = accepted_without_reply
    runner._gateway_loop = asyncio.get_running_loop()
    monkeypatch.setattr(GatewayControlServer, "start", AsyncMock(return_value=True))
    server = await _start_gateway_start_control_socket(runner)
    try:
        result = await private_exchange(server, params(home))
        assert "unknown" in result["result"]["error"].lower()
        assert wire == ["accepted"]
        assert not adapter._platform_send_tasks
    finally:
        await server.stop()


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
async def test_standalone_process_delivers_over_real_authenticated_control_socket(owner, monkeypatch, tmp_path):
    """Parent's namespace gate runs this native-socket path; no SDK client may be created."""
    import os
    import subprocess
    import sys
    from tests.waiting import async_wait_until
    home, runner, adapter, wire = owner
    runner._gateway_loop = asyncio.get_running_loop()
    media = tmp_path / "file.pdf"
    media.write_bytes(b"synthetic document")
    async def send_document(chat_id, path, metadata=None):
        wire.append((chat_id, path, metadata))
        return SimpleNamespace(success=True, message_id="$document")
    adapter.send_document = send_document
    adapter.max_message_length = 100
    server = await _start_gateway_start_control_socket(runner)
    assert server is not None, "native control socket unavailable"
    script = '''
import asyncio, json, sys
from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter, _standalone_send
async def forbidden(*args, **kwargs):
    raise AssertionError("second SDK connect forbidden")
MatrixAdapter.connect = forbidden
print(json.dumps(asyncio.run(_standalone_send(PlatformConfig(), "!room:example.org", "hello " * 100,
    media_files=[(sys.argv[1], False)], thread_id="$thread"))), flush=True)
'''
    import threading
    replies = []
    def run_client():
        replies.append(subprocess.run([sys.executable, "-c", script, str(media)], capture_output=True,
            text=True, timeout=15, check=False, env={**os.environ, "HERMES_HOME": str(home)}))
    thread = threading.Thread(target=run_client)
    thread.start()
    try:
        await async_wait_until(lambda: replies, timeout=17, what="standalone IPC delivery")
        reply = replies[0]
        assert reply.returncode == 0, reply.stderr
        assert json.loads(reply.stdout).get("message_id") == "$document", reply.stdout
        assert len(wire) > 2
        assert wire[-1] == ("!room:example.org", str(media), {"thread_id": "$thread"})
    finally:
        thread.join(timeout=2)
        await server.stop()
