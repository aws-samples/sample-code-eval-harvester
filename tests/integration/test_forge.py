"""Tests for the deep forge fetch: iterations, inline comments, verdicts, timing (task B-2).

The recovery and diff behaviour B-2 produces *is* git's behaviour, so every scenario runs against a
real fixture repository whose PR head lives at ``refs/pull/<n>/head`` (a normal clone never fetches
that ref — only the explicit head fetch recovers a squash-merged, branch-deleted PR). The ``gh``
REST payloads are replayed from recorded templates rather than hitting GitHub (§12). The properties
that matter: a squash-merged before-state is recovered rather than lost, a comment binds to the
iteration it was written against, an unreachable tip is reported unrecoverable rather than
fabricated, and the whole bundle is deterministic on fixed inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fixtures.repo_builder import RepoBuilder

from eval_harvest.forge import Forge, PullRequestFacts, ReviewIteration
from eval_harvest.gitcmd import GitCommandRunner


def _reconstruct(fixture: Any) -> PullRequestFacts:
    """Reconstruct the fact bundle from a built fixture's clone and its recorded payloads."""
    return Forge.reconstruct_facts(
        fixture.pr_number,
        fixture.clone,
        fixture.base_ref,
        reviews=fixture.reviews,
        comments=fixture.comments,
        timeline=fixture.timeline,
    )


def _iteration_with_tip(facts: PullRequestFacts, tip_sha: str) -> ReviewIteration:
    """The single iteration whose tip is ``tip_sha`` — fails loudly if absent."""
    matches = [iteration for iteration in facts.iterations if iteration.tip_sha == tip_sha]
    assert matches, f"no iteration with tip {tip_sha[:8]}; tips were {[it.tip_sha[:8] for it in facts.iterations]}"
    return matches[0]


def test_squash_merged_before_state_recovered(tmp_path: Path) -> None:
    """A squash-merged, branch-deleted PR still yields both iteration tips and diffs via the pull head.

    Catches the squash-recovery chain silently producing the wrong before-state — the failure the
    north star turns on (S-1, FR-8). Before the head fetch the clone cannot even name the tip; the
    fetch recovers it, and the round-1 diff is the pre-fix state, not the merged one.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path, pull_head_fetched=False)

    before_fetch, _ = GitCommandRunner.git(fixture.clone, "rev-parse", "--verify", "--quiet", f"{fixture.round2_tip}^{{commit}}")
    assert before_fetch != 0, "the plain clone must not already hold the pull head (else the test proves nothing)"

    Forge.fetch_pull_request_head(fixture.clone, fixture.pr_number)
    facts = _reconstruct(fixture)

    round1 = _iteration_with_tip(facts, fixture.round1_tip)
    round2 = _iteration_with_tip(facts, fixture.round2_tip)
    assert round1.recoverable and round2.recoverable
    assert round1.base_sha == fixture.base_sha and round2.base_sha == fixture.base_sha
    assert "return a - b" in round1.diff, "round 1 must show the pre-fix (buggy) state"
    assert "return a + b" in round2.diff, "round 2 must show the fix reviewers approved"


def test_comment_bound_to_correct_iteration(tmp_path: Path) -> None:
    """A round-1 comment binds to iteration 0 and a round-2 comment to iteration 1 (FR-12).

    Catches a comment bound to the wrong iteration, which would corrupt the oracle. Binding is by
    ``original_commit_id`` so it survives the later push that moved ``commit_id`` forward.
    """
    facts = _reconstruct(RepoBuilder.build_squash_merged_pull_request(tmp_path))
    by_id = {comment.id: comment for comment in facts.comments}

    assert by_id[9001].iteration_index == 0
    assert by_id[9004].iteration_index == 1
    round1 = _iteration_with_tip(facts, facts.iterations[0].tip_sha)
    assert facts.iterations[0].comment_ids == (9001, 9002, 9003)
    assert facts.iterations[1].comment_ids == (9004,)
    assert round1.tip_sha != facts.iterations[1].tip_sha


def test_comment_carries_six_fields(tmp_path: Path) -> None:
    """Each inline comment carries body, path, line range, role, timestamp, and iteration (FR-12).

    Catches US-3's six-field capture dropping a field. The multi-line comment (9002) also proves the
    line *range* is preserved, not collapsed to a single line.
    """
    facts = _reconstruct(RepoBuilder.build_squash_merged_pull_request(tmp_path))
    comment = next(comment for comment in facts.comments if comment.id == 9002)

    assert comment.body.startswith("`range(len(items) + 1)`")
    assert comment.path == "code.py"
    assert (comment.line_start, comment.line_end) == (5, 7)
    assert comment.author_role == "MEMBER"
    assert comment.created_at == "2026-08-01T10:01:00Z"
    assert comment.iteration_index == 0


def test_verdicts_ordered_with_state_and_timing(tmp_path: Path) -> None:
    """Every review submission is listed with its state and submission time, in order (FR-7).

    Catches an agent unable to tell a rejected state from an approved one, or verdicts out of order.
    """
    facts = _reconstruct(RepoBuilder.build_squash_merged_pull_request(tmp_path))

    assert [verdict.state for verdict in facts.review_verdicts] == ["CHANGES_REQUESTED", "APPROVED"]
    assert [verdict.submitted_at for verdict in facts.review_verdicts] == ["2026-08-01T10:00:00Z", "2026-08-02T12:00:00Z"]
    assert all(verdict.author == "reviewer-alice" and verdict.author_role == "MEMBER" for verdict in facts.review_verdicts)


def test_unreachable_iteration_reported_not_fabricated(tmp_path: Path) -> None:
    """A force-pushed, unfetchable round-1 tip is marked unrecoverable with no invented diff (FR-8).

    Catches a fabricated before-state passing as real — the round-1 review still names the discarded
    tip, and reporting a diff for a commit the clone cannot reach would be pure fabrication.
    """
    fixture = RepoBuilder.build_pull_request_with_unrecoverable_iteration(tmp_path)
    facts = _reconstruct(fixture)

    unrecoverable = _iteration_with_tip(facts, fixture.unrecoverable_tip)
    assert not unrecoverable.recoverable
    assert unrecoverable.base_sha == "" and unrecoverable.diff == ""

    approved = _iteration_with_tip(facts, fixture.round2_tip)
    assert approved.recoverable and approved.diff != ""


def test_forge_deterministic_twice(tmp_path: Path) -> None:
    """Two reconstructions over the same clone and payloads produce identical bundles (FR-10).

    Catches nondeterministic ordering (of iterations, comments, or verdicts) breaking downstream
    byte-stability.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    assert _reconstruct(fixture) == _reconstruct(fixture)


def test_capture_makes_a_bounded_number_of_forge_round_trips(tmp_path: Path, monkeypatch: Any) -> None:
    """`capture` costs one pull-head fetch plus one gh call each for reviews, comments, timeline.

    NFR-7's target is a PRD ``[TODO]``, so this does not assert against a target — it *measures* the
    round trips (surfacing four: one network `git fetch` and three `gh api` reads) so any later
    change that multiplies them is visible. `gh` is faked from the recorded payloads; the local
    `git` work `reconstruct_facts` does runs for real against the fetched clone.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path, pull_head_fetched=False)
    payload_by_suffix = {"reviews": fixture.reviews, "comments": fixture.comments, "timeline": fixture.timeline}
    counts = {"gh": 0, "git_fetch": 0}

    def fake_gh(argv_tail: list[str], *, repo: str | None = None) -> tuple[int, str, str]:
        counts["gh"] += 1
        api_path = argv_tail[1]
        payload = payload_by_suffix[api_path.rsplit("/", 1)[1]]
        return 0, json.dumps(payload), ""

    # The fetch routes through `git_with_stderr` (it captures git's failure reason so a failed
    # fetch can be refused, not crashed), and `git` delegates to it — so spying there counts every
    # fetch however it was issued, while rev-parse/merge-base/diff still run for real.
    real_git_with_stderr = GitCommandRunner.git_with_stderr

    def counting_git_with_stderr(repo: Path, *args: str) -> tuple[int, str, str]:
        if args and args[0] == "fetch":
            counts["git_fetch"] += 1
        return real_git_with_stderr(repo, *args)

    monkeypatch.setattr(GitCommandRunner, "gh", staticmethod(fake_gh))
    monkeypatch.setattr(GitCommandRunner, "git_with_stderr", staticmethod(counting_git_with_stderr))

    facts = Forge.capture("owner/name", fixture.pr_number, fixture.clone, base_ref=fixture.base_ref)

    assert counts["gh"] == 3, "reviews + comments + timeline — the fetch cost driver (TP-3, NFR-7)"
    assert counts["git_fetch"] == 1, "one pull-head fetch recovers the branch-deleted PR (FR-8)"
    assert len(facts.iterations) == 2 and all(iteration.recoverable for iteration in facts.iterations)
