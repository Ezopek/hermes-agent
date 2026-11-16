"""Optional local bootstrap, pinned for the lifetime of an agent session."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WIKI_ORIENTATION_ERROR_STATUS = (
    "Wiki orientation failed; this session runs without Wiki context. "
    "Check wiki.orientation_file and start a new session."
)


def build_wiki_orientation_block() -> str:
    """Read configured UTF-8 policy locally; unset means no bootstrap."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home

    path: Path | str = "wiki.orientation_file"
    try:
        wiki = load_config_readonly().get("wiki")
        if wiki is None:
            return ""
        if not isinstance(wiki, dict):
            raise ValueError("wiki must be a mapping")
        value = wiki.get("orientation_file")
        if value is None:
            return ""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("wiki.orientation_file must be a non-empty path")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = get_hermes_home() / path
        content = path.read_text(encoding="utf-8-sig")
        if not content.strip():
            raise ValueError("empty orientation file")
        return content
    except (OSError, ValueError, RuntimeError) as exc:
        return (
            "## Wiki Orientation — WIKI_ORIENTATION_ERROR\n\n"
            f"Wiki orientation could not be loaded: {path} ({type(exc).__name__}); "
            "check wiki.orientation_file.\n\n"
            "Alert the operator before relying on Wiki-derived context. "
            "Treat the Wiki as unavailable for this session. "
            "Fix wiki.orientation_file and start a new session."
        )


def agent_wiki_orientation_block(agent: Any) -> str:
    """Read once under the owning profile, including when context files are skipped."""
    block = getattr(agent, "_wiki_orientation_block", None)
    if not isinstance(block, str):
        from agent.system_prompt import _agent_home
        from hermes_constants import (
            get_hermes_home, reset_hermes_home_override, set_hermes_home_override,
        )

        token = set_hermes_home_override(_agent_home(agent) or get_hermes_home())
        try:
            block = build_wiki_orientation_block()
        finally:
            reset_hermes_home_override(token)
        agent._wiki_orientation_block = block
    if "WIKI_ORIENTATION_ERROR" in block and not getattr(agent, "_wiki_orientation_status_emitted", False):
        emit = getattr(agent, "_emit_status", None)
        if callable(emit):
            agent._wiki_orientation_status_emitted = True
            try:
                emit(WIKI_ORIENTATION_ERROR_STATUS)
            except Exception:
                logger.debug("Could not emit Wiki orientation status", exc_info=True)
    return block


def reset_wiki_orientation(agent: Any) -> None:
    """A reused agent reads again only when its session changes."""
    agent._wiki_orientation_block = None
    agent._wiki_orientation_status_emitted = False
