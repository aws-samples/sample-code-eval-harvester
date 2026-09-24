"""Tests for the candidate file and the `capture` verb (task B-3).

The candidate is the ADR-3 hand-off artefact: `capture` writes *mechanical facts* and leaves the
*judgment slots blank*, the agent fills them, and `emit`/`verify` read both halves back. These tests
pin the two properties that make that split trustworthy — the CLI writes facts and never a label
(FR-9), and the never-raising validators name every unfilled or incoherent slot (FR-13, FR-15,
FR-16) — plus the determinism the whole pipeline rests on (FR-10). They reuse the B-2 recorded `gh`
payloads and the real fixture repo, so the facts are reconstructed exactly as `capture` would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fixtures.repo_builder import RepoBuilder

from eval_harvest.candidate import Candidate, CandidateDict, FindingDict, Violation, patch_relpath
from eval_harvest.cli import Cli, ExitCode
from eval_harvest.emit import Emit
from eval_harvest.forge import Forge, PullRequestFacts, ReviewComment, ReviewIteration
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.riskmap import RiskMap
from eval_harvest.tomlw import emit_document

#: A risk map whose only rule marks the fixture's single file high, so the fixture's change (which
#: touches `code.py`) computes a non-default structural risk we can assert on.
_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}


def _facts(tmp_path: Path) -> PullRequestFacts:
    """Reconstruct the fixture PR's fact bundle the way `capture` does (offline replay)."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    return Forge.reconstruct_facts(
        fixture.pr_number,
        fixture.clone,
        fixture.base_ref,
        reviews=fixture.reviews,
        comments=fixture.comments,
        timeline=fixture.timeline,
    )


def _scaffold(tmp_path: Path) -> CandidateDict:
    """A freshly scaffolded candidate from the fixture facts: facts filled, judgment slots blank."""
    facts = _facts(tmp_path)
    risk_structural, risk_rule = RiskMap.structural_risk(Candidate.changed_paths(facts), _RISK_MAP)
    return Candidate.scaffold(
        facts,
        repo="our-org/our-repo",
        pr_url="https://github.com/our-org/our-repo/pull/1234",
        risk_structural=risk_structural,
        risk_structural_rule=risk_rule,
    )


def _scaffold_unrecoverable(tmp_path: Path) -> CandidateDict:
    """A scaffolded candidate for the force-pushed PR, whose round-1 iteration cannot be recovered."""
    fixture = RepoBuilder.build_pull_request_with_unrecoverable_iteration(tmp_path)
    facts = Forge.reconstruct_facts(
        fixture.pr_number,
        fixture.clone,
        fixture.base_ref,
        reviews=fixture.reviews,
        comments=fixture.comments,
        timeline=fixture.timeline,
    )
    risk_structural, risk_rule = RiskMap.structural_risk(Candidate.changed_paths(facts), _RISK_MAP)
    return Candidate.scaffold(
        facts,
        repo="our-org/our-repo",
        pr_url="https://github.com/our-org/our-repo/pull/1234",
        risk_structural=risk_structural,
        risk_structural_rule=risk_rule,
    )


#: One iteration whose only hunk covers new-side lines 10..12 of `touched.py`. Hand-written rather than
#: built from a repo because the geometry under test — a comment *outside* every hunk — needs a diff
#: narrower than any fixture repo produces, and the parser reads the text, not the repository.
_NARROW_DIFF = """diff --git a/touched.py b/touched.py
--- a/touched.py
+++ b/touched.py
@@ -9,2 +10,3 @@ def touched() -> None:
 context
+added
 context
"""


def _comment_at(comment_id: int, path: str, line_start: int, line_end: int) -> ReviewComment:
    """One synthetic comment at a given location; the non-geometric fields are fixed and irrelevant here."""
    return ReviewComment(
        id=comment_id,
        body="…",
        path=path,
        line_start=line_start,
        line_end=line_end,
        author_role="MEMBER",
        created_at="2026-08-01T10:00:00Z",
        iteration_index=0,
    )


def _facts_with_unrecoverable_iteration_carrying_a_diff() -> PullRequestFacts:
    """Two iterations covering the same line, the first of them unrecoverable *and* carrying a diff.

    The forge never produces this — an unrecoverable iteration's diff is empty by construction (FR-8),
    which is precisely why the real fixture cannot pin the rule: with an empty diff the iteration
    yields no spans and is excluded by accident, so deleting the explicit `recoverable` skip changes
    nothing observable. Constructing the state the skip actually guards against makes the test pin the
    rule ("an unrecoverable iteration is never claimed") rather than the coincidence that hides it.
    """
    return PullRequestFacts(
        pr_number=78,
        iterations=(
            ReviewIteration(tip_sha="c" * 40, base_sha="", diff=_NARROW_DIFF, comment_ids=(1,), recoverable=False),
            ReviewIteration(tip_sha="d" * 40, base_sha="e" * 40, diff=_NARROW_DIFF, comment_ids=(), recoverable=True),
        ),
        review_verdicts=(),
        comments=(_comment_at(1, "touched.py", 11, 11),),
    )


def _facts_with_out_of_diff_comments() -> PullRequestFacts:
    """A synthetic one-iteration PR whose four comments cover every way a location can miss the diff."""
    iteration = ReviewIteration(
        tip_sha="a" * 40, base_sha="b" * 40, diff=_NARROW_DIFF, comment_ids=(1, 2, 3, 4), recoverable=True
    )
    return PullRequestFacts(
        pr_number=77,
        iterations=(iteration,),
        review_verdicts=(),
        comments=(
            _comment_at(1, "touched.py", 11, 11),
            _comment_at(2, "touched.py", 900, 900),
            _comment_at(3, "never-touched.py", 11, 11),
            _comment_at(4, "", 0, 0),
        ),
    )


def _facts_reject_iteration_covers_nothing() -> PullRequestFacts:
    """Two recoverable iterations where the reject one (iteration 0) covers no comment and approve does.

    Reject builds from the first recoverable iteration, approve from the last; giving iteration 0 a diff
    on a *different* file than the single comment points at makes reject's count zero while approve's is
    one — the shape where one kind is buildable and the other warns."""
    reject_diff_on_other_file = _NARROW_DIFF.replace("touched.py", "other.py")
    return PullRequestFacts(
        pr_number=88,
        iterations=(
            ReviewIteration(
                tip_sha="a" * 40, base_sha="b" * 40, diff=reject_diff_on_other_file, comment_ids=(1,), recoverable=True
            ),
            ReviewIteration(tip_sha="c" * 40, base_sha="d" * 40, diff=_NARROW_DIFF, comment_ids=(1,), recoverable=True),
        ),
        review_verdicts=(),
        comments=(_comment_at(1, "touched.py", 11, 11),),
    )


def _facts_all_unrecoverable() -> PullRequestFacts:
    """Three iterations, none recoverable — every reviewed state force-pushed away (the refusal case).

    Uses PR 1234 so the `capture` verb writes `candidates/pr-1234.json`; the diffs are empty, as an
    unrecoverable iteration's diff is by construction (FR-8)."""
    return PullRequestFacts(
        pr_number=1234,
        iterations=(
            ReviewIteration(tip_sha="a" * 40, base_sha="", diff="", comment_ids=(1,), recoverable=False),
            ReviewIteration(tip_sha="b" * 40, base_sha="", diff="", comment_ids=(2,), recoverable=False),
            ReviewIteration(tip_sha="c" * 40, base_sha="", diff="", comment_ids=(), recoverable=False),
        ),
        review_verdicts=(),
        comments=(_comment_at(1, "code.py", 1, 1), _comment_at(2, "code.py", 2, 2)),
    )


def _fully_filled(tmp_path: Path) -> CandidateDict:
    """A candidate with every judgment slot filled coherently — the baseline the failure tests mutate."""
    candidate = _scaffold(tmp_path)
    for comment in candidate["comments"]:
        comment["classification"] = "nit"
        comment["classification_rationale"] = "stylistic preference"
    candidate["findings"] = [
        {
            "comment_ids": [9001],
            "statement": "`add` subtracts instead of adds",
            "severity": "high",
            "severity_evidence": "return a - b in round-1 diff",
            "severity_rationale": "wrong result for every caller",
            "reference_only": False,
        }
    ]
    candidate["change_risk"]["risk_classified"] = "low"
    candidate["change_risk"]["risk_classified_rationale"] = "isolated helper, well-tested"
    candidate["rubric_version"] = "v1"
    return candidate


def test_scaffold_fills_facts_leaves_slots_empty(tmp_path: Path) -> None:
    """Scaffold fills every mechanical fact and leaves every judgment slot present-and-empty (FR-9).

    The ADR-3 property, and it is what stops the CLI silently writing a label or omitting a slot the
    agent must fill. Every judgment key is asserted *present* (not merely falsy) so a dropped slot —
    which would hide work from the agent — fails here.
    """
    candidate = _scaffold(tmp_path)

    assert candidate["repo"] == "our-org/our-repo"
    assert candidate["pr_number"] == 1234
    assert candidate["pr_url"].endswith("/pull/1234")
    assert candidate["iterations"] and all(
        iteration["tip_sha"] and iteration["patch_path"] for iteration in candidate["iterations"]
    )
    assert candidate["review_verdicts"], "verdicts are facts and must be filled"

    for comment in candidate["comments"]:
        assert comment["path"] and comment["created_at"], "the six per-comment fields are facts"
        assert "in_diff_iterations" in comment, "the iterations whose diff covers the line are a fact, not a slot"
        assert comment["in_diff_iterations"] == sorted(comment["in_diff_iterations"]), "sorted, not set-ordered (FR-10)"
        assert "classification" in comment and comment["classification"] == ""
        assert "classification_rationale" in comment and comment["classification_rationale"] == ""

    assert candidate["findings"] == []
    assert candidate["change_risk"]["risk_structural"] == "high", "structural risk is a fact, computed by the CLI"
    assert candidate["change_risk"]["risk_structural_rule"]
    assert candidate["change_risk"]["risk_classified"] == "" and candidate["change_risk"]["risk_classified_rationale"] == ""
    assert candidate["rubric_version"] == ""


def test_capture_binds_comments_to_iterations(tmp_path: Path) -> None:
    """Each comment's `iteration_index` names the iteration it was written against (FR-12).

    A comment bound to the wrong before-state corrupts the oracle. 9001–9003 were left on round 1
    (index 0) and 9004 on round 2 (index 1); the iteration comment-id lists must agree.
    """
    candidate = _scaffold(tmp_path)
    by_id = {comment["id"]: comment for comment in candidate["comments"]}

    assert by_id[9001]["iteration_index"] == 0
    assert by_id[9004]["iteration_index"] == 1
    assert candidate["iterations"][0]["comment_ids"] == [9001, 9002, 9003]
    assert candidate["iterations"][1]["comment_ids"] == [9004]


def test_comment_carries_all_six_fields(tmp_path: Path) -> None:
    """Every inline comment carries body, path, line range, role, timestamp, and iteration (FR-12).

    Catches US-3's six-field capture silently dropping a field. The multi-line comment (9002) also
    proves the line *range* survives rather than collapsing to a single line.
    """
    candidate = _scaffold(tmp_path)
    comment = next(comment for comment in candidate["comments"] if comment["id"] == 9002)

    assert comment["body"].startswith("`range(len(items) + 1)`")
    assert comment["path"] == "code.py"
    assert (comment["line_start"], comment["line_end"]) == (5, 7)
    assert comment["author_role"] == "MEMBER"
    assert comment["created_at"] == "2026-08-01T10:01:00Z"
    assert comment["iteration_index"] == 0


def test_in_diff_iterations_differs_from_iteration_index(tmp_path: Path) -> None:
    """`in_diff_iterations` is geometric, `iteration_index` is chronological — they are not the same fact.

    Comment 9004 was written on round 2 (`iteration_index == 1`), but the line it points at is inside
    *both* iterations' diffs, so it binds to `[0, 1]`. Conflating the two fields is the exact
    mis-binding that puts a finding on the iteration the comment was written against rather than on one
    whose diff shows the line, which `verify` refuses as `finding-line-absent`.
    """
    candidate = _scaffold(tmp_path)
    by_id = {comment["id"]: comment for comment in candidate["comments"]}

    assert by_id[9004]["iteration_index"] == 1, "9004 was written on round 2"
    assert by_id[9004]["in_diff_iterations"] == [0, 1], "both rounds' diffs cover the line it points at"
    assert by_id[9001]["in_diff_iterations"] == [0, 1]


def test_in_diff_iterations_excludes_unrecoverable(tmp_path: Path) -> None:
    """An unrecoverable iteration never appears in any comment's list, however the geometry falls.

    Its `diff` is empty by construction (FR-8) and `emit` cannot build a change patch from it, so
    claiming a comment lives in it would hand the agent a slot that cannot be emitted. Comment 8001
    was written against the force-pushed-away round 1 (iteration 0, unrecoverable) and its line *is*
    inside the surviving iteration's diff — so the list must be exactly `[1]`.
    """
    candidate = _scaffold_unrecoverable(tmp_path)
    by_id = {comment["id"]: comment for comment in candidate["comments"]}

    assert candidate["iterations"][0]["patch_path"] == "", "iteration 0 is the unrecoverable one"
    assert by_id[8001]["iteration_index"] == 0, "8001 was written against the unrecoverable round"
    assert by_id[8001]["in_diff_iterations"] == [1], "the unrecoverable iteration is never claimed"
    for comment in candidate["comments"]:
        assert 0 not in comment["in_diff_iterations"]

    # The real fixture above cannot distinguish "skipped because unrecoverable" from "skipped because
    # its diff was empty" — both exclude iteration 0. This second bundle carries a diff *on* the
    # unrecoverable iteration, so only the explicit `recoverable` check keeps it out of the list.
    synthetic = Candidate.scaffold(
        _facts_with_unrecoverable_iteration_carrying_a_diff(),
        repo="our-org/our-repo",
        pr_url="https://github.com/our-org/our-repo/pull/78",
        risk_structural="low",
        risk_structural_rule="default → low",
    )
    assert synthetic["comments"][0]["in_diff_iterations"] == [1], (
        "an unrecoverable iteration must be skipped for being unrecoverable, not for having an empty diff"
    )


def test_comment_outside_every_diff_gets_empty_list(tmp_path: Path) -> None:
    """A comment on a line no iteration touches binds to `[]` — a fact, not a violation.

    An outdated or file-level comment is legitimate input, so an empty list must survive
    `validate_facts` untouched. Catches both halves of the plausible mistake: treating the empty list
    as malformed, and omitting the field when nothing matched.
    """
    candidate = Candidate.scaffold(
        _facts_with_out_of_diff_comments(),
        repo="our-org/our-repo",
        pr_url="https://github.com/our-org/our-repo/pull/77",
        risk_structural="low",
        risk_structural_rule="default → low",
    )
    by_id = {comment["id"]: comment for comment in candidate["comments"]}

    assert by_id[1]["in_diff_iterations"] == [0], "a comment on a touched line binds to the iteration"
    assert by_id[2]["in_diff_iterations"] == [], "line 900 is outside the only hunk"
    assert by_id[3]["in_diff_iterations"] == [], "a file the diff never mentions is outside"
    assert by_id[4]["in_diff_iterations"] == [], "an empty path yields [], not a crash"
    violations = Candidate.validate_facts(candidate)
    assert all(not violation.pointer.endswith(".in_diff_iterations") for violation in violations), (
        f"an out-of-diff comment is a fact, not a defect; got {violations}"
    )


def test_validate_facts_rejects_out_of_range_iteration_index(tmp_path: Path) -> None:
    """An `in_diff_iterations` entry past the end of `iterations[]` is a `candidate.facts-invalid` violation.

    Catches a hand-edited candidate pointing at an iteration that does not exist — which `emit` would
    otherwise carry into a selection it cannot satisfy. Returned as a list entry, never raised (ADR-2).
    """
    candidate = _scaffold(tmp_path)
    candidate["comments"][0]["in_diff_iterations"] = [0, 99]

    violations = Candidate.validate_facts(candidate)
    assert any(
        violation.code == "candidate.facts-invalid" and violation.pointer.endswith(".in_diff_iterations")
        for violation in violations
    ), f"expected a facts-invalid violation on in_diff_iterations, got {violations}"


def test_validate_facts_reports_a_candidate_captured_before_the_field(tmp_path: Path) -> None:
    """A candidate written by an older `capture` has no `in_diff_iterations`; that is reported, not tolerated.

    There is deliberately no back-fill (`capture` is cheap and deterministic, FR-10), so the missing
    fact must surface as a violation naming the field — the refusal already tells the reader to re-run
    `capture`. Catches the alternative failures: silently defaulting the field to `[]`, which would
    claim every comment is out of diff, or raising a `KeyError` out of a validator that must not raise.
    """
    candidate = _scaffold(tmp_path)
    del candidate["comments"][0]["in_diff_iterations"]  # type: ignore[misc]  # simulating an older file

    violations = Candidate.validate_facts(candidate)
    assert any(
        violation.code == "candidate.facts-missing" and violation.pointer.endswith(".in_diff_iterations")
        for violation in violations
    ), f"expected a facts-missing violation naming the field, got {violations}"


def test_slot_summary_does_not_report_per_kind_counts(tmp_path: Path) -> None:
    """`slot_summary` lists only the fill slots; per-kind emittability belongs to `emittable_report`.

    The per-kind reachable counts belong in the `emittable:` block, not the slot list: `emittable_report`
    computes them once via `emittability` and prints them under a distinct heading, so keeping them in
    the slot list too would print the same fact twice. This pins the split.
    """
    summary = "\n".join(Candidate.slot_summary(_scaffold(tmp_path)))

    assert "builds from iteration" not in summary, "per-kind reporting belongs to emittable_report, not slot_summary"
    assert "inside its diff" not in summary
    assert "comments[].classification" in summary, "slot_summary still names the fill slots"


def test_emittability_reject_is_first_approve_is_last(tmp_path: Path) -> None:
    """Emittability selects the first recoverable iteration for reject, the last for approve — as `emit` does.

    Two verbs disagreeing about which iteration a kind uses would let `capture` report a count for one
    iteration while `emit` builds from another. Pinned directly against `Emit._select_iteration` so the
    two cannot drift.
    """
    candidate = _scaffold(tmp_path)  # both iterations 0 and 1 are recoverable
    emittability = Candidate.emittability(candidate)

    assert emittability.recoverable_indices == [0, 1]
    assert emittability.reject_index == 0 and emittability.approve_index == 1
    # `_select_iteration` returns (index, iteration); the default (iteration_index=None) must agree
    # with emittability on both the index and the iteration it names.
    reject_index, reject_iteration = Emit._select_iteration(candidate, "reject", "pr-1234", None)
    approve_index, approve_iteration = Emit._select_iteration(candidate, "approve", "pr-1234", None)
    assert reject_index == emittability.reject_index
    assert reject_iteration == candidate["iterations"][emittability.reject_index]
    assert approve_index == emittability.approve_index
    assert approve_iteration == candidate["iterations"][emittability.approve_index]
    # All four fixture comments fall inside both iterations' diffs.
    assert emittability.reject_in_diff_comment_ids == [9001, 9002, 9003, 9004]
    assert emittability.approve_in_diff_comment_ids == [9001, 9002, 9003, 9004]


def test_emittability_ignores_unrecoverable_indices(tmp_path: Path) -> None:
    """An unrecoverable iteration is never named for either kind, nor listed as recoverable.

    Naming iteration 0 (force-pushed away) for a kind would hand the agent a selection `emit` refuses.
    The fixture's iteration 0 is unrecoverable and iteration 1 recoverable, so both kinds must resolve
    to 1 and 0 must be absent everywhere.
    """
    candidate = _scaffold_unrecoverable(tmp_path)
    emittability = Candidate.emittability(candidate)

    assert emittability.recoverable_indices == [1]
    assert emittability.reject_index == 1 and emittability.approve_index == 1
    assert 0 not in emittability.recoverable_indices


def test_emittability_none_when_no_iteration_recoverable(tmp_path: Path) -> None:
    """With no recoverable iteration, both kinds have no index and no comments — the refusal case (FR-11).

    `as_json` must render the documented `null`-per-kind shape so a driver can branch on it; the comment
    id lists are empty rather than absent so the shape is stable.
    """
    candidate = _scaffold(tmp_path)
    for iteration in candidate["iterations"]:
        iteration["patch_path"] = ""  # every reviewed state force-pushed away

    emittability = Candidate.emittability(candidate)

    assert emittability.recoverable_indices == []
    assert emittability.reject_index is None and emittability.approve_index is None
    assert emittability.reject_in_diff_comment_ids == [] and emittability.approve_in_diff_comment_ids == []
    assert emittability.as_json() == {"recoverable_indices": [], "reject": None, "approve": None}


def test_emittable_report_warns_only_when_a_kind_covers_no_comment() -> None:
    """The report names each kind's iteration and count, and warns under a kind that covers nothing.

    Reject builds from iteration 0, whose diff touches `other.py` and so covers none of the single
    comment (on `touched.py`); approve builds from iteration 1, whose diff covers it. The warning must
    sit under reject only, and it must name `finding-line-absent` so the agent knows why.
    """
    candidate = Candidate.scaffold(
        _facts_reject_iteration_covers_nothing(),
        repo="our-org/our-repo",
        pr_url="https://github.com/our-org/our-repo/pull/88",
        risk_structural="low",
        risk_structural_rule="default → low",
    )
    lines = Candidate.emittable_report(candidate, Candidate.emittability(candidate))

    assert lines[0] == "reject   iteration 0 — 0 of 1 comment(s) inside its diff"
    assert lines[1].strip() == "warning: a finding built on any of these comments will fail verify's finding-line-absent"
    assert lines[2] == "approve  iteration 1 — 1 of 1 comment(s) inside its diff"
    assert len(lines) == 3, "a covered kind carries no warning line"


def test_slot_summary_names_finding_fields(tmp_path: Path) -> None:
    """`capture`'s stdout names every `findings[]` field, so filling one needs no source read.

    Without this, filling a finding would require reading `candidate.py` to learn the shape. Naming the
    fields where the agent already looks avoids that; asserting on `FindingDict.__annotations__` rather
    than a literal list is what keeps stdout honest when a field is added.
    """
    summary = "\n".join(Candidate.slot_summary(_scaffold(tmp_path)))

    for field in FindingDict.__annotations__:
        assert field in summary, f"`capture` stdout does not name the findings[] field {field!r}"
    assert "required unless reference_only" in summary, "stdout must state the severity-evidence rule"
    assert "bot" in summary, "stdout must name all five classification values, `bot` included"


def test_validate_filled_baseline_is_clean(tmp_path: Path) -> None:
    """A coherently filled candidate has no slot violations — so the failure tests isolate their defect."""
    assert Candidate.validate_filled(_fully_filled(tmp_path)) == []


def test_validate_filled_rejects_empty_classification(tmp_path: Path) -> None:
    """`validate_filled` returns a violation when a comment classification is blank (FR-13).

    Catches an unclassified comment reaching `emit`. The validator returns the violation as a list
    entry pointing at the offending comment — it never raises, so `emit` can aggregate.
    """
    candidate = _fully_filled(tmp_path)
    candidate["comments"][0]["classification"] = ""

    violations = Candidate.validate_filled(candidate)
    assert any(v.code == "candidate.unclassified-comment" and "classification" in v.pointer for v in violations)


def test_validate_filled_rejects_finding_missing_evidence(tmp_path: Path) -> None:
    """A finding with a severity but no evidence/rationale is a violation (FR-15).

    Catches a severity asserted with no recorded basis — the failure that would let the eval grade
    on an unsupported judgment.
    """
    candidate = _fully_filled(tmp_path)
    candidate["findings"][0]["severity_evidence"] = ""
    candidate["findings"][0]["severity_rationale"] = ""

    violations = Candidate.validate_filled(candidate)
    codes = {v.code for v in violations}
    assert "candidate.finding-missing-evidence" in codes
    assert all(v.code != "candidate.finding-severity-invalid" for v in violations), (
        "severity itself is valid; only evidence is missing"
    )


def test_reference_only_finding_skips_severity_gate(tmp_path: Path) -> None:
    """A `reference_only` finding is exempt from the severity-evidence gate (FR-16).

    Catches over-strict validation blocking a legitimate late-comment reference: a reference-only
    finding may carry no severity and no evidence and still be coherent.
    """
    candidate = _fully_filled(tmp_path)
    candidate["findings"] = [
        {
            "comment_ids": [9004],
            "statement": "",
            "severity": "",
            "severity_evidence": "",
            "severity_rationale": "",
            "reference_only": True,
        }
    ]

    violations = Candidate.validate_filled(candidate)
    assert all(not v.pointer.startswith("findings[") for v in violations), (
        "a reference-only finding must not trip the severity gate"
    )


def test_validate_facts_rejects_unbound_comment(tmp_path: Path) -> None:
    """`validate_facts` flags a comment bound to no real iteration (FR-12).

    Catches a fact-level defect — a comment whose `iteration_index` points outside the recorded
    iterations — before the agent ever fills a slot.
    """
    candidate = _scaffold(tmp_path)
    candidate["comments"][0]["iteration_index"] = 99

    violations = Candidate.validate_facts(candidate)
    assert any(v.code == "candidate.comment-unbound" for v in violations)


def test_risk_structural_written_not_classified(tmp_path: Path) -> None:
    """`risk_structural` is set from the path rule; `risk_classified` stays blank (FR-19, FR-20).

    Catches the CLI leaking into the agent's judgment territory. The structural half is mechanical
    (a path rule); the classified half is the agent's and must be left empty by `capture`.
    """
    candidate = _scaffold(tmp_path)

    assert candidate["change_risk"]["risk_structural"] == "high"
    assert candidate["change_risk"]["risk_structural_rule"] == "code.py → high"
    assert candidate["change_risk"]["risk_classified"] == ""
    assert candidate["change_risk"]["risk_classified_rationale"] == ""


def _risk_map_bytes() -> bytes:
    """The dataset's `risk-map.toml` bytes, written the byte-stable way `init` would (via the emitter)."""
    return emit_document(_RISK_MAP)


def _run_capture(fixture: Any, dataset: Path, monkeypatch: Any, *, json_mode: bool = False) -> int:
    """Drive the full `capture` verb end-to-end with `gh` replayed from the fixture's recorded payloads."""
    payload_by_suffix = {"reviews": fixture.reviews, "comments": fixture.comments, "timeline": fixture.timeline}

    def fake_gh(argv_tail: list[str], *, repo: str | None = None) -> tuple[int, str, str]:
        payload = payload_by_suffix[argv_tail[1].rsplit("/", 1)[1]]
        return 0, json.dumps(payload), ""

    monkeypatch.setattr(GitCommandRunner, "gh", staticmethod(fake_gh))
    json_flag = ["--json"] if json_mode else []
    return Cli.run([*json_flag, "capture", "1234", "--clone", str(fixture.clone), "--dataset", str(dataset)])


def _run_capture_returning(facts: PullRequestFacts, tmp_path: Path, monkeypatch: Any) -> tuple[int, Path]:
    """Drive `capture` with `Forge.capture` stubbed to return `facts`, over a real fixture clone.

    Lets a test pin `capture`'s post-fetch behaviour — the no-emittable refusal, and the warning for a
    kind whose iteration covers nothing — on fact bundles the recorded fixtures cannot produce, without
    recording a new `gh` payload. The clone is a real git repo so the repo-slug derivation runs for
    real; only the forge fetch is replaced. Returns the exit code and the dataset root."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(_risk_map_bytes())
    monkeypatch.setattr(Forge, "capture", staticmethod(lambda repo, pr_number, clone: facts))
    code = Cli.run(["capture", "1234", "--clone", str(fixture.clone), "--dataset", str(dataset)])
    return code, dataset


def test_capture_refuses_when_no_iteration_recoverable(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """`capture` refuses `no-emittable-iteration` (exit 3) with all four fields when nothing is recoverable.

    When every reviewed state was force-pushed away, no datapoint of either kind is possible. Catching
    it at `capture` saves the agent a full fill that `emit` would only reject afterward. The offending
    field must count the iterations and the next field must name the pull ref.
    """
    code, _ = _run_capture_returning(_facts_all_unrecoverable(), tmp_path, monkeypatch)

    assert code == ExitCode.REFUSAL
    stderr = capsys.readouterr().err
    assert "no-emittable-iteration" in stderr
    assert "pr-1234" in stderr
    assert "all 3 reviewed states were force-pushed away" in stderr
    assert "refs/pull/1234/head" in stderr


def test_capture_still_writes_candidate_on_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    """The candidate is written before the refusal — it is the only record of what the forge returned.

    Catches "tidying" the refusal above the write: an empty result and a lost record look identical to
    the agent otherwise, and the refusal's own wording promises the file exists.
    """
    code, dataset = _run_capture_returning(_facts_all_unrecoverable(), tmp_path, monkeypatch)

    assert code == ExitCode.REFUSAL
    assert (dataset / "candidates" / "pr-1234.json").is_file(), "the candidate must survive the refusal for the record"


def test_capture_reports_per_kind_in_diff_counts(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """`capture` prints, per kind, the iteration it builds from and the in-diff comment count (exit 0).

    Without it the agent fills every finding blind and learns which comments were reachable only from
    `emit`'s `finding-line-absent`. Both fixture iterations cover all four comments, so both kinds read
    `4 of 4` under a distinct `emittable:` block.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(_risk_map_bytes())

    assert _run_capture(fixture, dataset, monkeypatch) == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert "emittable:" in out
    assert "reject   iteration 0 — 4 of 4 comment(s) inside its diff" in out
    assert "approve  iteration 1 — 4 of 4 comment(s) inside its diff" in out


def test_capture_warns_when_a_kind_has_no_in_diff_comments(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """A kind whose iteration covers no comment gets a warning line, and `capture` still exits 0.

    Reject builds from iteration 0 (a diff on a different file), which covers none of the single
    comment; approve builds from iteration 1, which covers it. Refusing here would cost the buildable
    approve datapoint, so it is a warning, not a refusal — the whole point of the warning-not-refusal distinction.
    """
    code, _ = _run_capture_returning(_facts_reject_iteration_covers_nothing(), tmp_path, monkeypatch)

    assert code == ExitCode.SUCCESS, "one buildable kind must not be refused for the other's zero coverage"
    out = capsys.readouterr().out
    assert "reject   iteration 0 — 0 of 1 comment(s) inside its diff" in out
    assert "warning: a finding built on any of these comments will fail verify's finding-line-absent" in out
    assert "approve  iteration 1 — 1 of 1 comment(s) inside its diff" in out


def test_capture_does_not_refuse_with_one_recoverable_iteration(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """A PR with a single recoverable iteration is reported, not refused (exit 0) — no over-refusal.

    The force-pushed fixture leaves only iteration 1 recoverable; both kinds must resolve to it and the
    verb must succeed, or the harvestable set shrinks for no reason.
    """
    fixture = RepoBuilder.build_pull_request_with_unrecoverable_iteration(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(_risk_map_bytes())

    assert _run_capture(fixture, dataset, monkeypatch) == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert "emittable:" in out
    assert "reject   iteration 1" in out and "approve  iteration 1" in out


def test_capture_json_payload_carries_emittable(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """`capture --json` carries the `emittable` object so a driver can branch without parsing prose.

    Both fixture iterations are recoverable and cover all four comments, so the payload names iteration
    0 for reject and 1 for approve, each with the comment ids inside its diff.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(_risk_map_bytes())

    assert _run_capture(fixture, dataset, monkeypatch, json_mode=True) == ExitCode.SUCCESS
    payload = json.loads(capsys.readouterr().out)
    emittable = payload["emittable"]
    assert emittable["recoverable_indices"] == [0, 1]
    assert emittable["reject"] == {"iteration": 0, "in_diff_comment_ids": [9001, 9002, 9003, 9004]}
    assert emittable["approve"] == {"iteration": 1, "in_diff_comment_ids": [9001, 9002, 9003, 9004]}


def test_capture_output_byte_identical_twice(tmp_path: Path, monkeypatch: Any) -> None:
    """Two `capture` runs on the fixed fixture and replayed payloads produce identical bytes (FR-10).

    Catches nondeterministic dict/JSON ordering breaking downstream byte-stability. Drives the whole
    verb — repo derivation, forge fetch (gh replayed), risk computation, patch materialization, and
    dump — so the determinism guarantee holds over the real code path, not just `scaffold`.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(_risk_map_bytes())

    code = _run_capture(fixture, dataset, monkeypatch)
    assert code == ExitCode.SUCCESS
    candidate_path = dataset / "candidates" / "pr-1234.json"
    first = candidate_path.read_bytes()

    code_again = _run_capture(fixture, dataset, monkeypatch)
    assert code_again == ExitCode.SUCCESS
    assert candidate_path.read_bytes() == first, "capture must be byte-identical on identical inputs (FR-10)"

    # The materialized patch bytes back the candidate's patch_path, so emit needs no git (NFR-2).
    written = json.loads(first)
    for index in range(len(written["iterations"])):
        assert (dataset / patch_relpath(1234, index)).is_file()

    # `in_diff_iterations` is derived from a dict of spans, so it is the field most able to reintroduce
    # set-iteration order into the bytes above. Assert it landed sorted as well as identical (FR-10).
    for comment in written["comments"]:
        assert comment["in_diff_iterations"] == sorted(comment["in_diff_iterations"])


def test_materialized_patch_ends_with_newline(tmp_path: Path, monkeypatch: Any) -> None:
    """Every patch `capture` writes is newline-terminated, so `git apply` will accept it (FR-34).

    Guards a real failure mode: `gitcmd` returns stripped stdout, so an unterminated final line makes
    the image build die with `corrupt patch at line <last>`. Asserted on the bytes of the shipped file,
    not on a normalized copy — a checker that normalizes cannot see this.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(_risk_map_bytes())

    assert _run_capture(fixture, dataset, monkeypatch) == ExitCode.SUCCESS

    patches = sorted((dataset / "patches").glob("*.patch"))
    assert patches, "capture materialized no patch to check"
    for patch in patches:
        assert patch.read_bytes().endswith(b"\n"), f"{patch.name} ships unterminated; `git apply` will refuse it"


def test_materialized_patch_is_not_double_terminated(tmp_path: Path) -> None:
    """A diff that already ends in `\\n` gains no second one — the fix normalizes, it does not append.

    Catches an unconditional `+ b"\\n"`, which would change the stored bytes for no reason and make the
    patch's tail depend on how the diff reached us rather than on the diff itself (NFR-1).
    """
    already_terminated = "--- a/touched.py\n+++ b/touched.py\n@@ -11 +11 @@\n-a\n+b\n"
    terminated_iteration = ReviewIteration(
        tip_sha="a" * 40, base_sha="b" * 40, diff=already_terminated, comment_ids=(), recoverable=True
    )
    facts = PullRequestFacts(pr_number=79, iterations=(terminated_iteration,), review_verdicts=(), comments=())
    dataset = tmp_path / "ds-terminated"
    dataset.mkdir()

    Candidate._materialize_patches(facts, dataset)

    written = (dataset / patch_relpath(79, 0)).read_bytes()
    assert written == already_terminated.encode("utf-8"), "an already-terminated diff must be stored verbatim"


def test_capture_refuses_without_risk_map(tmp_path: Path, monkeypatch: Any) -> None:
    """`capture` refuses (exit 3) when the dataset has no `risk-map.toml`, rather than guessing risk."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    dataset = tmp_path / "empty-ds"
    dataset.mkdir()

    code = _run_capture(fixture, dataset, monkeypatch)
    assert code == ExitCode.REFUSAL


def test_patch_relpath_is_deterministic() -> None:
    """The patch path is a pure function of PR number and index — the seam scaffold and capture share."""
    assert patch_relpath(1234, 0) == "patches/pr-1234-iter0.patch"
    assert patch_relpath(7, 2) == "patches/pr-7-iter2.patch"


def test_violation_str_is_readable() -> None:
    """A `Violation` renders as `code at pointer: detail`, the shape emit folds into a refusal."""
    assert (
        str(Violation("candidate.rubric-unpinned", "rubric_version", "pin it"))
        == "candidate.rubric-unpinned at rubric_version: pin it"
    )
