"""Text-response stop gates for the conversation turn loop.

When the model stops with a text answer, four gates may instead append the answer as an
interim row plus a user-role nudge and continue the turn: late language-server verdicts on this
turn's edits (a durable row, see ``_late_lsp_verdicts``), verify-on-stop (#65919),
the ``pre_verify`` plugin hook after code edits, the generic ``pre_stop`` plugin hook (any task, no
edits needed; it may also end the turn as unresolved), and the kanban worker terminal-tool guard.
Each keeps the candidate answer as a budget-exhaustion fallback
(``pending_verification_response``) and clears ``final_response`` so the finalizer can tell
this gate from error exits (#61631). Nothing here imports ``agent.conversation_loop`` at
module level (cycle).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.message_metadata import append_message

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class StopGateVerdict:
    """``continue_turn`` True → a nudge was appended; re-enter the turn loop with
    ``final_response=None`` and the pending-verification fields updated."""

    continue_turn: bool
    final_response: Any
    pending_verification_response: Any
    pending_verification_response_previewed: Any


def _verify_on_stop_nudge(agent) -> Optional[str]:
    try:
        from agent.verification_stop import (
            build_verify_on_stop_nudge, verify_on_stop_enabled
        )

        if verify_on_stop_enabled():
            return build_verify_on_stop_nudge(
                session_id=getattr(agent, "session_id", None),
                changed_paths=getattr(agent, "_turn_file_mutation_paths", set()),
                attempts=getattr(agent, "_verification_stop_nudges", 0),
            )
    except Exception:
        logger.debug("verification stop-loop check failed", exc_info=True)
    return None


def _pre_verify_nudge(agent, final_response, attempt: int) -> Optional[str]:
    """After code edits a registered ``pre_verify`` hook may keep the agent going one
    more turn; no default continuation cost."""
    _edited = sorted(getattr(agent, "_turn_file_mutation_paths", set()) or [])
    try:
        from agent.verify_hooks import max_verify_nudges
        from hermes_cli.lifecycle import has_hook
        from hermes_cli.plugins import get_pre_verify_continue_message

        if _edited and has_hook("pre_verify") and attempt < max_verify_nudges():
            # Posture is fixed for the session — resolve once + cache.
            coding = getattr(agent, "_resolved_is_coding", None)
            if coding is None:
                from agent.coding_context import is_coding_context
                coding = bool(is_coding_context(platform=getattr(agent, "platform", "") or ""))
                agent._resolved_is_coding = coding
            return get_pre_verify_continue_message(
                session_id=getattr(agent, "session_id", None) or "",
                platform=getattr(agent, "platform", "") or "",
                model=getattr(agent, "model", "") or "", coding=coding, attempt=attempt,
                final_response=final_response, changed_paths=_edited,
            )
    except Exception:
        logger.debug("pre_verify hook check failed", exc_info=True)
    return None


_PRE_STOP_NOTICE_MAX_CHARS = 1500


def _pre_stop_turn_state(agent) -> dict:
    """This turn's ``pre_stop`` bookkeeping, replaced when the turn id changes."""
    turn_id = getattr(agent, "_current_turn_id", "") or ""
    state = getattr(agent, "_pre_stop_turn", None)
    if not isinstance(state, dict) or state.get("turn_id") != turn_id:
        state = {"turn_id": turn_id, "nudges": 0, "unresolved": None}
        agent._pre_stop_turn = state
    return state


def pre_stop_unresolved_notice(agent) -> Optional[str]:
    """Text to publish instead of the candidate when the latest ``pre_stop`` evaluation of this turn
    did not pass (a rejected candidate is never an accepted answer), else ``None``."""
    state = getattr(agent, "_pre_stop_turn", None)
    if isinstance(state, dict) and state.get("turn_id") == (getattr(agent, "_current_turn_id", "") or ""):
        message = state.get("unresolved")
        if isinstance(message, str) and message:
            return ("Unresolved: this turn ended without satisfying its completion check. "
                    + message[:_PRE_STOP_NOTICE_MAX_CHARS])
    return None


def _pre_stop_verdict(agent, final_response) -> tuple[str, str]:
    """``("pass" | "continue" | "unresolved", message)`` from the generic ``pre_stop`` hook. Unlike
    ``pre_verify`` it needs no edits and does not fail open: a hook that raised or timed out, or a
    continuation that hit its cap, is ``unresolved``. An interrupt wins, and with no registered
    hook this is a no-op."""
    if getattr(agent, "_interrupt_requested", False) is True:
        return "pass", ""
    try:
        from hermes_cli.lifecycle import has_hook

        registered = has_hook("pre_stop")
        if not registered:  # the safe-worker policy hides a registered check from has_hook: do not read that as "none"
            from hermes_cli.plugins_dispatch import pre_stop_suppressed_by_policy

            registered = pre_stop_suppressed_by_policy()
    except Exception as exc:
        logger.warning("pre_stop registration check failed (%s)", type(exc).__name__, exc_info=True)
        return "unresolved", "the pre_stop registry could not be read, so the answer was not confirmed"
    if not registered:
        return "pass", ""
    state = _pre_stop_turn_state(agent)
    try:
        from agent.verify_hooks import max_verify_nudges
        from hermes_cli.plugins_dispatch import get_pre_stop_directive

        cap = max_verify_nudges()
        action, message = get_pre_stop_directive(
            session_id=getattr(agent, "session_id", None) or "", platform=getattr(agent, "platform", "") or "",
            model=getattr(agent, "model", "") or "", turn_id=state["turn_id"],
            task_id=getattr(agent, "_current_task_id", "") or "", attempt=state["nudges"], max_attempts=cap,
            final_response=final_response if isinstance(final_response, str) else "",
        )
    except Exception as exc:
        logger.warning("pre_stop gate failed (%s)", type(exc).__name__, exc_info=True)
        return "unresolved", "the pre_stop check could not run, so the answer was not confirmed"
    if action == "continue" and state["nudges"] >= cap:
        return "unresolved", message
    return action, message


def _kanban_stop_nudge(agent, messages) -> Optional[str]:
    """Workers must end with a terminal board tool (kanban_complete / kanban_block /
    kanban_request_review / kanban_request_changes); a narrated stop is recorded
    as protocol_violation, so nudge once or twice first."""
    try:
        from agent.kanban_stop import build_kanban_stop_nudge

        return build_kanban_stop_nudge(
            messages=messages, attempts=getattr(agent, "_kanban_stop_nudges", 0)
        )
    except Exception:
        logger.debug("kanban stop-loop check failed", exc_info=True)
        return None


_MAX_LSP_TURN_END_NOTES = 3


def _late_lsp_verdicts(agent) -> Optional[str]:
    """Language-server verdicts on this turn's edits that no tool result carried yet (the edit was the
    last tool call; a one-shot run has no next turn to carry them): waits briefly for checks still
    running (``agent.lsp.deferred.settle_for_agent``) and returns them as a message for the model."""
    issued = getattr(agent, "_lsp_turn_end_notes", 0)
    if not isinstance(issued, int) or issued >= _MAX_LSP_TURN_END_NOTES:
        return None
    try:
        from agent.lsp.deferred import settle_for_agent

        notes = settle_for_agent(agent)
    except Exception:
        logger.debug("late LSP verdict check failed", exc_info=True)
        return None
    if not notes:
        return None
    body = "\n\n".join(text for _path, text in notes)
    return ("[System note: the language server reported problems in files you edited this turn, after "
            "those tool results had returned. Fix them if they are real, then give your final answer.]\n\n"
            + body)


def _append_interim_answer(agent, final_msg, messages, conversation_history, flush_fail_msg: str) -> None:
    """Real content: persist and emit as interim so the user sees the attempted answer;
    only the nudge is flagged synthetic (#65919). A held turn's answer (typed hidden) is not shown
    and flushes with the turn instead, so a budget fallback can still publish it as the rewrite."""
    agent._emit_interim_assistant_message(final_msg)
    append_message(messages, final_msg)
    if final_msg.get("display_kind") == "hidden":
        return
    try:
        agent._flush_messages_to_session_db(messages, conversation_history)
    except Exception:
        logger.debug(flush_fail_msg, exc_info=True)


def apply_stop_gates(
    agent: Any, final_msg: dict[str, Any], *, final_response: Any, messages: list[dict[str, Any]],
    conversation_history: Any, pending_verification_response: Any,
    pending_verification_response_previewed: Any,
) -> StopGateVerdict:
    """Run verify-on-stop → pre_verify hook → pre_stop hook → kanban stop guard, in that order. Nudges
    are user-role rows appended only after the assistant answer row, so role alternation
    holds. Hook lookups are imported lazily from their origin modules (tests patch them
    there)."""

    def _continue(nudge: str, flag: str) -> StopGateVerdict:
        """Append the synthetic nudge row and hand the turn back to the loop."""
        append_message(messages, {"role": "user", "content": nudge, flag: True})
        agent._session_messages = messages
        # Keep the answer only as a budget-exhaustion fallback; clear ``final_response`` so
        # the finalizer can tell this gate from error exits. Mark previewed only if the
        # candidate is reused (#61631).
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            # Prefix semantics on purpose: the streamed prefix IS on screen here. The gateway
            # interim verdict uses _interim_content_fully_streamed instead (#88954).
            pending_verification_response_previewed=agent._interim_content_was_streamed(
                final_response or ""
            ),
        )

    _lsp_note = _late_lsp_verdicts(agent)
    if _lsp_note:
        agent._lsp_turn_end_notes = getattr(agent, "_lsp_turn_end_notes", 0) + 1  # int: checked above
        final_msg["finish_reason"] = "verification_required"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "late-LSP interim flush failed"
        )
        # A durable row (no scaffolding flag): it is never stripped, so no sent byte changes later.
        append_message(messages, {"role": "user", "content": _lsp_note})
        agent._session_messages = messages
        logger.debug("late LSP verdicts delivered before the final answer (%d)", agent._lsp_turn_end_notes)
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            pending_verification_response_previewed=agent._interim_content_was_streamed(final_response or ""),
        )

    _verify_nudge = _verify_on_stop_nudge(agent)
    if _verify_nudge:
        agent._verification_stop_nudges = getattr(agent, "_verification_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "verification_required"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "verify-on-stop interim flush failed"
        )
        verdict = _continue(_verify_nudge, "_verification_stop_synthetic")
        # Internal nudge: stay silent on the terminal, debug-log only.
        logger.debug("verification stop-loop nudge issued (attempt %d)", agent._verification_stop_nudges)
        return verdict

    _attempt = getattr(agent, "_pre_verify_nudges", 0)
    _verify_nudge2 = _pre_verify_nudge(agent, final_response, _attempt)
    if _verify_nudge2:
        agent._pre_verify_nudges = _attempt + 1
        final_msg["finish_reason"] = "verify_hook_continue"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "pre_verify interim flush failed"
        )
        verdict = _continue(_verify_nudge2, "_pre_verify_synthetic")
        logger.debug("pre_verify nudge issued (attempt %d)", agent._pre_verify_nudges)
        return verdict

    _stop_action, _stop_message = _pre_stop_verdict(agent, final_response)
    if _stop_action == "pass":
        _state = getattr(agent, "_pre_stop_turn", None)
        if isinstance(_state, dict):
            _state["unresolved"] = None
    elif _stop_action == "continue":
        _state = _pre_stop_turn_state(agent)
        _state["nudges"] += 1
        _state["unresolved"] = _stop_message  # published instead of the candidate if the turn ends here
        final_msg["finish_reason"] = "verify_hook_continue"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "pre_stop interim flush failed"
        )
        # Same synthetic flag as pre_verify: its strip/compression/alternation handling is the contract.
        verdict = _continue(_stop_message, "_pre_verify_synthetic")
        logger.debug("pre_stop nudge issued (attempt %d)", _state["nudges"])
        return verdict
    else:
        _pre_stop_turn_state(agent)["unresolved"] = _stop_message  # the kanban guard below may still nudge

    _kanban_nudge = _kanban_stop_nudge(agent, messages)
    if _kanban_nudge:
        agent._kanban_stop_nudges = getattr(agent, "_kanban_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "kanban_terminal_required"
        final_msg["_kanban_stop_synthetic"] = True
        append_message(messages, final_msg)
        verdict = _continue(_kanban_nudge, "_kanban_stop_synthetic")
        logger.info(
            "kanban stop-loop nudge issued (attempt %d) task=%s",
            agent._kanban_stop_nudges,
            os.environ.get("HERMES_KANBAN_TASK", ""),
        )
        agent._emit_diagnostic_status(
            "⚠️ Kanban worker tried to exit without a terminal board call "
            "(kanban_complete/kanban_request_review/kanban_block) — nudging to finish"
        )
        return verdict
    if _stop_action == "unresolved":
        # The notice is generated, safe text: it replaces the candidate (and its reasoning sidecar) as
        # ordinary content, and the turn is reported not completed.
        final_msg.pop("api_content", None)
        final_response = pre_stop_unresolved_notice(agent)
    return StopGateVerdict(
        continue_turn=False, final_response=final_response,
        pending_verification_response=pending_verification_response,
        pending_verification_response_previewed=pending_verification_response_previewed,
    )
