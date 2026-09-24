"""Pydantic models for the datapoint-quality judge: the datapoint it reads and the verdict it returns.

All models are frozen (matching the rest of the project) and fully typed. The judge reads a
:class:`Datapoint` (loaded from a built task directory or constructed directly in a test) plus the
source :class:`PrContext`, and returns a :class:`QualityVerdict`. The verdict round-trips as JSON so
G-3 can persist and re-read it alongside the run manifest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The two datapoint kinds and the two verdicts, matching ``eval_harvest.emit`` (reject⇒block, approve⇒approve).
DatapointKind = Literal["reject", "approve"]
ExpectedVerdict = Literal["block", "approve"]
Severity = Literal["low", "medium", "high"]


class OracleFindingView(BaseModel):
    """One oracle finding as the judge sees it: the reviewer's claim, its severity, and its role.

    A read-only projection of a ``tests/oracle.json`` finding — enough for the judge to decide
    whether it reflects the real review and whether a nit was promoted, without the file/line spans
    the structural ``verify`` already checks."""

    model_config = ConfigDict(frozen=True)

    statement: str
    severity: Severity
    reference_only: bool
    blocking: bool


class Datapoint(BaseModel):
    """A built datapoint as the quality judge reads it — the agent-visible surface plus the oracle.

    Loaded from a produced task directory (:class:`~eval.judge.datapoint.DatapointLoader`) or built
    directly in a test. Carries only what the judge grades: the instruction and change the evaluated
    agent would see, the oracle findings, the declared kind/verdict, and both change-risk values."""

    model_config = ConfigDict(frozen=True)

    directory_name: str
    kind: DatapointKind
    expected_verdict: ExpectedVerdict
    blocking_severity: Severity
    instruction: str
    change_patch: str
    oracle_findings: tuple[OracleFindingView, ...] = ()
    change_risk_structural: Severity | None = None
    change_risk_classified: Severity | None = None


class PrContext(BaseModel):
    """The source-PR facts the datapoint is graded against — what the human review actually was.

    Supplied to the judge (from the objective record, a captured candidate, or by hand for the
    alignment set), because the judge cannot re-fetch the PR offline: it compares the datapoint to
    this stated context rather than to a live forge call."""

    model_config = ConfigDict(frozen=True)

    repo: str
    pr_number: int
    reviewed_state: str
    review_summary: str


class CriterionVerdict(BaseModel):
    """The judge's verdict on one rubric criterion: whether it passed, a score, and the rationale."""

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    rationale: str


class QualityVerdict(BaseModel):
    """The judge's full verdict on one datapoint: a verdict per criterion, an overall roll-up, notes.

    ``overall_pass`` and ``overall_score`` are *derived* by the harness from ``per_criterion`` (all
    criteria must pass; the score is their mean), not taken from the model — so a model that miscounts
    its own summary cannot inflate the roll-up. ``notes`` carries the model's free-text commentary."""

    model_config = ConfigDict(frozen=True)

    per_criterion: tuple[CriterionVerdict, ...]
    overall_pass: bool
    overall_score: float = Field(ge=0.0, le=1.0)
    notes: str = ""


class AlignmentResult(BaseModel):
    """The recorded record that licenses trusting the judge: how well it agreed with human labels.

    Persisted to ``eval/alignment/alignment_result.json`` by the alignment run and read by the gate
    (:class:`~eval.judge.gate.AlignmentGate`). ``passed`` is ``agreement >= bar``; ``judge_version``
    ties the record to the rubric it was measured against so a stale alignment for an older rubric no
    longer licenses the judge."""

    model_config = ConfigDict(frozen=True)

    n_labeled: int = Field(ge=0)
    agreement: float = Field(ge=0.0, le=1.0)
    bar: float = Field(ge=0.0, le=1.0)
    passed: bool
    judge_version: str
    model: str
