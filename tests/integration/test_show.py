"""Tests for the `show` verb: print only the diff hunk covering one review comment.

The problem this verb exists to solve is a token bill: without it, classifying a handful of comments
means reading megabytes of patch text. So the load-bearing assertions here are (a) `show` prints *only* the
covering hunk out of a multi-hunk diff, and (b) the rendered bytes are a small fraction of the patch
bytes. The rest pin the iteration selection, the four refusals, and the `--json`/help contracts.

The candidates are built by hand rather than through `capture`: the geometry under test — one hunk
among five covering a comment, the same line present across rounds, a comment in no diff — needs
diffs narrower and more numerous than a fixture repo produces, and `show` reads the patch text, not
a repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval_harvest.candidate import CommentDict
from eval_harvest.cli import Cli, ExitCode
from eval_harvest.show import Show

_PR = 523

#: Each hunk spans 20 new-side lines, with a single added line at offset 5 (so a hunk starting at
#: new-side 40 puts its added line at 45). Enough lines either side that `--context` trimming is
#: observable, and enough total bytes that the covering hunk is a small fraction of the whole patch —
#: real-world patches can run to 128 KB, which is the ratio this fixture stands in for.
_HUNK_SPAN = 20
_ADDED_OFFSET = 5


def _hunk(new_start: int, added: str) -> list[str]:
    """One 20-line hunk starting at ``new_start``, its added line (``+added``) at offset 5."""
    header = f"@@ -{new_start - 2},{_HUNK_SPAN - 1} +{new_start},{_HUNK_SPAN} @@ def block{new_start}():"
    body = [f"+{added}" if offset == _ADDED_OFFSET else f" line{new_start + offset}" for offset in range(_HUNK_SPAN)]
    return [header, *body]


def _diff(*hunks: tuple[int, str], path: str = "app.py") -> str:
    """A unified diff over ``path`` made of the given ``(new_start, added-marker)`` hunks."""
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
    for new_start, added in hunks:
        lines += _hunk(new_start, added)
    return "\n".join(lines) + "\n"


#: A five-hunk diff; only the hunk starting at new-side 40 (its added line at 45) covers a comment at 45.
_FIVE_HUNK_DIFF = _diff((5, "beta"), (20, "epsilon"), (40, "workspaceArn"), (80, "theta"), (100, "lambda"))

#: A second round's diff covering the same line 45 with a *different* added line, so an explicit
#: `--iteration` pointing here renders visibly different bytes than round 0.
_ROUND_TWO_DIFF = _diff((40, "ROUND_TWO_MARKER"))


def _comment(comment_id: int, *, path: str, line_start: int, line_end: int, in_diff: list[int]) -> CommentDict:
    """One comment record with the fields `show` reads and a chosen `in_diff_iterations`."""
    return {
        "id": comment_id,
        "body": "The mirror in cli/src/types.ts still has the old shape; these drift silently.",
        "path": path,
        "line_start": line_start,
        "line_end": line_end,
        "author_role": "MEMBER",
        "created_at": "2026-08-02T10:00:00Z",
        "iteration_index": 0,
        "in_diff_iterations": in_diff,
        "classification": "",
        "classification_rationale": "",
    }


def _iteration(index: int, *, recoverable: bool) -> dict[str, Any]:
    """One iteration record; `patch_path` is the recoverability answer (set only when recoverable)."""
    return {
        "tip_sha": chr(ord("a") + index) * 40,
        "base_sha": ("b" * 40) if recoverable else "",
        "patch_path": f"patches/pr-{_PR}-iter{index}.patch" if recoverable else "",
        "comment_ids": [],
    }


def _write_candidate(
    tmp_path: Path,
    *,
    iterations: list[dict[str, Any]],
    comments: list[CommentDict],
    patches: dict[int, str],
) -> Path:
    """Write a candidate JSON plus each iteration's patch bytes under a dataset root; return the candidate path."""
    dataset = tmp_path / "ds"
    (dataset / "candidates").mkdir(parents=True)
    (dataset / "patches").mkdir(parents=True)
    candidate = {
        "repo": "our-org/our-repo",
        "pr_number": _PR,
        "pr_url": f"https://github.com/our-org/our-repo/pull/{_PR}",
        "iterations": iterations,
        "review_verdicts": [],
        "comments": comments,
        "findings": [],
        "change_risk": {
            "risk_structural": "low",
            "risk_structural_rule": "*",
            "risk_classified": "",
            "risk_classified_rationale": "",
        },
        "rubric_version": "",
    }
    candidate_path = dataset / "candidates" / f"pr-{_PR}.json"
    candidate_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
    for index, diff in patches.items():
        (dataset / "patches" / f"pr-{_PR}-iter{index}.patch").write_text(diff, encoding="utf-8")
    return candidate_path


def _simple_candidate(tmp_path: Path) -> Path:
    """One recoverable iteration carrying the five-hunk diff, one comment at line 45 inside hunk three."""
    return _write_candidate(
        tmp_path,
        iterations=[_iteration(0, recoverable=True)],
        comments=[_comment(2145918431, path="app.py", line_start=45, line_end=45, in_diff=[0])],
        patches={0: _FIVE_HUNK_DIFF},
    )


# ───────────────────────────── hunk selection (the fix) ─────────────────────────────


def test_show_prints_only_the_covering_hunk() -> None:
    """A diff with five hunks yields exactly the one whose new-side span covers the comment's line."""
    hunks = Show.hunks_for_location(_FIVE_HUNK_DIFF, "app.py", 45, 45)
    assert len(hunks) == 1
    assert "workspaceArn" in hunks[0]
    for stranger in ("beta", "epsilon", "theta", "lambda"):
        assert stranger not in hunks[0], f"the non-covering hunk containing {stranger!r} leaked into the output"


def test_show_output_is_smaller_than_the_patch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The rendered bytes are a small fraction of the patch bytes — the ratio is the point of the task."""
    candidate_path = _simple_candidate(tmp_path)
    code = Cli.run(["show", str(candidate_path), "--comment", "2145918431"])
    rendered = capsys.readouterr().out
    assert code == ExitCode.SUCCESS
    patch_bytes = len(_FIVE_HUNK_DIFF.encode("utf-8"))
    assert len(rendered.encode("utf-8")) < patch_bytes // 2, "show rendered nearly the whole patch — it trimmed nothing"


def test_show_respects_context_lines() -> None:
    """`--context 0` keeps only the comment's own line; `--context 5` adds five body lines either side."""
    comment = _comment(1, path="app.py", line_start=45, line_end=45, in_diff=[0])
    hunks = Show.hunks_for_location(_FIVE_HUNK_DIFF, "app.py", 45, 45)
    tight = Show.render(comment, 0, hunks, 0)
    wide = Show.render(comment, 0, hunks, 5)
    assert wide.count("\n") - tight.count("\n") == 10, "widening context by 5 either side must add 10 lines"
    assert "line40" not in tight and "line40" in wide


# ───────────────────────────── iteration selection ─────────────────────────────


def test_show_defaults_to_first_in_diff_iteration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With no `--iteration`, the iteration chosen is `in_diff_iterations[0]`, not iteration 0."""
    candidate_path = _write_candidate(
        tmp_path,
        iterations=[_iteration(0, recoverable=True), _iteration(1, recoverable=True), _iteration(2, recoverable=True)],
        comments=[_comment(7, path="app.py", line_start=45, line_end=45, in_diff=[1, 2])],
        patches={0: "diff --git a/x b/x\n+++ b/x\n@@ -1 +1 @@\n+unrelated\n", 1: _FIVE_HUNK_DIFF, 2: _ROUND_TWO_DIFF},
    )
    code = Cli.run(["--json", "show", str(candidate_path), "--comment", "7"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.SUCCESS
    assert payload["iteration_index"] == 1
    assert "workspaceArn" in payload["hunk"] and "ROUND_TWO_MARKER" not in payload["hunk"]


def test_show_explicit_iteration_overrides_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--iteration 2` renders iteration 2's hunk for the same comment, beating the `in_diff_iterations[0]` default."""
    candidate_path = _write_candidate(
        tmp_path,
        iterations=[_iteration(0, recoverable=True), _iteration(1, recoverable=True), _iteration(2, recoverable=True)],
        comments=[_comment(7, path="app.py", line_start=45, line_end=45, in_diff=[1, 2])],
        patches={0: "diff --git a/x b/x\n+++ b/x\n@@ -1 +1 @@\n+unrelated\n", 1: _FIVE_HUNK_DIFF, 2: _ROUND_TWO_DIFF},
    )
    code = Cli.run(["--json", "show", str(candidate_path), "--comment", "7", "--iteration", "2"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.SUCCESS
    assert payload["iteration_index"] == 2
    assert "ROUND_TWO_MARKER" in payload["hunk"] and "workspaceArn" not in payload["hunk"]


# ───────────────────────────── refusals (all exit 3, four fields) ─────────────────────────────


def test_show_refuses_unknown_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An absent comment id refuses `unknown-comment` at exit 3, listing the ids that do exist."""
    candidate_path = _simple_candidate(tmp_path)
    code = Cli.run(["show", str(candidate_path), "--comment", "999"])
    stderr = capsys.readouterr().err
    assert code == ExitCode.REFUSAL
    assert "unknown-comment" in stderr
    assert "2145918431" in stderr, "the refusal must name the comment ids that exist"


def test_show_refuses_comment_outside_every_diff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A comment with `in_diff_iterations == []` refuses `comment-outside-every-diff`, naming why."""
    candidate_path = _write_candidate(
        tmp_path,
        iterations=[_iteration(0, recoverable=True)],
        comments=[_comment(7, path="app.py", line_start=900, line_end=900, in_diff=[])],
        patches={0: _FIVE_HUNK_DIFF},
    )
    code = Cli.run(["show", str(candidate_path), "--comment", "7"])
    stderr = capsys.readouterr().err
    assert code == ExitCode.REFUSAL
    assert "comment-outside-every-diff" in stderr
    assert "no recoverable iteration" in stderr and "reference_only" in stderr


def test_show_refuses_unrecoverable_iteration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--iteration <n>` on an iteration with no patch refuses, naming the recoverable indices."""
    candidate_path = _write_candidate(
        tmp_path,
        iterations=[_iteration(0, recoverable=True), _iteration(1, recoverable=False)],
        comments=[_comment(7, path="app.py", line_start=45, line_end=45, in_diff=[0])],
        patches={0: _FIVE_HUNK_DIFF},
    )
    code = Cli.run(["show", str(candidate_path), "--comment", "7", "--iteration", "1"])
    stderr = capsys.readouterr().err
    assert code == ExitCode.REFUSAL
    assert "iteration-not-recoverable" in stderr
    assert "[0]" in stderr, "the refusal must name the recoverable indices"


def test_show_refuses_missing_patch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A recorded `patch_path` absent under the dataset refuses `missing-patch`, matching emit's wording."""
    candidate_path = _write_candidate(
        tmp_path,
        iterations=[_iteration(0, recoverable=True)],
        comments=[_comment(7, path="app.py", line_start=45, line_end=45, in_diff=[0])],
        patches={},  # the candidate references iter0's patch, but no bytes are written
    )
    code = Cli.run(["show", str(candidate_path), "--comment", "7"])
    stderr = capsys.readouterr().err
    assert code == ExitCode.REFUSAL
    assert "missing-patch" in stderr
    assert "re-run `eval-harvest capture`" in stderr


def test_show_missing_candidate_is_usage_error(tmp_path: Path) -> None:
    """A candidate path that is not a file is a usage error (exit 2), not a refusal."""
    code = Cli.run(["show", str(tmp_path / "nope.json"), "--comment", "1"])
    assert code == ExitCode.USAGE


# ───────────────────────────── contracts ─────────────────────────────


def test_show_json_payload_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` emits exactly the documented keys and nothing else."""
    candidate_path = _simple_candidate(tmp_path)
    code = Cli.run(["--json", "show", str(candidate_path), "--comment", "2145918431"])
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.SUCCESS
    assert set(payload) == {"comment", "iteration_index", "path", "line_start", "line_end", "body", "hunk"}
    assert payload["comment"] == 2145918431
    assert payload["path"] == "app.py"
    assert payload["line_start"] == 45 and payload["line_end"] == 45
    assert "workspaceArn" in payload["hunk"]


def test_show_help_teaches_methodology() -> None:
    """`show --help` teaches classifying from the pointed-at lines, not just its flags (FR-1)."""
    help_text = Cli.help_for("show").lower()
    assert "methodology" in help_text
    assert "the lines it points at" in help_text
    assert "reading whole patch files is never necessary" in help_text


def test_show_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`show` is read-only: the dataset tree is byte-identical before and after a successful render."""
    candidate_path = _simple_candidate(tmp_path)
    dataset = candidate_path.parent.parent
    before = {path: path.read_bytes() for path in sorted(dataset.rglob("*")) if path.is_file()}
    Cli.run(["show", str(candidate_path), "--comment", "2145918431"])
    capsys.readouterr()
    after = {path: path.read_bytes() for path in sorted(dataset.rglob("*")) if path.is_file()}
    assert before == after, "show must not write, delete, or modify any file"
