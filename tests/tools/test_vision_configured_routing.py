"""Vision registry gates and execution must honor the same configured routes."""

import json

import pytest

from tests.agent.test_auxiliary_codex_vision_routing import (
    OR_KEY,
    OR_MODEL,
    configure_vision,
)

pytest_plugins = ("tests.agent.test_auxiliary_codex_vision_routing",)


@pytest.mark.parametrize("policy,available", [("explicit", False), ("fallback", True), ("auto", True)])
def test_video_and_aux_image_gates_match_configured_execution(
    synthetic_profile, trapped_sdk, tmp_path, policy, available,
):
    image = pytest.importorskip("PIL.Image")
    from tools import vision_tools
    from tools.registry import invalidate_check_fn_cache, registry

    invalidate_check_fn_cache()
    configure_vision(synthetic_profile, policy)
    local_image = tmp_path / "synthetic.png"
    image.new("RGB", (2, 2), "red").save(local_image)
    # Dispatch even when the gate refuses, so gate and execution are independently observed.
    video_gate = vision_tools.check_video_requirements()
    image_gate = vision_tools.check_vision_requirements()
    definitions = registry.get_definitions({"vision_analyze", "video_analyze"}, quiet=True)
    assert trapped_sdk.built == [], "Availability probes must not construct SDK clients"
    result = json.loads(registry.dispatch("vision_analyze", {
        "image_url": str(local_image), "question": "What color is the synthetic image?",
    }))

    assert video_gate is available
    assert image_gate is available
    assert {definition["function"]["name"] for definition in definitions} == (
        {"vision_analyze", "video_analyze"} if available else set())
    assert result["success"] is available
    if available:
        assert result["analysis"] == "synthetic vision ok"
        request, = trapped_sdk.requests
        assert request.url.host == "openrouter.ai"
        assert request.headers["authorization"] == f"Bearer {OR_KEY}"
        if policy == "fallback":
            assert json.loads(request.content)["model"] == OR_MODEL
    else:
        assert "openai-codex" in result["error"]
        assert trapped_sdk.built == []
        assert trapped_sdk.requests == []


def test_native_image_entrypoint_remains_available_without_aux_auth(
    synthetic_profile, trapped_sdk, tmp_path,
):
    image = pytest.importorskip("PIL.Image")
    from tools import vision_tools
    from tools.registry import invalidate_check_fn_cache, registry

    invalidate_check_fn_cache()
    configure_vision(synthetic_profile, native=True)
    local_image = tmp_path / "native.png"
    image.new("RGB", (2, 2), "blue").save(local_image)

    assert vision_tools.check_vision_requirements() is True
    result = registry.dispatch("vision_analyze", {
        "image_url": str(local_image), "question": "Describe this synthetic image.",
    })
    assert isinstance(result, dict) and result["_multimodal"] is True
    assert any(part.get("type") == "image_url" for part in result["content"])
    assert trapped_sdk.built == []
    assert trapped_sdk.requests == []
