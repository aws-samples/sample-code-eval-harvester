"""``calibrate`` finds a task's trials by what Harbor records, not by the folder name Harbor cuts short.

Harbor names a trial folder after the first 32 characters of the task's folder, then ``__<id>``. A
task folder such as ``pallets__itsdangerous__pr378-code`` (33 characters) therefore gets trials named
``pallets__itsdangerous__pr378-cod__A6nFS8d``; its ``result.json`` names the task by id. Seen on a real
AgentCore + Harbor run, where calibrate first found no trial at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval_harvest.calibrate import CodingTaskCalibration
from eval_harvest.codetask import CALIBRATION_BEGIN, CALIBRATION_END, CodingRefusal

_TASK = "pallets__itsdangerous__pr378-code"
_TASK_ID = "pallets/itsdangerous__pr378-code"


def _task(root: Path) -> Path:
    task_dir = root / "tasks" / _TASK
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "tests" / "grader.py").write_text("# the grader\n")
    (task_dir / "task.toml").write_text(f'[task]\nname = "{_TASK_ID}"\n')
    (task_dir / "instruction.md").write_text(f"# t\n\n{CALIBRATION_BEGIN}\n{CALIBRATION_END}\n")
    return task_dir


def _trial(jobs: Path, folder: str, task_id: str, statuses: dict[str, str]) -> None:
    (jobs / folder / "verifier").mkdir(parents=True)
    (jobs / folder / "result.json").write_text(json.dumps({"task_name": task_id, "trial_name": folder}))
    (jobs / folder / "verifier" / "results.json").write_text(json.dumps({"statuses": statuses}))


class TestCalibrationTrials:
    def test_a_trial_folder_cut_short_by_harbor_is_found_by_its_result(self, tmp_path: Path) -> None:
        task_dir = _task(tmp_path)
        cut = _TASK[:32]
        _trial(tmp_path / "nop", f"{cut}__A6nFS8d", _TASK_ID, {"t.py::fixed": "failed", "t.py::kept": "passed"})
        _trial(tmp_path / "oracle", f"{cut}__dzzBHGq", _TASK_ID, {"t.py::fixed": "passed", "t.py::kept": "passed"})
        # Another task whose folder starts with the same 32 characters must not be counted.
        _trial(tmp_path / "oracle", f"{cut}__other01", "pallets/itsdangerous__pr378-cods", {"t.py::fixed": "failed"})
        result = CodingTaskCalibration.calibrate(task_dir, tmp_path / "nop", tmp_path / "oracle")
        assert not isinstance(result, CodingRefusal), result
        assert (result.fail_to_pass, result.pass_to_pass, result.oracle_trials) == (("t.py::fixed",), ("t.py::kept",), 1)

    def test_a_trial_with_no_harbor_files_still_matches_by_its_full_folder_name(self, tmp_path: Path) -> None:
        task_dir = _task(tmp_path)
        for kind, status in (("nop", "failed"), ("oracle", "passed")):
            (tmp_path / kind / f"{_TASK}__x" / "verifier").mkdir(parents=True)
            (tmp_path / kind / f"{_TASK}__x" / "verifier" / "results.json").write_text(
                json.dumps({"statuses": {"t.py::f": status}})
            )
        result = CodingTaskCalibration.calibrate(task_dir, tmp_path / "nop", tmp_path / "oracle")
        assert not isinstance(result, CodingRefusal), result
        assert result.fail_to_pass == ("t.py::f",)
