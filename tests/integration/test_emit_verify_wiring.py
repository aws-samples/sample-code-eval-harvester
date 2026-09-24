"""Tests for `emit`→`verify` refuse-before-write and recorded `--override` (task E-4, FR-38, §7.2).

`emit` (D-3) assembled the task against a `verify` *hook*; E-4 wires that hook to the real `verify`
(E-2) so a datapoint that fails a check is refused, not silently shipped — the loop that makes an
unsupervised agent safe. Where `test_emit` exercised the hook seam with a fixed-failure stub, these
scenarios drive the *real* `verify` end to end: they emit a genuine datapoint whose `change.patch`
has been corrupted so it will not apply at the base, pass the fixture clone `emit` hands `verify`,
and assert the wiring's three properties — a failure writes nothing (S-2/FR-38), an explicit
`--override <check>` promotes and records the check verbatim so the §4 audit can find it, and an
override that names one check never suppresses a different failing one.

The corruption changes a *context* line (`def add`), so the hunk headers — and thus the changed-line
spans the oracle is checked against — are unchanged: `patch-does-not-apply` is the single failure,
not a cascade, exactly as `test_verify` isolates it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from fixtures.repo_builder import RepoBuilder

from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.cli import ExitCode
from eval_harvest.emit import Emit, EmitRefusalError
from eval_harvest.forge import Forge
from eval_harvest.riskmap import RiskMap
from eval_harvest.tomlw import emit_document

#: A risk map marking the fixture's only file high — enough for `emit` to compute a structural risk.
_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}


def _substantive_findings() -> list[FindingDict]:
    """A reject oracle: one high defect (operator bug) and one medium (off-by-one), bound to comments."""
    return [
        {
            "comment_ids": [9001],
            "statement": "`add` subtracts instead of adds",
            "severity": "high",
            "severity_evidence": "return a - b in the round-1 diff",
            "severity_rationale": "wrong result for every caller",
            "reference_only": False,
        },
        {
            "comment_ids": [9002],
            "statement": "loop runs one index past the end",
            "severity": "medium",
            "severity_evidence": "range(len(items) + 1)",
            "severity_rationale": "IndexError on the last iteration",
            "reference_only": False,
        },
    ]


def _fill(candidate: CandidateDict, findings: list[FindingDict]) -> CandidateDict:
    """Classify every comment, set the findings, classify the risk, and pin the rubric version."""
    for comment in candidate["comments"]:
        comment["classification"] = "defect"
        comment["classification_rationale"] = "a substantive problem a reviewer would block on"
    candidate["findings"] = findings
    candidate["change_risk"]["risk_classified"] = "low"
    candidate["change_risk"]["risk_classified_rationale"] = "isolated helper, well covered by tests"
    candidate["rubric_version"] = "v1"
    return candidate


def _prepare(tmp_path: Path) -> tuple[Path, CandidateDict, Path]:
    """A dataset holding a filled reject candidate, plus the fixture clone `emit` hands `verify`.

    Mirrors `test_verify`'s setup but stops before emitting, so a test can corrupt the materialized
    patch first and then drive `emit` over the real `verify`.
    """
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
    Candidate.dump(_fill(Candidate.load(candidate_path), _substantive_findings()), candidate_path)
    return dataset, Candidate.load(candidate_path), fixture.clone


def _corrupt_reject_patch(dataset: Path, candidate: CandidateDict) -> None:
    """Corrupt a context line in the patch the reject iteration uses, so it will not apply at the base.

    Changes `def add` (an unchanged context line) rather than a hunk header, so the diff still parses
    to the same changed-line spans — `patch-does-not-apply` is the only check that fails.
    """
    recoverable = [iteration for iteration in candidate["iterations"] if iteration["patch_path"]]
    patch_file = dataset / recoverable[0]["patch_path"]
    text = patch_file.read_text(encoding="utf-8")
    assert "def add" in text, "the fixture's reject patch is expected to carry `def add` as a context line"
    patch_file.write_text(text.replace("def add", "def subtract_totally_different"), encoding="utf-8")


def _harvest(task_dir: Path) -> dict[str, Any]:
    """The parsed `[metadata.harvest]` table of the emitted task."""
    harvest: dict[str, Any] = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))["metadata"]["harvest"]
    return harvest


def _no_task_written(dataset: Path) -> bool:
    """True when `emit` promoted nothing — the refuse-before-write guarantee (§7.2)."""
    return not (dataset / "tasks").exists() or not any((dataset / "tasks").iterdir())


def test_failing_verify_writes_nothing(tmp_path: Path) -> None:
    """A datapoint that fails the real `verify` is refused (exit 4) and nothing is promoted (FR-38, S-2)."""
    dataset, candidate, clone = _prepare(tmp_path)
    _corrupt_reject_patch(dataset, candidate)

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, clone=clone)

    assert refusal.value.check == "patch-does-not-apply", "the real verify check names the broken input"
    assert refusal.value.exit_code == ExitCode.VERIFICATION
    assert _no_task_written(dataset), "a failing datapoint must leave no partial task behind"


def test_override_records_and_promotes(tmp_path: Path) -> None:
    """`--override <check>` promotes the failing datapoint and records the check verbatim (FR-38, §7.2)."""
    dataset, candidate, clone = _prepare(tmp_path)
    _corrupt_reject_patch(dataset, candidate)

    task = Emit.emit_datapoint(
        candidate, kind="reject", dataset_dir=dataset, clone=clone, overrides=("patch-does-not-apply",)
    ).path

    assert task.is_dir(), "an overridden check promotes the task"
    assert _harvest(task)["overrides"] == ["patch-does-not-apply"], "the override is recorded verbatim for the §4 audit"


def test_unoverridden_failure_still_refuses(tmp_path: Path) -> None:
    """Overriding one check never suppresses a *different* failing check (FR-38 — no widening)."""
    dataset, candidate, clone = _prepare(tmp_path)
    _corrupt_reject_patch(dataset, candidate)

    # Override a check that is *not* the one failing; `patch-does-not-apply` must still refuse.
    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, clone=clone, overrides=("finding-line-absent",))

    assert refusal.value.check == "patch-does-not-apply", "the override must not widen past the check it named"
    assert refusal.value.exit_code == ExitCode.VERIFICATION
    assert _no_task_written(dataset)
