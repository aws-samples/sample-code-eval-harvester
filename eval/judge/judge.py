"""The datapoint-quality judge: render a prompt, ask a model, parse the reply into a quality verdict.

This is *not* the in-task verifier's judge (``src/eval_harvest/verifier_tpl/score.py``), which scores
an agent-under-review's findings against an oracle. This judge scores whether the *datapoint the eval
agent built* is good, against the rubric in :mod:`eval.judge.rubric`. It is layered on top of
``eval-harvest verify`` (structural validity), never a replacement for it.

The judge is a pure function of the datapoint, the PR context, and one model call: it builds a prompt
(:meth:`QualityJudge.build_prompt`), hands it to a :data:`~eval.judge.client.ModelClient`, and parses
the reply (:meth:`QualityJudge.parse_response`). The overall pass/score is *derived* by the harness
from the per-criterion verdicts, never read from the model, so a model that miscounts its own summary
cannot inflate the roll-up. Every rubric criterion must appear in the reply or parsing fails — the
judge cannot silently drop a criterion.

:class:`QualityJudge` is the ungated engine; the *trusted* entry point G-3 calls is
:func:`eval.judge.gate.judge`, which refuses unless a recorded alignment result licenses the judge.
"""

from __future__ import annotations

import json
from typing import Any  # A model's JSON reply is arbitrary until validated against the rubric below.

from eval.judge.client import ModelClient
from eval.judge.models import CriterionVerdict, Datapoint, OracleFindingView, PrContext, QualityVerdict
from eval.judge.rubric import CRITERION_NAMES, render_rubric

_PROMPT_TEMPLATE = """\
You are auditing the quality of a PR-review evaluation datapoint. A datapoint is a single review task:
a code change under review, an oracle of the defects a competent reviewer should catch, and an
expected verdict. Your job is to decide whether this datapoint is a *good* one — one that faithfully
captures a real pull request's review — against the criteria below. You are not reviewing the code;
you are judging whether the datapoint was built well.

Score each criterion independently. A datapoint can fail one criterion and pass the rest.

## Criteria
{rubric}

## Source pull request (the ground truth the datapoint should reflect)
- Repository: {repo}
- Pull request: #{pr_number}
- Reviewed state the datapoint claims to capture: {reviewed_state}
- What the human review actually was: {review_summary}

## The datapoint under audit
- Kind: {kind}
- Expected verdict: {expected_verdict}
- Blocking severity: {blocking_severity}
- Change-risk (structural / classified): {risk_structural} / {risk_classified}

### Oracle findings
{oracle_block}

### Instruction shown to the evaluated agent
{instruction}

### Change under review (unified diff)
{change_patch}

## Output
Respond with ONLY a JSON object of this shape (no prose outside it):
{{"criteria": [{{"name": "<criterion-name>", "passed": true, "score": 0.0, "rationale": "<why>"}}, ...],
  "notes": "<overall commentary>"}}
Include exactly one entry per criterion, using the criterion names above.
"""


class QualityJudge:
    """Builds the prompt, calls the model, and parses the reply into a verdict. Holds no state."""

    @classmethod
    def evaluate(cls, datapoint: Datapoint, pr_context: PrContext, *, model_client: ModelClient) -> QualityVerdict:
        """Grade one datapoint: render the prompt, call ``model_client``, parse its reply to a verdict."""
        prompt = cls.build_prompt(datapoint, pr_context)
        raw_response = model_client(prompt)
        return cls.parse_response(raw_response)

    @classmethod
    def build_prompt(cls, datapoint: Datapoint, pr_context: PrContext) -> str:
        """Render the full judge prompt for ``datapoint`` given its source ``pr_context``."""
        return _PROMPT_TEMPLATE.format(
            rubric=render_rubric(),
            repo=pr_context.repo,
            pr_number=pr_context.pr_number,
            reviewed_state=pr_context.reviewed_state,
            review_summary=pr_context.review_summary,
            kind=datapoint.kind,
            expected_verdict=datapoint.expected_verdict,
            blocking_severity=datapoint.blocking_severity,
            risk_structural=datapoint.change_risk_structural or "unset",
            risk_classified=datapoint.change_risk_classified or "unset",
            oracle_block=cls._render_oracle(datapoint),
            instruction=datapoint.instruction.strip(),
            change_patch=datapoint.change_patch.strip(),
        )

    @staticmethod
    def _render_oracle_finding(finding: OracleFindingView) -> str:
        """One oracle finding as a prompt bullet: its severity, whichever flags it carries, its statement."""
        blocking_flag = ", blocking" if finding.blocking else ""
        reference_flag = ", reference-only" if finding.reference_only else ""
        return f"- [{finding.severity}{blocking_flag}{reference_flag}] {finding.statement}"

    @classmethod
    def _render_oracle(cls, datapoint: Datapoint) -> str:
        """The oracle findings as a readable list for the prompt, or a note when there are none."""
        if not datapoint.oracle_findings:
            return "(no oracle findings — an approve datapoint may legitimately have none)"
        return "\n".join(cls._render_oracle_finding(finding) for finding in datapoint.oracle_findings)

    @classmethod
    def parse_response(cls, raw_response: str) -> QualityVerdict:
        """Parse a model reply into a :class:`QualityVerdict`, deriving the overall roll-up ourselves.

        Raises ``ValueError`` if the reply is not the expected JSON or omits any rubric criterion — a
        dropped criterion must fail loudly rather than silently shrink the audit."""
        document = cls._extract_json_object(raw_response)
        per_criterion = cls._parse_criteria(document)
        overall_score = sum(verdict.score for verdict in per_criterion) / len(per_criterion)
        return QualityVerdict(
            per_criterion=per_criterion,
            overall_pass=all(verdict.passed for verdict in per_criterion),
            overall_score=overall_score,
            notes=str(document.get("notes", "")),
        )

    @staticmethod
    def _extract_json_object(raw_response: str) -> dict[str, Any]:
        """The JSON object in ``raw_response``, tolerating a ```` ```json ```` fence around it."""
        text = raw_response.strip()
        if text.startswith("```"):
            # Strip a leading fence line (``` or ```json) and any trailing fence.
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0]
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"quality judge reply is not JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ValueError("quality judge reply is not a JSON object")
        return parsed

    @staticmethod
    def _parse_criteria(document: dict[str, Any]) -> tuple[CriterionVerdict, ...]:
        """Every rubric criterion as a :class:`CriterionVerdict`, in rubric order; a missing one is fatal."""
        raw_criteria = document.get("criteria", [])
        if not isinstance(raw_criteria, list):
            raise ValueError("quality judge reply has no 'criteria' list")
        by_name = {str(entry["name"]): entry for entry in raw_criteria if isinstance(entry, dict) and "name" in entry}
        missing = [name for name in CRITERION_NAMES if name not in by_name]
        if missing:
            raise ValueError(f"quality judge reply is missing criteria: {', '.join(missing)}")
        return tuple(_criterion_verdict(name, by_name[name]) for name in CRITERION_NAMES)


def _criterion_verdict(name: str, entry: dict[str, Any]) -> CriterionVerdict:
    """One criterion's verdict from its reply entry, defaulting a fail to score 0 and a pass to 1."""
    passed = bool(entry.get("passed", False))
    score = float(entry["score"]) if "score" in entry else (1.0 if passed else 0.0)
    return CriterionVerdict(name=name, passed=passed, score=score, rationale=str(entry.get("rationale", "")))
