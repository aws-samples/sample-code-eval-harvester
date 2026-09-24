"""The agent-under-test interface and the agents that implement it.

An agent-under-test maps to Harbor's ``-a <agent>`` selector: Harbor runs the agent; this harness
only names it and states the environment it needs. Modelling it behind a small interface is what
lets a second agent (Kiro) be added later behind its own API key (G-1 acceptance) — v1 ships Claude
Code only. :class:`RecordedAgent` is the offline stand-in the tests use: always available, needs no
credentials, and drives no live model or MicroVM.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping


class AgentUnderTest(ABC):
    """One agent Harbor can drive. Carries its Harbor selector and the environment it requires."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier recorded in every captured trajectory."""

    @property
    @abstractmethod
    def harbor_agent(self) -> str:
        """The value passed to ``harbor run -a <agent>``."""

    @abstractmethod
    def required_env(self) -> tuple[str, ...]:
        """The environment variables that must be present for a live run (credentials, the toggle)."""

    def missing_env(self, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
        """The required env vars absent (or empty) in ``environ`` — defaults to the process env."""
        source = os.environ if environ is None else environ
        return tuple(name for name in self.required_env() if not source.get(name))

    def is_available(self, environ: Mapping[str, str] | None = None) -> bool:
        """True when every required env var is present — the gate ``mise run eval`` skips on if False."""
        return not self.missing_env(environ)


class ClaudeCodeAgent(AgentUnderTest):
    """Claude Code driven on AWS Bedrock.

    ``CLAUDE_CODE_USE_BEDROCK=1`` routes Claude Code at Bedrock; the AWS region and credentials come
    from the environment; the model id is configurable via ``ANTHROPIC_MODEL`` / Harbor's ``-m`` and
    is never hardcoded (G-1 What To Build §3). Only the region and the Bedrock toggle are treated as
    hard requirements here — credentials may arrive via an access key, a profile, or an instance
    role, so their presence is left to Harbor's own preflight to enforce at run time.
    """

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def harbor_agent(self) -> str:
        return "claude-code"

    def required_env(self) -> tuple[str, ...]:
        return ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION")


class ClaudeCodeReviewerAgent(ClaudeCodeAgent):
    """The starter PR-review agent: Claude Code on Bedrock with a custom review system prompt.

    A *real* agent-under-test to compare against the harvested gold standard (unlike ``nop`` /
    ``oracle``). It reuses :class:`ClaudeCodeAgent`'s Bedrock environment gate; only its Harbor
    selector differs — it is a custom import path, so Harbor's ``AgentFactory`` loads the concrete
    :class:`eval.agents.claude_code_reviewer.ClaudeCodeReviewer` by import path (any ``-a`` value
    containing ``:`` is loaded that way — no Harbor fork). Kept harbor-free here: only the string is
    named, so the offline suite can select the agent without the ``eval`` group synced.
    """

    @property
    def name(self) -> str:
        return "claude-code-reviewer"

    @property
    def harbor_agent(self) -> str:
        return "eval.agents.claude_code_reviewer:ClaudeCodeReviewer"


class RecordedAgent(AgentUnderTest):
    """An offline stand-in for a live agent: always available, needs no credentials.

    Holds a per-instance name (its one piece of genuine state) so a test can distinguish two recorded
    agents; it drives no model and no MicroVM, so the harness wiring can be exercised without Bedrock.
    """

    def __init__(self, name: str = "recorded") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def harbor_agent(self) -> str:
        return self._name

    def required_env(self) -> tuple[str, ...]:
        return ()
