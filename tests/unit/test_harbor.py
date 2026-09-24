"""Tests for the Harbor serializer: constants, the §8 task.toml builder, and the validators (D-2).

Every case here defends one Harbor-source fact that a compiler re-derived from Harbor's docs gets
wrong (see reference/harbor_emitter.py's module docstring). The document builder is held to the
tech-plan §8 shape; the validators are held to the constraints Harbor genuinely enforces, plus the
two it accepts-and-erases (which are worse because they are silent).
"""

import tomllib
from typing import Any

from eval_harvest.harbor import (
    SCHEMA_MIRROR_SOURCE,
    CompiledTask,
    EmittedFile,
    Harbor,
    HarvestFields,
    TaskOrigin,
)
from eval_harvest.tomlw import emit_document


def _origin() -> TaskOrigin:
    return TaskOrigin(
        repo="our-org/our-repo",
        pr_numbers=(1234,),
        base_commit="abc123def4567890abc123def4567890abc123de",
        base_commit_date="2026-08-01T14:22:00Z",
        merged_at="2026-08-03T09:10:00Z",
    )


def _harvest() -> HarvestFields:
    return HarvestFields(
        kind="reject",
        expected_verdict="block",
        blocking_severity="high",
        finding_severities=("high", "medium", "medium"),
        change_risk_structural="high",
        change_risk_classified="low",
        change_risk_disagreement=True,
        rubric_version="v1",
        overrides=(),
    )


def _valid_document() -> dict[str, Any]:
    """A correct §8 task.toml, round-tripped through tomlw + tomllib.

    A parsed TOML document is genuinely ``dict[str, Any]`` (that is tomllib's return type), so the
    ``Any`` here is honest, not a shortcut: the validators exist precisely to check these values.
    """
    document = Harbor.task_config_document(
        name="our-org/our-repo__pr1234-reject",
        description="Review the change on this pull-request iteration.",
        origin=_origin(),
        harvest=_harvest(),
    )
    return tomllib.loads(emit_document(document).decode())


class TestTaskConfigDocument:
    """The §8 shape the builder emits, and that it survives a tomlw + tomllib round trip."""

    def test_valid_config_has_no_violations(self) -> None:
        # A false-positive validator that blocked a correct task would make the whole pipeline
        # unusable, so the happy path is pinned first.
        assert Harbor.validate_task_config(_valid_document()) == []

    def test_timeout_is_float(self) -> None:
        # Harbor's timeout_sec is a float; an int (1800) fails its schema. The wrapper must keep
        # the decimal point through emission.
        document = Harbor.task_config_document(
            name="our-org/our-repo__pr1234-reject",
            description="d",
            origin=_origin(),
            harvest=_harvest(),
        )
        emitted = emit_document(document)
        assert b"timeout_sec = 1800.0\n" in emitted
        parsed = tomllib.loads(emitted.decode())
        assert isinstance(parsed["agent"]["timeout_sec"], float)
        assert isinstance(parsed["verifier"]["timeout_sec"], float)

    def test_metadata_preserved_under_metadata_namespace(self) -> None:
        # Harbor's TaskConfig has extra="ignore": an unknown top-level key loads then vanishes on
        # any rewrite. Provenance must therefore live under [metadata.*], never at the top level.
        document = _valid_document()
        assert document["metadata"]["origin"]["repo"] == "our-org/our-repo"
        assert document["metadata"]["harvest"]["kind"] == "reject"
        # None of the provenance fields may leak to a top-level key Harbor would drop.
        for dropped_key in ("repo", "pr_numbers", "base_commit", "kind", "expected_verdict"):
            assert dropped_key not in document


class TestValidateTaskConfig:
    """Every way a task.toml value tree fails — the schema Harbor enforces, mirrored structurally."""

    def test_shared_mode_rejected(self) -> None:
        # shared verifier mode lets the evaluated agent pre-plant a passing reward file into
        # /logs/verifier (config.py:599-610). It must never validate clean (FR-32).
        document = _valid_document()
        document["verifier"]["environment_mode"] = "shared"
        violations = Harbor.validate_task_config(document)
        assert any(violation.pointer == "verifier.environment_mode" for violation in violations)

    def test_unknown_top_level_key_flagged(self) -> None:
        # A top-level key that is not a TaskConfig field loads and then silently vanishes; the
        # validator surfaces it rather than letting provenance disappear.
        document = _valid_document()
        document["repo"] = "our-org/our-repo"
        violations = Harbor.validate_task_config(document)
        assert any(violation.pointer == "repo" for violation in violations)

    def test_deprecated_version_alias_flagged(self) -> None:
        document = _valid_document()
        document["version"] = "1.0"
        violations = Harbor.validate_task_config(document)
        assert any(violation.pointer == "version" for violation in violations)


class TestValidateTaskLayout:
    """A task directory that only fails at `harbor run` must fail here instead (FR-28)."""

    def test_layout_requires_all_parts(self) -> None:
        # Only task.toml is present: instruction.md, environment/, and the separate-mode
        # tests/Dockerfile are all missing and each must be named.
        task = CompiledTask(
            name="our-org/our-repo__pr1234-reject",
            files=(EmittedFile("task.toml"),),
            directory="our-repo__pr1234-reject",
            verifier_mode="separate",
            required_directories=(),
        )
        violations = Harbor.validate_task_layout(task)
        missing = {violation.pointer for violation in violations}
        assert "instruction.md" in missing
        assert "environment/" in missing
        assert "tests/Dockerfile" in missing

    def test_complete_layout_has_no_violations(self) -> None:
        task = CompiledTask(
            name="our-org/our-repo__pr1234-reject",
            files=(
                EmittedFile("task.toml"),
                EmittedFile("instruction.md"),
                EmittedFile("environment/Dockerfile"),
                EmittedFile("tests/Dockerfile"),
            ),
            directory="our-repo__pr1234-reject",
            verifier_mode="separate",
            required_directories=("environment",),
        )
        assert Harbor.validate_task_layout(task) == []


class TestSchemaMirror:
    """The drift anchor: an untraceable mirror is one nobody can compare to a real Harbor."""

    def test_schema_mirror_source_recorded(self) -> None:
        assert isinstance(SCHEMA_MIRROR_SOURCE, str)
        assert SCHEMA_MIRROR_SOURCE


class TestDatasetManifest:
    """The dataset.toml builder and validator F-1 builds on, and Harbor's content hash."""

    def test_manifest_round_trips_and_validates(self) -> None:
        manifest = Harbor.dataset_manifest_document(
            "our-org/pr-review-eval",
            {"our-org/our-repo__pr1234-reject": f"sha256:{'a' * 64}"},
        )
        parsed = tomllib.loads(emit_document(manifest).decode())
        assert Harbor.validate_dataset_manifest(parsed) == []

    def test_manifest_rejects_bare_hex_digest(self) -> None:
        # Harbor rejects a bare-hex digest; only the sha256:-prefixed form is accepted
        # (manifest.py:31,44-53).
        manifest = Harbor.dataset_manifest_document(
            "our-org/pr-review-eval",
            {"our-org/our-repo__pr1234-reject": "a" * 64},
        )
        violations = Harbor.validate_dataset_manifest(manifest)
        assert any("digest" in violation.pointer for violation in violations)

    def test_content_hash_is_order_insensitive_and_duplicate_sensitive(self) -> None:
        one = {"a/x": f"sha256:{'1' * 64}", "b/y": f"sha256:{'2' * 64}"}
        reordered = {"b/y": f"sha256:{'2' * 64}", "a/x": f"sha256:{'1' * 64}"}
        assert Harbor.dataset_content_hash(one, {}) == Harbor.dataset_content_hash(reordered, {})
        duplicated = {"a/x": f"sha256:{'1' * 64}", "b/y": f"sha256:{'1' * 64}"}
        assert Harbor.dataset_content_hash(one, {}) != Harbor.dataset_content_hash(duplicated, {})


class TestLocalRegistry:
    """The registry.json entry Harbor's --registry-path path consumes (no hub, no network)."""

    def test_registry_uses_short_name_and_task_paths(self) -> None:
        # A dataset name with a slash is unreachable through --registry-path (it routes to the
        # hosted client), so the registry entry uses the short name.
        entry = Harbor.local_registry_document(
            name="our-org/pr-review-eval",
            version="1.0",
            description="pr-review eval",
            task_directories=("our-repo__pr1234-reject", "our-repo__pr99-approve"),
        )
        assert entry["name"] == "pr-review-eval"
        assert entry["tasks"] == [
            {"name": "our-repo__pr1234-reject", "path": "./our-repo__pr1234-reject"},
            {"name": "our-repo__pr99-approve", "path": "./our-repo__pr99-approve"},
        ]
