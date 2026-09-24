"""Tests for the `init` verb: dataset skeleton, convention surfacing, and the rubric version reader (C-1).

The contract is fourfold: `init` writes the skeleton a later verb needs (candidates/, tasks/,
patches/, both templates), so downstream commands do not fail on a missing input directory; it
*surfaces* the repo's own convention files so the agent overlays them rather than guessing (FR-25);
the rubric's declared version reads back so `emit` (D-3) can stamp `rubric_version` (FR-26, FR-27);
and it makes no forge/network call — `init` is the online layer but reads the local clone only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eval_harvest.cli import Cli, ExitCode
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.riskmap import RISK_MAP_TEMPLATE_FILENAME
from eval_harvest.rubric import RUBRIC_TEMPLATE_FILENAME, Rubric

#: A checked-in clone carrying CONTRIBUTING, .github/, CODEOWNERS (root + docs/), a style file, and
#: two non-convention files (README.md, src/service.go) that must *not* be surfaced. No `.py` files
#: live here on purpose — ruff/mypy scan the whole repo, and a fixture module would be linted.
FIXTURE_CLONE = Path(__file__).resolve().parents[1] / "fixtures" / "clone_with_conventions"


def _run_init(clone: Path, dataset: Path, extra: tuple[str, ...] = ()) -> int:
    """Drive the whole `init` verb end-to-end through the CLI dispatcher, as the agent's shell would."""
    return Cli.run(["init", str(clone), "--dataset", str(dataset), *extra])


def test_init_writes_skeleton_and_templates(tmp_path: Path) -> None:
    """`init` creates candidates/, tasks/, patches/, and both templates.

    Catches a later verb failing because its input directory does not exist, and pins that the
    emitted rubric template carries a version the helper can read back (FR-26, FR-27).
    """
    dataset = tmp_path / "ds"
    assert _run_init(FIXTURE_CLONE, dataset) == ExitCode.SUCCESS
    for skeleton_dir in ("candidates", "tasks", "patches"):
        assert (dataset / skeleton_dir).is_dir(), f"init did not create {skeleton_dir}/"
    assert (dataset / RUBRIC_TEMPLATE_FILENAME).is_file()
    assert (dataset / RISK_MAP_TEMPLATE_FILENAME).is_file()
    assert Rubric.read_version(dataset / RUBRIC_TEMPLATE_FILENAME) != "", "the template must carry a readable version"


def test_init_surfaces_convention_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CONTRIBUTING, .github/*, and CODEOWNERS paths appear in stdout; a plain source file does not.

    Catches the agent guessing conventions instead of overlaying the repo's own (FR-25). The
    negative control (`service.go`) guards against surfacing every file rather than the stated ones.
    """
    dataset = tmp_path / "ds"
    assert _run_init(FIXTURE_CLONE, dataset) == ExitCode.SUCCESS
    stdout = capsys.readouterr().out
    assert "CONTRIBUTING.md" in stdout
    assert "CODEOWNERS" in stdout
    assert ".github" in stdout
    assert ".editorconfig" in stdout
    # Negative controls: a top-level non-convention file and a deeper source file are both left out —
    # `init` surfaces the stated-convention files, not the whole tree (FR-25).
    assert "README.md" not in stdout, "a top-level non-convention file must not be surfaced"
    assert "service.go" not in stdout, "init surfaces stated-convention files, not every file"


def test_init_surfaces_codeowners_under_docs(tmp_path: Path) -> None:
    """The docs/ CODEOWNERS location is surfaced too, not only the root and .github/ ones."""
    found = {str(path) for path in Rubric.discover_convention_files(FIXTURE_CLONE)}
    assert "CODEOWNERS" in found
    assert str(Path("docs") / "CODEOWNERS") in found
    assert str(Path(".github") / "PULL_REQUEST_TEMPLATE.md") in found


def test_init_help_carries_overlay_method() -> None:
    """`init --help` instructs the agent to build a rubric first and describes the overlay method (FR-24)."""
    help_text = Cli.help_for("init").lower()
    assert "methodology" in help_text
    assert "rubric" in help_text
    assert "overlay" in help_text


def test_rubric_version_read_back(tmp_path: Path) -> None:
    """The version helper reads a declared version from frontmatter or a plain `version:` line, else blank.

    Catches a datapoint stamped with the wrong or blank `rubric_version` (FR-27): the helper `emit`
    calls must return exactly what the author declared.
    """
    frontmatter = tmp_path / "frontmatter.md"
    frontmatter.write_text("---\nversion: v3\n---\n# Review rubric\n", encoding="utf-8")
    assert Rubric.read_version(frontmatter) == "v3"

    plain_line = tmp_path / "plain.md"
    plain_line.write_text('# Review rubric\n\nversion: "2026.09"\n', encoding="utf-8")
    assert Rubric.read_version(plain_line) == "2026.09"

    undeclared = tmp_path / "undeclared.md"
    undeclared.write_text("# Review rubric\n\nno version declared here\n", encoding="utf-8")
    assert Rubric.read_version(undeclared) == ""


def test_declared_version_ignores_lookalike_keys() -> None:
    """A `rubric_version:` line is not mistaken for the rubric's own `version:` declaration."""
    assert Rubric.declared_version("rubric_version: nope\ndescription: none\n") == ""
    assert Rubric.declared_version("version: v9\n") == "v9"


def test_init_makes_no_forge_call(tmp_path: Path, monkeypatch: Any) -> None:
    """`init` reads the local clone but issues no `gh`/network call (the online-but-local property).

    Catches `init` reaching the forge when it only needs local files, which would muddy the offline
    guarantee (NFR-2). Any `gh` invocation trips the stubbed forge and fails the test.
    """

    def forbidden_gh(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        raise AssertionError("init must not call gh / the forge — it reads the local clone only")

    monkeypatch.setattr(GitCommandRunner, "gh", staticmethod(forbidden_gh))
    dataset = tmp_path / "ds"
    assert _run_init(FIXTURE_CLONE, dataset) == ExitCode.SUCCESS
