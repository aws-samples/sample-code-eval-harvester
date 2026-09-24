"""The alignment gate and the trusted :func:`judge` entry point G-3 calls.

An unaligned judge is an opinion, not a measurement (task Notes). So the *trusted* way to run the
judge is :func:`judge`, which refuses unless a recorded :class:`~eval.judge.models.AlignmentResult`
at or above the bar exists for the current rubric version. The raw engine
(:class:`~eval.judge.judge.QualityJudge`) is still importable for the offline parsing tests and for
the alignment run that *produces* the record — but code that scores real eval runs goes through the
gate, so a judge nobody has checked against human labels cannot be trusted by accident.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.judge.client import LiveModelClient, ModelClient
from eval.judge.judge import QualityJudge
from eval.judge.models import AlignmentResult, Datapoint, PrContext, QualityVerdict
from eval.judge.rubric import JUDGE_VERSION

#: Where the recorded alignment result lives — the record the gate reads to license the judge.
DEFAULT_ALIGNMENT_PATH = Path(__file__).parents[1] / "alignment" / "alignment_result.json"


class UnalignedJudgeError(RuntimeError):
    """Raised when the judge is used before a recorded alignment result licenses it (the gate tripping)."""


class AlignmentGate:
    """Reads the recorded alignment result and decides whether the judge may be trusted. Holds no state."""

    @classmethod
    def require_aligned(cls, alignment_path: Path = DEFAULT_ALIGNMENT_PATH) -> AlignmentResult:
        """Return the recorded alignment result if it licenses the judge, else raise ``UnalignedJudgeError``.

        Three ways to be unlicensed, each its own message: no result recorded, the result did not
        clear its bar, or the result was measured against a different rubric version than this one."""
        result = cls._load(alignment_path)
        if result is None:
            raise UnalignedJudgeError(
                f"no alignment result at {alignment_path}: run `python -m eval.alignment.run_alignment` and record it "
                "before trusting the quality judge"
            )
        if not result.passed or result.agreement < result.bar:
            raise UnalignedJudgeError(
                f"recorded judge↔human agreement {result.agreement:.3f} is below the bar {result.bar:.3f}: "
                "improve the rubric/prompt and re-run alignment before trusting the judge"
            )
        if result.judge_version != JUDGE_VERSION:
            raise UnalignedJudgeError(
                f"alignment was measured for judge version {result.judge_version!r} but this is {JUDGE_VERSION!r}: "
                "re-run alignment for the current rubric"
            )
        return result

    @staticmethod
    def _load(alignment_path: Path) -> AlignmentResult | None:
        """The recorded alignment result, or ``None`` when none has been recorded yet."""
        if not alignment_path.is_file():
            return None
        document: Any = json.loads(alignment_path.read_text(encoding="utf-8"))
        return AlignmentResult.model_validate(document)


def judge(
    datapoint: Datapoint,
    pr_context: PrContext,
    *,
    model_client: ModelClient | None = None,
    alignment_path: Path = DEFAULT_ALIGNMENT_PATH,
) -> QualityVerdict:
    """Score one datapoint's quality — the trusted entry point, gated on a recorded alignment result.

    Refuses (``UnalignedJudgeError``) unless a recorded alignment result at or above the bar exists
    for this rubric version, *before* any model call. When licensed, it runs the judge with
    ``model_client`` (defaulting to the human-gated :class:`LiveModelClient` built from the
    environment). G-3 calls this; the alignment run itself uses :class:`QualityJudge` directly, since
    it is the step that produces the record this gate reads."""
    AlignmentGate.require_aligned(alignment_path)
    client = model_client if model_client is not None else LiveModelClient.from_env()
    return QualityJudge.evaluate(datapoint, pr_context, model_client=client)
