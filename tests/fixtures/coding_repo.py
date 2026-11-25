"""Real git repositories with merged pull requests, for the coding-task kind (``code``, ``calibrate``).

The repository is a tiny synthetic one: ``calc.py``, a stdlib test runner that writes JUnit XML
(``run_tests.py``), and a test file. Pull request #7 adds ``mul`` with a test for it, through a real
``git merge --no-ff`` whose message is the one GitHub writes. Nothing here is from a real project.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 — fixed argv lists to local git only
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eval_harvest.gitcmd import GIT_EXECUTABLE, GitCommandRunner

PR_NUMBER: Final = 7
PR_TITLE: Final = "Add multiplication"

#: A test runner that needs only python3: it runs every ``test_*`` function of each file and writes
#: JUnit XML, and leaves a cache file in its working directory as real runners do.
RUNNER: Final = """import sys
junit, files = sys.argv[1], sys.argv[2:]
open(".runner-cache", "w").write("cache")
cases = []
for path in files:
    namespace = {"__name__": "t"}
    exec(compile(open(path).read(), path, "exec"), namespace)
    for name in sorted(n for n in namespace if n.startswith("test_")):
        try:
            namespace[name]()
            cases.append('<testcase classname="t" name="%s" file="%s"/>' % (name, path))
        except Exception:
            cases.append('<testcase classname="t" name="%s" file="%s"><failure message="x"/></testcase>' % (name, path))
open(junit, "w").write("<testsuites>%s</testsuites>" % "".join(cases))
"""
BASE_CALC: Final = "def add(a, b):\n    return a + b\n"
GOLD_CALC: Final = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
BASE_TEST: Final = "import sys\nsys.path.insert(0, '.')\nimport calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"
HIDDEN_TEST: Final = BASE_TEST + "\n\ndef test_mul():\n    assert calc.mul(2, 3) == 6\n"
PROFILE: Final = """[coding]
image = "python:3.12-bookworm"
command = "python3 run_tests.py {junit} {files}"
display_command = "python3 run_tests.py report.xml {files}"
run = ["tests/test_*.py"]
"""

_ENV: Final = {
    **GitCommandRunner.isolated_git_env(os.environ, inherit=("PATH",)),
    "GIT_AUTHOR_NAME": "Eval Harvest",
    "GIT_AUTHOR_EMAIL": "eval@harvest.test",
    "GIT_COMMITTER_NAME": "Eval Harvest",
    "GIT_COMMITTER_EMAIL": "eval@harvest.test",
    "TZ": "UTC",
}


@dataclass(frozen=True, slots=True)
class CodingRepoFixture:
    """A clone holding merged pull requests, and the dataset folder with its profile. Plain data."""

    clone: Path
    dataset: Path
    base_commit: str
    merge_commit: str


class CodingRepoBuilder:
    """Builds the synthetic repository and its pull requests. Holds no state."""

    @classmethod
    def build_merged_pull_request(cls, root: Path) -> CodingRepoFixture:
        """``main`` with: the base; PR #7 (merged); PR #8 (docs only); PR #9 (squashed); PR #10 (with .gitignore)."""
        clone = root / "clone"
        clone.mkdir(parents=True)
        cls.git(clone, "init", "--quiet", "--initial-branch=main")
        cls.git(clone, "remote", "add", "origin", "https://github.com/acme/calc.git")
        base_files = {"calc.py": BASE_CALC, "run_tests.py": RUNNER, "tests/test_calc.py": BASE_TEST, "README.md": "calc\n"}
        cls.write(clone, {**base_files, ".gitignore": "__pycache__/\n"})
        base = cls.commit(clone, "base", "2026-01-01T00:00:00")
        cls.merge_branch(clone, "acme/mul", {"calc.py": GOLD_CALC, "tests/test_calc.py": HIDDEN_TEST}, PR_NUMBER, PR_TITLE)
        merge = cls.head(clone)
        cls.merge_branch(clone, "acme/docs", {"README.md": "calc, now with mul\n"}, 8, "Document mul")
        cls.git(clone, "checkout", "--quiet", "-b", "acme/tests-only")
        cls.write(clone, {"tests/test_more.py": BASE_TEST.replace("test_add", "test_add_again")})
        cls.git(clone, "checkout", "--quiet", "main")
        cls.git(clone, "merge", "--quiet", "--squash", "acme/tests-only")
        cls.commit(clone, "Test add again (#9)", "2026-01-04T00:00:00")
        # PR #10 also adds ignore patterns that a Dockerfile could contain: they are the answer, not a leak.
        sub = {
            "calc.py": GOLD_CALC + "\n\ndef sub(a, b):\n    return a - b\n",
            ".gitignore": "__pycache__/\nnode_modules/\n.venv/\n",
            "tests/test_calc.py": HIDDEN_TEST + "\n\ndef test_sub():\n    assert calc.sub(3, 2) == 1\n",
        }
        cls.merge_branch(clone, "acme/sub", sub, 10, "Add subtraction")
        dataset = root / "dataset"
        dataset.mkdir()
        (dataset / "coding-profile.toml").write_text(PROFILE, encoding="utf-8")
        return CodingRepoFixture(clone=clone, dataset=dataset, base_commit=base, merge_commit=merge)

    @classmethod
    def merge_branch(cls, clone: Path, branch: str, files: dict[str, str], number: int, title: str) -> None:
        """A branch with ``files``, merged into ``main`` with GitHub's merge message."""
        cls.git(clone, "checkout", "--quiet", "-b", branch)
        cls.write(clone, files)
        cls.commit(clone, title, f"2026-01-0{number - 5}T00:00:00")
        cls.git(clone, "checkout", "--quiet", "main")
        date = f"2026-01-0{number - 5}T12:00:00"
        cls.git(
            clone,
            "merge",
            "--quiet",
            "--no-ff",
            "-m",
            f"Merge pull request #{number} from {branch}",
            "-m",
            title,
            branch,
            date=date,
        )

    @staticmethod
    def write(clone: Path, files: dict[str, str]) -> None:
        """Write files, creating folders."""
        for relative, text in files.items():
            path = clone / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")

    @classmethod
    def commit(cls, clone: Path, message: str, date: str) -> str:
        """Commit everything at a fixed date; the new SHA."""
        cls.git(clone, "add", "-A")
        cls.git(clone, "commit", "--quiet", "--message", message, date=date)
        return cls.head(clone)

    @classmethod
    def head(cls, clone: Path) -> str:
        """HEAD's SHA."""
        return cls.git(clone, "rev-parse", "HEAD").strip()

    @staticmethod
    def git(repo: Path, *args: str, date: str = "2026-01-01T00:00:00") -> str:
        """Run git with the isolated environment and a fixed date; its stdout."""
        completed = subprocess.run(  # nosec B603,B607 — literal argv[0], the resolved binary via executable=
            ["git", "-C", str(repo), *args],
            executable=GIT_EXECUTABLE,
            capture_output=True,
            text=True,
            env={**_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
            check=True,
        )
        return completed.stdout
