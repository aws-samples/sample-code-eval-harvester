"""Offline tests for `HarborCliExecutor`'s `harbor run` invocation.

The live executor shells out to `harbor run`, so its argv is a contract with a third party (Harbor
0.22.0) that nothing in the gate could see — three wrong flags shipped, one of them (`-d` for a local
path) fatal: it sends Harbor down registry resolution, which aborts in `abort_dataset_resolution`
with "No job was started". These tests pin the argv — positively and negatively — and assert that a
non-zero `harbor run` exit is recorded distinguishably rather than consumed as if the agent simply
produced nothing. All run offline with a stubbed `subprocess.run`; none needs Harbor or AWS.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from eval.agent import RecordedAgent
from eval.models import Objective
from eval.runner import _TRACE_FILENAME, LAMBDA_MICROVMS_IMPORT_PATH, HarborCliExecutor


def _executor(harbor_executable: str = "harbor") -> HarborCliExecutor:
    """A `HarborCliExecutor` with a fixed executable and agent, so its argv is deterministic."""
    return HarborCliExecutor(RecordedAgent("oracle"), "anthropic.claude-x", harbor_executable=harbor_executable)


def test_harbor_argv_uses_path_not_dataset(tmp_path: Path) -> None:
    """`-p` (local task directory) is present and `-d` (dataset name@version) is absent — the blocker."""
    argv = _executor()._build_argv(tmp_path)
    assert "-p" in argv
    assert "-d" not in argv
    assert argv[argv.index("-p") + 1] == str(tmp_path.resolve())


def test_harbor_argv_uses_env_not_deprecated_import_path(tmp_path: Path) -> None:
    """`-e`/`--env` is present with the import path; the deprecated `--environment-import-path` is gone."""
    argv = _executor()._build_argv(tmp_path)
    assert "-e" in argv
    assert "--environment-import-path" not in argv
    assert argv[argv.index("-e") + 1] == LAMBDA_MICROVMS_IMPORT_PATH


def test_harbor_argv_snapshot(tmp_path: Path) -> None:
    """The full argv, pinned so any flag drift — additions included — has to be deliberate."""
    argv = _executor()._build_argv(tmp_path)
    assert argv == [
        "harbor",
        "run",
        "-p",
        str(tmp_path.resolve()),
        "-m",
        "anthropic.claude-x",
        "-a",
        "oracle",
        "-e",
        LAMBDA_MICROVMS_IMPORT_PATH,
    ]


def test_task_path_is_passed_absolute_or_relative_as_harbor_expects(tmp_path: Path) -> None:
    """The `-p` value is absolute, so it resolves from `cwd=run_dir` rather than the repo root.

    `_invoke_harbor` runs with `cwd=run_dir` while the task directory is built under a repo-relative
    staging root — a live footgun the moment `-p` starts being honoured."""
    relative_task_dir = Path("eval/runs/_staging/obj/trial-0/task-definition")
    argv = _executor()._build_argv(relative_task_dir)
    passed = Path(argv[argv.index("-p") + 1])
    assert passed.is_absolute()
    assert passed == relative_task_dir.resolve()


@dataclass(frozen=True)
class _StubHarborRun:
    """Stand-in for the `subprocess.run` result the executor consumes (returncode/stdout/stderr only).

    Not `subprocess.CompletedProcess`: the audit rule flags any subprocess-imported callable regardless
    of args, and this is a fixture return value that never spawns a process.
    """

    returncode: int
    stdout: str
    stderr: str


def _stub_completed(returncode: int) -> _StubHarborRun:
    """A `harbor run` result: the recorded abort text on a non-zero exit, a normal log on success."""
    stderr = "No job was started.\nabort_dataset_resolution: dataset not found" if returncode else ""
    return _StubHarborRun(returncode=returncode, stdout="harbor log", stderr=stderr)


def _run_trace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int) -> dict[str, object]:
    """Invoke the executor with a stubbed `harbor run` and return the trace it wrote."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _stub_completed(returncode))
    executor = HarborCliExecutor(RecordedAgent("oracle"), "m", harbor_executable="harbor", task_staging_root=tmp_path / "staging")
    objective = Objective.model_validate(
        {
            "id": "obj",
            "repo_url": "https://github.com/pydantic/pydantic",
            "commit_sha": "0" * 40,
            "pr": 13611,
            "kind": "reject",
            "expected_properties": {"expected_verdict": "block", "minimum_findings": 1},
        }
    )
    result = executor(objective, 0)
    trace: dict[str, object] = json.loads((result.run_dir / _TRACE_FILENAME).read_text(encoding="utf-8"))
    return trace


def test_nonzero_harbor_exit_is_reported_as_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-zero `harbor run` exit is recorded as a distinguishable launch failure, not a bare log.

    The abort case: Harbor exits non-zero at argument resolution, before any agent runs. That must
    not read the same as "the agent produced nothing"."""
    trace = _run_trace(monkeypatch, tmp_path, returncode=2)
    assert trace["harbor_launch_failed"] is True
    assert trace["harbor_exit_code"] == 2
    turns = trace["turns"]
    assert isinstance(turns, list)
    assert turns[0]["role"] == "harbor-launch-failure"


def test_zero_harbor_exit_is_not_flagged_as_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A clean `harbor run` (exit 0) carries no failure flag and the ordinary `harbor` turn role."""
    trace = _run_trace(monkeypatch, tmp_path, returncode=0)
    assert trace["harbor_launch_failed"] is False
    assert trace["harbor_exit_code"] == 0
    turns = trace["turns"]
    assert isinstance(turns, list)
    assert turns[0]["role"] == "harbor"
