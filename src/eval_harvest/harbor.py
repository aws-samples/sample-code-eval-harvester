"""The Harbor serializer: source-verified constants, the task.toml builder, and the validators.

Everything here was verified against Harbor 0.22.0 *source* rather than its documentation, because
the two disagree in load-bearing places — eighteen of Harbor's own doc claims were falsified by
reading its source. This module carries the source-verified constants, the ``task.toml``
value-tree builder, the schema validators, and the dataset-manifest/registry builders in a
stdlib-only form. It carries no heavy machinery: no pydantic (ADR-2 — the validators are plain
functions returning violation lists, and that shape is what to keep), no third-party imports, and
no ``CompileError``/wire-protocol. Serialization goes through
``tomlw.py`` (D-1); assembling the actual task *directory* is ``emit.py`` (D-3), which calls the
builders and refuses on any violation these validators return.

The five Harbor-source facts that shape this module:

- **The exit code is ignored; Harbor reads only a reward file** — so ``separate`` verifier mode is
  mandatory, because in ``shared`` mode ``/logs/verifier`` is agent-writable and the evaluated
  agent can pre-plant a passing reward. :meth:`Harbor.validate_task_config` refuses ``shared``
  (FR-32).
- **Unknown ``task.toml`` keys load and then vanish** (``TaskConfig`` defaults ``extra="ignore"``),
  so provenance lives under ``[metadata.*]`` — the only nested placement that survives a Harbor
  rewrite — and the validator flags any other top-level key.
- **Timeouts are floats**; an integer-valued float without its ``.0`` fails Harbor's schema, so
  they are emitted through :class:`~eval_harvest.tomlw.TomlFloat`.
- **Dataset digests are ``sha256:``-prefixed**; a bare-hex digest is rejected.
- **The mirror would drift silently** — :data:`SCHEMA_MIRROR_SOURCE` records the Harbor version
  this schema was taken from so a later drift check has something to compare against (§4 risk).

The emitted document follows tech-plan §8 exactly, which is *not* an earlier ``AcceptedTask``
shape: ``[metadata.origin]`` for provenance and ``[metadata.harvest]`` for this project's fields.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from eval_harvest.tomlw import TomlFloat, TomlValue

HARBOR_TARGET: Final = "harbor"

#: The value Harbor 0.22.0's ``TaskConfig`` defaults ``schema_version`` to (``config.py:796``).
#: Inert — never validated, nothing branches on it — but emitted so the file is complete (§8).
TASK_SCHEMA_VERSION: Final = "1.4"

#: The Harbor release this module's schema mirror was taken from. The drift anchor (§4 risk): a
#: later check against a real Harbor checkout asserts the installed version still matches this.
SCHEMA_MIRROR_SOURCE: Final = "harbor 0.22.0"

TASK_CONFIG_FILENAME: Final = "task.toml"
DATASET_MANIFEST_FILENAME: Final = "dataset.toml"
INSTRUCTION_FILENAME: Final = "instruction.md"

#: Harbor's own ``task.toml`` emission order (``config.py:908-968``). The document this module
#: builds follows §8's ordering rather than this, but the *set* of known top-level sections is what
#: :meth:`Harbor.validate_task_config` checks a document's keys against.
TASK_SECTION_ORDER: Final = (
    "task",
    "steps",
    "metadata",
    "verifier",
    "agent",
    "environment",
    "solution",
)

#: The exact network-mode strings (``config.py:36-41``). A differently-spelled one would fail
#: Harbor's schema at run time rather than at emission.
NETWORK_MODES: Final = ("no-network", "public", "allowlist")

#: ``constants.py:23`` plus the ``".." not in name`` rule (``config.py:313-322``).
_ORG_NAME_PATTERN: Final = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$")

#: ``manifest.py:31,44-53``. Bare-hex digests are rejected by Harbor; the manifest carries the
#: ``sha256:``-prefixed form.
_DATASET_DIGEST_PATTERN: Final = re.compile(r"^sha256:[a-f0-9]{64}$")

#: Compose service names (``config.py:20``).
_SERVICE_PATTERN: Final = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

#: The default agent/verifier timeouts (§8). Floats, because Harbor's schema demands it.
_DEFAULT_TIMEOUT_SEC: Final = 1800.0

#: The default task version and keywords the §8 example ships.
_DEFAULT_TASK_VERSION: Final = "1.0.0"
_DEFAULT_KEYWORDS: Final = ("pr-review", "eval-harvest")

#: Why the two long schema-violation details below are module constants rather than literals at the
#: raise site: a multi-line string sitting inside a list literal reads as a list of forgotten commas,
#: so the message is named here and the raise site stays one line per argument.
_SHARED_VERIFIER_REFUSED_DETAIL: Final = (
    "'shared' verifier mode is refused: /logs/verifier is agent-writable during the agent phase and "
    "Harbor does not clear it before a shared single-step verification, so the evaluated agent can "
    "pre-plant a passing reward file (config.py:599-610, FR-32)"
)
_SEPARATE_VERIFIER_IMAGE_DETAIL: Final = (
    "in separate mode the verifier image is built from tests/ and Harbor uploads nothing, so the "
    "image must self-provide /tests/test.sh (trial.py:612,686-693)"
)


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    """One way a value tree fails Harbor's schema: a stable code, a JSON-ish pointer, and why.

    Plain data — no third-party error type. The validators return a *list* of these and never
    raise, so ``emit`` (D-3) can report every problem at once rather than the first (ADR-2).
    """

    code: str
    pointer: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code} at {self.pointer}: {self.detail}"


@dataclass(frozen=True, slots=True)
class EmittedFile:
    """One file in a compiled task directory. Plain data; ``emit`` (D-3) fills the bytes."""

    path: str
    data: bytes = b""
    role: str = "agent"
    executable: bool = False


@dataclass(frozen=True, slots=True)
class CompiledTask:
    """A compiled task's identity and file list, as :meth:`Harbor.validate_task_layout` sees it.

    A structural stand-in for the old framework's ``CompiledTask``, carrying only what layout
    validation reads. ``emit`` (D-3) is what actually assembles the files.
    """

    name: str
    files: tuple[EmittedFile, ...]
    directory: str
    verifier_mode: str = "separate"
    required_directories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskOrigin:
    """The provenance that lands under ``[metadata.origin]`` (§8, FR-29). Timestamps are ISO text
    taken from the forge/commit data, never the wall clock (NFR-1)."""

    repo: str
    pr_numbers: tuple[int, ...]
    base_commit: str
    base_commit_date: str
    merged_at: str


@dataclass(frozen=True, slots=True)
class HarvestFields:
    """This project's fields under ``[metadata.harvest]`` (§8, FR-14/FR-19/FR-21/FR-27/FR-38)."""

    kind: str
    expected_verdict: str
    blocking_severity: str
    finding_severities: tuple[str, ...]
    change_risk_structural: str
    change_risk_classified: str
    change_risk_disagreement: bool
    rubric_version: str
    overrides: tuple[str, ...]


class Harbor:
    """Builds and validates Harbor artefacts against the mirrored 0.22.0 schema. Holds no state."""

    # ───────────────────────────── task.toml emission ─────────────────────────────

    @classmethod
    def task_config_document(  # noqa: PLR0913 — the §8 shape has this many knobs; each is a field
        cls,
        *,
        name: str,
        description: str,
        origin: TaskOrigin,
        harvest: HarvestFields,
        version: str = _DEFAULT_TASK_VERSION,
        keywords: Sequence[str] = _DEFAULT_KEYWORDS,
        agent_timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        verifier_timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        agent_network_mode: str = "no-network",
        verifier_network_mode: str = "no-network",
    ) -> dict[str, TomlValue]:
        """The ``task.toml`` value tree for one datapoint, in the tech-plan §8 shape.

        Insertion order *is* emission order (:func:`~eval_harvest.tomlw.emit_document` does no
        sorting), so the dict is assembled in §8's section order. The network defaults are
        ``no-network`` on both sides expressed as the default rather than an option: the agent
        environment must not reach the source forge (§3), and ``separate`` verifier mode is fixed
        because ``shared`` mode is agent-scoreable (FR-32). This function only *builds* the tree;
        the caller runs :meth:`validate_task_config` and refuses on any violation.
        """
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "task": {
                "name": name,
                "version": version,
                "description": description,
                "keywords": list(keywords),
            },
            "agent": {"timeout_sec": TomlFloat(agent_timeout_sec)},
            "verifier": {
                "timeout_sec": TomlFloat(verifier_timeout_sec),
                "environment_mode": "separate",
                "environment": {"network_mode": verifier_network_mode, "os": "linux"},
            },
            "environment": {"network_mode": agent_network_mode, "os": "linux"},
            "metadata": {
                "origin": cls._origin_table(origin),
                "harvest": cls._harvest_table(harvest),
            },
        }

    @staticmethod
    def _origin_table(origin: TaskOrigin) -> dict[str, TomlValue]:
        """``[metadata.origin]`` — Harbor's provenance shape (§8, FR-29)."""
        return {
            "repo": origin.repo,
            "pr_numbers": list(origin.pr_numbers),
            "base_commit": origin.base_commit,
            "base_commit_date": origin.base_commit_date,
            "merged_at": origin.merged_at,
        }

    @staticmethod
    def _harvest_table(harvest: HarvestFields) -> dict[str, TomlValue]:
        """``[metadata.harvest]`` — this project's fields Harbor preserves but does not validate."""
        return {
            "kind": harvest.kind,
            "expected_verdict": harvest.expected_verdict,
            "blocking_severity": harvest.blocking_severity,
            "finding_severities": list(harvest.finding_severities),
            "change_risk_structural": harvest.change_risk_structural,
            "change_risk_classified": harvest.change_risk_classified,
            "change_risk_disagreement": harvest.change_risk_disagreement,
            "rubric_version": harvest.rubric_version,
            "overrides": list(harvest.overrides),
        }

    # ──────────────────────────── task.toml validation ────────────────────────────

    @classmethod
    def validate_task_config(cls, document: Mapping[str, TomlValue]) -> list[SchemaViolation]:
        """Every way a ``task.toml`` value tree fails Harbor's schema (FR-28, FR-32).

        A structural mirror of ``harbor.models.task.config.TaskConfig``, not an import: Harbor is
        kept out of this process. What is checked is the set of constraints Harbor genuinely
        enforces, plus the two it *accepts and then erases* (an unknown top-level key and the
        deprecated ``version`` alias), which are worse than a rejection because they are silent.
        """
        violations: list[SchemaViolation] = []
        violations += cls._check_top_level_keys(document)
        violations += cls._check_task_section(document.get("task"))
        violations += cls._check_verifier_section(document.get("verifier"))
        violations += cls._check_environment_section(document.get("environment"), "environment")
        violations += cls._check_artifacts(document.get("artifacts"))
        violations += cls._check_reward_strategy(document.get("multi_step_reward_strategy"))
        return violations

    @staticmethod
    def _check_top_level_keys(document: Mapping[str, TomlValue]) -> list[SchemaViolation]:
        """Flag any top-level key Harbor would silently drop, and the deprecated ``version`` alias."""
        known_roots = {"schema_version", "source", "multi_step_reward_strategy", "artifacts", *TASK_SECTION_ORDER}
        violations: list[SchemaViolation] = []
        for key in sorted(document):
            if key == "version":
                violations.append(
                    SchemaViolation(
                        "compile.schema-invalid",
                        "version",
                        "top-level `version` is the deprecated alias Harbor silently renames to "
                        "schema_version (config.py:822-827); emit schema_version instead",
                    )
                )
            elif key not in known_roots:
                violations.append(
                    SchemaViolation(
                        "compile.metadata-placement-lost",
                        key,
                        f"{key!r} is not a TaskConfig field; extra='ignore' accepts it and "
                        "model_dump_toml erases it, so it would vanish on any Harbor rewrite "
                        "(config.py:908-968) — put provenance under [metadata.*]",
                    )
                )
        return violations

    @classmethod
    def _check_task_section(cls, value: TomlValue | None) -> list[SchemaViolation]:
        section = cls._as_table(value)
        if section is None:
            # The whole [task] section is optional; Harbor falls back to the directory name
            # (task.py:69-72). The builder always emits it, but a caller-assembled document may not.
            return []
        violations: list[SchemaViolation] = []
        name = section.get("name")
        if not isinstance(name, str) or not name:
            violations.append(
                SchemaViolation("compile.name-form", "task.name", "task.name is required within [task] (config.py:285-332)")
            )
        elif not _ORG_NAME_PATTERN.match(name) or ".." in name:
            violations.append(
                SchemaViolation("compile.name-form", "task.name", f"{name!r} does not match ORG_NAME_PATTERN (constants.py:23)")
            )
        task_version = section.get("version")
        if task_version is not None and (not isinstance(task_version, str) or not task_version):
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    "task.version",
                    "task.version has min_length=1 when present (config.py:295-299)",
                )
            )
        return violations

    @classmethod
    def _check_verifier_section(cls, value: TomlValue | None) -> list[SchemaViolation]:
        section = cls._as_table(value)
        if section is None:
            return []
        violations: list[SchemaViolation] = []
        violations += cls._check_verifier_mode(section.get("environment_mode"))
        nested = cls._as_table(section.get("environment"))
        if nested is not None:
            violations += cls._check_environment_section(nested, "verifier.environment")
        return violations

    @staticmethod
    def _check_verifier_mode(mode: TomlValue | None) -> list[SchemaViolation]:
        """``separate`` is mandatory; ``shared`` is refused because it is agent-scoreable (FR-32)."""
        if mode is None:
            return [
                SchemaViolation(
                    "compile.schema-invalid",
                    "verifier.environment_mode",
                    "environment_mode is required and must be 'separate' (config.py:554-558)",
                )
            ]
        if mode == "shared":
            return [
                SchemaViolation(
                    "compile.verifier-isolation-unavailable",
                    "verifier.environment_mode",
                    _SHARED_VERIFIER_REFUSED_DETAIL,
                )
            ]
        if mode != "separate":
            return [
                SchemaViolation(
                    "compile.schema-invalid",
                    "verifier.environment_mode",
                    f"{mode!r} is not 'separate' (config.py:554-558)",
                )
            ]
        return []

    @classmethod
    def _check_environment_section(cls, value: TomlValue | None, pointer: str) -> list[SchemaViolation]:
        section = cls._as_table(value)
        if section is None:
            return []
        violations: list[SchemaViolation] = []
        violations += cls._check_deprecated_environment_keys(section, pointer)
        violations += cls._check_os(section.get("os"), pointer)
        violations += cls._check_network(section, pointer)
        return violations

    @staticmethod
    def _check_deprecated_environment_keys(section: Mapping[str, TomlValue], pointer: str) -> list[SchemaViolation]:
        deprecated = (
            ("allow_internet", "deprecated and excluded from round-trip (config.py:509-515)"),
            ("memory", "deprecated in favor of memory_mb (config.py:517-532)"),
            ("storage", "deprecated in favor of storage_mb (config.py:534-549)"),
        )
        return [SchemaViolation("compile.schema-invalid", f"{pointer}.{key}", note) for key, note in deprecated if key in section]

    @staticmethod
    def _check_os(os_value: TomlValue | None, pointer: str) -> list[SchemaViolation]:
        if os_value is not None and os_value not in ("linux", "windows"):
            return [
                SchemaViolation(
                    "compile.schema-invalid", f"{pointer}.os", f"{os_value!r} is not 'linux' or 'windows' (config.py:271-275)"
                )
            ]
        return []

    @staticmethod
    def _check_network(section: Mapping[str, TomlValue], pointer: str) -> list[SchemaViolation]:
        violations: list[SchemaViolation] = []
        mode = section.get("network_mode")
        if mode is None:
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"{pointer}.network_mode",
                    "network_mode is NOT nullable and defaults to 'public'; omitting it means the "
                    "environment gets public egress (config.py:249-252)",
                )
            )
        elif mode not in NETWORK_MODES:
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"{pointer}.network_mode",
                    f"{mode!r} is not one of {list(NETWORK_MODES)} (config.py:36-41)",
                )
            )
        hosts = section.get("allowed_hosts")
        if hosts is not None and mode != "allowlist":
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"{pointer}.allowed_hosts",
                    "allowed_hosts is legal only when network_mode == 'allowlist' (config.py:72-78,189-208)",
                )
            )
        return violations

    @staticmethod
    def _check_reward_strategy(strategy: TomlValue | None) -> list[SchemaViolation]:
        if strategy is not None and strategy not in ("mean", "final"):
            return [
                SchemaViolation(
                    "compile.schema-invalid",
                    "multi_step_reward_strategy",
                    f"{strategy!r} is not 'mean' or 'final' (config.py:788-792)",
                )
            ]
        return []

    @classmethod
    def _check_artifacts(cls, value: TomlValue | None) -> list[SchemaViolation]:
        if value is None:
            return []
        if isinstance(value, str) or not isinstance(value, Sequence):
            return [
                SchemaViolation(
                    "compile.schema-invalid",
                    "artifacts",
                    "artifacts is a list of strings or inline tables (config.py:820)",
                )
            ]
        violations: list[SchemaViolation] = []
        for index, entry in enumerate(value):
            violations += cls._check_one_artifact(index, entry)
        return violations

    @classmethod
    def _check_one_artifact(cls, index: int, entry: TomlValue) -> list[SchemaViolation]:
        source, destination, service = cls._artifact_fields(entry)
        if source is None:
            return [
                SchemaViolation(
                    "compile.schema-invalid",
                    f"artifacts/{index}",
                    "an artifact entry is a bare string or an inline table (config.py:639-722)",
                )
            ]
        violations: list[SchemaViolation] = []
        violations += cls._check_artifact_source(index, source, service)
        violations += cls._check_artifact_destination(index, destination)
        return violations

    @classmethod
    def _artifact_fields(cls, entry: TomlValue) -> tuple[str | None, str | None, str | None]:
        """A ``(source, destination, service)`` triple, or ``(None, ...)`` for a malformed entry."""
        if isinstance(entry, str):
            return entry, None, None
        table = cls._as_table(entry)
        if table is None:
            return None, None, None
        raw_source = table.get("source")
        source = raw_source if isinstance(raw_source, str) else ""
        raw_destination = table.get("destination")
        destination = raw_destination if isinstance(raw_destination, str) else None
        raw_service = table.get("service")
        service = raw_service if isinstance(raw_service, str) else None
        return source, destination, service

    @staticmethod
    def _check_artifact_source(index: int, source: str, service: str | None) -> list[SchemaViolation]:
        violations: list[SchemaViolation] = []
        if not source:
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"artifacts/{index}/source",
                    "source is required on an artifact entry (config.py:660)",
                )
            )
        elif ".." in source.split("/"):
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"artifacts/{index}/source",
                    "an artifact source must not contain '..' (config.py:660-672)",
                )
            )
        if service is not None and not _SERVICE_PATTERN.match(service):
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"artifacts/{index}/service",
                    f"{service!r} is not a compose service name (config.py:655-658)",
                )
            )
        elif service is not None and service != "main" and source and not source.startswith("/"):
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"artifacts/{index}/source",
                    "a sidecar artifact source must be an absolute path (config.py:712-722)",
                )
            )
        return violations

    @staticmethod
    def _check_artifact_destination(index: int, destination: str | None) -> list[SchemaViolation]:
        if destination is None:
            return []
        violations: list[SchemaViolation] = []
        if destination.startswith("/") or "\\" in destination:
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"artifacts/{index}/destination",
                    "a destination is relative with forward slashes (config.py:674-710)",
                )
            )
        if destination == "manifest.json":
            violations.append(
                SchemaViolation(
                    "compile.schema-invalid",
                    f"artifacts/{index}/destination",
                    "'manifest.json' is reserved (config.py:674-710)",
                )
            )
        return violations

    # ─────────────────────────────── layout validation ───────────────────────────────

    @classmethod
    def validate_task_layout(cls, task: CompiledTask) -> list[SchemaViolation]:
        """Every way a task directory fails Harbor's own validity rules (FR-28).

        Requiredness is where a compiler written from the docs goes wrong: only ``task.toml`` and
        the *existence* of ``environment/`` are unconditional; ``instruction.md`` is required
        single-step and absent multi-step; the verifier image source depends on the mode.
        """
        has_steps = cls._task_has_prefix(task, "steps/")
        violations: list[SchemaViolation] = []
        violations += cls._check_required_files(task, has_steps=has_steps)
        violations += cls._check_environment_dir(task)
        violations += cls._check_verifier_image_source(task)
        violations += cls._check_forbidden_files(task)
        return violations

    @staticmethod
    def _task_has_file(task: CompiledTask, path: str) -> bool:
        return any(emitted.path == path for emitted in task.files)

    @staticmethod
    def _task_has_prefix(task: CompiledTask, prefix: str) -> bool:
        return any(emitted.path.startswith(prefix) for emitted in task.files)

    @classmethod
    def _check_required_files(cls, task: CompiledTask, *, has_steps: bool) -> list[SchemaViolation]:
        violations: list[SchemaViolation] = []
        if not cls._task_has_file(task, TASK_CONFIG_FILENAME):
            violations.append(
                SchemaViolation(
                    "compile.layout-invalid",
                    TASK_CONFIG_FILENAME,
                    "task.toml is the only unconditionally required file (task.py:102)",
                )
            )
        if has_steps and cls._task_has_file(task, INSTRUCTION_FILENAME):
            violations.append(
                SchemaViolation(
                    "compile.layout-invalid",
                    INSTRUCTION_FILENAME,
                    "a multi-step task's root instruction.md is intentionally absent; Harbor sets "
                    "Task.instruction to '' when steps are declared (task.py:76-77)",
                )
            )
        if not has_steps and not cls._task_has_file(task, INSTRUCTION_FILENAME):
            violations.append(
                SchemaViolation(
                    "compile.layout-invalid",
                    INSTRUCTION_FILENAME,
                    "instruction.md is required for a single-step task (task.py:128-132)",
                )
            )
        return violations

    @classmethod
    def _check_environment_dir(cls, task: CompiledTask) -> list[SchemaViolation]:
        if "environment" in task.required_directories or cls._task_has_prefix(task, "environment/"):
            return []
        return [
            SchemaViolation(
                "compile.layout-invalid",
                "environment/",
                "environment/ must exist as a directory even when empty; Task.is_valid_dir checks its existence (task.py:102)",
            )
        ]

    @classmethod
    def _check_verifier_image_source(cls, task: CompiledTask) -> list[SchemaViolation]:
        if task.verifier_mode == "separate":
            if cls._task_has_file(task, "tests/Dockerfile"):
                return []
            return [
                SchemaViolation(
                    "compile.layout-invalid",
                    "tests/Dockerfile",
                    _SEPARATE_VERIFIER_IMAGE_DETAIL,
                )
            ]
        if cls._task_has_file(task, "tests/test.sh"):
            return []
        return [
            SchemaViolation(
                "compile.layout-invalid",
                "tests/test.sh",
                "a shared-verifier task needs an OS-matched tests/test.sh (task.py:136-143)",
            )
        ]

    @staticmethod
    def _check_forbidden_files(task: CompiledTask) -> list[SchemaViolation]:
        violations: list[SchemaViolation] = []
        for emitted in task.files:
            if emitted.path.endswith((".ps1", ".cmd")):
                violations.append(
                    SchemaViolation(
                        "compile.layout-invalid",
                        emitted.path,
                        "Harbor's SUPPORTED_EXTENSIONS is ['.sh', '.bat'] only; .ps1 and .cmd appear "
                        "in docstrings but are never discovered (utils/scripts.py:23-25)",
                    )
                )
            if emitted.path == "environment/docker-compose.yml":
                violations.append(
                    SchemaViolation(
                        "compile.layout-invalid",
                        emitted.path,
                        "the shared COMPOSE_FILE_NAME is docker-compose.yaml; a .yml task fails "
                        "has_agent_environment_definition (definition.py:8)",
                    )
                )
        return violations

    # ─────────────────────────────── dataset emission ───────────────────────────────

    @staticmethod
    def metric_script(reward_keys: Sequence[str]) -> bytes:
        """A dataset ``metric.py`` that averages each reward key separately (ported from the emitter).

        Required, not optional, for a multi-key task: Harbor's default metric template raises unless
        every reward dict has exactly one key (``cli/template-metric/metric.py:17-21``), so a
        multi-dimensional dataset with no ``metric.py`` fails at scoring rather than at emission.

        Shipping it is *not* sufficient, and that is stated in the generated file: Harbor loads a
        dataset ``metric.py`` only on the hub-package path — ``is_local()``/``is_registry()``/
        ``is_repo()`` fall back to ``Mean()`` (``job.py:777-802``). Private publication targets exactly
        those local paths, so the script is emitted so the aggregation is *stated and runnable* even
        where Harbor will not run it, which is also why every reward key must be meaningful under a
        mean (§3). Keys are sorted so the output is a function of the key *set*, not a caller's order.
        """
        names = sorted(reward_keys)
        keys = ", ".join(repr(key) for key in names) + ("," if len(names) == 1 else "")
        return f'''# /// script
# dependencies = []
# ///
"""Generated by eval-harvest: per-key aggregation, never a collapse.

Harbor's default metric template raises on a reward dict with more than one key
(cli/template-metric/metric.py:17-21), and its built-in aggregation collapses to a single value
whenever it sees at most one key across all trials (metrics/base.py:14-36). Both would erase the
separation these dimensions exist to preserve. A missing key counts as 0, matching Harbor's own
aggregate_reward_dicts, so a failed trial does not silently raise the mean of the trials that ran.
"""

import argparse
import json
from pathlib import Path

KEYS = ({keys})


def main(input_path: Path, output_path: Path) -> None:
    rows = []
    for line in input_path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))

    out = {{}}
    for key in KEYS:
        values = [0 if row is None else row.get(key, 0) for row in rows]
        out[f"mean_{{key}}"] = sum(values) / len(values) if values else 0
    out["trials"] = len(rows)
    output_path.write_text(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input-path", type=Path, required=True)
    parser.add_argument("-o", "--output-path", type=Path, required=True)
    args = parser.parse_args()
    main(args.input_path, args.output_path)
'''.encode()

    @classmethod
    def dataset_manifest_document(
        cls,
        name: str,
        task_digests: Mapping[str, str],
        *,
        files: Sequence[str] = (),
        version: str = "",
        description: str = "",
    ) -> dict[str, TomlValue]:
        """The ``dataset.toml`` value tree (``manifest.py:161-177``).

        Exactly four top-level fields, and no place for provenance: ``DatasetManifest`` has
        ``schema_version``, ``dataset``, ``tasks``, ``files``. Dataset-level provenance therefore
        lives in *our* manifest beside this one, not squeezed into a field Harbor would ignore.
        Tasks and files are sorted so the document is a function of the *set*, not a caller's order.

        Raises:
            ValueError: when ``name`` is not Harbor's ``org/name`` form (a dataset that cannot be
                addressed is unrecoverable, so this fails fast rather than at publish time).
        """
        if not _ORG_NAME_PATTERN.match(name) or ".." in name:
            raise ValueError(f"{name!r} is not Harbor's org/name form for a dataset (manifest.py:117,139-148)")
        dataset: dict[str, TomlValue] = {"name": name, "description": description}
        if version:
            dataset["version"] = version
        document: dict[str, TomlValue] = {
            "schema_version": "1.0",
            "dataset": dataset,
            "tasks": [{"name": task_name, "digest": task_digests[task_name]} for task_name in sorted(task_digests)],
        }
        if files:
            # `harbor add` restricts [[files]] to metric.py; the digest is auto-filled at publish
            # time, which we do ourselves because there is no Harbor publish on the private path.
            document["files"] = [{"path": path, "digest": ""} for path in sorted(files)]
        return document

    @classmethod
    def validate_dataset_manifest(cls, document: Mapping[str, TomlValue]) -> list[SchemaViolation]:
        """Every way a ``dataset.toml`` value tree fails ``DatasetManifest``."""
        violations: list[SchemaViolation] = []
        violations += cls._check_dataset_info(cls._as_table(document.get("dataset")))
        violations += cls._check_dataset_tasks(document.get("tasks"))
        violations += cls._check_dataset_files(document.get("files"))
        return violations

    @staticmethod
    def _check_dataset_info(dataset: Mapping[str, TomlValue] | None) -> list[SchemaViolation]:
        if dataset is None:
            return [SchemaViolation("compile.schema-invalid", "dataset", "[dataset] is required (manifest.py:172)")]
        violations: list[SchemaViolation] = []
        name = dataset.get("name")
        if not isinstance(name, str) or not _ORG_NAME_PATTERN.match(name) or ".." in name:
            violations.append(
                SchemaViolation("compile.name-form", "dataset.name", f"{name!r} is not org/name (manifest.py:139-148)")
            )
        version = dataset.get("version")
        if version is not None and (not isinstance(version, str) or not version):
            violations.append(
                SchemaViolation("compile.schema-invalid", "dataset.version", "min_length=1 when present (manifest.py:118-122)")
            )
        return violations

    @classmethod
    def _check_dataset_tasks(cls, tasks: TomlValue | None) -> list[SchemaViolation]:
        if not isinstance(tasks, Sequence) or isinstance(tasks, str):
            return []
        violations: list[SchemaViolation] = []
        for index, entry in enumerate(tasks):
            table = cls._as_table(entry)
            if table is None:
                continue
            name = table.get("name")
            if not isinstance(name, str) or not _ORG_NAME_PATTERN.match(name):
                violations.append(
                    SchemaViolation("compile.name-form", f"tasks/{index}/name", f"{name!r} is not org/name (manifest.py:33-42)")
                )
            digest = table.get("digest")
            if not isinstance(digest, str) or not _DATASET_DIGEST_PATTERN.match(digest):
                violations.append(
                    SchemaViolation(
                        "compile.schema-invalid",
                        f"tasks/{index}/digest",
                        f"{digest!r} does not match ^sha256:[a-f0-9]{{64}}$; the digest is REQUIRED "
                        "and strictly validated (manifest.py:31,44-53)",
                    )
                )
        return violations

    @classmethod
    def _check_dataset_files(cls, files: TomlValue | None) -> list[SchemaViolation]:
        if not isinstance(files, Sequence) or isinstance(files, str):
            return []
        violations: list[SchemaViolation] = []
        for index, entry in enumerate(files):
            table = cls._as_table(entry)
            if table is None:
                continue
            path = table.get("path")
            if not isinstance(path, str) or "/" in path or "\\" in path:
                violations.append(
                    SchemaViolation(
                        "compile.schema-invalid", f"files/{index}/path", f"{path!r} is not a bare filename (manifest.py:86-94)"
                    )
                )
        return violations

    @staticmethod
    def dataset_content_hash(task_digests: Mapping[str, str], files: Mapping[str, str]) -> str:
        """Harbor's client-side dataset content hash (``manifest.py:245-262``).

        Order-insensitive (task digests are sorted) and duplicate-sensitive (a repeated ref changes
        it); it covers neither the dataset name nor its description, authors, keywords, or README.
        """
        digests = sorted(digest.removeprefix("sha256:") for digest in task_digests.values())
        base = ",".join(digests)
        if files:
            base += ";" + ",".join(f"{path}:{files[path].removeprefix('sha256:')}" for path in sorted(files))
        return f"sha256:{hashlib.sha256(base.encode()).hexdigest()}"

    @classmethod
    def local_registry_document(
        cls,
        *,
        name: str,
        version: str,
        description: str,
        task_directories: Sequence[str],
        root: str = ".",
    ) -> dict[str, object]:
        """A ``registry.json`` entry Harbor's ``--registry-path`` consumption path accepts.

        The private-publication counterpart to the hosted hub: writing this is how the artefacts we
        control become a dataset Harbor can run with no hub, no auth, and no network. A dataset
        name containing a slash is unreachable through this path (``is_package()`` routes it to the
        hosted client), so the entry uses the dataset's *short* name while ``dataset.toml`` keeps
        the ``org/name`` its schema requires.

        Raises:
            ValueError: when ``name`` is not Harbor's ``org/name`` form.
        """
        if not _ORG_NAME_PATTERN.match(name) or ".." in name:
            raise ValueError(f"{name!r} is not Harbor's org/name form (constants.py:23, config.py:313-322)")
        _, _, short = name.partition("/")
        clean_root = root.rstrip("/")
        return {
            "name": short,
            "version": version or "1.0",
            "description": description,
            "tasks": [{"name": directory, "path": f"{clean_root}/{directory}"} for directory in sorted(task_directories)],
        }

    # ─────────────────────────────────── helpers ───────────────────────────────────

    @staticmethod
    def _as_table(value: object) -> dict[str, TomlValue] | None:
        """``value`` as a TOML table, or ``None``.

        ``object`` in rather than :data:`~eval_harvest.tomlw.TomlValue`: the alias is recursive and
        a checker cannot narrow a value out of it by ``isinstance`` (the recursive arm survives), so
        these validators read ``object`` — a parsed document's values genuinely are ``object`` until
        checked, and checking is what they are for. Keys are re-keyed to ``str`` so that a
        caller-assembled ``{1: ...}`` reaching a section check is a caught error, not a hidden cast.
        """
        if not isinstance(value, Mapping):
            return None
        return {str(key): item for key, item in value.items()}
