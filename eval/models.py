"""Pydantic models for the eval harness: objectives and captured trial trajectories.

These are the offline-checkable contract the later eval tickets rest on. G-2 (the datapoint-quality
judge) and G-3 (tool-use/trajectory metrics + report) both consume the :class:`TrialTrajectory`
this file defines, and every objective is validated through :class:`Objective` before a trial runs,
so a malformed record fails at load rather than mid-run.

Models are frozen (``ConfigDict(frozen=True)``) to match the immutability the rest of the project
prefers, and every field is fully typed. Pydantic is used — rather than the ``dataclass`` the CLI
uses — because ``eval/`` is exempt from the CLI's zero-dependency spine (NFR-5) and the trajectory
artifact is persisted as JSON and re-loaded by G-2/G-3, which is exactly Pydantic's round-trip.
"""

from __future__ import annotations

# ``dict[str, Any]`` is the honest shape for a tool call's arguments: they are arbitrary JSON drawn
# from whatever tool the agent invoked, so narrowing them would be a lie (mirrors cli.py's reasoning).
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The two datapoint kinds ``eval-harvest emit`` builds, and the only kinds an objective may name.
DatapointKind = Literal["reject", "approve"]


class ExpectedProperties(BaseModel):
    """What a *good* datapoint built from an objective should look like — the target G-2/G-3 grade
    against. Held as data on the objective; the harness never checks it, the eval measures agreement."""

    model_config = ConfigDict(frozen=True)

    expected_verdict: Literal["block", "approve"]
    minimum_findings: int = Field(ge=0)
    must_mention: tuple[str, ...] = ()


class Objective(BaseModel):
    """One eval objective: a public repo pinned at a commit SHA, the PR to work from, the datapoint
    kind expected, and the properties later tickets grade against.

    Public + pinned is deliberate: a public repo needs no ``gh`` credentials, and a pinned SHA means
    the history the agent sees cannot drift out from under the eval (G-1 Notes & Gotchas)."""

    model_config = ConfigDict(frozen=True)

    id: str
    repo_url: str
    commit_sha: str
    pr: int
    kind: DatapointKind
    expected_properties: ExpectedProperties


class ToolCall(BaseModel):
    """One tool invocation in a trial: the tool name, its arguments, the result text, and the exit
    code when the tool was a process (a CLI verb's 0/2/3/4/5). ``exit_code`` is ``None`` for a tool
    with no process exit (a file read, say)."""

    model_config = ConfigDict(frozen=True)

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    exit_code: int | None = None


class AgentTurn(BaseModel):
    """One turn of the agent transcript: who spoke, and the text."""

    model_config = ConfigDict(frozen=True)

    role: str
    text: str


class TrialTrajectory(BaseModel):
    """The full record of one trial, normalized from Harbor's run trace — the analysis input G-2 and
    G-3 consume. Persisted as JSON under ``eval/runs/…``; a stored artifact, not a shipped API."""

    model_config = ConfigDict(frozen=True)

    objective_id: str
    trial_index: int
    agent: str
    model: str
    turns: tuple[AgentTurn, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    produced_task_dir: str | None = None
    wall_time_sec: float = 0.0
    exit_code: int = 0


class TrialResult(BaseModel):
    """One row of a run manifest: where a trial's trajectory landed and how the process exited."""

    model_config = ConfigDict(frozen=True)

    objective_id: str
    trial_index: int
    trajectory_path: str
    exit_code: int


class RunManifest(BaseModel):
    """The index of one whole eval run: the agent, the model, the trial count, and every trial's
    result. Written to ``eval/runs/<timestamp>/run.json`` as the entry point G-2/G-3 read."""

    model_config = ConfigDict(frozen=True)

    timestamp: str
    agent: str
    model: str
    trials_per_objective: int
    objective_ids: tuple[str, ...]
    trials: tuple[TrialResult, ...]
