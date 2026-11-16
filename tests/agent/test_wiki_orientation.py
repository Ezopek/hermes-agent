"""Configured local orientation follows profile ownership and session boundaries."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt_parts


def _agent(**overrides):
    values = dict(
        load_soul_identity=False, skip_context_files=True, valid_tool_names=[],
        _task_completion_guidance=False, _tool_use_enforcement=False,
        _environment_probe=False, _bot_mode_protocol=False, _kanban_worker_guidance="",
        _memory_store=None, _memory_manager=None, model="", provider="", platform="",
        pass_session_id=False, session_id="", _emit_status=lambda *_a, **_kw: None,
        _transition_context_engine_session=lambda **_kw: None,
    )
    return SimpleNamespace(**(values | overrides))


def _context(agent):
    with (
        patch("agent.prompt_builder.load_soul_md", return_value=""),
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompt_builder.build_context_files_prompt", return_value=""),
    ):
        parts = build_system_prompt_parts(agent, system_message="CALLER")
    assert "BOOTSTRAP" not in parts["stable"] + parts["volatile"]
    return parts["context"]


@pytest.mark.parametrize("owner", ["ambient", "session_db", "bound"])
def test_profile_orientation_is_frozen_until_session_reset(tmp_path, monkeypatch, owner):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from run_agent import AIAgent

    profile, ambient = tmp_path / "profile", tmp_path / "ambient"
    profile.mkdir()
    ambient.mkdir()
    (ambient / "config.yaml").write_text("wiki:\n  orientation_file: missing.md\n")
    config = profile / "config.yaml"
    config.write_text("wiki:\n  orientation_file: bootstrap.md\n")
    policy = profile / "bootstrap.md"
    policy.write_text("\ufeffFIRST-BOOTSTRAP", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile if owner == "ambient" else ambient))
    db_home = profile if owner == "session_db" else ambient
    agent = _agent(**({} if owner == "ambient" else {"_session_db": SimpleNamespace(db_path=str(db_home / "state.db"))}))
    token = set_hermes_home_override(profile if owner == "bound" else None)
    try:
        before = get_hermes_home()
        first = _context(agent)
        assert first == "CALLER\n\nFIRST-BOOTSTRAP"
        assert get_hermes_home() == before
        policy.write_text("SECOND-BOOTSTRAP", encoding="utf-8")
        assert _context(agent) == first
        AIAgent.reset_session_state(agent)
        assert _context(agent) == "CALLER\n\nSECOND-BOOTSTRAP"
        other = tmp_path / "other.md"
        other.write_text("THIRD-BOOTSTRAP", encoding="utf-8")
        config.write_text(f"wiki:\n  orientation_file: {other}\n")
        assert _context(agent) == "CALLER\n\nSECOND-BOOTSTRAP"
        AIAgent.reset_session_state(agent)
        assert _context(agent) == "CALLER\n\nTHIRD-BOOTSTRAP"
        assert get_hermes_home() == before
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("failure", ["missing", "empty", "unicode", "directory", "type", "blank", "section"])
def test_orientation_error_is_visible_and_frozen_until_reset(tmp_path, monkeypatch, failure):
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    policy = tmp_path / "bootstrap.md"
    if failure == "empty":
        policy.write_text(" \n\t")
    elif failure == "unicode":
        policy.write_bytes(b"\xff")
    elif failure == "directory":
        policy.mkdir()
    value = {"type": "[]", "blank": "''"}.get(failure, "bootstrap.md")
    config = "wiki: []\n" if failure == "section" else f"wiki:\n  orientation_file: {value}\n"
    (tmp_path / "config.yaml").write_text(config)
    statuses = []
    agent = _agent(_emit_status=statuses.append)
    broken = _context(agent)
    assert "WIKI_ORIENTATION_ERROR" in broken
    assert "check wiki.orientation_file" in broken
    assert "No such file or directory" not in broken
    assert len(statuses) == 1
    assert str(tmp_path) not in statuses[0]
    assert _context(agent) == broken and len(statuses) == 1
    AIAgent.reset_session_state(agent)
    assert _context(agent) == broken and len(statuses) == 2
    if policy.is_dir():
        policy.rmdir()
    policy.write_text("REPAIRED-BOOTSTRAP", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("wiki:\n  orientation_file: bootstrap.md\n")
    assert _context(agent) == broken
    AIAgent.reset_session_state(agent)
    assert _context(agent) == "CALLER\n\nREPAIRED-BOOTSTRAP"
    assert len(statuses) == 2


@pytest.mark.parametrize("config", ["{}", "wiki: null", "wiki: {orientation_file: null}"])
def test_unconfigured_orientation_adds_nothing(tmp_path, monkeypatch, config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(config)
    assert _context(_agent()) == "CALLER"


def test_status_failure_does_not_abort_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("wiki: {orientation_file: missing.md}")

    def broken_status(_message):
        raise RuntimeError("status unavailable")

    assert "WIKI_ORIENTATION_ERROR" in _context(_agent(_emit_status=broken_status))
