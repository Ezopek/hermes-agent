"""``pre_stop`` registration and directive aggregation through the real ``PluginManager``."""

import logging
import threading

import pytest

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import VALID_HOOKS, PluginContext, PluginManager, PluginManifest
from hermes_cli.plugins_dispatch import get_pre_stop_directive


@pytest.fixture
def manager(monkeypatch):
    mgr = PluginManager()
    mgr._discovered = True
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return mgr


def _directive(**kwargs):
    base = dict(session_id="s", platform="cli", model="m", turn_id="t", task_id="k",
                attempt=0, max_attempts=3, final_response="draft")
    return get_pre_stop_directive(**{**base, **kwargs})


def test_pre_stop_is_a_valid_hook_and_registers_without_warning(caplog):
    assert "pre_stop" in VALID_HOOKS
    mgr = PluginManager()
    ctx = PluginContext(PluginManifest(name="probe", source="user"), mgr)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        ctx.register_hook("pre_stop", lambda **_kw: None)
    assert mgr.has_hook("pre_stop")
    assert not [r for r in caplog.records if "unknown" in r.getMessage().lower()]


def test_no_callback_passes(manager):
    assert _directive() == ("pass", "")


def test_non_directive_returns_pass(manager):
    manager._hooks["pre_stop"] = [lambda **_kw: "text", lambda **_kw: {"action": "other"}, lambda **_kw: {}]
    assert _directive() == ("pass", "")


def test_continue_and_claude_stop_block_aliases(manager):
    manager._hooks["pre_stop"] = [lambda **_kw: {"action": "continue", "message": "  keep going  "}]
    assert _directive() == ("continue", "keep going")
    manager._hooks["pre_stop"] = [lambda **_kw: {"decision": "block", "reason": "because"}]
    assert _directive() == ("continue", "because")


def test_empty_message_is_not_a_directive(manager):
    manager._hooks["pre_stop"] = [lambda **_kw: {"action": "continue", "message": "  "}]
    assert _directive() == ("pass", "")


def test_unresolved_wins_over_continue(manager):
    manager._hooks["pre_stop"] = [
        lambda **_kw: {"action": "continue", "message": "go on"},
        lambda **_kw: {"action": "unresolved", "message": "cannot be resolved"},
    ]
    assert _directive() == ("unresolved", "cannot be resolved")


def test_scope_fields_reach_the_callback(manager):
    seen = {}
    manager._hooks["pre_stop"] = [lambda **kw: seen.update(kw)]
    _directive(attempt=2, final_response="the draft")
    for key in ("session_id", "platform", "model", "turn_id", "task_id", "attempt", "max_attempts", "final_response"):
        assert key in seen
    assert seen["attempt"] == 2 and seen["final_response"] == "the draft"


def test_raising_callback_is_unresolved_and_does_not_echo_the_error(manager):
    def exploding(**_kw):
        raise ValueError("synthetic-secret-marker-9921")

    manager._hooks["pre_stop"] = [exploding]
    action, message = _directive()
    assert action == "unresolved"
    assert "exploding" in message
    assert "synthetic-secret-marker-9921" not in message


def test_timed_out_callback_is_unresolved(manager, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.1)
    manager._hooks["pre_stop"] = [lambda **_kw: release.wait(timeout=10.0)]
    try:
        action, _message = _directive()
    finally:
        release.set()
    assert action == "unresolved"


def test_other_hooks_keep_the_fail_open_contract(manager):
    """A raising ``pre_verify`` callback is still skipped, not turned into a directive."""
    from hermes_cli.plugins import get_pre_verify_continue_message

    def exploding(**_kw):
        raise ValueError("boom")

    manager._hooks["pre_verify"] = [exploding]
    assert get_pre_verify_continue_message() is None
