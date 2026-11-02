"""``pre_stop`` under the private safe-worker policy.

The policy is bound once in a fresh exec before plugin imports and disables plugin hooks; that must stay untouched:
callbacks never run under it, there is no switch, and a normal safe bootstrap with no hook is a plain no-op.
Only the fault state "a pre_stop callback is registered but the policy suppresses it" must not read as a pass.
The in-process tests below SIMULATE that state with a monkeypatched policy check (an ordinary bootstrap cannot toggle it).
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager
from hermes_cli.plugins_dispatch import get_pre_stop_directive
from run_agent import AIAgent

ROOT = Path(__file__).resolve().parents[2]


def _suppressed(monkeypatch):
    monkeypatch.setattr("agent.safe_worker_policy.safe_worker_enabled", lambda: True)


def _manager(monkeypatch, *callbacks):
    mgr = PluginManager()
    mgr._discovered = True
    mgr._hooks["pre_stop"] = list(callbacks)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return mgr


def _args(**kw):
    return dict(session_id="s", platform="cli", model="m", turn_id="t", task_id="k", attempt=0, max_attempts=3,
                final_response="draft", **kw)


def test_registered_but_suppressed_is_unresolved_and_the_callback_never_runs(monkeypatch):
    calls = []
    _manager(monkeypatch, lambda **kw: calls.append(kw) or None)
    _suppressed(monkeypatch)

    action, message = get_pre_stop_directive(**_args())

    assert action == "unresolved"
    assert "policy" in message
    assert calls == []


def test_suppressed_without_any_registered_callback_is_a_plain_pass(monkeypatch):
    _manager(monkeypatch)
    _suppressed(monkeypatch)
    assert get_pre_stop_directive(**_args()) == ("pass", "")


def test_policy_off_still_runs_the_callback_normally(monkeypatch):
    calls = []
    _manager(monkeypatch, lambda **kw: calls.append(kw) or {"action": "continue", "message": "go on"})
    assert get_pre_stop_directive(**_args()) == ("continue", "go on")
    assert len(calls) == 1


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(session_id="pre-stop-safe", api_key="k", base_url="https://example.invalid/v1",
                           provider="openai-compat", model="test/model", max_iterations=6, quiet_mode=True,
                           skip_context_files=True, skip_memory=True)
    instance._cached_system_prompt = "p"
    instance._session_db = None
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_k: None
    instance._save_trajectory = lambda *_a, **_k: None
    instance._handle_max_iterations = MagicMock(return_value="summary")
    return instance


def _text(content):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], model="test/model", usage=None)


def test_real_turn_with_a_suppressed_registered_check_never_publishes_the_draft(agent, monkeypatch):
    calls = []
    _manager(monkeypatch, lambda **kw: calls.append(kw))
    _suppressed(monkeypatch)
    agent._interruptible_api_call = lambda _kw: _text("unchecked draft")

    result = agent.run_conversation("report")

    assert result["final_response"].startswith("Unresolved")
    assert "unchecked draft" not in result["final_response"]
    assert result["completed"] is False
    assert calls == []


def test_real_turn_with_policy_and_no_hook_is_unchanged(agent, monkeypatch):
    _manager(monkeypatch)
    _suppressed(monkeypatch)
    agent._interruptible_api_call = lambda _kw: _text("plain answer")

    result = agent.run_conversation("report")

    assert result["final_response"] == "plain answer"
    assert result["completed"] is True


def _fresh_safe_worker(tmp_path, body):
    home = tmp_path / "safe"
    home.mkdir()
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(home))
    script = home / "probe.py"
    script.write_text(f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + textwrap.dedent(body), encoding="utf-8")
    done = subprocess.run([sys.executable, str(script)], env=env, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_fresh_safe_bootstrap_without_a_hook_is_a_no_op_for_the_gate_and_the_directive(tmp_path):
    out = _fresh_safe_worker(tmp_path, '''
        import json
        from agent.safe_worker_policy import _bind_safe_worker_policy, safe_worker_enabled
        _bind_safe_worker_policy(safe_mode=True, ignore_user_config=True, config={})
        from types import SimpleNamespace
        from agent.turn_stop_gates import _pre_stop_verdict
        from hermes_cli.plugins_dispatch import get_pre_stop_directive
        agent = SimpleNamespace(session_id="s", platform="cli", model="m", _current_turn_id="t", _current_task_id="k")
        print(json.dumps({"safe": safe_worker_enabled(), "gate": _pre_stop_verdict(agent, "draft"),
                          "directive": get_pre_stop_directive(session_id="s", final_response="d")}))
    ''')
    assert out == {"safe": True, "gate": ["pass", ""], "directive": ["pass", ""]}
