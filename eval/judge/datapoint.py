"""Load a built datapoint (a Harbor task directory) into the :class:`~eval.judge.models.Datapoint`.

A produced datapoint is what ``eval-harvest emit`` writes and what a G-1 trial captures under
``…/task/tasks/<name>__<kind>/``: ``task.toml`` (with the ``[metadata.harvest]`` table), the
agent-visible ``instruction.md`` and ``environment/change.patch``, and the verifier-only
``tests/oracle.json``.
This reader turns that directory into the read-only view the quality judge grades — stdlib only
(``tomllib`` + ``json``), no model and no network here.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

from eval.judge.models import Datapoint, OracleFindingView
from eval_harvest.verify import CHANGE_PATCH_RELPATH


class DatapointLoader:
    """Reads a built task directory into a :class:`Datapoint`. Holds no state."""

    @classmethod
    def load(cls, task_dir: Path) -> Datapoint:
        """Load the datapoint at ``task_dir`` (the directory holding ``task.toml``) for grading.

        Raises ``FileNotFoundError`` if the required files are absent — a directory that is not a
        built datapoint is a caller error, not something to paper over with empty defaults."""
        harvest = cls._harvest_table(task_dir)
        oracle = cls._oracle_document(task_dir)
        return Datapoint(
            directory_name=task_dir.name,
            kind=cast("Any", harvest["kind"]),
            expected_verdict=cast("Any", harvest["expected_verdict"]),
            blocking_severity=cast("Any", harvest.get("blocking_severity", "high")),
            instruction=(task_dir / "instruction.md").read_text(encoding="utf-8"),
            change_patch=(task_dir / CHANGE_PATCH_RELPATH).read_text(encoding="utf-8"),
            oracle_findings=cls._oracle_findings(oracle),
            change_risk_structural=cast("Any", harvest.get("change_risk_structural")),
            change_risk_classified=cast("Any", harvest.get("change_risk_classified")),
        )

    @staticmethod
    def _harvest_table(task_dir: Path) -> dict[str, Any]:
        """The ``[metadata.harvest]`` table from ``task.toml`` — where emit records the datapoint's labels."""
        document: dict[str, Any] = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        metadata = document.get("metadata", {})
        harvest = metadata.get("harvest", {})
        if not harvest:
            raise ValueError(f"{task_dir}/task.toml has no [metadata.harvest]; it is not an eval-harvest datapoint")
        return cast("dict[str, Any]", harvest)

    @staticmethod
    def _oracle_document(task_dir: Path) -> dict[str, Any]:
        """The parsed ``tests/oracle.json`` — the reference findings the datapoint grades against."""
        parsed: dict[str, Any] = json.loads((task_dir / "tests" / "oracle.json").read_text(encoding="utf-8"))
        return parsed

    @staticmethod
    def _oracle_findings(oracle: dict[str, Any]) -> tuple[OracleFindingView, ...]:
        """The oracle findings as read-only views, dropping the file/line spans the judge does not grade."""
        findings: list[dict[str, Any]] = oracle.get("findings", [])
        return tuple(
            OracleFindingView(
                statement=str(finding["statement"]),
                severity=cast("Any", finding["severity"]),
                reference_only=bool(finding["reference_only"]),
                blocking=bool(finding["blocking"]),
            )
            for finding in findings
        )
