"""The quality rubric: what a *good* datapoint is, as concrete criteria the judge scores.

This is the judge's contract. It is short and inspectable on purpose — the prompt is rendered from
it and every :class:`~eval.judge.models.QualityVerdict` is validated against it, so the criteria the
model is asked about and the criteria the harness enforces cannot drift apart (there is one source,
this file). The criteria operationalize PRD §6 US-1 (what makes a datapoint valid: the base precedes
the change, the instruction does not name the outcome, a defect is not a nit) — the *quality* the
structural US-7 checks in ``verify`` leave open.

Bumping :data:`JUDGE_VERSION` when the rubric or prompt changes is what forces a re-run of alignment
(the recorded :class:`~eval.judge.models.AlignmentResult` records the version it was measured at, so
a stale alignment for an older rubric no longer licenses the judge).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

#: The rubric/prompt version. Bump on any change to the criteria or the rendered prompt so a recorded
#: alignment result measured against an older rubric stops licensing the judge (see eval.alignment).
JUDGE_VERSION = "quality-judge-v1"


class QualityCriterion(BaseModel):
    """One rubric criterion the judge scores: a stable name, the question, and why it matters."""

    model_config = ConfigDict(frozen=True)

    name: str
    question: str
    why: str


#: The five criteria that define a "good datapoint". Each maps to a US-1/US-7 concern the structural
#: ``verify`` cannot judge because it needs a reading of the PR's review, not just the files on disk.
QUALITY_CRITERIA: tuple[QualityCriterion, ...] = (
    QualityCriterion(
        name="oracle-reflects-review",
        question=(
            "Do the datapoint's oracle findings reflect the defects the PR's reviewers actually raised — "
            "not invented findings, and not dropping the substantive ones the review turned on?"
        ),
        why="An oracle that does not match the real review measures the wrong thing while looking valid.",
    ),
    QualityCriterion(
        name="change-matches-reviewed-state",
        question=(
            "Does the change under review correspond to the PR state the datapoint's kind names — the "
            "change-requested (earliest reviewed) state for a reject, the approved final state for an approve?"
        ),
        why="A reject built from the fixed state, or an approve from the pre-fix state, inverts the label.",
    ),
    QualityCriterion(
        name="verdict-fits-kind",
        question=(
            "Does the expected verdict follow the kind (reject expects block, approve expects approve) rather "
            "than being contradicted by the findings — e.g. a reject with no substantive finding, or an "
            "approve carrying a blocking finding?"
        ),
        why="'Should this block?' and 'what was wrong?' must not disagree inside one datapoint (FR-14).",
    ),
    QualityCriterion(
        name="instruction-outcome-neutral",
        question=(
            "Is the instruction free of anything that names or hints at the expected outcome — the verdict, the "
            "kind, the PR number, 'reject'/'approve'/'block' — so the answer is not inferable from the phrasing?"
        ),
        why="A verdict readable from the wording tests the prose, not the agent's review (FR-33).",
    ),
    QualityCriterion(
        name="nits-not-promoted",
        question=(
            "Are style nits and preferences kept non-blocking (low severity) rather than promoted to blocking "
            "findings that teach the eval that bikeshedding is good review?"
        ),
        why="A nit promoted to a blocker is the assumption PRD §4 warns is the datapoint's main failure mode.",
    ),
)

#: The criterion names, in order — the exact set every quality verdict must cover (a missing one is a
#: parse error, so the judge cannot silently drop a criterion).
CRITERION_NAMES: tuple[str, ...] = tuple(criterion.name for criterion in QUALITY_CRITERIA)


def render_rubric() -> str:
    """The rubric as the prompt-ready text block, one numbered criterion per line with its question."""
    lines = [f"{index}. {criterion.name}: {criterion.question}" for index, criterion in enumerate(QUALITY_CRITERIA, start=1)]
    return "\n".join(lines)
