"""Configured Codex routing with real config/auth/pools and trapped SDK transports.

Regression contracts for #125695/#126768; related upstream work: PR136629
(maxcanada, 43481f2) and PR132164 (khungate, acf0431).
"""

import asyncio
import json
from pathlib import Path
import socket
import time
from types import SimpleNamespace

import httpx
import pytest
import hermes_yaml as yaml


TARGET = "gpt-6-luna"
CODEX_URL = "https://chatgpt.com/backend-api/codex"
POOL_URL = "https://codex-pool.example.test/backend-api/codex"
OR_MODEL = "openai/gpt-6-sol"
OR_KEY = "synthetic-profile-openrouter"
MESSAGES = [{"role": "user", "content": "Describe this synthetic image."}]
ALIASES = [TARGET, "openai/" + TARGET, TARGET + "-900k", "openai/" + TARGET + "-900k"]


def configure_vision(home, policy="explicit", *, native=False):
    vision = {"provider": "auto" if policy == "auto" else "openai-codex", "model": TARGET}
    if policy == "fallback":
        vision["fallback_chain"] = [{"provider": "openrouter", "model": OR_MODEL}]
    config = {
        "model": {"provider": "openai-codex", "default": TARGET},
        "agent": {"image_input_mode": "native" if native else "text"},
        "auxiliary": {"vision": vision},
    }
    if native:
        config["model"]["supports_vision"] = True
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


@pytest.fixture
def synthetic_profile(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from agent.secret_scope import load_env_file, reset_secret_scope, set_secret_scope

    fake_home = tmp_path / "user"
    fake_home.mkdir()
    codex_home = fake_home / "codex"
    codex_home.mkdir()
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CODEX_BASE_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-ambient-openrouter")
    (home / ".env").write_text(f"OPENROUTER_API_KEY={OR_KEY}\n", encoding="utf-8")
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    configure_vision(home)
    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope(load_env_file(home / ".env"), profile_home=str(home))
    try:
        yield home
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


@pytest.fixture
def trapped_sdk(synthetic_profile, monkeypatch):
    """Keep real SDK classes/serialization, observe init, and isolate HTTP and metadata."""
    import openai
    from agent import auxiliary_client as aux
    from agent import models_dev

    # Synthetic capabilities use the real disk-cache reader, without fetching models.dev.
    catalog = {
        provider: {"id": provider, "models": {
            model: {"id": model, "attachment": True, "tool_call": True,
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                    "limit": {"context": 128000, "output": 4096}}
            for model in models
        }}
        for provider, models in {
            "openai": [TARGET, "gpt-6-sol"],
            "openrouter": ["openai/" + TARGET, OR_MODEL],
        }.items()
    }
    (synthetic_profile / "models_dev_cache.json").write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_retry_after", 0)

    aux.shutdown_cached_clients()
    aux._reset_aux_unhealthy_cache()
    aux.clear_runtime_main()
    built, requests, clients, network_attempts = [], [], [], []

    def refuse_network(*args, **kwargs):
        network_attempts.append(args)
        raise AssertionError("Unexpected network access outside MockTransport")

    monkeypatch.setattr(socket, "getaddrinfo", refuse_network)
    monkeypatch.setattr(socket.socket, "connect", refuse_network)

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/responses"):
            response = {
                "id": "resp_synthetic", "object": "response", "created_at": 0,
                "status": "completed", "model": body["model"],
                "output": [{"type": "message", "id": "msg_synthetic", "role": "assistant",
                            "status": "completed", "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "synthetic vision ok",
                                         "annotations": []}]}],
            }
            if body.get("stream"):
                events = [
                    {"type": "response.output_item.done", "output_index": 0,
                     "item": response["output"][0]},
                    {"type": "response.completed", "response": response},
                ]
                data = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                      content=data + "data: [DONE]\n\n")
            return httpx.Response(200, json=response)
        assert request.url.host == "openrouter.ai"
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={
            "id": "chatcmpl_synthetic", "object": "chat.completion", "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "synthetic vision ok"}}],
        })

    def http_kwargs(_url, *, async_mode=False):
        cls = httpx.AsyncClient if async_mode else httpx.Client
        return {"http_client": cls(transport=httpx.MockTransport(respond))}

    sync_constructor, async_constructor = openai.OpenAI, openai.AsyncOpenAI
    sync_init, async_init = sync_constructor.__init__, async_constructor.__init__

    def observe_init(init, mode, client, *args, **kwargs):
        built.append((mode, str(kwargs.get("base_url")), kwargs.get("api_key")))
        init(client, *args, **kwargs)
        clients.append(client)

    def observe_sync_init(client, *args, **kwargs):
        observe_init(sync_init, "sync", client, *args, **kwargs)

    def observe_async_init(client, *args, **kwargs):
        observe_init(async_init, "async", client, *args, **kwargs)

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", http_kwargs)
    # Keep class identity intact for auxiliary_wire's isinstance checks and SDK internals.
    monkeypatch.setattr(sync_constructor, "__init__", observe_sync_init)
    monkeypatch.setattr(async_constructor, "__init__", observe_async_init)
    try:
        yield SimpleNamespace(built=built, requests=requests)
    finally:
        aux.shutdown_cached_clients()
        aux.clear_runtime_main()
        for client in clients:
            if isinstance(client, async_constructor):
                asyncio.run(client.close())
            else:
                client.close()
        assert network_attempts == []


def seed_codex(home, cooled_model, *, singleton=False, base_url=POOL_URL):
    token = "synthetic-codex-pool-token"
    cooldowns = {cooled_model: time.time() + 86400}
    row = {
        "id": "codex-row", "label": "synthetic", "auth_type": "oauth", "priority": 0,
        "source": "device_code" if singleton else "manual",
        "access_token": token, "refresh_token": "synthetic-refresh",
        "expires_at_ms": 4_000_000_000_000, "base_url": base_url,
        "model_cooldowns": cooldowns,
    }
    providers = {}
    if singleton:
        providers["openai-codex"] = {"tokens": {
            "access_token": token, "refresh_token": "synthetic-refresh"}, "auth_mode": "chatgpt"}
    (home / "auth.json").write_text(json.dumps({
        "version": 1, "providers": providers, "credential_pool": {"openai-codex": [row]},
    }), encoding="utf-8")
    return token, cooldowns


def assert_cooldown_preserved(home, cooldowns):
    from agent.credential_pool import load_pool

    row = next(row for row in load_pool("openai-codex").entries() if row.id == "codex-row")
    assert row.model_cooldowns == cooldowns
    saved = json.loads((home / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert next(row for row in saved if row["id"] == "codex-row")["model_cooldowns"] == cooldowns


def codex_client(entrypoint, model):
    from agent import auxiliary_client as aux

    if entrypoint == "build":
        return aux._build_codex_client(model)
    return aux.resolve_provider_client("openai-codex", model=model,
                                       raw_codex=entrypoint == "raw", async_mode=entrypoint == "async")


def exercise_codex(client, model, entrypoint):
    from agent.codex_responses_adapter import _wire_model_identity
    from hermes_cli.model_normalize import normalize_model_for_provider

    # Raw Responses callers own the same wire conversion as the main Codex transport.
    wire_model = _wire_model_identity(normalize_model_for_provider(model, "openai-codex"))
    if entrypoint == "build":
        # Supply the provider-normalized request model that the ordinary router returns.
        model = normalize_model_for_provider(model, "openai-codex")
    if entrypoint == "raw":
        response = client.responses.create(model=wire_model, input=MESSAGES)
        assert response.output[0].content[0].text == "synthetic vision ok"
    elif entrypoint == "async":
        response = asyncio.run(client.chat.completions.create(model=model, messages=MESSAGES))
        assert response.choices[0].message.content == "synthetic vision ok"
    else:
        response = client.chat.completions.create(model=model, messages=MESSAGES)
        assert response.choices[0].message.content == "synthetic vision ok"


@pytest.mark.parametrize("entrypoint", ["build", "ordinary", "raw", "async"])
@pytest.mark.parametrize("requested", ALIASES)
def test_unrelated_cooldown_allows_normalized_target(synthetic_profile, trapped_sdk, entrypoint, requested):
    from agent.codex_responses_adapter import _wire_model_identity
    from hermes_cli.model_normalize import normalize_model_for_provider

    token, cooldowns = seed_codex(synthetic_profile, "gpt-5.5")
    expected = _wire_model_identity(normalize_model_for_provider(requested, "openai-codex"))
    assert expected == TARGET
    try:
        client, model = codex_client(entrypoint, requested)
        assert client is not None, "An unrelated model cooldown must not hide the pool credential"
        assert _wire_model_identity(normalize_model_for_provider(model, "openai-codex")) == expected
        exercise_codex(client, model, entrypoint)
        request, = trapped_sdk.requests
        assert json.loads(request.content)["model"] == expected
        assert request.url.host == "codex-pool.example.test"
        assert request.headers["authorization"] == f"Bearer {token}"
    finally:
        assert_cooldown_preserved(synthetic_profile, cooldowns)


@pytest.mark.parametrize("entrypoint", ["build", "ordinary", "raw", "async"])
@pytest.mark.parametrize("requested", ALIASES)
def test_target_cooldown_refuses_alias_and_singleton_bypass(
    synthetic_profile, trapped_sdk, entrypoint, requested,
):
    from agent import auxiliary_client as aux

    token, cooldowns = seed_codex(synthetic_profile, TARGET, singleton=True, base_url=CODEX_URL)
    assert aux._read_codex_singleton_token() == token
    try:
        assert codex_client(entrypoint, requested) == (None, None)
        assert trapped_sdk.built == []
        assert trapped_sdk.requests == []
    finally:
        assert_cooldown_preserved(synthetic_profile, cooldowns)


def test_unscoped_codex_lookup_retains_legacy_singleton_fallback(synthetic_profile, trapped_sdk):
    from agent import auxiliary_client as aux

    token, cooldowns = seed_codex(synthetic_profile, TARGET, singleton=True, base_url=CODEX_URL)
    try:
        assert aux._resolve_codex_credential_and_base() == (token, CODEX_URL)
        assert trapped_sdk.built == []
        assert trapped_sdk.requests == []
    finally:
        assert_cooldown_preserved(synthetic_profile, cooldowns)


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_explicit_vision_missing_auth_never_constructs_undeclared_openrouter(
    synthetic_profile, trapped_sdk, async_mode,
):
    from agent import auxiliary_client as aux

    try:
        with pytest.raises(aux.AuxiliaryClientUnavailable):
            if async_mode:
                asyncio.run(aux.async_call_llm(task="vision", messages=MESSAGES))
            else:
                aux.call_llm(task="vision", messages=MESSAGES)
    finally:
        assert trapped_sdk.built == []
        assert trapped_sdk.requests == []


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("policy", ["fallback", "auto"])
def test_authorized_vision_routes_execute_through_real_sdk(
    synthetic_profile, trapped_sdk, async_mode, policy,
):
    from agent import auxiliary_client as aux

    configure_vision(synthetic_profile, policy)
    route = {}
    kwargs = {"task": "vision", "messages": MESSAGES, "route_info": route}
    response = asyncio.run(aux.async_call_llm(**kwargs)) if async_mode else aux.call_llm(**kwargs)
    assert response.choices[0].message.content == "synthetic vision ok"
    assert route["provider"] == "openrouter"
    request, = trapped_sdk.requests
    assert request.url.host == "openrouter.ai"
    assert request.headers["authorization"] == f"Bearer {OR_KEY}"
    assert json.loads(request.content)["model"] == route["model"]
    if policy == "fallback":
        assert route["model"] == OR_MODEL
    assert all(key == OR_KEY for _, _, key in trapped_sdk.built)


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_configured_codex_vision_uses_pool_despite_other_model_cooldown(
    synthetic_profile, trapped_sdk, async_mode,
):
    from agent import auxiliary_client as aux

    token, cooldowns = seed_codex(synthetic_profile, "gpt-5.5")
    route = {}
    kwargs = {"task": "vision", "messages": MESSAGES, "route_info": route}
    try:
        response = asyncio.run(aux.async_call_llm(**kwargs)) if async_mode else aux.call_llm(**kwargs)
        assert response.choices[0].message.content == "synthetic vision ok"
        assert route == {"provider": "openai-codex", "model": TARGET}
        request, = trapped_sdk.requests
        assert request.url.host == "codex-pool.example.test"
        assert request.headers["authorization"] == f"Bearer {token}"
        assert all(key == token for _, _, key in trapped_sdk.built)
    finally:
        assert_cooldown_preserved(synthetic_profile, cooldowns)


@pytest.mark.parametrize("raw", [False, True])
def test_profile_override_keeps_pool_token_bound_to_profile_endpoint(synthetic_profile, trapped_sdk, raw):
    from agent.secret_scope import load_env_file, reset_secret_scope, set_secret_scope
    from agent import auxiliary_client as aux

    token, _ = seed_codex(synthetic_profile, "gpt-5.5")
    override = "https://profile-codex.example.test/backend-api/codex"
    (synthetic_profile / ".env").write_text(f"HERMES_CODEX_BASE_URL={override}\n", encoding="utf-8")
    scope = set_secret_scope(load_env_file(synthetic_profile / ".env"), profile_home=str(synthetic_profile))
    try:
        client, model = aux.resolve_provider_client("openai-codex", model=TARGET, raw_codex=raw)
        assert client is not None
        exercise_codex(client, model, "raw" if raw else "ordinary")
        request, = trapped_sdk.requests
        assert str(request.url).startswith(override + "/")
        assert request.headers["authorization"] == f"Bearer {token}"
    finally:
        reset_secret_scope(scope)


@pytest.mark.parametrize("credential_source", ["pool", "singleton"])
def test_codex_token_ignores_unrelated_main_model_endpoint(
    synthetic_profile, trapped_sdk, credential_source,
):
    from agent import auxiliary_client as aux

    config = {"model": {"provider": "custom", "default": "other-model",
                         "base_url": "https://unrelated.example.test/v1"}}
    (synthetic_profile / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if credential_source == "pool":
        token, _ = seed_codex(synthetic_profile, "gpt-5.5")
        expected_host = "codex-pool.example.test"
    else:
        token = "synthetic-singleton-token"
        expected_host = "chatgpt.com"
        (synthetic_profile / "auth.json").write_text(json.dumps({
            "version": 1, "providers": {"openai-codex": {
                "auth_mode": "chatgpt", "tokens": {"access_token": token,
                                                    "refresh_token": "synthetic-refresh"}}},
        }))
    client, model = aux.resolve_provider_client("openai-codex", model=TARGET, raw_codex=True)
    assert client is not None
    exercise_codex(client, model, "raw")
    request, = trapped_sdk.requests
    assert request.url.host == expected_host
    assert request.headers["authorization"] == f"Bearer {token}"
