"""Bounded delivery through a gateway-owned adapter and its event loop.

The private control verb accepts only message data. It never starts a transport,
invokes a caller-selected method, or retries an operation with an unknown outcome.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
from pathlib import Path

_SEND_TIMEOUT = 55.0
_IPC_TIMEOUT = 60.0
_MAX_ACTIVE = 16
_UNKNOWN = {"error": "Gateway owner delivery outcome unknown; do not retry automatically"}
_SHARED_BOT_REFUSED = {"error": "Profile shares the default profile's bot and no gateway.profile_routes entry routes this target to it"}
_FIELDS = {"profile_id", "platform", "chat_id", "message", "media_files", "thread_id", "force_document"}


def _validate(params):
    if not isinstance(params, dict) or set(params) != _FIELDS:
        raise ValueError("invalid platform send parameters")
    for key, limit in (("profile_id", 4096), ("platform", 64), ("chat_id", 1024), ("message", 32768)):
        value = params[key]
        if not isinstance(value, str) or len(value.encode("utf-8")) > limit or (key != "message" and not value):
            raise ValueError("invalid platform send field")
    thread = params["thread_id"]
    if thread is not None and (not isinstance(thread, str) or len(thread.encode("utf-8")) > 1024):
        raise ValueError("invalid platform send thread")
    media = params["media_files"]
    if not isinstance(media, list) or len(media) > 16 or type(params["force_document"]) is not bool:
        raise ValueError("invalid platform send media")
    for item in media:
        if (not isinstance(item, (list, tuple)) or len(item) != 2 or not isinstance(item[0], str)
                or not Path(item[0]).is_absolute() or len(item[0].encode("utf-8")) > 4096
                or type(item[1]) is not bool):
            raise ValueError("invalid platform send attachment")
    if len(json.dumps(params).encode("utf-8")) > 60 * 1024:
        raise ValueError("platform send payload too large")


def _result(value, params):
    """Return a fixed, bounded shape; transport objects never cross IPC."""
    if not isinstance(value, dict):
        return dict(_UNKNOWN)
    if value.get("success") is True:
        message_id = value.get("message_id")
        if message_id is not None and (not isinstance(message_id, str) or len(message_id) > 1024):
            return dict(_UNKNOWN)
        return {"success": True, "platform": params["platform"], "chat_id": params["chat_id"], "message_id": message_id}
    error = value.get("error")
    if not isinstance(error, str) or not error or len(error) > 2048:
        return dict(_UNKNOWN)
    from agent.redact import redact_sensitive_text
    return {"error": redact_sensitive_text(error)}


async def _deliver(adapter, params):
    consumer = getattr(adapter, "send_platform_message", None)
    if not callable(consumer):
        return {"error": "Gateway owner does not support standalone delivery for this platform"}
    if getattr(adapter, "_closing", False):
        return {"error": "Gateway owner is disconnecting"}
    sends = getattr(adapter, "_platform_send_tasks", None)
    if sends is None:
        sends = adapter._platform_send_tasks = set()
    if len(sends) >= _MAX_ACTIVE:
        return {"error": "Gateway owner busy"}
    task = asyncio.current_task()
    sends.add(task)
    try:
        async with asyncio.timeout(_SEND_TIMEOUT):
            value = await consumer(params["chat_id"], params["message"], media_files=params["media_files"],
                                   thread_id=params["thread_id"], force_document=params["force_document"])
        return _result(value, params)
    except Exception:  # health: allow BLE001 -- acceptance may precede failure; expose no exception data
        # A transport exception may follow server acceptance. Never trigger fallback/retry.
        return dict(_UNKNOWN)
    finally:
        sends.discard(task)


async def _on_loop(loop, operation):
    if loop is None or not loop.is_running():
        operation.close()
        return {"error": "Gateway owner event loop unavailable"}
    if loop is asyncio.get_running_loop():
        return await operation
    future = asyncio.run_coroutine_threadsafe(operation, loop)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), _IPC_TIMEOUT)
    except TimeoutError:
        future.cancel()
        return dict(_UNKNOWN)


async def send_on_owner_loop(runner, adapter, params):
    _validate(params)
    loop = getattr(adapter, "_platform_send_loop", None) or getattr(runner, "_gateway_loop", None)
    return await _on_loop(loop, _deliver(adapter, params))


async def _send_authorized(runner, params, check_target=True):
    from gateway.config import Platform
    from gateway.session_authorities import authority_for_profile_id, owner_scope
    if getattr(runner, "_draining", False):
        return {"error": "Gateway owner unavailable"}
    authority = authority_for_profile_id(runner, params["profile_id"])
    if authority is None or getattr(authority, "retiring", False):
        return {"error": "Gateway owner does not serve requested profile"}
    registry = getattr(runner, "session_authorities", None)
    profile = registry.profile_name(authority) if registry is not None else getattr(runner, "_primary_profile_name", None)
    with owner_scope(authority, hydrate_secrets=True):
        adapter = runner._authorization_adapter(Platform(params["platform"]), profile)
        if adapter is None:
            return {"error": "Gateway owner has no adapter for requested profile"}
        if check_target and not runner._shared_bot_target_allowed(
                Platform(params["platform"]), profile, params["chat_id"], params["thread_id"]):
            return dict(_SHARED_BOT_REFUSED)
        return await send_on_owner_loop(runner, adapter, params)


def register_platform_send(runner, server, loop):
    """Install only in private_handlers, behind the native peer authentication gate."""
    active = set()

    async def dispatch(params):
        if len(active) >= _MAX_ACTIVE:
            return {"error": "Gateway owner busy"}
        task = asyncio.current_task()
        active.add(task)
        try:
            return await _send_authorized(runner, params)
        finally:
            active.discard(task)

    def handler(params, peer_subject):
        _validate(params)
        if not peer_subject:
            raise PermissionError("authenticated platform send required")
        future = asyncio.run_coroutine_threadsafe(dispatch(params), loop)
        try:
            return future.result(timeout=_IPC_TIMEOUT - 1)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return dict(_UNKNOWN)
    server.private_handlers["platform-send"] = handler


async def send_via_owner(platform, chat_id, message, *, media_files=None, thread_id=None, force_document=False):
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    params = {"profile_id": str(home), "platform": platform.value, "chat_id": chat_id, "message": message,
              "media_files": list(media_files or []), "thread_id": thread_id, "force_document": force_document}
    try:
        _validate(params)
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        if runner is not None:
            # The shared-bot target rule reads the calling turn's session context, which does not
            # follow the call onto the owner loop: decide here, then deliver.
            from hermes_cli.profiles import get_active_profile_name
            if not runner._shared_bot_target_allowed(platform, get_active_profile_name(), chat_id, thread_id):
                return dict(_SHARED_BOT_REFUSED)
            return await _on_loop(getattr(runner, "_gateway_loop", None),
                                  _send_authorized(runner, params, check_target=False))
        # This exchange verifies the native peer and resolves a served profile's control home.
        # No diagnostic query, credential lookup, owner start, or second send on failure.
        from gateway.session_hosted_transport import owner_request
        value = await asyncio.wait_for(asyncio.to_thread(
            owner_request, home, "platform-send", params, timeout=_IPC_TIMEOUT), _IPC_TIMEOUT + 1)
        return _result(value, params)
    except (ValueError, TypeError):
        return {"error": "Invalid gateway owner delivery request"}
    except Exception:  # health: allow BLE001 -- IPC uncertainty must fail closed without leaking peer exception data
        return dict(_UNKNOWN)
