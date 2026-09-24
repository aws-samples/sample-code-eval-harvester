"""Deep-fetch one pull request's review iterations, inline comments, verdicts, and timing.

``survey`` (B-1) reports only the *count* of review rounds; this module produces the raw material
a datapoint is built from. For one PR it recovers, per review iteration, the tip and base SHA and
the unified diff against that base, binds every inline comment to the iteration it was written
against (with its path, line range, author role, and timestamp), and lists every review verdict
with its timing. This is the fetch ``survey`` deliberately does not do (TP-3): the verdict-level
``gh pr view --json reviews`` carries no per-line comments, so the REST endpoints
(``pulls/<n>/reviews``, ``/comments``, ``issues/<n>/timeline``) plus a
``git fetch +refs/pull/<n>/head`` are what US-3 (FR-12) needs.

**Facts, not judgment (FR-9).** Everything here is mechanical — no classification, no severity, no
label. The returned :class:`PullRequestFacts` bundle is exactly what ``candidate.scaffold`` (B-3)
consumes to write the candidate file's *facts* half, leaving the judgment slots blank.

**How iterations are reconstructed.** Each submitted review carries the ``commit_id`` the reviewer
saw, and each inline comment carries the ``original_commit_id`` it was first written against; the
union of those SHAs is the set of *states reviewers engaged with*. They are ordered by the push
sequence the timeline records (``committed`` events), falling back to engagement time then SHA so
the order is stable (FR-10). A comment binds to the iteration whose tip equals its
``original_commit_id`` — the field GitHub pins to the original commit even after later pushes move
``commit_id`` forward — which is why binding survives a rebase (FR-12).

**Recoverability is git's answer, never a guess (FR-8, §4 risk).** ``refs/pull/<n>/head`` is a
single tip: the added-commit iterations and the final pre-merge tip are reachable from it, but a
round-1 state later force-pushed away may be GC'd on the forge and unfetchable. An iteration whose
tip is not present in the fetched clone is reported ``recoverable=False`` with no diff — a
fabricated before-state that passes as real is the expensive silent failure this refuses to
produce.

**Layer (ADR-4).** This is the online layer: :meth:`Forge.capture` is the only entry that touches
the network, and every ``git``/``gh`` call goes through :class:`~eval_harvest.gitcmd.GitCommandRunner`
(A-2) so the argv-only, prompt-disabled, token-stripped discipline holds. :meth:`Forge.reconstruct_facts`
is offline: given already-fetched payloads and a clone that already holds the pull head, it makes no
network call, which is what lets the tests replay recorded ``gh`` payloads against a real fixture
repo (§12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any  # gh returns arbitrary JSON objects; dict[str, Any] is the honest shape.

from eval_harvest.gitcmd import GitCommandRunner

#: Sort rank for an iteration tip the timeline never mentions: it sorts after every tip the push
#: sequence *does* order, so a known push order always wins and unknown tips fall back to time+SHA.
_UNORDERED_RANK = 1 << 30

#: The iteration index of a comment that binds to no known iteration tip (neither its
#: ``original_commit_id`` nor its ``commit_id`` is a state a review or another comment engaged
#: with). Reported honestly rather than forced onto an arbitrary round.
_UNBOUND_ITERATION = -1


class ForgeError(RuntimeError):
    """A forge fetch could not complete — a ``gh``/``git`` call failed. Carries the underlying message."""


@dataclass(frozen=True, slots=True)
class ReviewIteration:
    """One state reviewers saw: its tip, the base the diff is against, that diff, and its comments.

    ``recoverable`` is ``False`` when the tip is not present in the fetched clone (force-pushed away
    and GC'd on the forge); ``base_sha`` and ``diff`` are then empty — never fabricated (FR-8).
    """

    tip_sha: str
    base_sha: str
    diff: str
    comment_ids: tuple[int, ...]
    recoverable: bool


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    """One submitted review: its state, who submitted it, their repo role, and when (FR-7)."""

    state: str
    author: str
    author_role: str
    submitted_at: str


@dataclass(frozen=True, slots=True)
class ReviewComment:
    """One inline review comment with the six fields US-3 requires, bound to its iteration (FR-12)."""

    id: int
    body: str
    path: str
    line_start: int
    line_end: int
    author_role: str
    created_at: str
    iteration_index: int


@dataclass(frozen=True, slots=True)
class PullRequestFacts:
    """The mechanical fact bundle for one PR — iterations, verdicts, comments — that B-3 consumes."""

    pr_number: int
    iterations: tuple[ReviewIteration, ...]
    review_verdicts: tuple[ReviewVerdict, ...]
    comments: tuple[ReviewComment, ...]


class Forge:
    """Fetch and reconstruct one PR's review iterations, comments, and verdicts. Holds no state."""

    # ───────────────────────────── online entry (network) ─────────────────────────────

    @classmethod
    def capture(cls, repo: str, pr_number: int, clone: Path, base_ref: str = "HEAD") -> PullRequestFacts:
        """Fetch one PR's facts: recover its head, read reviews/comments/timeline, reconstruct.

        The only network entry point. It makes a bounded number of forge round trips per PR — one
        ``git fetch`` of the pull head plus one ``gh api`` call each for reviews, comments, and the
        timeline (NFR-7; the count is measured in the tests, not asserted against a target). The
        local ``git`` work that :meth:`reconstruct_facts` does (reachability, merge-base, diff) runs
        against the fetched clone and touches no remote.
        """
        cls.fetch_pull_request_head(clone, pr_number)
        reviews = cls._fetch_api_list(f"repos/{repo}/pulls/{pr_number}/reviews")
        comments = cls._fetch_api_list(f"repos/{repo}/pulls/{pr_number}/comments")
        timeline = cls._fetch_api_list(f"repos/{repo}/issues/{pr_number}/timeline")
        return cls.reconstruct_facts(pr_number, clone, base_ref, reviews=reviews, comments=comments, timeline=timeline)

    @classmethod
    def fetch_pull_request_head(cls, clone: Path, pr_number: int, remote: str = "origin") -> None:
        """Fetch ``refs/pull/<n>/head`` into ``refs/remotes/pr/<n>`` (the same ref ``survey`` reads).

        This is what makes a squash-merged or rebased PR harvestable at all: the branch is usually
        deleted after merge, but the forge retains the head, so the pre-merge tip (and the
        added-commit iterations reachable from it) can still be recovered (FR-8).
        """
        cls.fetch_refs(clone, f"+refs/pull/{pr_number}/head:refs/remotes/pr/{pr_number}", remote=remote)

    @classmethod
    def fetch_refs(cls, clone: Path, refspec: str, *, remote: str = "origin") -> None:
        """Fetch one ``<refspec>`` from ``<remote>`` into ``clone``, or raise ``ForgeError`` with git's stderr.

        The single-refspec primitive both the per-PR head fetch and ``survey``'s bulk
        ``+refs/pull/*/head:refs/remotes/pr/*`` run through — so there is one fetch, one owner,
        and one place the failure reason is captured. Uses :meth:`GitCommandRunner.git_with_stderr`
        because a fetch that fails writes its reason ("``fatal: '…' does not appear to be a git
        repository``") to stderr, and the caller's refusal must be able to name it rather than crash.
        """
        code, out, err = GitCommandRunner.git_with_stderr(clone, "fetch", remote, refspec)
        if code != 0:
            raise ForgeError(f"git fetch {refspec!r} from {remote} failed ({code}): {err or out}")

    @classmethod
    def _fetch_api_list(cls, api_path: str) -> list[dict[str, Any]]:
        """Read a ``gh api`` endpoint that returns a JSON array, or fail loudly with its own message."""
        code, out, err = GitCommandRunner.gh(["api", api_path])
        if code != 0:
            raise ForgeError(f"gh api {api_path} failed ({code}): {err}")
        loaded = json.loads(out)
        if not isinstance(loaded, list):
            raise ForgeError(f"gh api {api_path} returned {type(loaded).__name__}, expected a JSON array")
        return loaded

    # ───────────────────────────── offline reconstruction ─────────────────────────────

    # Six parameters is the contract: the three recorded payloads are the replay seam the tests need
    # (a clone + the reviews/comments/timeline gh returned), so they are inputs, not fetched here.
    @classmethod
    def reconstruct_facts(  # noqa: PLR0913
        cls,
        pr_number: int,
        clone: Path,
        base_ref: str,
        *,
        reviews: list[dict[str, Any]],
        comments: list[dict[str, Any]],
        timeline: list[dict[str, Any]],
    ) -> PullRequestFacts:
        """Turn already-fetched payloads + a clone that holds the pull head into the fact bundle.

        Offline and deterministic: no network call, and every collection in the output is ordered by
        a stable key (iterations by push order then time+SHA, comments by id, verdicts by time+id),
        so two runs over identical inputs are byte-identical downstream (FR-10). The ``git`` work
        here reads the fetched clone only.
        """
        push_rank = cls._push_rank(timeline)
        iteration_tips = cls._ordered_iteration_tips(reviews, comments, push_rank)
        index_of_tip = {tip: index for index, tip in enumerate(iteration_tips)}
        parsed_comments = sorted((cls._parse_comment(raw, index_of_tip) for raw in comments), key=lambda c: c.id)
        iterations = tuple(
            cls._build_iteration(tip, index, clone, base_ref, parsed_comments) for index, tip in enumerate(iteration_tips)
        )
        verdicts = cls._ordered_verdicts(reviews)
        return PullRequestFacts(
            pr_number=pr_number,
            iterations=iterations,
            review_verdicts=verdicts,
            comments=tuple(parsed_comments),
        )

    # ───────────────────────────── iteration ordering ─────────────────────────────

    @classmethod
    def _ordered_iteration_tips(
        cls, reviews: list[dict[str, Any]], comments: list[dict[str, Any]], push_rank: dict[str, int]
    ) -> list[str]:
        """The distinct commit SHAs reviewers engaged with, in the order they became the head.

        The engagement set is every review's ``commit_id`` and every comment's ``original_commit_id``
        — the states a reviewer either submitted a verdict against or left a line comment on. They
        are ordered primarily by the timeline's push sequence (``push_rank``) so rounds read
        chronologically even when a review was submitted late, and secondarily by earliest engagement
        time then SHA so the order is total and stable when the timeline is silent (FR-10).
        """
        earliest_engagement = cls._earliest_engagement_time(reviews, comments)
        return sorted(
            earliest_engagement,
            key=lambda sha: (push_rank.get(sha, _UNORDERED_RANK), earliest_engagement[sha], sha),
        )

    @staticmethod
    def _earliest_engagement_time(reviews: list[dict[str, Any]], comments: list[dict[str, Any]]) -> dict[str, str]:
        """Each engaged commit SHA mapped to the earliest time a review or comment touched it."""
        earliest: dict[str, str] = {}
        for review in reviews:
            sha = review.get("commit_id") or ""
            when = review.get("submitted_at") or ""
            if sha:
                earliest[sha] = min(earliest.get(sha, when), when) if sha in earliest else when
        for comment in comments:
            sha = comment.get("original_commit_id") or ""
            when = comment.get("created_at") or ""
            if sha:
                earliest[sha] = min(earliest.get(sha, when), when) if sha in earliest else when
        return earliest

    @staticmethod
    def _push_rank(timeline: list[dict[str, Any]]) -> dict[str, int]:
        """Each committed SHA mapped to its position in the timeline's push sequence.

        Built from ``committed`` events, whose ``sha`` names a commit as it entered the PR head. This
        is the authoritative round order — a reviewer may submit a verdict long after the push it
        reviews, so ordering by push time rather than review time keeps round 1 before round 2.
        """
        rank: dict[str, int] = {}
        position = 0
        for event in timeline:
            if event.get("event") != "committed":
                continue
            sha = event.get("sha") or ""
            if sha and sha not in rank:
                rank[sha] = position
                position += 1
        return rank

    # ───────────────────────────── per-iteration facts (git) ─────────────────────────────

    @classmethod
    def _build_iteration(
        cls, tip_sha: str, index: int, clone: Path, base_ref: str, comments: list[ReviewComment]
    ) -> ReviewIteration:
        """One iteration's tip, base, diff, and bound comment ids — or an unrecoverable stub.

        When the tip is not present in the fetched clone the iteration is reported unrecoverable with
        an empty base and diff; a diff is never invented for a state that cannot be reached (FR-8).
        """
        comment_ids = tuple(sorted(comment.id for comment in comments if comment.iteration_index == index))
        if not cls._commit_is_present(clone, tip_sha):
            return ReviewIteration(tip_sha=tip_sha, base_sha="", diff="", comment_ids=comment_ids, recoverable=False)
        base_sha = cls._base_for_tip(clone, tip_sha, base_ref)
        diff = cls._unified_diff(clone, base_sha, tip_sha)
        return ReviewIteration(tip_sha=tip_sha, base_sha=base_sha, diff=diff, comment_ids=comment_ids, recoverable=True)

    @staticmethod
    def _commit_is_present(clone: Path, sha: str) -> bool:
        """True when ``sha`` resolves to a commit object in the clone (reachable after the head fetch)."""
        code, _ = GitCommandRunner.git(clone, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        return code == 0

    @staticmethod
    def _base_for_tip(clone: Path, tip_sha: str, base_ref: str) -> str:
        """The before-state the iteration's diff is taken against: the merge-base with the mainline.

        The merge-base of the tip and the mainline branch is the branch point — the state before this
        PR's work — so ``base..tip`` is exactly what this PR put up for review at that iteration, not
        the unrelated commits the mainline gained in between. Falls back to the tip's first parent
        when no merge-base exists (a base ref absent from this clone), so a diff is still produced.
        """
        code, merge_base = GitCommandRunner.git(clone, "merge-base", tip_sha, base_ref)
        if code == 0 and merge_base:
            return merge_base
        parent_code, parent = GitCommandRunner.git(clone, "rev-parse", "--verify", "--quiet", f"{tip_sha}^")
        return parent if parent_code == 0 else tip_sha

    @staticmethod
    def _unified_diff(clone: Path, base_sha: str, tip_sha: str) -> str:
        """The unified diff from ``base_sha`` to ``tip_sha`` (empty when the two are the same commit)."""
        if base_sha == tip_sha:
            return ""
        _, diff = GitCommandRunner.git(clone, "diff", base_sha, tip_sha)
        return diff

    # ───────────────────────────── comments & verdicts ─────────────────────────────

    @staticmethod
    def _parse_comment(raw: dict[str, Any], index_of_tip: dict[str, int]) -> ReviewComment:
        """One inline comment's six fields, bound to the iteration its ``original_commit_id`` names.

        ``original_commit_id`` is GitHub's stable pin to the commit the comment was first written
        against; ``commit_id`` moves forward on later pushes, so it is only the fallback. The line
        range prefers the ``original_*`` line numbers for the same reason — they describe the code as
        the reviewer saw it (FR-12).
        """
        line_end = raw.get("original_line") or raw.get("line") or 0
        line_start = raw.get("original_start_line") or raw.get("start_line") or line_end
        original_commit = raw.get("original_commit_id") or ""
        current_commit = raw.get("commit_id") or ""
        iteration_index = index_of_tip.get(original_commit, index_of_tip.get(current_commit, _UNBOUND_ITERATION))
        return ReviewComment(
            id=int(raw["id"]),
            body=raw.get("body") or "",
            path=raw.get("path") or "",
            line_start=int(line_start),
            line_end=int(line_end),
            author_role=raw.get("author_association") or "",
            created_at=raw.get("created_at") or "",
            iteration_index=iteration_index,
        )

    @classmethod
    def _ordered_verdicts(cls, reviews: list[dict[str, Any]]) -> tuple[ReviewVerdict, ...]:
        """Every submitted review as a verdict, ordered by submission time then id (FR-7, FR-10).

        A review with no ``submitted_at`` is a still-pending draft, never submitted, so it carries no
        verdict and is dropped — listing it would tell an agent a review happened that did not.
        """
        submitted = [review for review in reviews if review.get("submitted_at")]
        submitted.sort(key=lambda review: (review.get("submitted_at") or "", int(review.get("id") or 0)))
        return tuple(cls._parse_verdict(review) for review in submitted)

    @staticmethod
    def _parse_verdict(review: dict[str, Any]) -> ReviewVerdict:
        """One review's verdict fields: state, author login, author association, submission time."""
        return ReviewVerdict(
            state=review.get("state") or "",
            author=(review.get("user") or {}).get("login") or "",
            author_role=review.get("author_association") or "",
            submitted_at=review.get("submitted_at") or "",
        )
