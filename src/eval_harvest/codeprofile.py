"""The coding-task profile: how one repository's tests run inside a task image.

``coding-profile.toml`` lives in the dataset directory, next to ``rubric.md`` and ``risk-map.toml``,
and is the only per-repository input ``code`` needs besides the clone. Every key sits under
``[coding]``; an unknown key is an error, because a misspelled key silently falling back to a default
is the profile bug that costs a whole calibration run to notice.

Example (a pytest repository):

    [coding]
    image = "python:3.12-bookworm"
    install = "pip install --no-cache-dir -e . pytest"
    command = "python -m pytest -p no:cacheprovider -o junit_family=xunit1 --junitxml={junit} {files}"
    display_command = "python -m pytest {files}"
    run = ["tests/**/test_*.py"]
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final  # Any: a parsed TOML document is arbitrary until each key is narrowed below.

PROFILE_FILENAME: Final = "coding-profile.toml"

#: The test side when the profile names none: the folder and file-name conventions survey's
#: ``TEST_MARKERS`` look for, as globs (the grader restores the test side by glob).
DEFAULT_TEST_GLOBS: Final[tuple[str, ...]] = (
    "**/test/**",
    "**/tests/**",
    "**/spec/**",
    "**/__tests__/**",
    "**/test_*",
    "**/*_test.*",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*Test.java",
    "**/conftest.py",
)

#: What the agent may reach in the AgentCore + Harbor workshop: the Bedrock endpoints its agents use
#: (Codex on bedrock-mantle, Claude Code on bedrock-runtime), and nothing else.
DEFAULT_ALLOWED_HOSTS: Final[tuple[str, ...]] = ("bedrock-mantle.us-east-1.api.aws", "bedrock-runtime.us-east-1.amazonaws.com")
DEFAULT_TIMEOUT_SEC: Final = 900.0
_KNOWN_KEYS: Final = frozenset(
    {
        "image",
        "install",
        "command",
        "display_command",
        "run",
        "tests",
        "workdir",
        "timeout_sec",
        "env",
        "agent_setup",
        "allowed_hosts",
    }
)


@dataclass(frozen=True, slots=True)
class CodingProfile:
    """One repository's test runner and image, as ``code`` writes them into a task. Plain data."""

    image: str
    command: str
    display_command: str
    run_globs: tuple[str, ...]
    test_globs: tuple[str, ...] = DEFAULT_TEST_GLOBS
    install: str = ""
    workdir: str = "."
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    environment: tuple[tuple[str, str], ...] = ()
    agent_setup: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS


@dataclass(frozen=True, slots=True)
class ProfileProblem:
    """One way a profile is wrong: the key and what to change. Plain data."""

    key: str
    detail: str


class CodingProfileReader:
    """Reads and checks ``coding-profile.toml``. Holds no state."""

    @classmethod
    def read_profile(cls, path: Path) -> tuple[CodingProfile | None, list[ProfileProblem]]:
        """The profile in ``path``, or None and every problem found."""
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            return None, [ProfileProblem(str(path), f"cannot read the profile: {error}")]
        except tomllib.TOMLDecodeError as error:
            return None, [ProfileProblem(str(path), f"not valid TOML: {error}")]
        return cls.profile_from_document(document)

    @classmethod
    def profile_from_document(cls, document: Mapping[str, Any]) -> tuple[CodingProfile | None, list[ProfileProblem]]:
        """The profile in a parsed document, or None and every problem found (all of them, not the first)."""
        section = document.get("coding")
        if not isinstance(section, dict):
            return None, [ProfileProblem("coding", "the profile needs a [coding] table")]
        problems = [ProfileProblem(f"coding.{key}", "not a profile key") for key in sorted(set(section) - _KNOWN_KEYS)]
        image = cls.required_string(section, "image", problems)
        command = cls.required_string(section, "command", problems)
        for placeholder in ("{files}", "{junit}"):
            if command and placeholder not in command:
                problems.append(
                    ProfileProblem(
                        "coding.command", f"must contain {placeholder}: the grader fills in the run files and the report path"
                    )
                )
        run_globs = cls.string_list(section, "run", problems, required=True)
        profile = CodingProfile(
            image=image,
            command=command,
            display_command=cls.optional_string(section, "display_command", problems)
            or command.replace(" --junitxml={junit}", ""),
            run_globs=run_globs,
            test_globs=cls.string_list(section, "tests", problems) or DEFAULT_TEST_GLOBS,
            install=cls.optional_string(section, "install", problems),
            workdir=cls.optional_string(section, "workdir", problems) or ".",
            timeout_sec=cls.positive_number(section, "timeout_sec", problems),
            environment=cls.string_table(section, "env", problems),
            agent_setup=cls.string_list(section, "agent_setup", problems),
            allowed_hosts=cls.string_list(section, "allowed_hosts", problems) or DEFAULT_ALLOWED_HOSTS,
        )
        return (None, problems) if problems else (profile, [])

    @staticmethod
    def required_string(section: Mapping[str, Any], key: str, problems: list[ProfileProblem]) -> str:
        """A non-empty string, or a problem."""
        value = section.get(key)
        if isinstance(value, str) and value.strip():
            return value
        problems.append(ProfileProblem(f"coding.{key}", "required, a non-empty string"))
        return ""

    @staticmethod
    def optional_string(section: Mapping[str, Any], key: str, problems: list[ProfileProblem]) -> str:
        """A string when present, else empty."""
        value = section.get(key, "")
        if isinstance(value, str):
            return value
        problems.append(ProfileProblem(f"coding.{key}", "must be a string"))
        return ""

    @staticmethod
    def string_list(
        section: Mapping[str, Any], key: str, problems: list[ProfileProblem], *, required: bool = False
    ) -> tuple[str, ...]:
        """A list of non-empty strings (a lone string is an error: it would be read as its characters)."""
        value = section.get(key)
        if value is None and not required:
            return ()
        if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
            return tuple(value)
        problems.append(ProfileProblem(f"coding.{key}", "must be a non-empty list of strings"))
        return ()

    @staticmethod
    def positive_number(section: Mapping[str, Any], key: str, problems: list[ProfileProblem]) -> float:
        """A positive number when present, else the default timeout."""
        value = section.get(key, DEFAULT_TIMEOUT_SEC)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
        problems.append(ProfileProblem(f"coding.{key}", "must be a positive number of seconds"))
        return DEFAULT_TIMEOUT_SEC

    @staticmethod
    def string_table(section: Mapping[str, Any], key: str, problems: list[ProfileProblem]) -> tuple[tuple[str, str], ...]:
        """A table of string values, as sorted pairs (so the task bytes do not depend on key order)."""
        value = section.get(key, {})
        if isinstance(value, dict) and all(isinstance(item, str) for item in value.values()):
            return tuple(sorted((str(name), str(item)) for name, item in value.items()))
        problems.append(ProfileProblem(f"coding.{key}", "must be a table of strings"))
        return ()
