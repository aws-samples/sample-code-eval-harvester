"""Mechanical PR triage: the ``survey`` verb's per-PR facts.

``survey`` reports the cheap, literal, checkable facts a curator needs to decide whether a PR is
harvestable — SHAs, changed paths, review-round count, blockers — and never a quality label
(FR-5). ``forge.py`` (B-2) does the deep per-iteration capture; this reports only the *count* of
review rounds.

Every ``git``/``gh`` call routes through :class:`~eval_harvest.gitcmd.GitCommandRunner` (A-2), so
the argv-only, prompt-disabled, token-stripped discipline is enforced in one portable place (NFR-4)
rather than re-implemented here.

**The FR-11 refusal.** A repo whose PRs merge with no review iteration has no human verdict to grade
an agent against, so mining it would ship an empty, worthless dataset.
:meth:`Survey.no_review_iteration_offending` detects that (every PR at zero review rounds) so the
CLI can refuse ``no-review-iteration`` rather than emit it.

**Determinism (FR-10, NFR-1).** Every collection is ordered by a stable key (rows by PR number
descending, revert lists sorted-unique, path roots and issue candidates sorted), so two runs over a
fixed clone + payload are byte-identical — which is what the golden-file test pins.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any  # gh returns arbitrary JSON objects; dict[str, Any] is the honest shape.

from eval_harvest.forge import Forge
from eval_harvest.gitcmd import GitCommandRunner

#: The bulk refspec that fetches every PR's head into the ``refs/remotes/pr/*`` namespace ``survey``
#: reads. A plain ``git clone`` creates none of these, so without this fetch every PR is blocked
#: ``no-pull-head-ref`` and the table comes back empty. Kept as one constant so the fetch, the
#: printed remedy, and the ``--no-fetch`` documentation cannot spell it three different ways. The
#: globs must be **single-quoted** wherever this string is printed as a shell command: zsh expands
#: ``*`` otherwise and the operator gets ``no matches found``.
PULL_HEAD_REFSPEC = "+refs/pull/*/head:refs/remotes/pr/*"

#: The one-line remedy for each mechanical blocker `_blockers` can attach. ``no-pull-head-ref`` and
#: ``squash-without-change-tip`` both resolve to the bulk fetch (their remedy carries the live remote
#: name, so it is built by :func:`_blocker_remedy`, not stored here); the rest name why the PR cannot
#: become a task and what to do — usually "pick another PR". The set is pinned to `_blockers`' output,
#: not invented: a blocker with no entry falls back to a generic "inspect by hand" line.
_STATIC_BLOCKER_REMEDIES: dict[str, str] = {
    "no-integration-commit": "the merged PR records no merge commit; there is no before-state to build from — pick another PR",
    "no-changed-paths": "the integration commit touched no files — pick another PR",
    "no-test-change": "the change touched no test files; harvestable for review, but not for a hidden-test task",
}

#: The blockers whose remedy is the bulk pull-head fetch — a missing head ref, or a squash with no
#: recoverable change tip. Both are fixed by fetching :data:`PULL_HEAD_REFSPEC`.
_FETCH_REMEDY_BLOCKERS = frozenset({"no-pull-head-ref", "squash-without-change-tip"})


def _fetch_remedy(remote: str) -> str:
    """The runnable ``git fetch`` command that populates ``refs/remotes/pr/*``, quoted for the shell."""
    return f"git fetch {remote} '{PULL_HEAD_REFSPEC}'"


def _blocker_remedy(name: str, remote: str) -> str:
    """The one-line remedy for one blocker name, with the live ``remote`` folded into the fetch form."""
    if name in _FETCH_REMEDY_BLOCKERS:
        return _fetch_remedy(remote)
    return _STATIC_BLOCKER_REMEDIES.get(name, "no automatic remedy; inspect this PR by hand or pick another")


#: The metadata ``gh pr list`` is asked for, in one bulk query — every field a per-PR row derives
#: from, fetched once so the survey makes a single network call per page.
GH_FIELDS = (
    "number,title,body,state,createdAt,closedAt,mergedAt,mergeCommit,"
    "headRefName,headRefOid,baseRefName,baseRefOid,additions,deletions,"
    "changedFiles,labels,reviews,url,author"
)

#: Path fragments that mark a changed file as test content. Deliberately broad: over-reporting a
#: test costs one glance, under-reporting hides the only PRs that can become a task with hidden tests.
TEST_MARKERS = (
    "/test/",
    "/tests/",
    "/spec/",
    "/__tests__/",
    "test_",
    "_test.",
    ".test.",
    ".spec.",
    "Test.java",
    "conftest.py",
)

#: Field and record separators for the single ``git log`` pass. A commit message can contain any
#: newline or tab but cannot contain these, so a body cannot forge a record boundary.
_FS = "\x1f"
_RS = "\x1e"

#: A well-formed ``git log`` record splits into this many fields: sha, author-time, subject, body,
#: names. Short records are truncated output and get skipped.
_LOG_RECORD_FIELDS = 5

#: A commit with at least this many parents is a true merge (two sides joined), not a squash/ff.
_TRUE_MERGE_MIN_PARENTS = 2

#: Subjects that mark a mainline commit as repair work; matched anchored against the *subject* only.
_FIX_SUBJECT = re.compile(r"^(fix|hotfix|revert|patch)\b|^\w+\([^)]*\):\s*(fix|revert)\b", re.I)

#: ``Revert "<subject>"`` — what GitHub's revert button and ``git revert`` both write.
_REVERT_SUBJECT = re.compile(r'^Revert\s+"(?P<subject>.*)"\s*$')

#: ``This reverts commit <sha>.`` in a commit body, which survives a subject rewrite.
_REVERTS_COMMIT = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)

#: Issue references readable without a forge call: ``#123`` in the title/body, and a leading number
#: in a branch name such as ``fix/789-workshop-bootstrap``.
_HASH_REF = re.compile(r"#(\d+)\b")
_BRANCH_ISSUE = re.compile(r"^[a-z]+/(\d+)-")


class SurveyError(RuntimeError):
    """A ``gh``/``git`` call the survey needed failed — carries the underlying message."""


@dataclass(frozen=True, slots=True)
class Commit:
    """One mainline commit, with everything the follow-up and revert scans need."""

    sha: str
    at: int
    subject: str
    body: str
    paths: frozenset[str]


@dataclass
class RepairIndex:
    """Which mainline commits revert or repair which others.

    ``by_reverted_subject`` and ``by_reverted_sha`` are the two independent ways a revert names its
    target; both are kept because either can be absent (the revert button writes the subject form, a
    hand-written ``git revert`` writes the body form, a squash of a revert branch can lose one).
    """

    by_reverted_subject: dict[str, list[str]] = field(default_factory=dict)
    by_reverted_sha: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, commits: list[Commit]) -> RepairIndex:
        """Index every revert in ``commits`` by both the subject and the sha it names."""
        index = cls()
        for commit in commits:
            index._record(commit)
        return index

    def _record(self, commit: Commit) -> None:
        """File one commit's revert references under both of the target-naming forms."""
        if match := _REVERT_SUBJECT.match(commit.subject):
            key = match.group("subject").strip()
            self.by_reverted_subject.setdefault(key, []).append(commit.sha)
        for sha in _REVERTS_COMMIT.findall(commit.body):
            self.by_reverted_sha.setdefault(sha.lower(), []).append(commit.sha)

    def reverts_of(self, subject: str, sha: str) -> list[str]:
        """Mainline commits that revert the commit with this subject or this sha."""
        found = list(self.by_reverted_subject.get(subject.strip(), ()))
        for candidate, reverters in self.by_reverted_sha.items():
            if sha.startswith(candidate) or candidate.startswith(sha[:7]):
                found.extend(reverters)
        return sorted(set(found))


@dataclass(frozen=True, slots=True)
class _RangeFacts:
    """The range-derived facts about one PR: what it put on the mainline vs. what its branch held.

    Grouped so :meth:`Survey.survey_one` stays a flat sequence of named steps rather than one long
    block.
    """

    parents: list[str]
    merge_base: str
    tip_is_ancestor: bool
    branch_commits: int
    changed_paths: list[str]
    branch_changed_paths: list[str]


class Survey:
    """Read a repo's PR history for the mechanical facts a curator triages on. Holds no state."""

    # ───────────────────────────── online entry (network) ─────────────────────────────

    @classmethod
    def fetch_pull_requests(cls, repo: str, state: str, limit: int) -> list[dict[str, Any]]:
        """Fetch PR metadata with one bulk ``gh pr list`` query, or fail loudly.

        ``GITHUB_TOKEN`` is stripped from the child (in :class:`GitCommandRunner`) so an invalid
        ambient token cannot shadow the working keyring credential and report an HTTP 401 that reads
        like a network fault against a public repo.
        """
        code, out, err = GitCommandRunner.gh(
            ["pr", "list", "--state", state, "--limit", str(limit), "--json", GH_FIELDS], repo=repo
        )
        if code != 0:
            raise SurveyError(f"gh pr list failed ({code}): {err}")
        loaded = json.loads(out)
        if not isinstance(loaded, list):
            raise SurveyError(f"gh returned {type(loaded).__name__}, expected a list of pull requests")
        return loaded

    @classmethod
    def fetch_pull_head_refs(cls, clone: Path, remote: str) -> int:
        """Fetch every PR's head into ``refs/remotes/pr/*`` and return how many such refs then exist.

        Without this step a plain clone holds none of ``refs/pull/*/head``, so every PR is blocked
        ``no-pull-head-ref`` and the table comes back empty.
        Delegates the single fetch to :meth:`Forge.fetch_refs` (one refspec, one owner — FR-8), which
        raises :class:`ForgeError` with git's stderr on failure so the caller can refuse rather than
        crash. The count is read back from the refs themselves, not parsed from the fetch's progress
        output, so it is exactly what the per-PR ``have_tip`` check will see.
        """
        Forge.fetch_refs(clone, PULL_HEAD_REFSPEC, remote=remote)
        return cls._count_pull_head_refs(clone)

    @staticmethod
    def _count_pull_head_refs(clone: Path) -> int:
        """How many ``refs/remotes/pr/*`` refs the clone holds — the fetch's observable result."""
        _, out = GitCommandRunner.git(clone, "for-each-ref", "--format=%(refname)", "refs/remotes/pr/")
        return len([line for line in out.splitlines() if line.strip()])

    @staticmethod
    def fetch_remedy(remote: str) -> str:
        """The runnable bulk pull-head fetch for ``remote`` — the command a refusal's ``next:`` points at."""
        return _fetch_remedy(remote)

    @staticmethod
    def default_remote(clone: Path) -> str:
        """The remote a fetch and its printed remedy should name: ``origin`` if present, else the first.

        A remedy the operator can paste has to name the remote that actually exists in *their* clone,
        not a hard-coded ``origin`` — so the remedy string and the fetch both read this rather than
        assuming. Falls back to ``origin`` for a clone with no remote at all (the fetch will then fail
        and refuse, naming it).
        """
        _, out = GitCommandRunner.git(clone, "remote")
        remotes = [line for line in out.splitlines() if line.strip()]
        if "origin" in remotes:
            return "origin"
        return remotes[0] if remotes else "origin"

    # ───────────────────────────── payload assembly ─────────────────────────────

    @staticmethod
    def load_payload(path: Path) -> dict[str, Any]:
        """Load a ``survey --json`` payload file, or raise :class:`SurveyError` if it is not one.

        The one place the batch reads the payload's shape: a driver's ``capture --from-survey``
        goes through here rather than a hand-rolled ``json.load`` plus dict indexing, so a payload
        format change breaks loudly in a single place instead of silently mis-selecting PRs.
        """
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or "prs" not in loaded or not isinstance(loaded["prs"], list):
            raise SurveyError(f"{path} is not a `survey --json` payload (no 'prs' array); write one with `survey --json`")
        return loaded

    @classmethod
    def harvestable_numbers(cls, payload: dict[str, Any]) -> list[int]:
        """The PR numbers the payload marks harvestable, in payload order (merged, no mechanical blocker).

        The same :meth:`_harvestable` predicate the human table and the ``no-harvestable-pr`` refusal
        use, so `capture --from-survey` attempts exactly the PRs `survey` reported as clean and never a
        blocked one.
        """
        return [int(row["number"]) for row in cls._harvestable(payload["prs"])]

    @staticmethod
    def all_numbers(payload: dict[str, Any]) -> list[int]:
        """Every PR number present in the payload — the set a ``--pr <n>`` filter is validated against."""
        return [int(row["number"]) for row in payload["prs"]]

    @classmethod
    def build_payload(
        cls, clone: Path, prs: list[dict[str, Any]], *, branch: str, followup_days: int, repo: str
    ) -> dict[str, Any]:
        """Assemble ``{repo, mainline_commits, prs:[…]}`` sorted by PR number descending.

        One ``git log --first-parent`` pass builds the mainline; the revert index is built from it
        once and queried per PR. ``repo`` is echoed verbatim (``args.repo or ""``), so the
        ``--from-json`` path with no ``--repo`` yields the empty string.
        """
        commits = cls.mainline(clone, branch)
        repairs = RepairIndex.build(commits)
        rows = [cls.survey_one(pull_request, clone, commits, repairs, followup_days) for pull_request in prs]
        rows.sort(key=lambda row: row["number"], reverse=True)
        return {"repo": repo, "mainline_commits": len(commits), "prs": rows}

    @classmethod
    def mainline(cls, clone: Path, branch: str) -> list[Commit]:
        """Every commit on ``branch``'s first-parent chain, newest first, in one git call.

        One call rather than one per PR: the follow-up scan is an in-memory join between two path
        sets, which turns 194 ``git log -- <paths>`` invocations into zero. ``--first-parent`` is
        what "mainline" means — the integration history, linear on a squash-merging repo.
        """
        code, out = GitCommandRunner.git(
            clone, "log", "--first-parent", f"--format={_RS}%H{_FS}%at{_FS}%s{_FS}%b{_FS}", "--name-only", branch
        )
        if code != 0:
            raise SurveyError(f"git log {branch} failed in {clone}; is the clone complete?")
        return [commit for record in out.split(_RS) if (commit := cls._parse_commit_record(record)) is not None]

    @staticmethod
    def _parse_commit_record(record: str) -> Commit | None:
        """One ``git log`` record → a :class:`Commit`, or ``None`` for a blank or truncated record."""
        if not record.strip():
            return None
        parts = record.split(_FS)
        if len(parts) < _LOG_RECORD_FIELDS:
            return None
        sha, at, subject, body, names = parts[0], parts[1], parts[2], parts[3], parts[4]
        return Commit(
            sha=sha.strip(),
            at=int(at) if at.strip().isdigit() else 0,
            subject=subject.strip(),
            body=body,
            paths=frozenset(line for line in names.split("\n") if line.strip()),
        )

    # ───────────────────────────── per-PR facts ─────────────────────────────

    @classmethod
    def survey_one(
        cls, pull_request: dict[str, Any], clone: Path, commits: list[Commit], repairs: RepairIndex, days: int
    ) -> dict[str, Any]:
        """Every mechanical fact about one PR, plus what blocks harvesting it.

        ``blockers`` is the one derived field, and it is derived from facts rather than taste: a PR
        with no integration commit, no recoverable head, or no changed file cannot become a task no
        matter how good the change was. Everything a curator might weigh is reported as a signal.
        """
        tip_ref = f"pr/{pull_request['number']}"
        integration = (pull_request.get("mergeCommit") or {}).get("oid") or ""
        base = pull_request.get("baseRefOid") or ""
        have_tip = GitCommandRunner.git(clone, "rev-parse", "--verify", "--quiet", f"{tip_ref}^{{commit}}")[0] == 0
        tip = GitCommandRunner.git(clone, "rev-parse", tip_ref)[1] if have_tip else (pull_request.get("headRefOid") or "")

        ranges = cls._range_facts(clone, tip_ref, integration, have_tip=have_tip)
        tests = [path for path in ranges.changed_paths if any(marker in f"/{path}" for marker in TEST_MARKERS)]
        reverts = repairs.reverts_of(pull_request.get("title", ""), integration) if integration else []
        repairs_after = cls.followups(
            commits,
            after=cls._integrated_at(commits, integration),
            within_days=days,
            paths=frozenset(ranges.changed_paths),
            skip=integration,
        )
        blockers = cls._blockers(pull_request, integration, ranges, have_tip=have_tip, has_tests=bool(tests))
        change_tip_ref = tip_ref if have_tip else ""
        return cls._assemble_row(
            pull_request, tip, change_tip_ref, integration, base, ranges, tests, reverts, repairs_after, blockers
        )

    @staticmethod
    def _range_facts(clone: Path, tip_ref: str, integration: str, *, have_tip: bool) -> _RangeFacts:
        """The integrated effect and the branch's own effect, each as a changed-path set.

        The integrated effect is the integration commit's diff against its *first parent*, never
        ``merge-base..integration``: on a squash-merging repo the mainline is linear, so that range
        also contains every unrelated PR merged in between — measured against the reference repo it
        reported 97 changed files for a one-file change. The first-parent diff is what this PR put on
        the mainline, and it agrees with the forge's own ``changedFiles`` count.
        """
        parents: list[str] = []
        merge_base = ""
        tip_is_ancestor = False
        branch_commits = 0
        changed: list[str] = []
        branch_changed: list[str] = []
        if integration:
            parents = (GitCommandRunner.git(clone, "rev-list", "--parents", "-n", "1", integration)[1].split() or [""])[1:]
            if have_tip:
                merge_base = GitCommandRunner.git(clone, "merge-base", tip_ref, integration)[1]
                tip_is_ancestor = GitCommandRunner.git(clone, "merge-base", "--is-ancestor", tip_ref, integration)[0] == 0
                if merge_base:
                    counted = GitCommandRunner.git(clone, "rev-list", "--count", f"{merge_base}..{tip_ref}")[1]
                    branch_commits = int(counted) if counted.isdigit() else 0
                    branch_changed = Survey._names(clone, f"{merge_base}..{tip_ref}")
            if parents:
                changed = Survey._names(clone, f"{parents[0]}..{integration}")
        return _RangeFacts(parents, merge_base, tip_is_ancestor, branch_commits, changed, branch_changed)

    @staticmethod
    def _integrated_at(commits: list[Commit], integration: str) -> int:
        """The author-time of the integration commit on the mainline, or 0 if it is not there."""
        for commit in commits:
            if commit.sha == integration:
                return commit.at
        return 0

    @staticmethod
    def _blockers(
        pull_request: dict[str, Any], integration: str, ranges: _RangeFacts, *, have_tip: bool, has_tests: bool
    ) -> list[str]:
        """The mechanical reasons this PR cannot become a task."""
        blockers = []
        merged = pull_request.get("state") == "MERGED"
        if merged and not integration:
            blockers.append("no-integration-commit")
        if not have_tip:
            blockers.append("no-pull-head-ref")
        if merged and not ranges.changed_paths:
            blockers.append("no-changed-paths")
        if not has_tests:
            blockers.append("no-test-change")
        if len(ranges.parents) == 1 and not have_tip:
            blockers.append("squash-without-change-tip")
        return blockers

    # _assemble_row carries the full per-PR record; the field count is the row contract the
    # golden-file test pins, so the argument count is deliberate (PLR0913 suppressed).
    @staticmethod
    def _assemble_row(  # noqa: PLR0913, PLR0917
        pull_request: dict[str, Any],
        tip: str,
        change_tip_ref: str,
        integration: str,
        base: str,
        ranges: _RangeFacts,
        tests: list[str],
        reverts: list[str],
        repairs_after: list[dict[str, Any]],
        blockers: list[str],
    ) -> dict[str, Any]:
        """Build the per-PR result dict in a fixed key order (the golden-file contract, S-13)."""
        changed = ranges.changed_paths
        forge_changed_files = pull_request.get("changedFiles", 0)
        return {
            "number": pull_request["number"],
            "title": pull_request.get("title", ""),
            "url": pull_request.get("url", ""),
            "state": pull_request.get("state", ""),
            "author": (pull_request.get("author") or {}).get("login", ""),
            "created_at": pull_request.get("createdAt", ""),
            "merged_at": pull_request.get("mergedAt", ""),
            "closed_at": pull_request.get("closedAt", ""),
            # ── what the recipe needs to resolve a range and seal a before-state ──
            "integration_revision": integration,
            "integration_parents": len(ranges.parents),
            "integration_is_true_merge": len(ranges.parents) >= _TRUE_MERGE_MIN_PARENTS,
            "change_tip": tip,
            "change_tip_ref": change_tip_ref,
            "change_base": ranges.merge_base or base,
            "recorded_base": base,
            "tip_is_ancestor_of_integration": ranges.tip_is_ancestor,
            "branch_commits": ranges.branch_commits,
            # ── size and shape ──
            "changed_paths": changed,
            "n_changed": len(changed) or forge_changed_files,
            "branch_changed_paths": ranges.branch_changed_paths,
            "forge_changed_files": forge_changed_files,
            "path_count_disagrees": bool(changed) and len(changed) != forge_changed_files,
            "additions": pull_request.get("additions", 0),
            "deletions": pull_request.get("deletions", 0),
            "test_paths": tests,
            "has_test_change": bool(tests),
            "path_roots": Survey.path_roots(changed),
            # ── intent and review ──
            "branch": pull_request.get("headRefName", ""),
            "issue_candidates": Survey.issue_candidates(
                pull_request.get("title", ""), pull_request.get("body") or "", pull_request.get("headRefName") or ""
            ),
            "labels": [label.get("name", "") for label in pull_request.get("labels") or []],
            "review_rounds": len(pull_request.get("reviews") or []),
            "reviewers": sorted(
                {(review.get("author") or {}).get("login", "") for review in pull_request.get("reviews") or []} - {""}
            ),
            # ── quality signals, reported and not scored ──
            "reverted_by": reverts,
            "followup_fixes": repairs_after,
            "closed_unmerged": pull_request.get("state") == "CLOSED" and not pull_request.get("mergedAt"),
            # ── the one derived field ──
            "blockers": blockers,
        }

    # ───────────────────────────── signal scans ─────────────────────────────

    @staticmethod
    def _names(clone: Path, span: str) -> list[str]:
        """The paths a revision range touches, one per line, blanks dropped."""
        return [line for line in GitCommandRunner.git(clone, "diff", "--name-only", span)[1].splitlines() if line.strip()]

    @classmethod
    def followups(
        cls, commits: list[Commit], *, after: int, within_days: int, paths: frozenset[str], skip: str
    ) -> list[dict[str, Any]]:
        """Repair-shaped mainline commits touching ``paths`` within the window after ``after``.

        A signal, not a verdict. A follow-up fix touching the same file is the cheapest evidence a
        change shipped incomplete, and it is also what a normal iterative codebase looks like — which
        is why this reports the commits and lets a curator decide. ``skip`` excludes the change's own
        integration commit, which otherwise matches its own paths.
        """
        if not paths or after <= 0:
            return []
        horizon = after + within_days * 86400
        hits: list[dict[str, Any]] = []
        for commit in commits:
            shared = cls._followup_shared(commit, paths, skip, after, horizon)
            if shared:
                hits.append(cls._followup_hit(commit, after, shared))
        return hits

    @staticmethod
    def _followup_shared(commit: Commit, paths: frozenset[str], skip: str, after: int, horizon: int) -> frozenset[str]:
        """The paths a repair-shaped in-window commit shares with ``paths`` — empty when it is not one."""
        if commit.sha == skip or not (after < commit.at <= horizon):
            return frozenset()
        shared = commit.paths & paths
        return shared if shared and _FIX_SUBJECT.search(commit.subject) else frozenset()

    @staticmethod
    def _followup_hit(commit: Commit, after: int, shared: frozenset[str]) -> dict[str, Any]:
        """One follow-up commit's reported fields: sha, subject, days-after, and the shared paths."""
        return {
            "sha": commit.sha,
            "subject": commit.subject,
            "days_after": round((commit.at - after) / 86400, 1),
            "shared_paths": sorted(shared)[:10],
        }

    @staticmethod
    def path_roots(paths: list[str], depth: int = 2) -> list[str]:
        """The distinct leading path segments a change touches, to ``depth`` segments.

        Reported because a repo whose code lives at ``cdk/src/`` matches none of the visibility rules
        anchored at the root; seeing the real roots is how a curator writes the per-repo rule set.
        """
        roots = set()
        for path in paths:
            parts = path.split("/")
            roots.add("/".join(parts[:depth]) + "/" if len(parts) > depth else path)
        return sorted(roots)

    @staticmethod
    def issue_candidates(title: str, body: str, branch: str) -> list[str]:
        """Issue numbers this PR might close, from its title, body, and branch name."""
        found = set(_HASH_REF.findall(title)) | set(_HASH_REF.findall(body or ""))
        if match := _BRANCH_ISSUE.match(branch or ""):
            found.add(match.group(1))
        return sorted(found, key=int)

    # ───────────────────────────── refusal & rendering ─────────────────────────────

    @staticmethod
    def no_review_iteration_offending(rows: list[dict[str, Any]]) -> str | None:
        """The FR-11 ``offending`` string when no PR went through review, else ``None`` (new here).

        Triggers only when there is at least one PR and *every* one is at zero review rounds — a repo
        with nothing to grade an agent against, so the CLI refuses ``no-review-iteration`` rather than
        emit a worthless empty dataset (FR-11).
        """
        if not rows or any(row["review_rounds"] > 0 for row in rows):
            return None
        reviewed = sum(1 for row in rows if row["review_rounds"] > 0)
        merged = sum(1 for row in rows if row["state"] == "MERGED")
        denominator = merged if merged else len(rows)
        noun = "merged PRs" if merged else "PRs"
        return f"{reviewed} of {denominator} {noun} had review rounds"

    @staticmethod
    def _harvestable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The rows a survey can actually build a datapoint from: merged, with no mechanical blocker.

        The same predicate ``render_summary``'s table filters on, named once so the ``no-harvestable-pr``
        refusal and the table cannot drift on what "harvestable" means.
        """
        return [row for row in rows if not row["blockers"] and row["state"] == "MERGED"]

    @staticmethod
    def _blocker_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        """How many PRs carry each blocker name (a PR with two blockers counts toward both)."""
        counts: dict[str, int] = {}
        for row in rows:
            for name in row["blockers"]:
                counts[name] = counts.get(name, 0) + 1
        return counts

    @classmethod
    def blocker_histogram(cls, rows: list[dict[str, Any]], remote: str) -> list[tuple[str, int, str]]:
        """``(blocker, count, remedy)`` per blocker, most common first then name — the aggregate view.

        Turns "0 harvestable, empty table" into a named cause and a runnable fix. Ordered by
        descending count then name so it is
        deterministic (FR-10) and the dominant blocker reads first.
        """
        counts = cls._blocker_counts(rows)
        ordered = sorted(counts, key=lambda name: (-counts[name], name))
        return [(name, counts[name], _blocker_remedy(name, remote)) for name in ordered]

    @classmethod
    def no_harvestable_pr_offending(cls, rows: list[dict[str, Any]]) -> str | None:
        """The ``no-harvestable-pr`` ``offending`` string when every PR is blocked, else ``None``.

        Fires only when there is at least one PR and *none* is a merged PR with no blocker — the
        empty-table case that would otherwise exit 0 with no explanation. Names the dominant blocker and its share
        so the refusal points at the one fix that unblocks the most PRs.
        """
        if not rows or cls._harvestable(rows):
            return None
        total = len(rows)
        counts = cls._blocker_counts(rows)
        if not counts:
            return f"all {total} pull request(s) are blocked; none is a merged PR with no mechanical blocker"
        name, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
        return f"all {total} pull request(s) are blocked; the most common blocker is {name} ({count} of {total})"

    @classmethod
    def dominant_blocker_remedy(cls, rows: list[dict[str, Any]], remote: str) -> str:
        """The runnable remedy for the most common blocker — the ``next:`` for a ``no-harvestable-pr`` refusal."""
        counts = cls._blocker_counts(rows)
        if not counts:
            return "pick a repo whose merged PRs went through review iteration"
        name = sorted(counts, key=lambda blocker: (-counts[blocker], blocker))[0]
        return _blocker_remedy(name, remote)

    @classmethod
    def render_json(cls, payload: dict[str, Any], *, remote: str) -> str:
        """The ``--json`` payload rendered deterministically (indent 2 + trailing ``\\n``).

        Carries a top-level ``blockers`` object — ``{name: {count, remedy}}`` — so a driver can act on
        an empty survey without parsing the human histogram. Empty when nothing is blocked.
        """
        enriched = dict(payload)
        enriched["blockers"] = {
            name: {"count": count, "remedy": remedy} for name, count, remedy in cls.blocker_histogram(payload["prs"], remote)
        }
        return json.dumps(enriched, indent=2) + "\n"

    @classmethod
    def render_summary(cls, payload: dict[str, Any], *, branch: str, remote: str) -> str:
        """The human table: the merged-with-no-blocker PRs and their signal flags, then the blocker histogram."""
        rows = payload["prs"]
        clean = cls._harvestable(rows)
        header = f"{'#':>6} {'files':>5} {'+/-':>12} {'cmts':>4} {'rev':>3}  {'flags':<22} title"
        lines = [
            f"{len(rows)} pull requests, {payload['mainline_commits']} mainline commits on {branch}",
            f"{len(clean)} merged with no mechanical blocker\n",
            header,
        ]
        lines.extend(cls._summary_row(row) for row in clean)
        summary = "\n".join(lines) + "\n"
        return summary + cls._render_histogram(rows, remote)

    @classmethod
    def _render_histogram(cls, rows: list[dict[str, Any]], remote: str) -> str:
        """The ``blocked (N)`` section: per-blocker count and remedy, or ``""`` when nothing is blocked.

        Printed only when at least one PR is blocked — an always-present empty section would train the
        reader to skip the place the important information appears. ``N`` is the number of
        blocked PRs; the per-blocker counts below can sum higher when a PR carries more than one.
        """
        histogram = cls.blocker_histogram(rows, remote)
        if not histogram:
            return ""
        blocked = sum(1 for row in rows if row["blockers"])
        lines = [f"\nblocked ({blocked})"]
        lines.extend(f"  {name:<26} {count:>4}  {remedy}" for name, count, remedy in histogram)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _summary_row(row: dict[str, Any]) -> str:
        """One PR's summary line: number, size, review count, and signal flags."""
        flags = []
        if row["reverted_by"]:
            flags.append("REVERTED")
        if row["followup_fixes"]:
            flags.append(f"followup:{len(row['followup_fixes'])}")
        if not row["integration_is_true_merge"]:
            flags.append("squash")
        churn = f"+{row['additions']}/-{row['deletions']}"
        return (
            f"{row['number']:>6} {row['n_changed']:>5} {churn:>12} "
            f"{row['branch_commits']:>4} {row['review_rounds']:>3}  "
            f"{','.join(flags):<22} {row['title'][:60]}"
        )
