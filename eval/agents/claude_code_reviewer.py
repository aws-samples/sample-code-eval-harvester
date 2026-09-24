"""A starter PR-review agent: Claude Code driven by a custom review system prompt.

This is the template for a *real* agent-under-test — the thing you compare against the harvested
gold standard, as opposed to Harbor's model-free ``nop`` (submits nothing) and ``oracle`` (submits
the reference findings). It subclasses Harbor's installed ``ClaudeCode`` agent and layers one
customization on top: a review-focused system prompt appended to every session. Everything else —
installing the CLI, driving it in the MicroVM, extracting the trajectory — is inherited unchanged.

Loaded by import path: ``harbor run -a eval.agents.claude_code_reviewer:ClaudeCodeReviewer``.

The task the agent receives is the emitted datapoint's ``instruction.md``, which already directs it
to write its findings as a JSON array to ``/logs/artifacts/findings.json`` (``{path, line,
statement, severity}`` per entry). That file — and only a file under the ``/logs/artifacts``
convention dir — is what the separate-mode verifier scores against ``tests/oracle.json``. The custom
prompt below is the one knob to tune; edit it to change *how* this agent reviews, not *what contract*
it must meet.

To add your own agent (or Kiro), copy this file, change the prompt / behaviour, and register a
matching :class:`~eval.agent.AgentUnderTest` in :mod:`eval.agent` + ``eval.run._AGENTS``.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.claude_code import ClaudeCode

#: The review guidance appended to Claude Code's system prompt for every trial — the tuning knob.
#: It steers review behaviour without touching the task ``instruction.md`` (which must stay
#: byte-identical across agents and carries the output contract), so it deliberately restates no
#: verdict rule or output path: those belong to the instruction, not the agent.
REVIEW_SYSTEM_PROMPT = (
    "You are a meticulous senior code reviewer. Read the change under review in full before "
    "judging it. Prefer a small number of high-confidence, specific defects over a long list of "
    "speculative nits: for each defect, point to the exact file and line and state the concrete "
    "failure it causes. Reserve the highest severity for defects that would break correctness, "
    "security, or data integrity if merged. Do not invent problems, and do not wave a change "
    "through without reading it."
)


# `harbor` ships no `py.typed` and is not synced during `mise run typecheck`, so mypy sees `ClaudeCode`
# as `Any` and `disallow_subclassing_any` fires. This is the third-party agent base we extend by
# design — the same reason `harvest_env`'s environment subclasses a Harbor base — so the ignore is
# load-bearing, not incidental.
class ClaudeCodeReviewer(ClaudeCode):  # type: ignore[misc]
    """Claude Code with a review-oriented system prompt appended to each session.

    The prompt is injected through Harbor's ``--append-system-prompt`` CLI flag rather than by
    editing the task instruction, so the instruction stays byte-identical to every other agent's.
    Harbor renders that flag as ``--append-system-prompt <value>`` with **no** shell quoting, so the
    (multi-word) prompt is pre-quoted here to survive as a single shell token in the ``claude``
    command line.
    """

    @staticmethod
    @override
    def name() -> str:
        return "claude-code-reviewer"

    def __init__(self, logs_dir: Path, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("append_system_prompt", shlex.quote(REVIEW_SYSTEM_PROMPT))
        super().__init__(logs_dir, *args, **kwargs)
