"""Find a coding task's fail-to-pass and pass-to-pass tests from Harbor's nop and oracle runs of it.

``code`` cannot know which of the hidden tests the change makes pass: that takes running them. The task
does the running itself: before ``tests/expected.json`` exists, its grader records every test's status
and gives no reward. So a coding task is calibrated with the same Harbor path it is benchmarked on:

    harbor run -p <task> -a nop    ...   # the base: every hidden test as it is before the change
    harbor run -p <task> -a oracle ...   # the gold patch applied
    eval-harvest calibrate <task> --nop <nop job dir> --oracle <oracle job dir>

A test is fail-to-pass if it passed in no nop run and in every oracle run, and pass-to-pass if it
passed in every run of both. A test whose result changed between runs of the same agent is left out:
a flaky required test would make the task unsolvable at random. ``calibrate`` reads files only.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final  # Any: results.json is arbitrary JSON until narrowed to a status table below.

from eval_harvest.codetask import CALIBRATION_BEGIN, CALIBRATION_END, CodingRefusal

_NO_TESTS_REPORTED: Final = "(no tests reported)"
_MAX_LISTED: Final = 40


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """The tests a calibrated task requires, and the runs they came from. Plain data."""

    task_dir: Path
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    left_out: tuple[str, ...]
    nop_trials: int
    oracle_trials: int


class CodingTaskCalibration:
    """Turns nop and oracle trial results into ``tests/expected.json``. Holds no state."""

    @classmethod
    def calibrate(cls, task_dir: Path, nop_jobs: Path, oracle_jobs: Path) -> CalibrationResult | CodingRefusal:
        """Write ``tests/expected.json`` and list the fail-to-pass tests in ``instruction.md``, or say why not."""
        if not (task_dir / "tests" / "grader.py").is_file():
            return CodingRefusal("not-a-coding-task", str(task_dir), "pass a task directory that eval-harvest code wrote")
        task_id = cls.task_id(task_dir)
        nop = cls.trial_statuses(nop_jobs, task_dir, task_id)
        oracle = cls.trial_statuses(oracle_jobs, task_dir, task_id)
        if not nop or not oracle:
            missing = "nop" if not nop else "oracle"
            return CodingRefusal(
                "no-calibration-runs",
                f"no {missing} trial of {task_dir.name} with verifier/results.json",
                f"run `harbor run -p {task_dir} -a {missing} ...` and pass its job directory",
            )
        fail_to_pass, pass_to_pass, left_out = cls.classify(nop, oracle)
        if not fail_to_pass:
            return CodingRefusal(
                "no-fail-to-pass",
                f"{len(pass_to_pass)} tests pass with and without the change",
                "the hidden tests do not tell the change apart; this PR is not a coding task",
            )
        expected = {"fail_to_pass": list(fail_to_pass), "pass_to_pass": list(pass_to_pass)}
        (task_dir / "tests" / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        cls.list_tests_in_instruction(task_dir / "instruction.md", fail_to_pass)
        return CalibrationResult(task_dir, fail_to_pass, pass_to_pass, left_out, len(nop), len(oracle))

    @staticmethod
    def task_id(task_dir: Path) -> str:
        """The task's Harbor id (``[task] name`` in task.toml), or empty when it has none."""
        try:
            document: Any = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return ""
        task = document.get("task") if isinstance(document, dict) else None
        return str(task.get("name") or "") if isinstance(task, dict) else ""

    @classmethod
    def trial_statuses(cls, jobs_dir: Path, task_dir: Path, task_id: str) -> list[dict[str, str]]:
        """The status table of every trial of the task under ``jobs_dir``."""
        tables: list[dict[str, str]] = []
        for results in sorted(jobs_dir.rglob("verifier/results.json")):
            if cls.trial_is_of_task(results.parent.parent, task_dir, task_id):
                table = cls.status_table(results)
                if table:
                    tables.append(table)
        return tables

    @classmethod
    def trial_is_of_task(cls, trial_dir: Path, task_dir: Path, task_id: str) -> bool:
        """True when Harbor's own files say the trial ran this task.

        Harbor cuts a trial folder's name to the first 32 characters of the task's, so a long task name
        (``pallets__itsdangerous__pr378-cod__A6nFS8d``) does not identify it. Its ``result.json`` names
        the task by id and its ``config.json`` by path; the folder name is the last resort.
        """
        result = cls.read_json_object(trial_dir / "result.json")
        if task_id and result.get("task_name") == task_id:
            return True
        task = cls.read_json_object(trial_dir / "config.json").get("task")
        if isinstance(task, dict) and Path(str(task.get("path") or "")).name == task_dir.name:
            return True
        return trial_dir.name.startswith(f"{task_dir.name}__")

    @staticmethod
    def read_json_object(path: Path) -> dict[str, Any]:
        """A JSON object from ``path``, or empty when it is missing or not an object."""
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def status_table(path: Path) -> dict[str, str]:
        """The ``statuses`` table a grader wrote, or empty if the file is not one."""
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        statuses = raw.get("statuses") if isinstance(raw, dict) else None
        if not isinstance(statuses, dict):
            return {}
        return {str(test): str(status) for test, status in statuses.items()}

    @staticmethod
    def classify(
        nop: list[dict[str, str]], oracle: list[dict[str, str]]
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """(fail-to-pass, pass-to-pass, left out because their result changed between runs of one agent)."""
        tests = sorted({test for table in nop + oracle for test in table if not test.endswith(_NO_TESTS_REPORTED)})
        fail_to_pass, pass_to_pass, left_out = [], [], []
        for test in tests:
            base = [table.get(test) == "passed" for table in nop]
            gold = [table.get(test) == "passed" for table in oracle]
            if all(gold) and not any(base):
                fail_to_pass.append(test)
            elif all(gold) and all(base):
                pass_to_pass.append(test)
            elif any(gold) != all(gold) or any(base) != all(base):
                left_out.append(test)
        return tuple(fail_to_pass), tuple(pass_to_pass), tuple(left_out)

    @staticmethod
    def list_tests_in_instruction(instruction: Path, fail_to_pass: tuple[str, ...]) -> None:
        """Put the fail-to-pass test names between the calibration markers, replacing an earlier list."""
        text = instruction.read_text(encoding="utf-8")
        begin, end = text.find(CALIBRATION_BEGIN), text.find(CALIBRATION_END)
        if begin < 0 or end < begin:
            return
        shown = [CodingTaskCalibration.display_name(test) for test in fail_to_pass[:_MAX_LISTED]]
        more = [f"- and {len(fail_to_pass) - _MAX_LISTED} more in the same files"] if len(fail_to_pass) > _MAX_LISTED else []
        block = "\n".join(
            [
                CALIBRATION_BEGIN,
                "These tests must pass. Their names, as the test runner reports them:",
                "",
                *shown,
                *more,
                "",
                "Tests in those files that pass today must keep passing.",
            ]
        )
        instruction.write_text(text[:begin] + block + "\n" + text[end:], encoding="utf-8")

    @staticmethod
    def display_name(test: str) -> str:
        """``- name > sub (`file`)`` from a ``file::suite::name`` test id."""
        file, _, name = test.partition("::")
        return f"- {name.replace('::', ' > ')} (`{file}`)"
