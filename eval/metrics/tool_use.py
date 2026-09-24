"""Compute the tool-use metrics for one trial from its captured trajectory.

The metrics are the "tool accuracy" half of G-3's read: given the ordered tool calls G-1 recorded,
they say *how* the agent drove the CLI, not just *whether* it produced a datapoint. Every metric is
defined precisely here so the headline "did it use the tool well" is a number, not a vibe:

* **verb usage + order** — the ``eval-harvest`` sub-commands the agent ran, in invocation order. A
  command's verb is the token after ``eval-harvest`` (``eval-harvest survey --repo x`` → ``survey``;
  ``eval-harvest --help`` → ``--help``). Non-``eval-harvest`` tool calls (a file read, a raw ``ls``)
  are not verbs and do not appear here.
* **redundant calls** — tool calls byte-identical (same name *and* args) to an earlier call. A cache
  of what has been seen; the count is how many calls repeated something already done.
* **refusals + acting on them** — a refusal is an ``eval-harvest`` call that exited non-zero (the CLI
  uses 0 = ok, 2/3/4/5 = usage/refusal/verification/unresolved). ``refusals_acted_on`` counts the
  refusals *followed by a later* ``eval-harvest`` call — the agent tried again rather than giving up.
  ``acted_on_all_refusals`` is true when there were no refusals, or every one was followed by another
  attempt: the core-bet signal that the CLI's feedback is usable (PRD §8, straw man "100% after the
  agent acts on the CLI's feedback").
* **efficiency** — ``tool_calls_per_turn``: total tool calls over transcript turns, a coarse "did it
  loop needlessly" measure. 0.0 when the trace recorded no turns.

The exit codes come from the CLI's own convention (``src/eval_harvest``: 0 success, 2 usage, 3
refusal, 4 verification, 5 unresolved), read off each :class:`~eval.models.ToolCall`'s ``exit_code``.
"""

from __future__ import annotations

import shlex

from pydantic import BaseModel, ConfigDict

from eval.models import ToolCall, TrialTrajectory

#: The executable name the agent-under-test drives. A tool call is an ``eval-harvest`` invocation when
#: this token appears in its command line (bare or as a path component, e.g. ``/usr/bin/eval-harvest``).
_CLI_NAME = "eval-harvest"

#: The key under a tool call's ``args`` holding the shell command line (the harness records bash calls
#: as ``{"command": "<line>"}``; see the G-1 trajectory fixture).
_COMMAND_ARG = "command"


class ToolUseMetrics(BaseModel):
    """The tool-use metrics for one trial — the "tool accuracy" signal G-3's report summarizes.

    Frozen and JSON-round-tripping like the rest of the eval models, so a scored trial persists
    alongside the run it came from. Every field is defined in the module docstring; the report rolls
    these up per objective."""

    model_config = ConfigDict(frozen=True)

    total_turns: int
    total_tool_calls: int
    harvest_calls: int
    verb_sequence: tuple[str, ...]
    distinct_verbs: tuple[str, ...]
    redundant_calls: int
    refusals: int
    refusals_acted_on: int
    acted_on_all_refusals: bool
    tool_calls_per_turn: float


class ToolUseAnalyzer:
    """Computes :class:`ToolUseMetrics` from a trajectory. Holds no state (classmethods only)."""

    @classmethod
    def analyze(cls, trajectory: TrialTrajectory) -> ToolUseMetrics:
        """The tool-use metrics for ``trajectory`` — a pure function of its turns and tool calls."""
        calls = trajectory.tool_calls
        verbs = cls._verb_sequence(calls)
        refusals, refusals_acted_on = cls._refusal_counts(calls)
        return ToolUseMetrics(
            total_turns=len(trajectory.turns),
            total_tool_calls=len(calls),
            harvest_calls=len(verbs),
            verb_sequence=verbs,
            distinct_verbs=tuple(sorted(set(verbs))),
            redundant_calls=cls._redundant_calls(calls),
            refusals=refusals,
            refusals_acted_on=refusals_acted_on,
            acted_on_all_refusals=refusals == refusals_acted_on,
            tool_calls_per_turn=cls._tool_calls_per_turn(calls, trajectory.turns),
        )

    @classmethod
    def _verb_sequence(cls, calls: tuple[ToolCall, ...]) -> tuple[str, ...]:
        """The ``eval-harvest`` verbs invoked, in order — the token after ``eval-harvest`` in each call."""
        verbs = [verb for call in calls if (verb := cls._harvest_verb(call)) is not None]
        return tuple(verbs)

    @classmethod
    def _harvest_verb(cls, call: ToolCall) -> str | None:
        """The ``eval-harvest`` verb this call ran, or ``None`` when it is not an ``eval-harvest`` call.

        Splits the command line the way a shell would (``shlex``), finds the ``eval-harvest`` token
        (bare or as a path component), and returns the token after it. A bare ``eval-harvest`` with no
        following token yields ``None`` — there is no verb to record."""
        command = call.args.get(_COMMAND_ARG)
        if not isinstance(command, str):
            return None
        tokens = cls._split(command)
        for index, token in enumerate(tokens):
            if cls._is_cli_token(token) and index + 1 < len(tokens):
                return tokens[index + 1]
        return None

    @staticmethod
    def _split(command: str) -> list[str]:
        """``shlex.split`` the command, falling back to whitespace split on an unbalanced-quote line."""
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()

    @staticmethod
    def _is_cli_token(token: str) -> bool:
        """True when ``token`` names the CLI — bare ``eval-harvest`` or a path ending in it."""
        return token == _CLI_NAME or token.endswith(f"/{_CLI_NAME}")

    @staticmethod
    def _redundant_calls(calls: tuple[ToolCall, ...]) -> int:
        """Count of calls byte-identical (name + args) to an earlier one — needless repetition."""
        seen: set[str] = set()
        redundant = 0
        for call in calls:
            fingerprint = call.model_dump_json(include={"name", "args"})
            if fingerprint in seen:
                redundant += 1
            else:
                seen.add(fingerprint)
        return redundant

    @classmethod
    def _refusal_counts(cls, calls: tuple[ToolCall, ...]) -> tuple[int, int]:
        """``(refusals, refusals_acted_on)``: harvest calls that exited non-zero, and how many were
        followed by a *later* harvest call (a corrected retry rather than a giveup).

        "Acted on" is "this refusal is not the last ``eval-harvest`` call" — a later harvest call means
        the agent tried again instead of stopping at the refusal."""
        harvest_indices = [index for index, call in enumerate(calls) if cls._harvest_verb(call) is not None]
        last_harvest_index = harvest_indices[-1] if harvest_indices else -1
        refusals = [index for index in harvest_indices if cls._is_refusal(calls[index])]
        acted_on = sum(1 for index in refusals if index < last_harvest_index)
        return len(refusals), acted_on

    @staticmethod
    def _is_refusal(call: ToolCall) -> bool:
        """True when the CLI call exited non-zero — a usage error, a refusal, or a failed check."""
        return call.exit_code is not None and call.exit_code != 0

    @staticmethod
    def _tool_calls_per_turn(calls: tuple[ToolCall, ...], turns: tuple[object, ...]) -> float:
        """Tool calls per transcript turn — a coarse efficiency measure; 0.0 when no turns recorded."""
        return len(calls) / len(turns) if turns else 0.0
