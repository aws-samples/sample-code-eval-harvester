"""``coding-profile.toml``: every problem is reported, and none is filled in by a default."""

from __future__ import annotations

from eval_harvest.codeprofile import DEFAULT_ALLOWED_HOSTS, DEFAULT_TEST_GLOBS, CodingProfileReader

_MINIMAL = {
    "coding": {
        "image": "python:3.12-bookworm",
        "command": "python -m pytest --junitxml={junit} {files}",
        "run": ["tests/test_*.py"],
    }
}


class TestCodingProfile:
    def test_a_minimal_profile_gets_its_defaults(self) -> None:
        profile, problems = CodingProfileReader.profile_from_document(_MINIMAL)
        assert problems == []
        assert profile is not None
        assert profile.display_command == "python -m pytest {files}"
        assert (profile.test_globs, profile.allowed_hosts, profile.workdir) == (DEFAULT_TEST_GLOBS, DEFAULT_ALLOWED_HOSTS, ".")

    def test_every_problem_is_reported_at_once(self) -> None:
        document = {"coding": {"image": "", "command": "pytest", "run": "tests/*.py", "timeout": 5, "env": {"A": 1}}}
        profile, problems = CodingProfileReader.profile_from_document(document)
        assert profile is None
        assert sorted(problem.key for problem in problems) == [
            "coding.command",
            "coding.command",
            "coding.env",
            "coding.image",
            "coding.run",
            "coding.timeout",
        ]

    def test_a_lone_string_where_a_list_belongs_is_an_error(self) -> None:
        # Read as a list, "tests/*.py" would be its characters, each one a glob.
        document = {"coding": {**_MINIMAL["coding"], "agent_setup": "npm install -g x"}}
        _, problems = CodingProfileReader.profile_from_document(document)
        assert [problem.key for problem in problems] == ["coding.agent_setup"]

    def test_the_environment_is_sorted_so_the_task_bytes_do_not_depend_on_key_order(self) -> None:
        document = {"coding": {**_MINIMAL["coding"], "env": {"B": "2", "A": "1"}}}
        profile, _ = CodingProfileReader.profile_from_document(document)
        assert profile is not None
        assert profile.environment == (("A", "1"), ("B", "2"))

    def test_a_profile_without_a_coding_table_is_refused(self) -> None:
        profile, problems = CodingProfileReader.profile_from_document({"image": "x"})
        assert profile is None
        assert [problem.key for problem in problems] == ["coding"]
