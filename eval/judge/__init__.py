"""The datapoint-quality LLM judge (Workstream G, G-2).

``eval-harvest verify`` answers *is this datapoint structurally valid and leak-free?* (the US-7
checks). It does **not** answer *is this a good datapoint?* — one whose oracle reflects the PR's
real review, whose change matches the reviewed state, whose verdict fits the kind, whose instruction
leaks nothing, and which did not promote a nit to a blocker. This package is the judge that grades
that quality, layered on top of ``verify`` (it never replaces it).

Two different judges live in this repo; do not conflate them:

* The **in-task verifier's judge** (``src/eval_harvest/verifier_tpl/score.py``) scores the
  *agent-under-review's* findings against the emitted oracle. It ships inside every datapoint.
* **This judge** scores whether the *datapoint the eval agent built* is itself good. It lives in
  ``eval/`` and is never shipped.

Nothing under ``src/eval_harvest/`` imports this package (S-18); ``eval/`` may talk to a model and a
network, the package may not. The trusted entry point :func:`judge` is gated on a recorded alignment
result (see :mod:`eval.alignment`) — an unaligned judge is an opinion, not a measurement.
"""

from __future__ import annotations

from eval.judge.client import LiveModelClient, ModelClient, RecordedModelClient
from eval.judge.datapoint import DatapointLoader
from eval.judge.gate import DEFAULT_ALIGNMENT_PATH, AlignmentGate, UnalignedJudgeError, judge
from eval.judge.judge import QualityJudge
from eval.judge.models import CriterionVerdict, Datapoint, OracleFindingView, PrContext, QualityVerdict
from eval.judge.rubric import JUDGE_VERSION, QUALITY_CRITERIA, QualityCriterion, render_rubric

__all__ = [
    "DEFAULT_ALIGNMENT_PATH",
    "JUDGE_VERSION",
    "QUALITY_CRITERIA",
    "AlignmentGate",
    "CriterionVerdict",
    "Datapoint",
    "DatapointLoader",
    "LiveModelClient",
    "ModelClient",
    "OracleFindingView",
    "PrContext",
    "QualityCriterion",
    "QualityJudge",
    "QualityVerdict",
    "RecordedModelClient",
    "UnalignedJudgeError",
    "judge",
    "render_rubric",
]
