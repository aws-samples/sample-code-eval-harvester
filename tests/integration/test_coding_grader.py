"""A harvested coding task's ``test.sh``, run for real in the AgentCore + Harbor workshop's stages.

The workshop's staged grader runs ``test.sh --stage untrusted`` as the agent user, refuses the trial if
the repository changed, copies the stage's regular files (never a ``reward*`` name) into a root-owned
IN, and runs ``test.sh --stage trusted`` as another user with IN and a fresh OUT; only OUT/reward.json
becomes Harbor's reward. These tests take the same steps with local processes, on the task ``code``
wrote: its snapshot is unpacked and committed as the Dockerfile does, and the runner is stdlib Python.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess  # nosec B404 — fixed argv lists to bash and git only
import sys
import tarfile
from pathlib import Path

import pytest
from fixtures.coding_repo import GOLD_CALC, PR_NUMBER, CodingRepoBuilder

from eval_harvest.calibrate import CodingTaskCalibration
from eval_harvest.codeprofile import CodingProfileReader
from eval_harvest.codetask import CodingRefusal, CodingTaskHarvester

_TRUSTED_PYTHON = next(
    (
        path
        for path in ("/usr/bin/python3", "/usr/local/bin/python3")
        if os.path.isfile(path) and os.stat(os.path.realpath(path)).st_uid == 0
    ),
    None,
)
pytestmark = [
    pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="the task's test.sh is a bash script"),
    pytest.mark.skipif(
        _TRUSTED_PYTHON is None, reason="the trusted stage needs a root-owned /usr/bin/python3 or /usr/local/bin/python3"
    ),
]
_F2P = "tests/test_calc.py::test_mul"
_P2P = "tests/test_calc.py::test_add"


def _tree_digest(root: Path) -> str:
    """Every file outside .git, its path, mode and bytes: what the staged grader's root patch would see change."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        digest.update(str(relative).encode() + b"\0" + oct(path.lstat().st_mode).encode() + b"\0")
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class StagedRun:
    """One trial of a coding task, as the staged grader runs it, with local processes."""

    def __init__(self, root: Path, task_dir: Path) -> None:
        self.root, self.task_dir, self.app = root, task_dir, root / "app"
        self.app.mkdir(parents=True)
        with tarfile.open(
            fileobj=io.BytesIO(gzip.decompress((task_dir / "environment" / "repo.tar.gz").read_bytes()))
        ) as archive:
            archive.extractall(self.app, filter="data")
        for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "base"), ("tag", "harvest-base")):
            CodingRepoBuilder.git(self.app, *args)

    def apply_gold(self) -> None:
        (self.app / "calc.py").write_text(GOLD_CALC, encoding="utf-8")

    def sh(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        full = {**os.environ, "HARVEST_APP": str(self.app), **env}
        return subprocess.run(  # nosec B603,B607 — a fixed argv list
            ["bash", str(self.task_dir / "tests" / "test.sh"), *args],
            cwd=self.app,
            env=full,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def untrusted(self) -> Path:
        out = self.root / "untrusted-out"
        out.mkdir()
        before = _tree_digest(self.app)
        completed = self.sh("--stage", "untrusted", env={"OUT": str(out)})
        assert completed.returncode == 0, completed.stderr
        assert _tree_digest(self.app) == before, "the untrusted stage changed the repository"
        assert not list(out.glob("reward*")), "the untrusted stage wrote a reward"
        return out

    def root_copy(self, out: Path, plant: dict[str, str] | None = None) -> Path:
        evidence = self.root / "in"
        evidence.mkdir()
        for path in out.rglob("*"):
            if path.is_file() and not path.is_symlink() and not path.name.startswith("reward"):
                (evidence / path.relative_to(out)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, evidence / path.relative_to(out))
        for name, text in (plant or {}).items():
            (evidence / name).write_text(text, encoding="utf-8")
        return evidence

    def trusted(self, evidence: Path, env: dict[str, str] | None = None) -> tuple[dict[str, float] | None, Path]:
        out = self.root / "grade-out"
        out.mkdir(exist_ok=True)
        completed = self.sh("--stage", "trusted", env={"IN": str(evidence), "OUT": str(out), **(env or {})})
        assert completed.returncode == 0, completed.stderr
        reward = out / "reward.json"
        return (json.loads(reward.read_text()) if reward.exists() else None), out

    def trial(self) -> tuple[dict[str, float] | None, Path]:
        return self.trusted(self.root_copy(self.untrusted()))


def _harvested_task(tmp_path: Path) -> Path:
    fixture = CodingRepoBuilder.build_merged_pull_request(tmp_path / "repo")
    profile, _ = CodingProfileReader.read_profile(fixture.dataset / "coding-profile.toml")
    assert profile is not None
    result = CodingTaskHarvester.harvest_coding_task(fixture.clone, PR_NUMBER, profile, fixture.dataset)
    assert not isinstance(result, CodingRefusal), result
    return result.task_dir


def _as_harbor_job(results: Path, jobs: Path, task_dir: Path, trial: str) -> None:
    """Put a trusted stage's results.json where Harbor keeps a trial's verifier files."""
    (jobs / f"{task_dir.name}__{trial}" / "verifier").mkdir(parents=True)
    shutil.copy2(results, jobs / f"{task_dir.name}__{trial}" / "verifier" / "results.json")


@pytest.fixture
def calibrated_task(tmp_path: Path) -> Path:
    """The harvested task, calibrated from one nop and one oracle trial in stages."""
    task_dir = _harvested_task(tmp_path)
    for kind, gold in (("nop", False), ("oracle", True)):
        run = StagedRun(tmp_path / kind, task_dir)
        if gold:
            run.apply_gold()
        reward, out = run.trial()
        assert reward is None, "an uncalibrated task must give no reward"
        _as_harbor_job(out / "results.json", tmp_path / "jobs" / kind, task_dir, "aaaa")
    result = CodingTaskCalibration.calibrate(task_dir, tmp_path / "jobs" / "nop", tmp_path / "jobs" / "oracle")
    assert not isinstance(result, CodingRefusal), result
    assert (result.fail_to_pass, result.pass_to_pass) == ((_F2P,), (_P2P,))
    return task_dir


class TestStagedGrading:
    def test_calibration_finds_the_tests_and_lists_them_for_the_agent(self, calibrated_task: Path) -> None:
        expected = json.loads((calibrated_task / "tests" / "expected.json").read_text())
        assert expected == {"fail_to_pass": [_F2P], "pass_to_pass": [_P2P]}
        instruction = (calibrated_task / "instruction.md").read_text()
        assert "- test_mul (`tests/test_calc.py`)" in instruction
        assert "mul(a, b)" not in instruction

    def test_after_calibration_oracle_scores_1_and_nop_0(self, calibrated_task: Path, tmp_path: Path) -> None:
        nop = StagedRun(tmp_path / "nop2", calibrated_task)
        assert nop.trial()[0] == {"reward": 0.0}
        oracle = StagedRun(tmp_path / "oracle2", calibrated_task)
        oracle.apply_gold()
        reward, out = oracle.trial()
        assert reward == {"reward": 1.0}
        assert not (out / "reward.txt").exists()
        assert json.loads((out / "grade.json").read_text())["stage"] == "trusted"

    def test_nothing_the_agent_could_write_becomes_the_score(self, calibrated_task: Path, tmp_path: Path) -> None:
        run = StagedRun(tmp_path / "planted", calibrated_task)
        planted = {
            "reward.json": '{"reward": 1.0}',
            "grade.json": '{"reward": 1}',
            "results.json": json.dumps({"statuses": {_F2P: "passed", _P2P: "passed"}}),
        }
        evidence = run.root_copy(run.untrusted(), planted)
        (tmp_path / "planted" / "grade-out").mkdir()
        (tmp_path / "planted" / "grade-out" / "reward.json").write_text('{"reward": 1.0}')  # a stale reward is removed first
        assert run.trusted(evidence)[0] == {"reward": 0.0}

    def test_a_forged_passing_duplicate_and_a_dtd_score_0(self, calibrated_task: Path, tmp_path: Path) -> None:
        run = StagedRun(tmp_path / "forged", calibrated_task)
        evidence = run.root_copy(run.untrusted())
        junit = (evidence / "junit.xml").read_text()
        # The forged passing copy comes first, so it takes the plain id and the real failing one becomes "#2".
        forged = '<testsuites><testcase classname="t" name="test_mul" file="tests/test_calc.py"/>'
        (evidence / "junit.xml").write_text(junit.replace("<testsuites>", forged, 1))
        assert run.trusted(evidence)[0] == {"reward": 0.0}
        (evidence / "junit.xml").write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>' + junit)
        reward, out = run.trusted(evidence)
        assert reward == {"reward": 0.0}
        assert json.loads((out / "grade.json").read_text())["report"] == "the JUnit report declares a DTD or entities"

    def test_the_trusted_stage_reads_neither_the_repository_nor_path(self, calibrated_task: Path, tmp_path: Path) -> None:
        run = StagedRun(tmp_path / "isolated", calibrated_task)
        run.apply_gold()
        evidence = run.root_copy(run.untrusted())
        shutil.rmtree(run.app)
        run.app.mkdir()
        hostile = tmp_path / "hostile-bin"
        hostile.mkdir()
        (hostile / "python3").write_text('#!/bin/sh\necho \'{"reward": 1.0}\' > "$OUT/reward.json"; touch "$OUT/planted"\n')
        (hostile / "python3").chmod(0o755)
        reward, out = run.trusted(evidence, env={"PATH": f"{hostile}{os.pathsep}{os.environ['PATH']}"})
        assert reward == {"reward": 1.0}  # graded from the report and tests/files alone
        assert not (out / "planted").exists(), "the trusted stage ran a python3 from PATH"

    def test_harbors_one_stage_verifier_still_works_and_gives_the_same_ids(self, calibrated_task: Path, tmp_path: Path) -> None:
        run = StagedRun(tmp_path / "one-stage", calibrated_task)
        run.apply_gold()
        _, out = run.trial()
        logs = tmp_path / "one-stage" / "logs"
        run.sh(env={"HARVEST_LOGS": str(logs)})
        assert (logs / "reward.txt").read_text() == "1\n"
        assert (
            json.loads((logs / "results.json").read_text())["statuses"]
            == json.loads((out / "results.json").read_text())["statuses"]
        )

    def test_an_unknown_stage_is_a_usage_error(self, calibrated_task: Path, tmp_path: Path) -> None:
        completed = StagedRun(tmp_path / "usage", calibrated_task).sh("--stage", "both", env={})
        assert completed.returncode == 2
        assert "usage: test.sh [--stage untrusted|trusted]" in completed.stderr


class TestCalibrationRefusals:
    def test_no_fail_to_pass_test_is_refused(self, tmp_path: Path) -> None:
        task_dir = _harvested_task(tmp_path)
        for kind in ("nop", "oracle"):  # both on the base: nothing tells the change apart
            run = StagedRun(tmp_path / kind, task_dir)
            _, out = run.trial()
            _as_harbor_job(out / "results.json", tmp_path / "jobs" / kind, task_dir, "bbbb")
        result = CodingTaskCalibration.calibrate(task_dir, tmp_path / "jobs" / "nop", tmp_path / "jobs" / "oracle")
        assert isinstance(result, CodingRefusal)
        assert result.check == "no-fail-to-pass"
        assert not (task_dir / "tests" / "expected.json").exists()

    def test_missing_runs_are_refused(self, tmp_path: Path) -> None:
        task_dir = _harvested_task(tmp_path)
        result = CodingTaskCalibration.calibrate(task_dir, tmp_path / "none", tmp_path / "none")
        assert isinstance(result, CodingRefusal)
        assert result.check == "no-calibration-runs"
