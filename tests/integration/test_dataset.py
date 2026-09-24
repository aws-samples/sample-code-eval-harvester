"""Tests for the `dataset` verb (task F-1): the Harbor manifest, the registry, and the distribution.

`dataset` is a read-only aggregator over the emitted `tasks/` — it makes no forge/git call and no
judgment. These scenarios pin the properties the customer's automation decision rests on (US-4): the
risk distribution counts each datapoint by its classified change risk and surfaces the count where
structural and classified disagree (FR-23), the severity distribution counts every finding, the
emitted `dataset.toml` is a manifest Harbor would accept (FR-29), an empty dataset refuses rather
than shipping a meaningless manifest, a datapoint missing its `content_digest` fails loudly rather
than getting one invented, and the whole assembly is byte-stable (NFR-1). A closing end-to-end case
runs `dataset` over a *real* `emit` output so the manifest, the recorded digest, and the shipped
`metric.py` are exercised against the bytes `emit` actually writes, not a hand-built fixture.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fixtures.repo_builder import RepoBuilder

from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.cli import Cli, ExitCode
from eval_harvest.dataset import Dataset, DatasetRefusalError
from eval_harvest.emit import REWARD_KEYS, Emit
from eval_harvest.forge import Forge
from eval_harvest.harbor import Harbor
from eval_harvest.riskmap import RiskMap
from eval_harvest.tomlw import emit_document

_REPO = "our-org/our-repo"


def _write_task(  # noqa: PLR0913 — a fixture writer; each argument is one field of the task it fakes
    dataset_dir: Path,
    slug: str,
    *,
    classified: str,
    structural: str,
    severities: tuple[str, ...],
    repo: str = _REPO,
    digest: str | None = None,
    disagreement: bool | None = None,
) -> None:
    """Write one `tasks/<slug>/task.toml` carrying only the sections `dataset` reads.

    `digest` defaults to a real `sha256:`-prefixed digest derived from the slug (so the manifest
    validates); `disagreement` defaults to `structural != classified`, matching what `emit` records.
    """
    task_name = f"{repo}__{slug}"
    document: dict[str, Any] = {
        "schema_version": "1.4",
        "task": {"name": task_name, "version": "1.0.0"},
        "metadata": {
            "origin": {"repo": repo, "pr_numbers": [1]},
            "harvest": {
                "kind": "reject",
                "content_digest": digest if digest is not None else "sha256:" + hashlib.sha256(slug.encode()).hexdigest(),
                "change_risk_structural": structural,
                "change_risk_classified": classified,
                "change_risk_disagreement": disagreement if disagreement is not None else structural != classified,
                "finding_severities": list(severities),
            },
        },
    }
    task_dir = dataset_dir / "tasks" / slug
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_bytes(emit_document(document))


def _mixed_dataset(dataset_dir: Path) -> None:
    """A dataset of five datapoints whose classified and structural distributions deliberately differ.

    Classified risk buckets to {low: 3, medium: 1, high: 1}; structural to {low: 3, medium: 2, high: 0}
    — so a report that counted the wrong field would produce visibly different numbers, and the
    disagreement count (pr2 low/medium, pr5 high/low) is exercised independently of either bucketing.
    """
    _write_task(dataset_dir, "pr1-reject", classified="low", structural="low", severities=("high", "medium"))
    _write_task(dataset_dir, "pr2-reject", classified="low", structural="medium", severities=("medium",))
    _write_task(dataset_dir, "pr3-approve", classified="low", structural="low", severities=("low",))
    _write_task(dataset_dir, "pr4-reject", classified="medium", structural="medium", severities=("high",))
    _write_task(dataset_dir, "pr5-reject", classified="high", structural="low", severities=("high", "low"))


# ───────────────────────────── distributions (FR-23) ─────────────────────────────


def test_risk_distribution_counts_correct(tmp_path: Path) -> None:
    """Counts per change-risk level match the tasks' classified-risk metadata (FR-23)."""
    _mixed_dataset(tmp_path)

    report = Dataset.build_dataset(tmp_path)

    assert report.datapoints == 5
    assert report.risk_counts == {"low": 3, "medium": 1, "high": 1}


def test_severity_distribution_counts_correct(tmp_path: Path) -> None:
    """Counts per finding-severity level are the flattened finding counts across every task (FR-23)."""
    _mixed_dataset(tmp_path)

    report = Dataset.build_dataset(tmp_path)

    # findings: high×3 (pr1,pr4,pr5), medium×2 (pr1,pr2), low×2 (pr3,pr5).
    assert report.severity_counts == {"low": 2, "medium": 2, "high": 3}


def test_disagreement_count_surfaced(tmp_path: Path) -> None:
    """Datapoints where structural and classified disagree are counted, not collapsed (US-4)."""
    _mixed_dataset(tmp_path)

    report = Dataset.build_dataset(tmp_path)

    # pr2 (low vs high) and pr5 (high vs low) disagree; the other three agree.
    assert report.disagreements == 2
    assert report.as_json()["risk_distribution"] == {"low": 3, "medium": 1, "high": 1, "disagreements": 2}


# ───────────────────────────── manifest + registry (FR-29) ─────────────────────────────


def test_manifest_validates(tmp_path: Path) -> None:
    """The emitted `dataset.toml` passes `Harbor.validate_dataset_manifest` (a manifest `harbor run` accepts)."""
    _mixed_dataset(tmp_path)

    Dataset.build_dataset(tmp_path)

    document = tomllib.loads((tmp_path / "dataset.toml").read_text(encoding="utf-8"))
    assert Harbor.validate_dataset_manifest(document) == []
    # Every emitted task is referenced by its org/name id and its recorded sha256 digest.
    names = {entry["name"] for entry in document["tasks"]}
    assert names == {f"{_REPO}__pr{n}-{'approve' if n == 3 else 'reject'}" for n in (1, 2, 3, 4, 5)}


def test_registry_written_and_points_at_tasks(tmp_path: Path) -> None:
    """`registry.json` names the short dataset and points each entry at its task directory."""
    _mixed_dataset(tmp_path)

    Dataset.build_dataset(tmp_path)

    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert registry["name"] == "our-repo"
    paths = {entry["path"] for entry in registry["tasks"]}
    assert paths == {f"./tasks/pr{n}-{'approve' if n == 3 else 'reject'}" for n in (1, 2, 3, 4, 5)}


def test_metric_script_shipped(tmp_path: Path) -> None:
    """`dataset` ships a `metric.py` stating the per-key aggregation, identical to what `emit` writes."""
    _mixed_dataset(tmp_path)

    Dataset.build_dataset(tmp_path)

    assert (tmp_path / "metric.py").read_bytes() == Harbor.metric_script(REWARD_KEYS)


# ───────────────────────────── refusals ─────────────────────────────


def test_empty_dataset_refuses_no_datapoints(tmp_path: Path) -> None:
    """An empty dataset refuses `no-datapoints` (exit 3) and writes no manifest."""
    (tmp_path / "tasks").mkdir()

    with pytest.raises(DatasetRefusalError) as refusal:
        Dataset.build_dataset(tmp_path)
    assert refusal.value.check == "no-datapoints"
    assert refusal.value.exit_code == ExitCode.REFUSAL
    assert not (tmp_path / "dataset.toml").exists(), "no manifest may be written on a refusal"


def test_empty_dataset_cli_exit_code(tmp_path: Path) -> None:
    """The `dataset` verb maps `no-datapoints` onto exit 3 end-to-end (§9)."""
    (tmp_path / "tasks").mkdir()

    assert Cli.run(["dataset", "--dataset", str(tmp_path)]) == ExitCode.REFUSAL


def test_missing_content_digest_refuses(tmp_path: Path) -> None:
    """A task with no recorded `content_digest` fails loudly rather than getting one invented."""
    _write_task(tmp_path, "pr1-reject", classified="low", structural="low", severities=("high",), digest="")

    with pytest.raises(DatasetRefusalError) as refusal:
        Dataset.build_dataset(tmp_path)
    assert refusal.value.check == "missing-content-digest"
    assert refusal.value.exit_code == ExitCode.REFUSAL


# ───────────────────────────── determinism (NFR-1) ─────────────────────────────


def test_dataset_deterministic(tmp_path: Path) -> None:
    """The manifest, registry, metric, and report are byte-stable for the same set of tasks (NFR-1)."""
    _mixed_dataset(tmp_path)

    first_report = Dataset.build_dataset(tmp_path)
    artefacts = {name: (tmp_path / name).read_bytes() for name in ("dataset.toml", "registry.json", "metric.py")}
    second_report = Dataset.build_dataset(tmp_path)

    for name, first_bytes in artefacts.items():
        assert (tmp_path / name).read_bytes() == first_bytes, f"{name} is not byte-stable across two runs"
    assert first_report == second_report


# ───────────────────────────── end-to-end over a real emit ─────────────────────────────


def _substantive_findings() -> list[FindingDict]:
    """A reject oracle: one high defect and one medium, each bound to a captured comment."""
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


def _emit_one_reject_datapoint(tmp_path: Path) -> Path:
    """Reconstruct the offline squash-merge fixture, fill the candidate, and emit one reject datapoint."""
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
    risk_map: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}
    (dataset / "risk-map.toml").write_bytes(emit_document(risk_map))
    (dataset / "rubric.md").write_text("---\nversion: v1\n---\n# Review rubric\n", encoding="utf-8")
    risk_structural, rule = RiskMap.structural_risk(Candidate.changed_paths(facts), risk_map)
    Candidate.write_to_dataset(
        facts,
        repo=_REPO,
        pr_url=f"https://github.com/{_REPO}/pull/{fixture.pr_number}",
        dataset_dir=dataset,
        risk_structural=risk_structural,
        risk_structural_rule=rule,
    )
    candidate_path = dataset / "candidates" / f"pr-{fixture.pr_number}.json"
    candidate: CandidateDict = Candidate.load(candidate_path)
    for comment in candidate["comments"]:
        comment["classification"] = "defect"
        comment["classification_rationale"] = "a substantive problem a reviewer would block on"
    candidate["findings"] = _substantive_findings()
    candidate["change_risk"]["risk_classified"] = "low"
    candidate["change_risk"]["risk_classified_rationale"] = "isolated helper, well covered by tests"
    candidate["rubric_version"] = "v1"
    Candidate.dump(candidate, candidate_path)
    Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)
    return dataset


def test_dataset_over_real_emit_output(tmp_path: Path) -> None:
    """`dataset` reads a real `emit` output: the manifest validates against the digest `emit` recorded."""
    dataset = _emit_one_reject_datapoint(tmp_path)

    report = Dataset.build_dataset(dataset)

    assert report.datapoints == 1
    # The high/medium reject oracle lands in the severity distribution; the classified risk is `low`.
    assert report.risk_counts == {"low": 1, "medium": 0, "high": 0}
    assert report.severity_counts == {"low": 0, "medium": 1, "high": 1}

    manifest = tomllib.loads((dataset / "dataset.toml").read_text(encoding="utf-8"))
    assert Harbor.validate_dataset_manifest(manifest) == []
    # The manifest's digest is exactly the one emit stamped into the task's task.toml (not invented).
    task_toml = next((dataset / "tasks").glob("*/task.toml"))
    recorded_digest = tomllib.loads(task_toml.read_text(encoding="utf-8"))["metadata"]["harvest"]["content_digest"]
    assert manifest["tasks"][0]["digest"] == recorded_digest
