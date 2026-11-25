"""``code`` turns a merged pull request of a real git repository into a coding task (coding-task tech plan §4)."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import tomllib
from pathlib import Path

import pytest
from fixtures.coding_repo import BASE_CALC, PR_NUMBER, PR_TITLE, CodingRepoBuilder, CodingRepoFixture

from eval_harvest.cli import Cli
from eval_harvest.codeprofile import CodingProfile, CodingProfileReader
from eval_harvest.codetask import CodingRefusal, CodingTaskHarvester, CodingTaskResult
from eval_harvest.harbor import Harbor


@pytest.fixture
def fixture(tmp_path: Path) -> CodingRepoFixture:
    return CodingRepoBuilder.build_merged_pull_request(tmp_path)


def _profile(fixture: CodingRepoFixture) -> CodingProfile:
    profile, problems = CodingProfileReader.read_profile(fixture.dataset / "coding-profile.toml")
    assert profile is not None, problems
    return profile


def _harvest(fixture: CodingRepoFixture, pr_number: int = PR_NUMBER) -> CodingTaskResult | CodingRefusal:
    return CodingTaskHarvester.harvest_coding_task(fixture.clone, pr_number, _profile(fixture), fixture.dataset)


def _task(fixture: CodingRepoFixture) -> Path:
    result = _harvest(fixture)
    assert isinstance(result, CodingTaskResult), result
    return result.task_dir


def _snapshot_members(task_dir: Path) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress((task_dir / "environment" / "repo.tar.gz").read_bytes()))) as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}  # type: ignore[union-attr]


class TestCodingTask:
    def test_the_task_has_the_base_the_hidden_tests_and_the_gold_patch_apart(self, fixture: CodingRepoFixture) -> None:
        task_dir = _task(fixture)
        assert task_dir.name == f"acme__calc__pr{PR_NUMBER}-code"
        # The agent's image gets the base tree only: no mul, no new test, no .git.
        snapshot = _snapshot_members(task_dir)
        assert snapshot["calc.py"] == BASE_CALC.encode()
        assert b"test_mul" not in snapshot["tests/test_calc.py"]
        assert not any(name.startswith(".git/") for name in snapshot)
        # The hidden test is the merge's version; the gold patch is the source side only.
        assert "def test_mul" in (task_dir / "tests" / "files" / "tests" / "test_calc.py").read_text()
        gold = (task_dir / "solution" / "gold.patch").read_text()
        assert "+def mul(a, b):" in gold
        assert "test_calc.py" not in gold
        spec = json.loads((task_dir / "tests" / "spec.json").read_text())
        assert (spec["run_files"], spec["deleted"]) == (["tests/test_calc.py"], [])

    def test_the_config_is_a_staged_shared_verifier_with_no_network_for_the_grader(self, fixture: CodingRepoFixture) -> None:
        task_dir = _task(fixture)
        config = tomllib.loads((task_dir / "task.toml").read_text())
        assert Harbor.validate_coding_task_config(config) == []
        assert "environment_mode" not in config["verifier"]
        assert config["verifier"]["network_mode"] == "no-network"
        assert config["environment"]["network_mode"] == "allowlist"
        assert config["metadata"]["origin"]["merge_commit"] == fixture.merge_commit
        assert config["metadata"]["origin"]["base_commit"] == fixture.base_commit
        assert "--stage" in (task_dir / "tests" / "test.sh").read_text()

    def test_a_separate_verifier_is_refused_for_a_coding_task(self, fixture: CodingRepoFixture) -> None:
        config = tomllib.loads((_task(fixture) / "task.toml").read_text())
        config["verifier"]["environment_mode"] = "separate"
        assert [violation.pointer for violation in Harbor.validate_coding_task_config(config)] == ["verifier.environment_mode"]

    def test_the_instruction_is_the_title_and_the_test_files_never_the_fix(self, fixture: CodingRepoFixture) -> None:
        instruction = (_task(fixture) / "instruction.md").read_text()
        assert instruction.startswith(f"# {PR_TITLE}\n")
        assert "python3 run_tests.py report.xml tests/test_calc.py" in instruction
        assert "`tests/test_calc.py`" in instruction
        assert f"#{PR_NUMBER}" not in instruction
        assert "mul(a, b)" not in instruction

    def test_the_image_holds_the_repository_where_the_staged_grader_looks(self, fixture: CodingRepoFixture) -> None:
        dockerfile = (_task(fixture) / "environment" / "Dockerfile").read_text()
        assert "FROM python:3.12-bookworm" in dockerfile
        assert "WORKDIR /work/repo" in dockerfile
        assert "git tag harvest-base" in dockerfile
        assert "> /var/lib/task_base_ref" in dockerfile

    def test_the_same_pull_request_gives_the_same_bytes(self, fixture: CodingRepoFixture) -> None:
        first = {
            path.relative_to(_task(fixture)).as_posix(): path.read_bytes() for path in _task(fixture).rglob("*") if path.is_file()
        }
        second = {
            path.relative_to(_task(fixture)).as_posix(): path.read_bytes() for path in _task(fixture).rglob("*") if path.is_file()
        }
        assert first == second
        assert not [path for path in (fixture.dataset / "tasks").iterdir() if path.name.startswith(".")], "a staging dir was left"

    @pytest.mark.parametrize(
        ("pr_number", "check"),
        [(8, "no-test-change"), (9, "no-source-change"), (99, "pr-not-found")],
    )
    def test_a_pull_request_that_cannot_be_a_coding_task_is_refused(
        self, fixture: CodingRepoFixture, pr_number: int, check: str
    ) -> None:
        result = _harvest(fixture, pr_number)
        assert isinstance(result, CodingRefusal)
        assert result.check == check

    def test_base_text_in_the_gold_patch_context_is_not_a_leak(self, fixture: CodingRepoFixture) -> None:
        # Seen on a real repository: a PR that edits .gitignore has "__pycache__/" as a context line, and the
        # Dockerfile once held the same text. Context lines are the base's own text, which the agent has anyway.
        result = _harvest(fixture, 10)
        assert isinstance(result, CodingTaskResult), result
        assert " __pycache__/" in (result.task_dir / "solution" / "gold.patch").read_text()

    def test_a_line_the_change_adds_is_a_leak_where_the_agent_can_read_it(self, fixture: CodingRepoFixture) -> None:
        task_dir = _task(fixture)
        assert CodingTaskHarvester.answer_leak(task_dir) is None
        with (task_dir / "instruction.md").open("a") as instruction:
            instruction.write("Hint: def mul(a, b):\n")
        leak = CodingTaskHarvester.answer_leak(task_dir)
        assert leak is not None
        assert leak.check == "answer-absent:def mul(a, b):"

    def test_a_squash_merge_title_drops_the_pr_number(self, fixture: CodingRepoFixture) -> None:
        commit = CodingTaskHarvester.find_integration_commit(fixture.clone, 9)
        assert commit is not None
        assert CodingTaskHarvester.pull_request_title(fixture.clone, commit, 9) == "Test add again"

    def test_no_changed_test_file_matching_the_run_globs_is_refused(self, fixture: CodingRepoFixture) -> None:
        profile = _profile(fixture)
        narrow = CodingProfile(
            image=profile.image, command=profile.command, display_command=profile.display_command, run_globs=("spec/*",)
        )
        result = CodingTaskHarvester.harvest_coding_task(fixture.clone, PR_NUMBER, narrow, fixture.dataset)
        assert isinstance(result, CodingRefusal)
        assert result.check == "no-run-files"


class TestCodeVerb:
    def test_code_writes_the_task_and_reports_it_as_json(
        self, fixture: CodingRepoFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["--json", "code", "--pr", str(PR_NUMBER), "--clone", str(fixture.clone), "--dataset", str(fixture.dataset)]
        assert Cli.run(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["run_files"] == ["tests/test_calc.py"]
        assert Path(payload["task_dir"]).is_dir()

    def test_code_without_a_profile_is_a_usage_error(
        self, fixture: CodingRepoFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (fixture.dataset / "coding-profile.toml").unlink()
        argv = ["--json", "code", "--pr", str(PR_NUMBER), "--clone", str(fixture.clone), "--dataset", str(fixture.dataset)]
        assert Cli.run(argv) == 2
        assert json.loads(capsys.readouterr().out)["failures"][0]["check"] == "coding-profile-invalid"

    def test_a_refused_pull_request_exits_3_with_the_reason(
        self, fixture: CodingRepoFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert Cli.run(["code", "--pr", "8", "--clone", str(fixture.clone), "--dataset", str(fixture.dataset)]) == 3
        assert "refused: no-test-change" in capsys.readouterr().err
