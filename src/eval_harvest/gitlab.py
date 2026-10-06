"""Harvest GitLab merge requests: ``glab api graphql`` reads reshaped into the payloads GitHub's path consumes.

``survey`` and ``capture`` were written against ``gh``'s JSON. Rather than fork their logic, this module
reads a merge request through GitLab's GraphQL API and reshapes it into exactly those shapes — a
``gh pr list`` row for :meth:`Survey.survey_one`, and the ``reviews`` / ``comments`` / ``timeline``
REST lists for :meth:`Forge.reconstruct_facts` — so iteration ordering, comment binding, diffs, and
recoverability stay one implementation with one set of tests. GraphQL, not REST, because it is one
request per page and, on gitlab.com, readable without a token for a public project (REST's
``/discussions`` is not), which is what let the recorded fixtures be captured.

**What GitLab does not record, and how it is recovered.**

- *Which head a verdict saw.* An approval is a system note with a time and no SHA. Each push note's
  "Compare with previous version" link carries the previous head as ``start_sha=`` in full, so the
  ordered heads are every push's ``start_sha`` then the current ``diffHeadSha``, and a verdict binds
  to the head current at its time.
- *Which version a comment was written against.* A DiffNote's ``position`` is re-anchored onto later
  versions when its line survives a push, so it is not GitHub's ``original_commit_id``. A thread is
  anchored by its root note: the root's position if that head was already current when it was
  written, else the head current at that time. A reply inherits its thread's anchor, as a GitHub
  reply does. A line moved onto a later version is mapped back through a local ``git diff``.
- *The force-pushed rounds.* GitLab keeps every commit a note references (``refs/keep-around/*``)
  fetchable by SHA, so capture fetches each engaged commit the head ref does not reach; one the forge
  no longer serves is still reported unrecoverable, never fabricated (FR-8).

**Approximation, stated rather than hidden.** A GitLab reviewer has one current review state, not a
list of submissions, so ``survey``'s ``review_rounds`` counts reviewers who engaged (approved,
requested changes, or reviewed) plus approvers — a lower bound on GitHub's per-submission count.

**Layer (ADR-4).** Online: :meth:`GitLab.fetch_merge_requests` and :meth:`GitLab.capture` are the
only network entries, and every ``glab``/``git`` call goes through
:class:`~eval_harvest.gitcmd.GitCommandRunner`. :meth:`GitLab.review_payloads` and
:meth:`GitLab.survey_pull_request` are pure reshapes the tests drive from recorded gitlab.com responses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any  # GraphQL returns arbitrary JSON objects; dict[str, Any] is the honest shape.

from eval_harvest.forge import Forge, ForgeError, PullRequestFacts
from eval_harvest.forge_host import ForgeRemote
from eval_harvest.gitcmd import GitCommandRunner

#: One page of merge requests with every field a survey row derives from.
SURVEY_QUERY = """
query($fullPath: ID!, $state: MergeRequestState!, $first: Int!, $after: String) {
  project(fullPath: $fullPath) {
    mergeRequests(state: $state, first: $first, after: $after, sort: CREATED_DESC) {
      pageInfo { hasNextPage endCursor }
      nodes {
        iid title description state createdAt mergedAt closedAt
        sourceBranch targetBranch diffHeadSha mergeCommitSha squashOnMerge webUrl
        author { username }
        labels { nodes { title } }
        approvedBy { nodes { username } }
        reviewers { nodes { username mergeRequestInteraction { reviewState } } }
        diffRefs { baseSha headSha startSha }
        diffStatsSummary { additions deletions fileCount }
      }
    }
  }
}
"""

#: One merge request's head and one page of its notes — push, verdict, and line-comment records alike.
CAPTURE_QUERY = """
query($fullPath: ID!, $iid: String!, $first: Int!, $after: String) {
  project(fullPath: $fullPath) {
    mergeRequest(iid: $iid) {
      iid diffHeadSha
      notes(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id body system createdAt maxAccessLevelOfAuthor
          author { username }
          discussion { id }
          systemNoteMetadata { action }
          position { filePath oldLine newLine oldPath newPath positionType diffRefs { baseSha headSha startSha } }
        }
      }
    }
  }
}
"""

#: GitLab's GraphQL page-size ceiling.
_PAGE_SIZE = 100

#: How much of a non-JSON ``glab`` body a ``ForgeError`` quotes — enough to recognise a sign-in page.
_QUOTED_BODY_CHARS = 200

#: ``survey --state`` spelled as GitLab's ``MergeRequestState``. GitLab's ``closed`` excludes merged
#: MRs where gh's includes them; ``all`` and ``merged`` are the states a harvest uses.
_STATE_FILTER = {"all": "all", "open": "opened", "closed": "closed", "merged": "merged"}

#: GitLab MR state → the ``gh`` ``state`` survey compares against.
_PULL_REQUEST_STATE = {"merged": "MERGED", "opened": "OPEN", "locked": "OPEN", "closed": "CLOSED"}

#: Reviewer states that mean the reviewer engaged — what survey counts as a review.
_ENGAGED_REVIEW_STATES = frozenset({"APPROVED", "REQUESTED_CHANGES", "REVIEWED"})

#: System-note action → the GitHub review ``state`` it is the counterpart of.
_VERDICT_STATE_BY_ACTION = {"approved": "APPROVED", "requested_changes": "CHANGES_REQUESTED", "reviewed": "COMMENTED"}

#: The system-note action GitLab writes for a push to the source branch.
_PUSH_ACTION = "commit"

#: The previous head a push note's compare link names, always a full SHA.
_PREVIOUS_HEAD = re.compile(r"[?&]start_sha=([0-9a-f]{40})")

#: The numeric tail of a GraphQL global id (``gid://gitlab/DiffNote/3926531118``).
_GLOBAL_ID_NUMBER = re.compile(r"/(\d+)$")

#: A unified-diff hunk header; absent counts mean 1.
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)

#: Where capture stores an engaged commit fetched by SHA, outside the ``refs/remotes/pr/*`` survey counts.
_VERSION_REF_PREFIX = "refs/remotes/mr-versions"


@dataclass(frozen=True, slots=True)
class ForgePayloads:
    """A merge request reshaped into the three REST lists :meth:`Forge.reconstruct_facts` consumes."""

    reviews: list[dict[str, Any]]
    comments: list[dict[str, Any]]
    timeline: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _HeadTimeline:
    """The heads a merge request went through, in push order, with the time each became current."""

    heads: tuple[str, ...]
    became_current_at: tuple[str, ...]

    def head_at(self, when: str) -> str:
        """The head current at ``when`` (ISO-8601 UTC, which orders lexically); the first head before any push."""
        current = self.heads[0] if self.heads else ""
        for head, since in zip(self.heads, self.became_current_at, strict=True):
            if since <= when:
                current = head
        return current

    def position_of(self, sha: str) -> int | None:
        """``sha``'s place in the push order, or ``None`` when it was never a head."""
        return self.heads.index(sha) if sha in self.heads else None


class GitLab:
    """Read GitLab merge requests through ``glab`` and reshape them into GitHub-path payloads. Holds no state."""

    # ───────────────────────────── survey (network) ─────────────────────────────

    @classmethod
    def fetch_merge_requests(cls, remote: ForgeRemote, state: str, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` merge requests, newest first, as ``gh pr list`` rows — one GraphQL call per page."""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(rows) < limit:
            connection = cls._fetch_merge_request_page(remote, _STATE_FILTER[state], min(_PAGE_SIZE, limit - len(rows)), cursor)
            rows.extend(cls.survey_pull_request(node) for node in connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
        return rows[:limit]

    @classmethod
    def _fetch_merge_request_page(cls, remote: ForgeRemote, state: str, first: int, cursor: str | None) -> dict[str, Any]:
        """One ``mergeRequests`` connection page."""
        variables = ["-f", f"fullPath={remote.project_path}", "-f", f"state={state}", "-F", f"first={first}"]
        if cursor:
            variables += ["-f", f"after={cursor}"]
        project = cls._run_graphql(remote, SURVEY_QUERY, variables)
        connection: dict[str, Any] = project["mergeRequests"]
        return connection

    # ───────────────────────────── survey reshape (pure) ─────────────────────────────

    @classmethod
    def survey_pull_request(cls, node: dict[str, Any]) -> dict[str, Any]:
        """One GitLab MR node as the ``gh pr list --json`` row :meth:`Survey.survey_one` reads."""
        integration = cls._integration_revision(node)
        diff_stats = node.get("diffStatsSummary") or {}
        return {
            "number": int(node["iid"]),
            "title": node.get("title") or "",
            "body": node.get("description") or "",
            "state": _PULL_REQUEST_STATE.get(node.get("state") or "", "OPEN"),
            "createdAt": node.get("createdAt") or "",
            "mergedAt": node.get("mergedAt") or "",
            "closedAt": node.get("closedAt") or "",
            "mergeCommit": {"oid": integration} if integration else None,
            "headRefName": node.get("sourceBranch") or "",
            "headRefOid": node.get("diffHeadSha") or "",
            "baseRefName": node.get("targetBranch") or "",
            "baseRefOid": (node.get("diffRefs") or {}).get("baseSha") or "",
            "additions": diff_stats.get("additions") or 0,
            "deletions": diff_stats.get("deletions") or 0,
            "changedFiles": diff_stats.get("fileCount") or 0,
            "labels": [{"name": label["title"]} for label in (node.get("labels") or {}).get("nodes") or []],
            "reviews": cls._survey_reviews(node),
            "url": node.get("webUrl") or "",
            "author": {"login": (node.get("author") or {}).get("username") or ""},
        }

    @staticmethod
    def _integration_revision(node: dict[str, Any]) -> str:
        """The commit the MR put on the mainline.

        ``mergeCommitSha`` when a merge commit was made. A fast-forward merge makes none, and then the
        head itself landed — unless it was squashed, whose commit's SHA GraphQL does not expose, so
        that MR stays blocked ``no-integration-commit`` rather than be given a SHA that never landed.
        """
        if node.get("mergeCommitSha"):
            return str(node["mergeCommitSha"])
        if node.get("state") == "merged" and not node.get("squashOnMerge"):
            return str(node.get("diffHeadSha") or "")
        return ""

    @staticmethod
    def _survey_reviews(node: dict[str, Any]) -> list[dict[str, Any]]:
        """One ``{author: {login}}`` per reviewer who engaged, plus each approver not already listed."""
        logins: list[str] = []
        for reviewer in (node.get("reviewers") or {}).get("nodes") or []:
            if ((reviewer.get("mergeRequestInteraction") or {}).get("reviewState") or "") in _ENGAGED_REVIEW_STATES:
                logins.append(reviewer["username"])
        for approver in (node.get("approvedBy") or {}).get("nodes") or []:
            if approver["username"] not in logins:
                logins.append(approver["username"])
        return [{"author": {"login": login}} for login in logins]

    # ───────────────────────────── capture (network) ─────────────────────────────

    @classmethod
    def capture(cls, remote: ForgeRemote, iid: int, clone: Path, base_ref: str = "HEAD") -> PullRequestFacts:
        """Fetch one MR's facts: its head ref, its notes, and each engaged commit the head does not reach.

        Round trips: one ``git fetch`` of the MR head, one GraphQL call per 100 notes, and one ``git
        fetch`` per engaged commit missing after that — the force-pushed rounds, which GitLab keeps.
        """
        Forge.fetch_refs(clone, remote.single_pull_head_refspec(iid), remote=remote.git_remote)
        payloads = cls.review_payloads(cls.fetch_merge_request(remote, iid))
        cls._fetch_missing_engaged_commits(clone, iid, payloads, git_remote=remote.git_remote)
        comments = cls.remap_moved_comment_lines(payloads.comments, clone)
        return Forge.reconstruct_facts(
            iid, clone, base_ref, reviews=payloads.reviews, comments=comments, timeline=payloads.timeline
        )

    @classmethod
    def fetch_merge_request(cls, remote: ForgeRemote, iid: int) -> dict[str, Any]:
        """The ``mergeRequest`` node with every page of its notes concatenated into ``notes.nodes``."""
        notes: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            merge_request = cls._fetch_notes_page(remote, iid, cursor)
            notes.extend(merge_request["notes"]["nodes"])
            if not merge_request["notes"]["pageInfo"]["hasNextPage"]:
                break
            cursor = merge_request["notes"]["pageInfo"]["endCursor"]
        return {**merge_request, "notes": {"nodes": notes}}

    @classmethod
    def _fetch_notes_page(cls, remote: ForgeRemote, iid: int, cursor: str | None) -> dict[str, Any]:
        """One page of an MR's notes, or a ``ForgeError`` naming the MR when it does not exist."""
        variables = ["-f", f"fullPath={remote.project_path}", "-f", f"iid={iid}", "-F", f"first={_PAGE_SIZE}"]
        if cursor:
            variables += ["-f", f"after={cursor}"]
        merge_request: dict[str, Any] | None = cls._run_graphql(remote, CAPTURE_QUERY, variables).get("mergeRequest")
        if merge_request is None:
            raise ForgeError(f"{remote.host} has no merge request !{iid} in {remote.project_path}")
        return merge_request

    @staticmethod
    def _run_graphql(remote: ForgeRemote, query: str, variables: list[str]) -> dict[str, Any]:
        """Run one ``glab api graphql`` query against ``remote.host`` and return its ``project`` object.

        A missing *or* unreadable project comes back as ``project: null`` with exit 0 — GitLab does not
        distinguish them to an unauthenticated caller — so that is refused with the login that fixes
        the private case, never read as "no merge requests". A body that is not a JSON object (a proxy's
        sign-in page, exit 0) is a ``ForgeError`` too, so it refuses this one MR rather than abort a batch.
        """
        argv = ["api", "graphql", "--hostname", remote.host, "-f", f"query={query}", *variables]
        code, out, err = GitCommandRunner.glab(argv)
        if code != 0:
            raise ForgeError(f"glab api graphql on {remote.host} failed ({code}): {err or out}")
        loaded = GitLab._json_object_or_forge_error(remote, out)
        if loaded.get("errors"):
            raise ForgeError(f"glab api graphql on {remote.host} returned errors: {loaded['errors']}")
        project: dict[str, Any] | None = (loaded.get("data") or {}).get("project")
        if project is None:
            raise ForgeError(
                f"{remote.host} returned no project {remote.project_path!r}: it does not exist or this login cannot "
                f"read it — run `glab auth login --hostname {remote.host}`"
            )
        return project

    @staticmethod
    def _json_object_or_forge_error(remote: ForgeRemote, out: str) -> dict[str, Any]:
        """``out`` parsed as a JSON object, or a ``ForgeError`` quoting the start of what came back instead."""
        try:
            loaded = json.loads(out)
        except json.JSONDecodeError:
            loaded = None
        if not isinstance(loaded, dict):
            raise ForgeError(
                f"glab api graphql on {remote.host} returned a body that is not a JSON object: {out[:_QUOTED_BODY_CHARS]!r}"
            )
        return loaded

    @classmethod
    def _fetch_missing_engaged_commits(cls, clone: Path, iid: int, payloads: ForgePayloads, *, git_remote: str) -> None:
        """Fetch by SHA each reviewed or commented-on commit the MR head does not reach.

        Best effort by design: a commit the forge no longer serves is left absent, and
        :meth:`Forge.reconstruct_facts` reports its iteration unrecoverable (FR-8).
        """
        for sha in cls._engaged_commits(payloads):
            if not cls._commit_is_present(clone, sha):
                try:
                    Forge.fetch_refs(clone, f"+{sha}:{_VERSION_REF_PREFIX}/{iid}/{sha}", remote=git_remote)
                except ForgeError:
                    continue

    @staticmethod
    def _engaged_commits(payloads: ForgePayloads) -> list[str]:
        """Every commit a verdict or a comment names, deduplicated in first-mention order."""
        named = [review["commit_id"] for review in payloads.reviews]
        for comment in payloads.comments:
            named += [comment["original_commit_id"], comment["commit_id"]]
        return list(dict.fromkeys(sha for sha in named if sha))

    @staticmethod
    def _commit_is_present(clone: Path, sha: str) -> bool:
        code, _ = GitCommandRunner.git(clone, "cat-file", "-e", f"{sha}^{{commit}}")
        return code == 0

    # ───────────────────────────── capture reshape (pure) ─────────────────────────────

    @classmethod
    def review_payloads(cls, merge_request: dict[str, Any]) -> ForgePayloads:
        """Reshape an MR's notes into the ``reviews`` / ``comments`` / ``timeline`` lists GitHub's path reads."""
        notes = sorted(merge_request["notes"]["nodes"], key=lambda note: (note.get("createdAt") or "", note["id"]))
        timeline = cls._head_timeline(notes, merge_request.get("diffHeadSha") or "")
        reviews = [review for note in notes if (review := cls._review_from_note(note, timeline)) is not None]
        return ForgePayloads(
            reviews=reviews,
            comments=cls._comments_from_notes(notes, timeline),
            timeline=[{"event": "committed", "sha": head} for head in timeline.heads],
        )

    @staticmethod
    def _head_timeline(notes: list[dict[str, Any]], current_head: str) -> _HeadTimeline:
        """Every push's previous head, then the current head, each with the time it became current.

        The first head was current from the start (``""`` orders before any timestamp); each later
        one from the push that replaced its predecessor. A push note with no compare link names no
        previous head and is skipped.
        """
        heads: list[str] = []
        since: list[str] = []
        replaced_at = ""
        for note in notes:
            if not note.get("system") or (note.get("systemNoteMetadata") or {}).get("action") != _PUSH_ACTION:
                continue
            previous = _PREVIOUS_HEAD.search(note.get("body") or "")
            if previous is None:
                continue
            if not heads or heads[-1] != previous.group(1):
                heads.append(previous.group(1))
                since.append(replaced_at)
            replaced_at = note.get("createdAt") or ""
        if current_head and (not heads or heads[-1] != current_head):
            heads.append(current_head)
            since.append(replaced_at)
        return _HeadTimeline(tuple(heads), tuple(since))

    @classmethod
    def _review_from_note(cls, note: dict[str, Any], timeline: _HeadTimeline) -> dict[str, Any] | None:
        """A verdict system note as a GitHub review bound to the head current when it was given; else ``None``."""
        action = (note.get("systemNoteMetadata") or {}).get("action") or ""
        if not note.get("system") or action not in _VERDICT_STATE_BY_ACTION:
            return None
        created_at = note.get("createdAt") or ""
        return {
            "id": cls._global_id_number(note["id"]),
            "state": _VERDICT_STATE_BY_ACTION[action],
            "commit_id": timeline.head_at(created_at),
            "submitted_at": created_at,
            "user": {"login": (note.get("author") or {}).get("username") or ""},
            "author_association": note.get("maxAccessLevelOfAuthor") or "",
        }

    @classmethod
    def _comments_from_notes(cls, notes: list[dict[str, Any]], timeline: _HeadTimeline) -> list[dict[str, Any]]:
        """Every line comment as a GitHub review comment, each thread anchored by its root note."""
        anchor_by_thread: dict[str, str] = {}
        comments: list[dict[str, Any]] = []
        for note in notes:
            if note.get("system") or (note.get("position") or {}).get("positionType") != "text":
                continue
            thread = (note.get("discussion") or {}).get("id") or note["id"]
            if thread not in anchor_by_thread:
                anchor_by_thread[thread] = cls._thread_anchor(note, timeline)
            comments.append(cls._comment_from_note(note, anchor_by_thread[thread]))
        return comments

    @staticmethod
    def _thread_anchor(root: dict[str, Any], timeline: _HeadTimeline) -> str:
        """The version a thread was written against: the root's position, unless GitLab moved it forward.

        A position whose head became current only *after* the root was written was re-anchored by a
        later push; the head current at the root's time is then the version the reviewer saw.
        """
        positioned_head = root["position"]["diffRefs"]["headSha"]
        head_then = timeline.head_at(root.get("createdAt") or "")
        positioned_at = timeline.position_of(positioned_head)
        then_at = timeline.position_of(head_then)
        if positioned_at is not None and then_at is not None and positioned_at > then_at:
            return head_then
        return positioned_head or head_then

    @classmethod
    def _comment_from_note(cls, note: dict[str, Any], anchor: str) -> dict[str, Any]:
        """One DiffNote in the ``pulls/<n>/comments`` shape; ``original_*`` is remapped later if it moved."""
        position = note["position"]
        line = position.get("newLine") or position.get("oldLine") or 0
        return {
            "id": cls._global_id_number(note["id"]),
            "body": note.get("body") or "",
            "path": position.get("newPath") or position.get("oldPath") or position.get("filePath") or "",
            "line": line,
            "original_line": line,
            "start_line": None,
            "original_start_line": None,
            "side": "RIGHT" if position.get("newLine") else "LEFT",
            "commit_id": position["diffRefs"]["headSha"],
            "original_commit_id": anchor,
            "user": {"login": (note.get("author") or {}).get("username") or ""},
            "author_association": note.get("maxAccessLevelOfAuthor") or "",
            "created_at": note.get("createdAt") or "",
        }

    @staticmethod
    def _global_id_number(global_id: str) -> int:
        """The numeric id at the end of a GraphQL global id."""
        match = _GLOBAL_ID_NUMBER.search(global_id)
        if match is None:
            raise ForgeError(f"unexpected GitLab note id {global_id!r}")
        return int(match.group(1))

    # ───────────────────────────── moved-forward lines (local git) ─────────────────────────────

    @classmethod
    def remap_moved_comment_lines(cls, comments: list[dict[str, Any]], clone: Path) -> list[dict[str, Any]]:
        """Each comment whose position GitLab moved to a later version, with its line mapped back to its anchor.

        Reads ``git diff`` between the two versions in the clone. A comment whose versions are not both
        present, or whose line falls inside a changed hunk, keeps its positioned line.
        """
        return [cls._remap_comment_line(comment, clone) for comment in comments]

    @classmethod
    def _remap_comment_line(cls, comment: dict[str, Any], clone: Path) -> dict[str, Any]:
        anchor, positioned = comment["original_commit_id"], comment["commit_id"]
        if anchor == positioned or comment["side"] != "RIGHT":
            return comment
        if not (cls._commit_is_present(clone, anchor) and cls._commit_is_present(clone, positioned)):
            return comment
        code, diff = GitCommandRunner.git(clone, "diff", "--unified=0", anchor, positioned, "--", comment["path"])
        mapped = cls.line_before_change(diff, comment["line"]) if code == 0 else None
        return comment if mapped is None else {**comment, "original_line": mapped}

    @staticmethod
    def line_before_change(diff: str, line: int) -> int | None:
        """The pre-image line number of post-image ``line`` across a ``--unified=0`` diff, or ``None`` if it changed.

        Every hunk wholly above ``line`` shifts it by (old count − new count); a hunk containing it
        means the line itself was edited and has no pre-image.
        """
        shift = 0
        for hunk in _HUNK_HEADER.finditer(diff):
            old_count = int(hunk.group(2) or "1")
            new_start, new_count = int(hunk.group(3)), int(hunk.group(4) or "1")
            last_new_line = new_start + new_count - 1 if new_count else new_start
            if line < new_start or (new_count == 0 and line == new_start):
                break
            if new_count and line <= last_new_line:
                return None
            shift += old_count - new_count
        return line + shift
