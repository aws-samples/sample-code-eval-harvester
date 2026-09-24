"""Tests for the `brief` verb: one self-contained per-PR hand-off for filling a candidate.

`brief` exists to break the single-large-context walk that can cost a driver a million
tokens: it prints everything needed to fill *one* candidate and nothing else, so a driver
can hand each PR to a fresh context. The load-bearing assertions here are (a) every comment appears
with its in-diff marker, (b) both buildable kinds are named with their iteration and count, (c)
the output carries no diff bytes, and (d) the fill contract is the *same* text `capture` prints, from
one shared constant. The rest pin the body/truncation, next-command, refusal, and `--json` contracts.

Candidates are built by hand rather than through `capture`: the geometry under test — a comment
inside one round's diff but not another's, a single-recoverable-round PR — needs iterations and
`in_diff_iterations` set precisely, and `brief` reads the candidate, not a repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval_harvest.candidate import FILL_CONTRACT_GUIDE, Candidate, CommentDict
from eval_harvest.cli import Cli, ExitCode

_PR = 523
_NEUTRAL_BODY = "The mirror in cli/src/types.ts still has the old shape; these drift silently."


def _comment(comment_id: int, *, in_diff: list[int], body: str = _NEUTRAL_BODY, iteration_index: int = 0) -> CommentDict:
    """One comment record with the fields `brief` reads and a chosen `in_diff_iterations`."""
    return {
        "id": comment_id,
        "body": body,
        "path": "app.py",
        "line_start": 45,
        "line_end": 45,
        "author_role": "MEMBER",
        "created_at": "2026-08-02T10:00:00Z",
        "iteration_index": iteration_index,
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
    tmp_path: Path, *, iterations: list[dict[str, Any]], comments: list[CommentDict], rubric_version: str | None = None
) -> Path:
    """Write a candidate JSON (and optionally a rubric.md with a version) under a dataset root."""
    dataset = tmp_path / "ds"
    (dataset / "candidates").mkdir(parents=True)
    if rubric_version is not None:
        (dataset / "rubric.md").write_text(f"---\nversion: {rubric_version}\n---\n# Review rubric\n", encoding="utf-8")
    candidate = {
        "repo": "our-org/our-repo",
        "pr_number": _PR,
        "pr_url": f"https://github.com/our-org/our-repo/pull/{_PR}",
        "iterations": iterations,
        "review_verdicts": [],
        "comments": comments,
        "findings": [],
        "change_risk": {
            "risk_structural": "high",
            "risk_structural_rule": "cdk/src/ → high",
            "risk_classified": "",
            "risk_classified_rationale": "",
        },
        "rubric_version": "",
    }
    candidate_path = dataset / "candidates" / f"pr-{_PR}.json"
    candidate_path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
    return candidate_path


def _two_round_candidate(tmp_path: Path) -> Path:
    """Two recoverable rounds. Comment 100 is in both diffs (so in the reject diff, round 0); comment 200
    is only in round 1's diff (not in the reject diff)."""
    return _write_candidate(
        tmp_path,
        iterations=[_iteration(0, recoverable=True), _iteration(1, recoverable=True)],
        comments=[_comment(100, in_diff=[0, 1]), _comment(200, in_diff=[1], iteration_index=1)],
        rubric_version="v1",
    )


def _single_round_candidate(tmp_path: Path) -> Path:
    """One recoverable round — reject and approve resolve to the same iteration."""
    return _write_candidate(tmp_path, iterations=[_iteration(0, recoverable=True)], comments=[_comment(100, in_diff=[0])])


def _comments_section(rendered: str) -> str:
    """The text between the `comments (` heading and the `you fill` heading — brief's per-comment output."""
    start = rendered.index("comments (")
    end = rendered.index("you fill", start)
    return rendered[start:end]


# ───────────────────────────── comments and markers ─────────────────────────────


def test_brief_lists_every_comment_with_in_diff_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every comment appears, each marked in/out of the reject diff from its `in_diff_iterations`."""
    code = Cli.run(["brief", str(_two_round_candidate(tmp_path))])
    out = capsys.readouterr().out
    assert code == ExitCode.SUCCESS
    section = _comments_section(out)
    assert "id 100" in section and "id 200" in section, "a comment was dropped from the brief"
    assert "[in reject diff]" in section, "comment 100 (in round-0 diff) must be marked in the reject diff"
    assert "[not in reject diff]" in section, "comment 200 (round-1 only) must be marked outside the reject diff"


def test_brief_states_both_emittable_kinds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The reject and approve rows name their iteration and the count of comments that can anchor a finding."""
    Cli.run(["brief", str(_two_round_candidate(tmp_path))])
    out = capsys.readouterr().out
    emittable = out[out.index("emittable") : out.index("comments (")]
    assert "reject" in emittable and "iteration 0" in emittable and "1 comment(s) can anchor" in emittable
    assert "approve" in emittable and "iteration 1" in emittable and "2 comment(s) can anchor" in emittable


def test_brief_reports_kind_unavailable_with_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A one-recoverable-round PR says so plainly, rather than reading as two distinct buildable states."""
    Cli.run(["brief", str(_single_round_candidate(tmp_path))])
    out = capsys.readouterr().out
    assert "only one recoverable review round" in out
    assert "reject and approve build from the same state" in out


def test_brief_suggests_no_classification(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No comment line carries a CLI-suggested verdict (FR-9) — the taxonomy lives in the fill contract, not here."""
    Cli.run(["brief", str(_two_round_candidate(tmp_path))])
    section = _comments_section(capsys.readouterr().out)
    assert "classification" not in section, "brief must not print a per-comment classification slot"
    for verdict in ("defect", "nit", "question", "approval"):
        assert verdict not in section, f"brief leaked the verdict {verdict!r} into a comment line (FR-9)"


# ───────────────────────────── bodies ─────────────────────────────


def test_brief_body_full_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With no `--body-chars`, each comment body is carried byte-identically to the candidate's."""
    long_body = "This reviewer comment spells out the whole problem across a sentence long enough to be truncated."
    candidate_path = _write_candidate(
        tmp_path, iterations=[_iteration(0, recoverable=True)], comments=[_comment(100, in_diff=[0], body=long_body)]
    )
    Cli.run(["--json", "brief", str(candidate_path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["comments"][0]["body"] == long_body, "the default brief must not truncate the body"


def test_brief_body_chars_truncates_and_points_at_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--body-chars 40` truncates each body and names `show` as the route to the rest."""
    long_body = "This reviewer comment spells out the whole problem across a sentence long enough to be truncated."
    candidate_path = _write_candidate(
        tmp_path, iterations=[_iteration(0, recoverable=True)], comments=[_comment(100, in_diff=[0], body=long_body)]
    )
    Cli.run(["brief", str(candidate_path), "--body-chars", "40"])
    out = capsys.readouterr().out
    assert long_body not in out, "the body was not truncated"
    assert "…" in out and "show" in out and "100" in out, "a truncation must point at `show --comment <id>` for the rest"


# ───────────────────────────── next commands and no-diff guarantee ─────────────────────────────


def test_brief_next_commands_use_the_real_candidate_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The printed next commands are runnable as printed: the real path, a real comment id, both kinds."""
    candidate_path = _two_round_candidate(tmp_path)
    Cli.run(["brief", str(candidate_path)])
    out = capsys.readouterr().out
    assert f"eval-harvest show {candidate_path} --comment 100" in out
    assert f"eval-harvest annotate {candidate_path} --stdin" in out
    assert f"eval-harvest emit {candidate_path} --kind reject" in out
    assert f"eval-harvest emit {candidate_path} --kind approve" in out


def test_brief_prints_no_diff_bytes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No line of the brief is a diff header — printing hunks is `show`'s job, and the reason X exists."""
    Cli.run(["brief", str(_two_round_candidate(tmp_path))])
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert not line.startswith("@@"), f"brief printed a hunk header: {line!r}"
        assert not line.startswith("+++"), f"brief printed a diff file header: {line!r}"


# ───────────────────────────── contracts ─────────────────────────────


def test_brief_json_payload_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` emits exactly the documented keys."""
    Cli.run(["--json", "brief", str(_two_round_candidate(tmp_path))])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "repo",
        "pr_number",
        "pr_url",
        "rubric_version",
        "change_risk",
        "iterations",
        "emittable",
        "comments",
        "fill_contract",
        "next_commands",
    }
    assert payload["rubric_version"] == "v1"
    assert payload["pr_number"] == _PR
    assert payload["comments"][0]["in_reject_diff"] is True


def test_fill_contract_text_shared_with_slot_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The brief's fill contract and `capture`'s slot summary render the *same* constant — no drift.

    Two hand-written descriptions of one schema is the documentation bug this workstream keeps closing;
    both must render `FILL_CONTRACT_GUIDE` verbatim so a field added to the taxonomy shows up in both.
    """
    candidate_path = _two_round_candidate(tmp_path)
    Cli.run(["brief", str(candidate_path)])
    brief_out = capsys.readouterr().out
    slot_out = "\n".join(Candidate.slot_summary(Candidate.load(candidate_path)))
    for line in FILL_CONTRACT_GUIDE:
        assert line in brief_out, f"brief does not render the shared fill-contract line {line!r}"
        assert line in slot_out, f"slot_summary does not render the shared fill-contract line {line!r}"


# ───────────────────────────── refusals and read-only ─────────────────────────────


def test_brief_refuses_no_emittable_kind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A candidate with no recoverable iteration refuses `no-emittable-kind` at exit 3."""
    candidate_path = _write_candidate(
        tmp_path, iterations=[_iteration(0, recoverable=False)], comments=[_comment(100, in_diff=[])]
    )
    code = Cli.run(["brief", str(candidate_path)])
    stderr = capsys.readouterr().err
    assert code == ExitCode.REFUSAL
    assert "no-emittable-kind" in stderr


def test_brief_missing_candidate_is_usage_error(tmp_path: Path) -> None:
    """A candidate path that is not a file is a usage error (exit 2), not a refusal."""
    assert Cli.run(["brief", str(tmp_path / "nope.json")]) == ExitCode.USAGE


def test_brief_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`brief` is read-only: the dataset tree is byte-identical before and after a render."""
    candidate_path = _two_round_candidate(tmp_path)
    dataset = candidate_path.parent.parent
    before = {path: path.read_bytes() for path in sorted(dataset.rglob("*")) if path.is_file()}
    Cli.run(["brief", str(candidate_path)])
    capsys.readouterr()
    after = {path: path.read_bytes() for path in sorted(dataset.rglob("*")) if path.is_file()}
    assert before == after, "brief must not write, delete, or modify any file"


def test_brief_help_teaches_methodology() -> None:
    """`brief --help` teaches the per-PR hand-off, the no-diff rule, and FR-9 (no classification)."""
    help_text = Cli.help_for("brief").lower()
    assert "methodology" in help_text
    assert "self-contained brief" in help_text
    assert "no diff bytes" in help_text
    assert "suggests no classification" in help_text
