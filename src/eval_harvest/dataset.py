"""The `dataset` verb: assemble the Harbor manifest and report the risk/severity distribution.

Once `emit` has written a handful of task directories, the customer needs two things before they
trust any score (tech plan §9, US-4): a Harbor dataset manifest so `harbor run` can consume the
whole set, and a report of *how the datapoints are distributed by risk and severity* — because the
decision they actually care about is whether they have enough **low-risk** datapoints to justify
switching automated approval on, and a number computed over a dataset that is mostly high-risk
changes answers the wrong question.

This module reads every `tasks/*/task.toml` (a read-only aggregator — no forge call, no git call,
no judgment), collects each task's `content_digest` (recorded by `emit`, D-3) and its
`[metadata.harvest]` risk/severity fields, and:

- writes `dataset.toml` (the Harbor manifest, FR-29) via :meth:`Harbor.dataset_manifest_document`,
- writes a local `registry.json` via :meth:`Harbor.local_registry_document` so the set is runnable
  on Harbor's `--registry-path` consumption path with no hub, no auth, and no network,
- (re)writes the dataset `metric.py` via :meth:`Harbor.metric_script` so the per-key aggregation is
  *stated and runnable* even though Harbor's local path ignores it and falls back to `Mean()` (§3),
- and prints the **risk distribution** (count per change-risk level, plus the count where structural
  and classified risk disagree) and the **severity distribution** (count per finding-severity
  level) (FR-23).

Determinism (NFR-1): the manifest, the registry, the metric, and the report are byte-stable
functions of the tasks present — task lists are sorted by their builders, the report counts are
order-independent, and nothing here reads the wall clock.

**Digests are not invented.** F-1 does not define the per-task content-digest scheme — it reads
what `emit` recorded under `[metadata.harvest].content_digest`. A task missing its digest is a
broken input, and this refuses loudly rather than fabricating one (FR-23, task F-1 §What To Build).

The dataset's Harbor name is derived from the datapoints' shared origin repo (`[metadata.origin].repo`,
already `org/name` form): v1 mines one repository at a time (the workflow is "point at *your*
repository"), so a dataset that mixes origin repos is outside the v1 model and refuses rather than
silently pick one.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eval_harvest.emit import REWARD_KEYS
from eval_harvest.harbor import DATASET_MANIFEST_FILENAME, Harbor
from eval_harvest.riskmap import RISK_LEVELS
from eval_harvest.tomlw import emit_document

#: Exit codes carried by `DatasetRefusalError`, pinned to the tech-plan §9 values (`cli.ExitCode`).
#: Restated as an int rather than importing `cli` (which imports this module); `tests/test_cli.py`
#: and the CLI's own `ExitCode` lock the same number, so a drift is caught there.
_REFUSAL_EXIT_CODE: Final = 3

#: The dataset artefacts this verb writes at the dataset root.
_REGISTRY_FILENAME: Final = "registry.json"
_METRIC_FILENAME: Final = "metric.py"

#: The subdirectory `emit` promotes each verified datapoint into.
_TASKS_DIRNAME: Final = "tasks"

#: The default registry version when none is derived (`local_registry_document` uses "1.0" for "").
_DEFAULT_REGISTRY_VERSION: Final = "1.0"


class DatasetRefusalError(Exception):
    """A refusal `dataset` raises for the CLI to render in the FR-2 four-field shape with its exit code."""

    # Five attributes because FR-2 fixes the four-field shape and each refusal also carries its
    # exit-code class; the CLI maps them straight onto `Cli.refuse`.
    def __init__(self, *, check: str, datapoint: str, offending: str, next_: str, exit_code: int = _REFUSAL_EXIT_CODE) -> None:
        super().__init__(f"{check}: {offending}")
        self.check = check
        self.datapoint = datapoint
        self.offending = offending
        self.next_ = next_
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class _TaskRecord:
    """One emitted datapoint as the aggregator reads it: its identity, digest, and risk/severity fields.

    `task_name` is Harbor's `org/name` id from `[task].name` (the manifest key); `directory` is the
    on-disk directory name (the registry key); `content_digest` is the `sha256:`-prefixed digest
    `emit` recorded. The three risk fields and `finding_severities` come from `[metadata.harvest]`.
    """

    task_name: str
    directory: str
    origin_repo: str
    content_digest: str
    change_risk_structural: str
    change_risk_classified: str
    change_risk_disagreement: bool
    finding_severities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetReport:
    """The risk and severity distribution over a dataset's datapoints (FR-23), plus the datapoint count.

    `risk_counts` buckets every datapoint by its **classified** change risk — the agent's considered
    holistic judgment of the change's danger, of which the structural path-rule risk is one input;
    `disagreements` counts, separately and without collapsing, the datapoints where structural and
    classified risk disagree, so the contested calls are never hidden (decisions log: "records the
    disagreement rather than picking a winner"). `severity_counts` buckets every *finding* across all
    datapoints by its severity, so its total is the finding count, not the datapoint count.
    """

    datapoints: int
    risk_counts: dict[str, int]
    disagreements: int
    severity_counts: dict[str, int]

    def as_json(self) -> dict[str, object]:
        """The `--json` object (tech plan §9): risk (with the disagreement count folded in), severity, count."""
        risk_distribution: dict[str, int] = dict(self.risk_counts)
        risk_distribution["disagreements"] = self.disagreements
        return {
            "risk_distribution": risk_distribution,
            "severity_distribution": dict(self.severity_counts),
            "datapoints": self.datapoints,
        }

    def render_human(self) -> str:
        """The default human rendering: the risk distribution, the disagreement count, then severity."""
        lines = [f"datapoints: {self.datapoints}", "change-risk distribution (by classified risk):"]
        lines += [f"  {level}: {self.risk_counts[level]}" for level in RISK_LEVELS]
        lines.append(f"  structural/classified disagreements: {self.disagreements}")
        lines.append("finding-severity distribution:")
        lines += [f"  {level}: {self.severity_counts[level]}" for level in RISK_LEVELS]
        return "\n".join(lines) + "\n"


class Dataset:
    """Read every emitted task, assemble the Harbor manifest + registry + metric, and report. Holds no state."""

    @classmethod
    def build_dataset(cls, dataset_dir: Path) -> DatasetReport:
        """Assemble `dataset.toml`, `registry.json`, `metric.py`, and return the distribution report.

        Reads every `tasks/*/task.toml`, refusing `no-datapoints` (exit 3) on an empty dataset and
        loudly on any task missing its `content_digest`. Writes the three artefacts at the dataset
        root (byte-stable, NFR-1) and returns the risk/severity distribution for the CLI to print.
        """
        records = cls._read_task_records(dataset_dir)
        dataset_name = cls._dataset_name(records)
        cls._write_manifest(dataset_dir, dataset_name, records)
        cls._write_registry(dataset_dir, dataset_name, records)
        cls._write_metric(dataset_dir)
        return cls._report(records)

    # ───────────────────────────── reading the tasks ─────────────────────────────

    @classmethod
    def _read_task_records(cls, dataset_dir: Path) -> list[_TaskRecord]:
        """Every emitted datapoint under `tasks/`, sorted by directory name; refuses `no-datapoints` if none."""
        tasks_dir = dataset_dir / _TASKS_DIRNAME
        task_dirs = sorted(path for path in tasks_dir.glob("*") if (path / "task.toml").is_file()) if tasks_dir.is_dir() else []
        if not task_dirs:
            raise DatasetRefusalError(
                check="no-datapoints",
                datapoint=str(dataset_dir),
                offending=f"no emitted task directories under {tasks_dir}",
                next_="run `eval-harvest emit <candidate> --kind reject|approve` to build at least one datapoint first",
            )
        return [cls._read_one_task_record(task_dir) for task_dir in task_dirs]

    @classmethod
    def _read_one_task_record(cls, task_dir: Path) -> _TaskRecord:
        """Parse one `task.toml` into a `_TaskRecord`, refusing loudly on a missing name or digest."""
        document = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        task_section = cls._table(document.get("task"))
        metadata = cls._table(document.get("metadata"))
        origin = cls._table(metadata.get("origin"))
        harvest = cls._table(metadata.get("harvest"))
        datapoint = task_dir.name
        return _TaskRecord(
            task_name=cls._require_str(task_section.get("name"), "task.name", datapoint),
            directory=task_dir.name,
            origin_repo=cls._require_str(origin.get("repo"), "metadata.origin.repo", datapoint),
            content_digest=cls._require_digest(harvest.get("content_digest"), datapoint),
            change_risk_structural=cls._require_risk(harvest.get("change_risk_structural"), "change_risk_structural", datapoint),
            change_risk_classified=cls._require_risk(harvest.get("change_risk_classified"), "change_risk_classified", datapoint),
            change_risk_disagreement=cls._as_bool(harvest.get("change_risk_disagreement")),
            finding_severities=cls._severities(harvest.get("finding_severities"), datapoint),
        )

    @staticmethod
    def _table(value: object) -> dict[str, object]:
        """`value` as a parsed-TOML table, or an empty table — the missing-field paths handle emptiness."""
        return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}

    @staticmethod
    def _require_str(value: object, pointer: str, datapoint: str) -> str:
        """A non-empty string at `pointer`, or a loud refusal — a datapoint with no identity is broken."""
        if isinstance(value, str) and value:
            return value
        raise DatasetRefusalError(
            check="malformed-task",
            datapoint=datapoint,
            offending=f"{pointer} is missing or not a non-empty string in this task's task.toml",
            next_="re-run `eval-harvest emit` for this datapoint; do not hand-edit task.toml",
        )

    @classmethod
    def _require_risk(cls, value: object, pointer: str, datapoint: str) -> str:
        """A risk level (one of RISK_LEVELS) at `pointer`, or a loud refusal."""
        risk = cls._require_str(value, pointer, datapoint)
        if risk not in RISK_LEVELS:
            raise DatasetRefusalError(
                check="malformed-task",
                datapoint=datapoint,
                offending=f"{pointer} is {risk!r}, not one of {list(RISK_LEVELS)}",
                next_="re-run `eval-harvest emit` for this datapoint; do not hand-edit task.toml",
            )
        return risk

    @staticmethod
    def _require_digest(value: object, datapoint: str) -> str:
        """The `content_digest` `emit` recorded — required, never invented (task F-1 §What To Build)."""
        if isinstance(value, str) and value:
            return value
        raise DatasetRefusalError(
            check="missing-content-digest",
            datapoint=datapoint,
            offending="this task's [metadata.harvest] has no content_digest; the manifest cannot reference it",
            next_="re-run `eval-harvest emit` for this datapoint so the content_digest is recorded",
        )

    @staticmethod
    def _as_bool(value: object) -> bool:
        """The recorded `change_risk_disagreement` flag; anything non-boolean reads as no disagreement."""
        return value if isinstance(value, bool) else False

    @staticmethod
    def _severities(value: object, datapoint: str) -> tuple[str, ...]:
        """The `finding_severities` list as a tuple of risk-level strings; refuses on an off-scale entry."""
        if not isinstance(value, list):
            return ()
        severities: list[str] = []
        for entry in value:
            if not isinstance(entry, str) or entry not in RISK_LEVELS:
                raise DatasetRefusalError(
                    check="malformed-task",
                    datapoint=datapoint,
                    offending=f"finding_severities contains {entry!r}, not one of {list(RISK_LEVELS)}",
                    next_="re-run `eval-harvest emit` for this datapoint; do not hand-edit task.toml",
                )
            severities.append(entry)
        return tuple(severities)

    # ───────────────────────────── the dataset name ─────────────────────────────

    @staticmethod
    def _dataset_name(records: list[_TaskRecord]) -> str:
        """The dataset's Harbor `org/name`: the datapoints' shared origin repo (v1 mines one repo).

        A dataset that mixes origin repos is outside the v1 model, so this refuses rather than pick a
        name silently — the manifest and registry each need exactly one addressable name.
        """
        repos = sorted({record.origin_repo for record in records})
        if len(repos) != 1:
            raise DatasetRefusalError(
                check="mixed-origin-repos",
                datapoint=", ".join(repos),
                offending=f"the datapoints come from {len(repos)} repos ({repos}); a v1 dataset is one repository",
                next_="build one dataset per repository (one `--dataset` dir per repo)",
            )
        return repos[0]

    # ───────────────────────────── writing the artefacts ─────────────────────────────

    @staticmethod
    def _write_manifest(dataset_dir: Path, dataset_name: str, records: list[_TaskRecord]) -> None:
        """Write `dataset.toml` — the Harbor manifest keyed by each task's `org/name` and content digest."""
        task_digests = {record.task_name: record.content_digest for record in records}
        document = Harbor.dataset_manifest_document(dataset_name, task_digests, files=(_METRIC_FILENAME,))
        (dataset_dir / DATASET_MANIFEST_FILENAME).write_bytes(emit_document(document))

    @classmethod
    def _write_registry(cls, dataset_dir: Path, dataset_name: str, records: list[_TaskRecord]) -> None:
        """Write `registry.json` — the local `--registry-path` entry pointing at each task directory."""
        document = Harbor.local_registry_document(
            name=dataset_name,
            version=_DEFAULT_REGISTRY_VERSION,
            description="",
            task_directories=[record.directory for record in records],
            root=f"./{_TASKS_DIRNAME}",
        )
        text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        (dataset_dir / _REGISTRY_FILENAME).write_bytes(text.encode("utf-8"))

    @staticmethod
    def _write_metric(dataset_dir: Path) -> None:
        """(Re)write the dataset `metric.py` stating the per-key aggregation (identical to what `emit` ships)."""
        (dataset_dir / _METRIC_FILENAME).write_bytes(Harbor.metric_script(REWARD_KEYS))

    # ───────────────────────────── the distribution report ─────────────────────────────

    @staticmethod
    def _report(records: list[_TaskRecord]) -> DatasetReport:
        """Count datapoints per classified risk level, disagreements, and findings per severity level."""
        risk_counts = {level: 0 for level in RISK_LEVELS}
        severity_counts = {level: 0 for level in RISK_LEVELS}
        disagreements = 0
        for record in records:
            risk_counts[record.change_risk_classified] += 1
            if record.change_risk_disagreement:
                disagreements += 1
            for severity in record.finding_severities:
                severity_counts[severity] += 1
        return DatasetReport(
            datapoints=len(records),
            risk_counts=risk_counts,
            disagreements=disagreements,
            severity_counts=severity_counts,
        )
