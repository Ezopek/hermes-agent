"""Image availability uses the API image model while preserving native pool cooldowns."""

import base64
import json
import socket
import time
from pathlib import Path

import httpx
import pytest


@pytest.mark.parametrize("route", ["row", "config"])
@pytest.mark.parametrize(
    "state, available",
    [("ready", True), ("unrelated", True), ("image", False),
     ("provider", False), ("absent", False)],
)
def test_image_entrypoint_respects_only_applicable_pool_cooldowns(
    tmp_path, monkeypatch, route, state, available,
):
    from agent.auxiliary_client import _resolve_codex_credential_and_base
    from agent.credential_pool import load_pool
    from tools import image_generation_tool as image_tool

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("OPENAI_API_KEY", "OPENAI_IMAGE_MODEL", "HERMES_CODEX_BASE_URL", "FAL_KEY"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    now = time.time()
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(now + 86400)}).encode()).decode()
    token = f"eyJhbGciOiJub25lIn0.{claims}.sig"
    base = "https://images.example.test/backend-api/codex"
    row = {
        "id": "image-account", "label": "image-account", "priority": 0,
        "source": "manual", "auth_type": "oauth", "access_token": token,
        "refresh_token": "synthetic-refresh-token",
        "base_url": base if route == "row" else "https://chatgpt.com/backend-api/codex",
        "model_cooldowns": {},
    }
    if state in ("unrelated", "image"):
        row["model_cooldowns"] = {
            "chat-model" if state == "unrelated" else "gpt-image-2": now + 3600,
        }
    if state == "provider":
        row.update(last_status="exhausted", last_status_at=now, last_error_code=402,
                   last_error_reset_at=now + 3600, failure_reason="billing")
    rows = [] if state == "absent" else [row]
    (home / "auth.json").write_text(json.dumps({
        "version": 1, "providers": {}, "credential_pool": {"openai-codex": rows},
    }))
    (home / "config.yaml").write_text(json.dumps({
        "model": {"provider": "openai-codex", "default": "current-chat-model",
                  "base_url": base if route == "config" else "https://unused.example.test/codex"},
        "image_gen": {"provider": "openai-codex", "model": "gpt-image-2-high"},
    }))
    pool = load_pool("openai-codex")
    assert (pool.select(model="gpt-image-2") is not None) is available
    # Legacy callers remain conservative when they do not specify a model.
    legacy_token, legacy_base = _resolve_codex_credential_and_base()
    assert legacy_token == (token if state == "ready" else None)
    if legacy_token:
        assert legacy_base == base

    requests = []
    png = base64.b64encode(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )).decode()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": png}]}, request=request)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(
        transport=httpx.MockTransport(respond), headers=kwargs.get("headers"),
        timeout=kwargs.get("timeout")))
    # Real bundled discovery, tool availability, dispatch, request building and image persistence.
    assert image_tool.check_image_generation_requirements() is available
    result = json.loads(image_tool._dispatch_to_plugin_provider("a red circle", "square"))
    assert result["success"] is available, result
    if available:
        (request,) = requests
        assert str(request.url) == base + "/images/generations"
        assert request.headers["authorization"] == f"Bearer {token}"
        assert json.loads(request.content)["model"] == "gpt-image-2"
        assert Path(result["image"]).is_file()
    else:
        assert result["error_type"] == "auth_required"
        assert requests == []
    persisted = json.loads((home / "auth.json").read_text())
    assert persisted["providers"] == {}
    saved_rows = persisted["credential_pool"]["openai-codex"]
    if rows:
        (saved,) = saved_rows
        for key in ("access_token", "refresh_token", "base_url", "model_cooldowns",
                    "last_status", "last_error_code", "last_error_reset_at", "failure_reason"):
            assert saved.get(key) == row.get(key)
    else:
        assert saved_rows == []


@pytest.mark.parametrize("state", ["image", "provider", "quota"])
def test_image_cooldown_cannot_be_bypassed_via_singleton(tmp_path, monkeypatch, state):
    from agent.auxiliary_client import _resolve_codex_credential_and_base
    from agent.credential_pool import load_pool
    from hermes_cli import auth, auth_codex
    from tools import image_generation_tool as image_tool

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_CODEX_BASE_URL", raising=False)
    now = time.time()
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(now + 86400)}).encode()).decode()
    token = f"eyJhbGciOiJub25lIn0.{claims}.sig"
    tokens = {"access_token": token, "refresh_token": "synthetic-refresh-token"}
    row = {"id": "image-account", "label": "image-account", "priority": 0,
           "source": "device_code", "auth_type": "oauth", **tokens,
           "base_url": "https://chatgpt.com/backend-api/codex"}
    if state == "image":
        row["model_cooldowns"] = {"gpt-image-2": now + 3600}
    else:
        row.update(last_status="exhausted", last_status_at=now, last_error_code=402,
                   last_error_reset_at=now + 3600, failure_reason="billing")
        if state == "quota":
            row.update(last_error_code=429, last_error_reason="usage_limit_reached",
                       last_error_message="The usage limit has been reached")
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1, "providers": {"openai-codex": {"tokens": tokens, "auth_mode": "chatgpt"}},
        "credential_pool": {"openai-codex": [row]},
    }))
    (tmp_path / "config.yaml").write_text(json.dumps({"image_gen": {"provider": "openai-codex"}}))

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    probes = []

    def deny_quota(request):
        probes.append(request)
        return httpx.Response(429, request=request)

    monkeypatch.setattr(auth, "_codex_quota_probe_cache", {})
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(deny_quota), **kwargs))
    assert load_pool("openai-codex").select(model="gpt-image-2") is None
    assert _resolve_codex_credential_and_base()[0] == token  # Preserve legacy no-model fallback.
    assert image_tool.check_image_generation_requirements() is False
    result = json.loads(image_tool._dispatch_to_plugin_provider("a red circle", "square"))
    assert result["error_type"] == "auth_required"
    saved = json.loads((tmp_path / "auth.json").read_text())
    assert saved["providers"]["openai-codex"]["tokens"] == tokens
    (saved_row,) = saved["credential_pool"]["openai-codex"]
    for key in ("access_token", "refresh_token", "base_url", "model_cooldowns", "last_status",
                "last_error_code", "last_error_reason", "last_error_message", "last_error_reset_at"):
        assert saved_row.get(key) == row.get(key)
    if state == "quota":
        (probe,) = probes
        assert probe.url.host == "chatgpt.com"
        assert probe.headers["authorization"] == f"Bearer {token}"
    else:
        assert probes == []
