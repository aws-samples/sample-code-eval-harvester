"""Tests for the dataset's Harbor `org/name`, derived from the datapoints' shared origin repo.

`[metadata.origin].repo` keeps the project's full path as provenance — a nested GitLab group is the
project's identity — but the manifest name must be Harbor's exactly-one-slash `org/name`. So the
name folds nested segments the same way an emitted task name does, and the folded name is one
Harbor's manifest builder accepts.
"""

from __future__ import annotations

from eval_harvest.dataset import Dataset, _TaskRecord
from eval_harvest.harbor import Harbor


def _record(origin_repo: str) -> _TaskRecord:
    return _TaskRecord(
        task_name="unused/unused",
        directory="unused",
        origin_repo=origin_repo,
        content_digest="sha256:" + "0" * 64,
        change_risk_structural="low",
        change_risk_classified="low",
        change_risk_disagreement=False,
        finding_severities=(),
    )


def test_a_two_segment_origin_repo_is_the_dataset_name_unchanged() -> None:
    assert Dataset._dataset_name([_record("our-org/our-repo")]) == "our-org/our-repo"


def test_a_nested_gitlab_origin_repo_folds_into_a_harbor_shaped_dataset_name() -> None:
    dataset_name = Dataset._dataset_name([_record("platform/payments/api")])

    assert dataset_name == "platform/payments__api"
    Harbor.dataset_manifest_document(dataset_name, {}, files=())
