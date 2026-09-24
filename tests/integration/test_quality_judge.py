"""Offline tests for the datapoint-quality judge (Workstream G, G-2).

These exercise the judge's wiring without a live model (the recorded-judge pattern, tech plan §12):
the response parser maps a recorded reply to a well-formed verdict and refuses to silently drop a
criterion; the judge flags a known-bad datapoint given a recorded reply; the loader reads a real
emitted datapoint end-to-end; and the trusted `judge()` entry point refuses until a recorded
alignment result licenses it.

Do NOT add a test asserting a live model's exact wording (task Tests To Write).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fixtures.repo_builder import RepoBuilder

from eval.judge import (
    DatapointLoader,
    LiveModelClient,
    QualityJudge,
    RecordedModelClient,
    UnalignedJudgeError,
    judge,
)
from eval.judge.client import REQUIRED_ENDPOINT_SCHEME
from eval.judge.models import AlignmentResult, Datapoint, OracleFindingView, PrContext
from eval.judge.rubric import CRITERION_NAMES, JUDGE_VERSION

# eval-harvest emit builds a real datapoint for the end-to-end loader test.
from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.emit import Emit
from eval_harvest.forge import Forge
from eval_harvest.riskmap import RiskMap
from eval_harvest.tomlw import emit_document

_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}


def _reply(overrides: dict[str, bool] | None = None) -> str:
    """A recorded model reply covering every criterion; `overrides` flips named criteria to failed."""
    overrides = overrides or {}
    criteria = [
        {
            "name": name,
            "passed": overrides.get(name, True),
            "score": 0.0 if name in overrides else 0.9,
            "rationale": "recorded",
        }
        for name in CRITERION_NAMES
    ]
    return json.dumps({"criteria": criteria, "notes": "recorded reply"})


def _datapoint(**overrides: Any) -> Datapoint:
    """A minimal reject datapoint for the judge tests."""
    base: dict[str, Any] = {
        "directory_name": "org__repo__pr1-reject",
        "kind": "reject",
        "expected_verdict": "block",
        "blocking_severity": "high",
        "instruction": "Review the change and report the defects a competent reviewer should catch.",
        "change_patch": "--- a/x.py\n+++ b/x.py\n@@\n-a\n+b\n",
        "oracle_findings": (OracleFindingView(statement="bug", severity="high", reference_only=False, blocking=True),),
    }
    base.update(overrides)
    return Datapoint.model_validate(base)


def _context() -> PrContext:
    return PrContext(repo="org/repo", pr_number=1, reviewed_state="change-requested", review_summary="a real defect")


# ───────────────────────────── parsing a recorded reply ─────────────────────────────


def test_quality_verdict_parses_from_recorded_response() -> None:
    """The judge maps a recorded reply to a well-formed verdict covering every rubric criterion."""
    verdict = QualityJudge.parse_response(_reply())
    assert {criterion.name for criterion in verdict.per_criterion} == set(CRITERION_NAMES)
    assert len(verdict.per_criterion) == len(CRITERION_NAMES)
    assert verdict.overall_pass is True
    assert 0.0 <= verdict.overall_score <= 1.0


def test_parse_response_refuses_to_drop_a_criterion() -> None:
    """A reply missing a criterion fails loudly rather than silently shrinking the audit (schema drift)."""
    document = json.loads(_reply())
    document["criteria"] = [entry for entry in document["criteria"] if entry["name"] != "nits-not-promoted"]
    with pytest.raises(ValueError, match="missing criteria: nits-not-promoted"):
        QualityJudge.parse_response(json.dumps(document))


def test_parse_response_tolerates_a_json_code_fence() -> None:
    """A model that wraps its JSON in a ```json fence still parses — the common real-world reply shape."""
    fenced = f"```json\n{_reply()}\n```"
    verdict = QualityJudge.parse_response(fenced)
    assert len(verdict.per_criterion) == len(CRITERION_NAMES)


# ───────────────────────────── flagging a known-bad datapoint ─────────────────────────────


def test_judge_flags_known_bad_datapoint() -> None:
    """On a nit-as-blocking datapoint, the judge (recorded reply) returns overall_pass=False (no rubber-stamp)."""
    nit = OracleFindingView(statement="rename tmp to result", severity="high", reference_only=False, blocking=True)
    nit_as_blocking = _datapoint(oracle_findings=(nit,))
    client = RecordedModelClient(_reply(overrides={"nits-not-promoted": False}))
    verdict = QualityJudge.evaluate(nit_as_blocking, _context(), model_client=client)
    assert verdict.overall_pass is False
    failed = [criterion.name for criterion in verdict.per_criterion if not criterion.passed]
    assert failed == ["nits-not-promoted"]


# ───────────────────────────── the loader, end-to-end on a real emitted datapoint ─────────────────────────────


def test_loader_reads_a_real_emitted_datapoint(tmp_path: Path) -> None:
    """DatapointLoader reads an actual `eval-harvest emit` task directory into a gradeable Datapoint."""
    dataset, candidate = _prepare_candidate(tmp_path)
    task_dir = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    datapoint = DatapointLoader.load(task_dir)
    assert datapoint.kind == "reject"
    assert datapoint.expected_verdict == "block"
    assert datapoint.oracle_findings  # the reject oracle is non-empty
    assert datapoint.change_patch  # the change under review was read

    # The loaded datapoint feeds the judge unchanged (recorded reply), proving the wiring is end-to-end.
    verdict = QualityJudge.evaluate(datapoint, _context(), model_client=RecordedModelClient(_reply()))
    assert verdict.overall_pass is True


# ───────────────────────────── the alignment gate ─────────────────────────────


def test_alignment_gate_enforced(tmp_path: Path) -> None:
    """The trusted judge() refuses unless a recorded alignment result at/above the bar is present."""
    missing = tmp_path / "absent.json"
    with pytest.raises(UnalignedJudgeError, match="no alignment result"):
        judge(_datapoint(), _context(), model_client=RecordedModelClient(_reply()), alignment_path=missing)

    below_bar = tmp_path / "below.json"
    below_bar.write_text(_alignment_json(agreement=0.5, bar=0.8, passed=False), encoding="utf-8")
    with pytest.raises(UnalignedJudgeError, match="below the bar"):
        judge(_datapoint(), _context(), model_client=RecordedModelClient(_reply()), alignment_path=below_bar)

    aligned = tmp_path / "aligned.json"
    aligned.write_text(_alignment_json(agreement=0.9, bar=0.8, passed=True), encoding="utf-8")
    verdict = judge(_datapoint(), _context(), model_client=RecordedModelClient(_reply()), alignment_path=aligned)
    assert verdict.overall_pass is True


def test_alignment_gate_rejects_a_stale_rubric_version(tmp_path: Path) -> None:
    """An alignment measured for a different rubric version no longer licenses the current judge."""
    stale = tmp_path / "stale.json"
    stale.write_text(_alignment_json(agreement=0.9, bar=0.8, passed=True, version="quality-judge-v0"), encoding="utf-8")
    with pytest.raises(UnalignedJudgeError, match="re-run alignment for the current rubric"):
        judge(_datapoint(), _context(), model_client=RecordedModelClient(_reply()), alignment_path=stale)


# ───────────────────────────── helpers ─────────────────────────────


def _alignment_json(*, agreement: float, bar: float, passed: bool, version: str = JUDGE_VERSION) -> str:
    """A serialized AlignmentResult for the gate tests."""
    return AlignmentResult(
        n_labeled=8, agreement=agreement, bar=bar, passed=passed, judge_version=version, model="recorded-fixtures"
    ).model_dump_json()


def _substantive_findings() -> list[FindingDict]:
    """A reject oracle with one high defect bound to a comment (mirrors test_emit)."""
    return [
        {
            "comment_ids": [9001],
            "statement": "`add` subtracts instead of adds",
            "severity": "high",
            "severity_evidence": "return a - b in the round-1 diff",
            "severity_rationale": "wrong result for every caller",
            "reference_only": False,
        }
    ]


def _prepare_candidate(tmp_path: Path) -> tuple[Path, CandidateDict]:
    """A dataset holding a filled candidate the way `capture` would, ready for `emit` (mirrors test_emit)."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    facts = Forge.reconstruct_facts(
        fixture.pr_number,
        fixture.clone,
        fixture.base_ref,
        reviews=fixture.reviews,
        comments=fixture.comments,
        timeline=fixture.timeline,
    )
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(emit_document(_RISK_MAP))
    (dataset / "rubric.md").write_text("---\nversion: v1\n---\n# Review rubric\n", encoding="utf-8")
    risk_structural, rule = RiskMap.structural_risk(Candidate.changed_paths(facts), _RISK_MAP)
    Candidate.write_to_dataset(
        facts,
        repo="our-org/our-repo",
        pr_url="https://github.com/our-org/our-repo/pull/1234",
        dataset_dir=dataset,
        risk_structural=risk_structural,
        risk_structural_rule=rule,
    )
    candidate_path = dataset / "candidates" / "pr-1234.json"
    candidate = Candidate.load(candidate_path)
    for comment in candidate["comments"]:
        comment["classification"] = "defect"
        comment["classification_rationale"] = "a substantive problem a reviewer would block on"
    candidate["findings"] = _substantive_findings()
    candidate["change_risk"]["risk_classified"] = "low"
    candidate["change_risk"]["risk_classified_rationale"] = "isolated helper"
    candidate["rubric_version"] = "v1"
    Candidate.dump(candidate, candidate_path)
    return dataset, candidate


# ──────────────────── the live client opens https and nothing else ────────────────────


def test_live_model_client_refuses_a_non_https_endpoint() -> None:
    """`LiveModelClient` refuses `file://` and `http://` endpoints before `urllib` is reached.

    `urllib` honours every scheme it knows, and this client's endpoint comes from
    `$EVAL_JUDGE_ENDPOINT`. Over `file://` a model call becomes a local file read whose contents come
    back as the judge's verdict; over `http://` the prompt and the bearer token go out in clear text.
    Watched fail: dropping the `require_transport_security` call turns this red on the `file://` case.
    """
    for endpoint in ("file:///etc/passwd", "http://169.254.169.254/latest/meta-data/", "ftp://host/x"):
        client = LiveModelClient(model="a-model", endpoint=endpoint)
        with pytest.raises(ValueError, match="must start with 'https://'") as raised:
            client("does this reach the network?")
        assert endpoint in str(raised.value), "the refusal names the endpoint it rejected"


def test_live_model_client_accepts_an_https_endpoint() -> None:
    """The pair to the test above: the guard rejects a scheme, not a hostname.

    A guard that refused every endpoint would pass the refusal test for the wrong reason. Nothing
    here calls a model — only the scheme check runs.
    """
    LiveModelClient.require_transport_security("https://api.openai.com/v1/chat/completions")
    LiveModelClient.require_transport_security("https://bedrock-runtime.us-east-1.amazonaws.com/x")


def test_live_model_client_from_env_keeps_the_https_default() -> None:
    """`from_env` with no endpoint override lands on the https default, so the guard never trips there."""
    client = LiveModelClient.from_env({"EVAL_JUDGE_MODEL": "a-model"})
    assert client.endpoint.startswith(REQUIRED_ENDPOINT_SCHEME)
    client.require_transport_security(client.endpoint)
