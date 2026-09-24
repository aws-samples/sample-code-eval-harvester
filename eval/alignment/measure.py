"""Measure judge↔human agreement over the labeled set, against a stated bar.

The measurement runs the judge on each labeled datapoint and compares its overall verdict to the
human label. Agreement is the fraction of datapoints the judge and the human agree on (both pass or
both fail) — a coarse but honest first signal, kept alongside a per-criterion breakdown for
diagnosis. The bar is stated here with its rationale, not picked silently.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from eval.alignment.labeled_set import LabeledDatapoint
from eval.judge.client import ModelClient, RecordedModelClient
from eval.judge.judge import QualityJudge
from eval.judge.models import AlignmentResult, QualityVerdict
from eval.judge.rubric import CRITERION_NAMES, JUDGE_VERSION

#: The agreement bar the judge must clear to be trusted, stated (not manufactured) with its reason:
#: the judge is a coarse quality gate, so it must agree with an independent human on at least four of
#: every five datapoints; below that its verdicts are noise and no G-3 score built on it is quotable
#: (PRD §8, task Notes "don't manufacture the bar"). The achieved number is recorded, whatever it is.
ALIGNMENT_BAR = 0.80

#: The label recorded in ``AlignmentResult.model`` for the reproducible offline run over pinned replies.
#: A live refresh (``run_alignment --model <id>``) records the real model id instead.
RECORDED_FIXTURE_MODEL = "recorded-fixtures"

#: One judged case: a labeled datapoint paired with the verdict the judge returned for it.
JudgedCase = tuple[LabeledDatapoint, QualityVerdict]

#: Builds the model client for one labeled datapoint (recorded replay by default, live on refresh).
ClientFactory = Callable[[LabeledDatapoint], ModelClient]


@dataclass(frozen=True, slots=True)
class AlignmentReport:
    """The full alignment measurement: the headline result plus the per-criterion agreement and misses."""

    result: AlignmentResult
    per_criterion_agreement: dict[str, float]
    disagreements: tuple[str, ...]


def _recorded_client_for(case: LabeledDatapoint) -> ModelClient:
    """The default factory: replay the datapoint's own pinned reply — the reproducible offline run."""
    return RecordedModelClient(case.recorded_response)


class AlignmentMeasurement:
    """Runs the judge over the labeled set and computes agreement with the human labels. Holds no state."""

    @classmethod
    def run(
        cls,
        labeled: Sequence[LabeledDatapoint],
        *,
        model: str = RECORDED_FIXTURE_MODEL,
        client_for: ClientFactory | None = None,
        bar: float = ALIGNMENT_BAR,
    ) -> AlignmentReport:
        """Judge every labeled datapoint and roll the agreement up into an :class:`AlignmentReport`.

        ``client_for`` builds the model client for a labeled datapoint; it defaults to replaying that
        datapoint's pinned reply (the reproducible offline run). A live refresh passes a factory that
        returns a :class:`~eval.judge.client.LiveModelClient`."""
        if not labeled:
            raise ValueError("the alignment set is empty; there is nothing to measure agreement against")
        make_client = client_for if client_for is not None else _recorded_client_for
        judged = [cls._judge_one(case, make_client(case)) for case in labeled]
        agreement = cls._agreement(judged)
        return AlignmentReport(
            result=AlignmentResult(
                n_labeled=len(judged),
                agreement=agreement,
                bar=bar,
                passed=agreement >= bar,
                judge_version=JUDGE_VERSION,
                model=model,
            ),
            per_criterion_agreement=cls._per_criterion_agreement(judged),
            disagreements=tuple(case.id for case, verdict in judged if verdict.overall_pass != case.human_overall_pass),
        )

    @staticmethod
    def _judge_one(case: LabeledDatapoint, client: ModelClient) -> JudgedCase:
        """Run the judge on one labeled datapoint and pair its verdict with the human label."""
        verdict = QualityJudge.evaluate(case.datapoint, case.pr_context, model_client=client)
        return (case, verdict)

    @staticmethod
    def _agreement(judged: list[JudgedCase]) -> float:
        """The fraction of datapoints where the judge's overall verdict matches the human label."""
        agree = sum(1 for case, verdict in judged if verdict.overall_pass == case.human_overall_pass)
        return agree / len(judged)

    @staticmethod
    def _per_criterion_agreement(judged: list[JudgedCase]) -> dict[str, float]:
        """Per-criterion consistency, for diagnosis — which criterion to sharpen, not the gate itself.

        Coarse by design: the human label is overall, so we approximate per-criterion truth as "a good
        datapoint should pass every criterion; a bad one may fail any" and count how often the judge's
        per-criterion pass is consistent with that. The gate is the overall agreement, not this."""
        totals = dict.fromkeys(CRITERION_NAMES, 0.0)
        for case, verdict in judged:
            for criterion in verdict.per_criterion:
                consistent = criterion.passed if case.human_overall_pass else True
                totals[criterion.name] += 1.0 if consistent else 0.0
        return {name: totals[name] / len(judged) for name in CRITERION_NAMES}
