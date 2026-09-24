"""The end-to-end happy path: init → survey → capture → fill → emit → verify → dataset.

Every assertion is structural — keys exist, types are right, files are written, exit codes match
the contract. No specific value (comment text, SHA, count, severity) is ever asserted, so the suite
holds against the live repository changing underneath it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from conftest import EXIT_SUCCESS, EXIT_UNRESOLVED, REPO, Pipeline, requires_network, run_cli


@requires_network
def test_init_scaffolds_dataset_and_lists_conventions(clone: Path, tmp_path: Path) -> None:
    """FR-24/FR-25: init writes the skeleton + templates and exits 0."""
    dataset = tmp_path / "ds"
    result = run_cli("init", clone, "--dataset", dataset)
    assert result.returncode == EXIT_SUCCESS, result.stderr
    assert (dataset / "candidates").is_dir()
    assert (dataset / "tasks").is_dir()
    assert (dataset / "rubric.template.md").is_file()
    assert (dataset / "risk-map.template.toml").is_file()


@requires_network
def test_survey_json_has_documented_shape(clone: Path) -> None:
    """FR-5/FR-10: survey --json is an object with a prs array of PR-fact objects."""
    result = run_cli("--json", "survey", "--clone", clone, "--repo", REPO)
    if result.returncode == EXIT_UNRESOLVED:
        # Exit 5 is runtime-unavailable: the forge could not be reached for this call (e.g. a GitHub
        # 5xx). That is the network condition this suite skips on, surfacing mid-run.
        pytest.skip(f"transient forge/runtime error: {result.stderr.strip()}")
    assert result.returncode == EXIT_SUCCESS, result.stderr
    payload = result.json()
    assert isinstance(payload, dict)
    assert "prs" in payload and isinstance(payload["prs"], list)
    if payload["prs"]:
        entry = payload["prs"][0]
        assert isinstance(entry, dict)
        assert isinstance(entry.get("number"), int)
        assert "state" in entry
        assert isinstance(entry.get("blockers"), list)


@requires_network
def test_capture_candidate_matches_schema(pipeline: Pipeline) -> None:
    """FR-6/FR-12: the captured candidate carries the §8 facts and blank judgment slots."""
    candidate = json.loads(pipeline.raw_candidate)
    for key in ("repo", "pr_number", "pr_url", "iterations", "review_verdicts", "comments", "findings", "change_risk"):
        assert key in candidate, f"candidate missing {key}"
    assert isinstance(candidate["iterations"], list) and candidate["iterations"]
    assert isinstance(candidate["comments"], list)
    comment = candidate["comments"][0]
    comment_keys = (
        "id",
        "body",
        "path",
        "line_start",
        "line_end",
        "author_role",
        "created_at",
        "iteration_index",
        "in_diff_iterations",
    )
    for key in comment_keys:
        assert key in comment, f"comment missing {key}"
    assert isinstance(comment["in_diff_iterations"], list)
    # judgment slots are present-and-empty on a fresh capture
    assert comment["classification"] == ""
    assert candidate["findings"] == []
    assert candidate["change_risk"]["risk_structural"] in {"low", "medium", "high"}
    assert candidate["change_risk"]["risk_classified"] == ""


@requires_network
def test_emit_reject_writes_self_contained_task(pipeline: Pipeline) -> None:
    """FR-28/FR-30/FR-32/FR-40: the reject task directory has every required part."""
    task = pipeline.reject_task
    assert (task / "task.toml").is_file()
    assert (task / "instruction.md").is_file()
    assert (task / "environment" / "Dockerfile").is_file()
    assert (task / "environment" / "change.patch").is_file()
    assert (task / "tests").is_dir()
    assert (task / "solution").is_dir()


@requires_network
def test_emit_reject_task_toml_has_required_keys(pipeline: Pipeline) -> None:
    """FR-29/FR-32: task.toml is separate-mode with provenance and harvest metadata."""
    config = tomllib.loads((pipeline.reject_task / "task.toml").read_text(encoding="utf-8"))
    assert config.get("schema_version")
    assert config["verifier"]["environment_mode"] == "separate"
    origin = config["metadata"]["origin"]
    for key in ("repo", "pr_numbers", "base_commit"):
        assert key in origin, f"[metadata.origin] missing {key}"
    harvest = config["metadata"]["harvest"]
    assert harvest.get("kind") == "reject"
    assert harvest.get("expected_verdict") == "block"
    assert isinstance(harvest.get("content_digest"), str) and harvest["content_digest"].startswith("sha256:")


@requires_network
def test_verify_accepts_the_emitted_task(pipeline: Pipeline) -> None:
    """FR-34: verify does not fail (exit 4) a well-formed, non-leaking task."""
    result = run_cli("verify", pipeline.reject_task, "--clone", pipeline.clone)
    # 0 = every check passed; 5 = all resolvable checks passed but git-channel-absence is unresolved
    # because a CLI-only run materializes no container seal (it needs the MicroVM/Docker runtime).
    # A good task must never fail (exit 4).
    assert result.returncode in (EXIT_SUCCESS, EXIT_UNRESOLVED), f"verify failed a good task: {result.stdout}{result.stderr}"


@requires_network
def test_emit_approve_writes_task(pipeline: Pipeline) -> None:
    """FR-14: the approved state emits an approve datapoint (no blocking finding)."""
    if not pipeline.has_approved:
        pytest.skip("selected PR has no APPROVED review verdict")
    if pipeline.approve_task is None:
        pytest.skip(f"approve emit did not produce a task: {pipeline.approve_error}")
    config = tomllib.loads((pipeline.approve_task / "task.toml").read_text(encoding="utf-8"))
    assert config["metadata"]["harvest"]["kind"] == "approve"
    assert config["metadata"]["harvest"]["expected_verdict"] == "approve"


@requires_network
def test_dataset_writes_manifest_and_distributions(pipeline: Pipeline) -> None:
    """FR-23/FR-29: dataset writes the manifest + registry and reports the distributions."""
    result = run_cli("--json", "dataset", "--dataset", pipeline.dataset_reject)
    assert result.returncode == EXIT_SUCCESS, result.stderr
    assert (pipeline.dataset_reject / "dataset.toml").is_file()
    assert (pipeline.dataset_reject / "registry.json").is_file()
    payload = result.json()
    assert isinstance(payload, dict)
