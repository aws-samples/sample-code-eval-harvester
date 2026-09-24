"""Refusal and guard contracts: usage errors, coherence refusals, and broken-datapoint detection.

The usage-error and empty-dataset cases are offline (no forge), so they run everywhere. The
coherence and broken-datapoint cases reuse the session pipeline and are network-gated.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import (
    EXIT_REFUSAL,
    EXIT_USAGE,
    EXIT_VERIFY_FAILURE,
    Pipeline,
    _fill_records,
    requires_network,
    run_cli,
)

# ── Offline: argument/usage contracts (exit 2) and the empty-dataset refusal (exit 3) ──────────────


def test_emit_requires_candidate_or_all() -> None:
    """Neither <candidate> nor --all is a usage error (exit 2)."""
    result = run_cli("emit", "--kind", "reject")
    assert result.returncode == EXIT_USAGE, f"{result.returncode}: {result.stderr}"


def test_capture_pr_and_from_survey_are_mutually_exclusive(tmp_path: Path) -> None:
    """Passing both <pr> and --from-survey is a usage error (exit 2)."""
    survey_file = tmp_path / "survey.json"
    survey_file.write_text("{}", encoding="utf-8")
    result = run_cli("capture", "5", "--from-survey", survey_file, "--clone", tmp_path)
    assert result.returncode == EXIT_USAGE, f"{result.returncode}: {result.stderr}"


def test_survey_requires_repo_or_from_json(tmp_path: Path) -> None:
    """Neither --repo nor --from-json is a usage error (exit 2)."""
    result = run_cli("survey", "--clone", tmp_path)
    assert result.returncode == EXIT_USAGE, f"{result.returncode}: {result.stderr}"


def test_dataset_empty_refuses_no_datapoints(tmp_path: Path) -> None:
    """An empty dataset refuses with no-datapoints (exit 3)."""
    dataset = tmp_path / "empty"
    (dataset / "candidates").mkdir(parents=True)
    (dataset / "tasks").mkdir(parents=True)
    result = run_cli("dataset", "--dataset", dataset)
    assert result.returncode == EXIT_REFUSAL, f"{result.returncode}: {result.stdout}{result.stderr}"


# ── Network-gated: coherence refusal and broken-datapoint detection ─────────────────────────────────


@requires_network
def test_emit_approve_with_high_finding_is_refused(pipeline: Pipeline, tmp_path: Path) -> None:
    """FR-14: an approve candidate carrying a high-severity finding is refused (exit 3)."""
    # Full-copy the captured dataset so the iteration patches are present; otherwise emit would refuse
    # with `missing-patch` and this test would pass for the wrong reason.
    dataset = tmp_path / "ds-high"
    shutil.copytree(pipeline.dataset_reject, dataset)
    candidate_path = dataset / "candidates" / f"pr-{pipeline.pr}.json"
    candidate_path.write_text(pipeline.raw_candidate, encoding="utf-8")

    candidate = json.loads(pipeline.raw_candidate)
    stream = _fill_records(candidate, pipeline.defect_ids, with_findings=True, high=True, rubric_version=pipeline.rubric_version)
    filled = run_cli("annotate", candidate_path, "--stdin", input_text=stream)
    assert filled.returncode == 0, filled.stderr

    result = run_cli("emit", candidate_path, "--kind", "approve", "--clone", pipeline.clone)
    assert result.returncode == EXIT_REFUSAL, f"expected refusal, got {result.returncode}: {result.stdout}{result.stderr}"
    combined = result.stdout + result.stderr
    assert combined.strip(), "refusal produced no message"
    assert "missing-patch" not in combined, f"refused for the wrong reason (patches missing, not the high finding): {combined}"


@requires_network
def test_verify_catches_a_broken_datapoint(pipeline: Pipeline, tmp_path: Path) -> None:
    """FR-34: a datapoint whose patch no longer applies fails verification (exit 4) with the four-field shape.

    (A PR-number leak into `instruction.md` is *not* a reliable exit-4 trigger: the content scan
    suppresses a token that already exists in the base tree — a pre-existing string is not a leak the
    datapoint introduced — which is exactly the documented base-tree-suppression behaviour. Corrupting
    the patch exercises a check that cannot be suppressed.)
    """
    broken = tmp_path / "broken-task"
    shutil.copytree(pipeline.reject_task, broken)
    (broken / "environment" / "change.patch").write_text("this is not a valid unified diff\n", encoding="utf-8")
    result = run_cli("--json", "verify", broken, "--clone", pipeline.clone)
    assert result.returncode == EXIT_VERIFY_FAILURE, (
        f"expected verification failure, got {result.returncode}: {result.stdout}{result.stderr}"
    )
    payload = result.json()
    assert isinstance(payload, dict)
    failures = payload.get("failures", [])
    assert failures, "verify reported no failures"
    for failure in failures:
        assert {"check", "datapoint", "offending", "next"} <= set(failure), f"refusal missing the four fields: {failure}"
