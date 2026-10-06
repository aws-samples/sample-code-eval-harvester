"""Tests for harvesting GitLab merge requests through ``glab api graphql``.

GitLab's records differ from GitHub's in three ways that matter, and each has a test here:

- **No per-review commit.** An approval is a system note with a time and no SHA, so the head it
  approved is read off the push timeline: each push note's compare link names the previous head
  (``start_sha=``) in full.
- **Positions move forward.** A DiffNote's ``position`` is re-anchored onto later versions when the
  line survives a push, so it is not GitHub's ``original_commit_id``. The thread's root note, read
  against the head current when it was written, is; a reply inherits its thread's anchor; and a line
  moved onto a later version is mapped back through ``git diff``.
- **Kept-around commits.** GitLab keeps every commit a note references fetchable by SHA, so a round
  force-pushed away is recovered rather than reported unrecoverable.

The recorded payloads under ``tests/fixtures/glab/recorded/`` are verbatim gitlab.com responses for
``gitlab-org/cli``; the git-backed scenario replays a template against a real fixture repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fixtures.repo_builder import GLAB_PAYLOAD_DIR, MergeRequestFixture, RepoBuilder

from eval_harvest.candidate import Candidate
from eval_harvest.cli import Cli, ExitCode
from eval_harvest.forge import Forge, ForgeError
from eval_harvest.forge_host import ForgeKind, ForgeRemote
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.gitlab import GitLab
from eval_harvest.tomlw import emit_document

_RECORDED = GLAB_PAYLOAD_DIR / "recorded"

#: The three heads MR !3978 in gitlab-org/cli went through: an initial head force-pushed away, a
#: two-commit rewrite, and a one-commit follow-up — read off the recording, not invented.
_MR_3978_HEADS = (
    "4e263fdb343c065e37720d92ccf0016398161359",
    "0b9a676727df6650b7c1151f0b1f7329058947dc",
    "a29c81f123cb257d062920fda775c41a0f5cf845",
)


def _recorded_merge_request() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((_RECORDED / "mr-3978-notes.json").read_text(encoding="utf-8"))
    merge_request: dict[str, Any] = loaded["data"]["project"]["mergeRequest"]
    return merge_request


def _recorded_survey_nodes() -> list[dict[str, Any]]:
    loaded: dict[str, Any] = json.loads((_RECORDED / "survey-page.json").read_text(encoding="utf-8"))
    nodes: list[dict[str, Any]] = loaded["data"]["project"]["mergeRequests"]["nodes"]
    return nodes


def _graphql_response(merge_request: dict[str, Any]) -> str:
    return json.dumps({"data": {"project": {"mergeRequest": merge_request}}})


def _glab_variables(argv_tail: list[str]) -> dict[str, str]:
    """The ``-f``/``-F`` GraphQL variables a ``glab api graphql`` argv carries (query excluded), plus ``--hostname``."""
    variables: dict[str, str] = {}
    for flag, value in zip(argv_tail, argv_tail[1:], strict=False):
        if flag == "--hostname":
            variables["hostname"] = value
        elif flag in {"-f", "-F"} and not value.startswith("query="):
            name, _, text = value.partition("=")
            variables[name] = text
    return variables


# ───────────────────────────── recorded gitlab.com payloads (pure) ─────────────────────────────


def test_recorded_push_notes_yield_the_head_timeline() -> None:
    """The push timeline is every push note's ``start_sha`` then the current head, as full SHAs.

    Catches reading only the abbreviated SHAs listed in a push note's body — the force-pushed initial
    head appears nowhere else — and an approval then binding to no reviewed state.
    """
    payloads = GitLab.review_payloads(_recorded_merge_request())

    assert [event["sha"] for event in payloads.timeline] == list(_MR_3978_HEADS)
    assert all(event["event"] == "committed" for event in payloads.timeline)


def test_recorded_approvals_bind_to_the_head_current_when_they_were_given() -> None:
    """Each approval names the head at its time: the first the rewrite, the second the follow-up.

    Catches binding every verdict to the final head (``diffHeadSha``), which would claim the
    reviewer approved code they had not yet seen.
    """
    payloads = GitLab.review_payloads(_recorded_merge_request())

    assert [(review["state"], review["commit_id"], review["submitted_at"]) for review in payloads.reviews] == [
        ("APPROVED", _MR_3978_HEADS[1], "2026-09-30T00:53:04Z"),
        ("APPROVED", _MR_3978_HEADS[2], "2026-10-01T03:30:41Z"),
    ]
    assert {review["user"]["login"] for review in payloads.reviews} == {"jay_mccure"}
    assert {review["author_association"] for review in payloads.reviews} == {"Maintainer"}


def test_recorded_comment_threads_anchor_to_the_version_they_were_written_against() -> None:
    """A thread binds to its root's version; GitLab's moved-forward position is not taken at face value.

    The recording holds both cases. The first thread's position still names the initial head, and a
    reply written after the next push stays on it. The second thread was written against the
    rewrite at 00:52 but GitLab now positions it on the next day's follow-up. Catches binding by the
    raw position (the second thread lands on a version it predates) or by each note's own time (the
    first thread's reply splits from its root).
    """
    payloads = GitLab.review_payloads(_recorded_merge_request())

    bound = [(comment["id"], comment["original_commit_id"], comment["commit_id"]) for comment in payloads.comments]
    assert bound == [
        (3926531118, _MR_3978_HEADS[0], _MR_3978_HEADS[0]),
        (3926588297, _MR_3978_HEADS[0], _MR_3978_HEADS[0]),
        (3927200396, _MR_3978_HEADS[1], _MR_3978_HEADS[2]),
        (3934550211, _MR_3978_HEADS[1], _MR_3978_HEADS[2]),
    ]
    assert [(comment["path"], comment["line"]) for comment in payloads.comments] == [
        ("internal/config/main_test.go", 14),
        ("internal/config/main_test.go", 14),
        ("Makefile", 8),
        ("Makefile", 8),
    ]


def test_recorded_payloads_reconstruct_three_iterations_offline(tmp_path: Path) -> None:
    """The reshaped payloads drive the unchanged ``Forge.reconstruct_facts`` into three ordered rounds.

    Runs against an empty repository, so every round is reported unrecoverable rather than invented
    — this pins the ordering and binding, not git recovery (the fixture-repo test below does that).
    """
    clone = tmp_path / "empty"
    clone.mkdir()
    GitCommandRunner.git(clone, "init", "--quiet")
    payloads = GitLab.review_payloads(_recorded_merge_request())

    facts = Forge.reconstruct_facts(
        3978, clone, "HEAD", reviews=payloads.reviews, comments=payloads.comments, timeline=payloads.timeline
    )

    assert [iteration.tip_sha for iteration in facts.iterations] == list(_MR_3978_HEADS)
    assert [comment.iteration_index for comment in facts.comments] == [0, 0, 1, 1]
    assert not any(iteration.recoverable for iteration in facts.iterations)


def test_recorded_survey_nodes_reshape_into_the_pull_request_rows_survey_reads() -> None:
    """A GitLab MR list node becomes the ``gh pr list`` shape ``Survey.survey_one`` already consumes.

    Catches survey silently reading an empty field — a merged MR with no ``mergeCommit.oid`` is
    blocked ``no-integration-commit`` and a missing ``reviews`` list trips the FR-11 refusal.
    """
    rows = [GitLab.survey_pull_request(node) for node in _recorded_survey_nodes()]
    first = rows[0]

    assert [row["number"] for row in rows] == [3991, 3988, 3982]
    assert {row["state"] for row in rows} == {"MERGED"}
    assert first["mergeCommit"] == {"oid": "d348beeb567c64d4f9a69fc728511c660c6cf2d0"}
    assert first["url"] == "https://gitlab.com/gitlab-org/cli/-/merge_requests/3991"
    assert first["author"]["login"] and first["headRefName"] and first["baseRefName"] == "main"
    assert first["baseRefOid"] and first["headRefOid"] and first["changedFiles"] > 0
    assert {review["author"]["login"] for review in first["reviews"]} == {"phikai", "rkumar555", "GitLabDuo"}


def test_a_merged_fast_forward_merge_request_integrates_at_its_head() -> None:
    """A fast-forward merge records no merge commit; the head itself is what landed on the mainline."""
    node = {**_recorded_survey_nodes()[1], "mergeCommitSha": None, "squashOnMerge": False}
    assert GitLab.survey_pull_request(node)["mergeCommit"] == {"oid": node["diffHeadSha"]}

    squashed = {**node, "squashOnMerge": True}
    assert GitLab.survey_pull_request(squashed)["mergeCommit"] is None, "the squash commit's SHA is not in GraphQL"


# ───────────────────────────── glab transport (faked) ─────────────────────────────


def test_survey_fetch_pages_through_merge_requests_up_to_the_limit(monkeypatch: Any) -> None:
    """``--limit`` is honoured across pages: the second request asks only for what is still missing.

    Catches pagination that stops after one page (a 500-MR survey silently returns 100) or overruns
    the limit.
    """
    nodes = _recorded_survey_nodes()
    pages = [(nodes[:2], True, "cursor-1"), (nodes[2:], False, None)]
    requests: list[dict[str, str]] = []

    def fake_glab(argv_tail: list[str]) -> tuple[int, str, str]:
        requests.append(_glab_variables(argv_tail))
        page_nodes, has_next, cursor = pages[len(requests) - 1]
        connection = {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": page_nodes}
        return 0, json.dumps({"data": {"project": {"mergeRequests": connection}}}), ""

    monkeypatch.setattr(GitCommandRunner, "glab", staticmethod(fake_glab))
    remote = ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "group/sub/project")

    pulled = GitLab.fetch_merge_requests(remote, "merged", 3)

    assert [row["number"] for row in pulled] == [3991, 3988, 3982]
    assert requests[0] == {"hostname": "gitlab.example.com", "fullPath": "group/sub/project", "state": "merged", "first": "3"}
    assert requests[1]["first"] == "1" and requests[1]["after"] == "cursor-1"


def test_an_unreadable_project_is_refused_with_the_login_remedy(monkeypatch: Any) -> None:
    """GraphQL answers ``project: null`` (exit 0) for a missing *or* private project; that must not read as "no MRs".

    Catches an unauthenticated self-managed survey reporting an empty repo instead of naming the
    ``glab auth login`` that fixes it.
    """
    monkeypatch.setattr(GitCommandRunner, "glab", staticmethod(lambda argv_tail: (0, '{"data":{"project":null}}', "")))
    remote = ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "group/project")

    with pytest.raises(ForgeError, match="glab auth login --hostname gitlab.example.com"):
        GitLab.fetch_merge_requests(remote, "all", 10)


# ───────────────────────────── capture against a real fixture repo ─────────────────────────────


def _capture_with_fake_glab(fixture: MergeRequestFixture, monkeypatch: Any) -> Any:
    monkeypatch.setattr(
        GitCommandRunner, "glab", staticmethod(lambda argv_tail: (0, _graphql_response(fixture.merge_request), ""))
    )
    remote = ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "group/sub/project")
    return GitLab.capture(remote, fixture.iid, fixture.clone, base_ref=fixture.base_ref)


def test_capture_recovers_a_force_pushed_round_gitlab_keeps_around(tmp_path: Path, monkeypatch: Any) -> None:
    """All three rounds are recoverable, including the one force-pushed away, because GitLab keeps it.

    The clone holds none of them before capture. Catches capture fetching only the MR head ref —
    the force-pushed round 1 would then be reported unrecoverable although the forge still serves it.
    """
    fixture = RepoBuilder.build_gitlab_merge_request_with_force_push(tmp_path)
    present, _ = GitCommandRunner.git(fixture.clone, "cat-file", "-e", f"{fixture.force_pushed_tip}^{{commit}}")
    assert present != 0, "the clone must not already hold the force-pushed round (else the test proves nothing)"

    facts = _capture_with_fake_glab(fixture, monkeypatch)

    assert [iteration.tip_sha for iteration in facts.iterations] == [
        fixture.force_pushed_tip,
        fixture.round2_tip,
        fixture.round3_tip,
    ]
    assert all(iteration.recoverable for iteration in facts.iterations)
    assert "return a - b" in facts.iterations[0].diff, "round 1's diff is the reviewed pre-fix state"
    assert [verdict.state for verdict in facts.review_verdicts] == ["CHANGES_REQUESTED", "APPROVED", "APPROVED"]


def test_capture_reports_a_collected_round_unrecoverable_rather_than_failing(tmp_path: Path, monkeypatch: Any) -> None:
    """When the forge no longer has the force-pushed round, that one round is unrecoverable; capture still succeeds."""
    fixture = RepoBuilder.build_gitlab_merge_request_with_force_push(tmp_path, keep_around=False)

    facts = _capture_with_fake_glab(fixture, monkeypatch)

    assert [iteration.recoverable for iteration in facts.iterations] == [False, True, True]
    assert facts.iterations[0].diff == ""


def test_capture_maps_a_moved_forward_comment_back_to_the_line_the_reviewer_saw(tmp_path: Path, monkeypatch: Any) -> None:
    """The round-2 comment GitLab re-anchored onto round 3 (line 6) is reported at round 2's line 5.

    Round 3 prepends one line, so taking the moved position verbatim would point ``show`` at the
    wrong line of round 2's diff. The reply in the same thread inherits the same anchor.
    """
    fixture = RepoBuilder.build_gitlab_merge_request_with_force_push(tmp_path)

    facts = _capture_with_fake_glab(fixture, monkeypatch)

    by_id = {comment.id: comment for comment in facts.comments}
    assert sorted(by_id) == [101, 104, 108], "only line comments are captured; the general note 109 is not"
    assert (by_id[101].iteration_index, by_id[101].line_end) == (0, 2)
    assert (by_id[104].iteration_index, by_id[104].line_start, by_id[104].line_end) == (1, 5, 5)
    assert (by_id[108].iteration_index, by_id[108].line_end) == (1, 5)
    _, round2_code = GitCommandRunner.git(fixture.clone, "show", f"{fixture.round2_tip}:code.py")
    assert round2_code.splitlines()[4] == "    for item in items:", "line 5 of round 2 is the line the comment names"


# ───────────────────────────── the capture verb, end to end ─────────────────────────────

#: The SSH remote a GitLab clone's ``origin`` names; ``insteadOf`` routes its fetches to the fixture origin.
_GITLAB_SSH_REMOTE = "git@gitlab.example.com:group/sub/project.git"


def _point_clone_at_gitlab(fixture: MergeRequestFixture) -> None:
    """Make the clone's ``origin`` a GitLab SSH remote whose fetches still resolve to the local fixture origin."""
    GitCommandRunner.git(fixture.clone, "remote", "set-url", "origin", _GITLAB_SSH_REMOTE)
    GitCommandRunner.git(fixture.clone, "config", f"url.{fixture.origin}.insteadOf", _GITLAB_SSH_REMOTE)


def test_capture_verb_writes_a_gitlab_candidate_from_an_ssh_remote(tmp_path: Path, monkeypatch: Any) -> None:
    """``capture <iid>`` on a GitLab SSH clone records the full project path and the MR's own web URL.

    Catches the verb still dispatching to ``gh`` (the stub below fails any ``gh`` call), writing a
    ``github.com/.../pull/`` URL that ``emit`` would then clone from, or trimming the nested group.
    """
    fixture = RepoBuilder.build_gitlab_merge_request_with_force_push(tmp_path)
    _point_clone_at_gitlab(fixture)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(emit_document({"version": "v1", "default": "medium", "rule": []}))
    monkeypatch.setattr(GitCommandRunner, "gh", staticmethod(lambda *_, **__: pytest.fail("a GitLab capture called gh")))
    monkeypatch.setattr(
        GitCommandRunner, "glab", staticmethod(lambda argv_tail: (0, _graphql_response(fixture.merge_request), ""))
    )

    code = Cli.run(["capture", str(fixture.iid), "--clone", str(fixture.clone), "--dataset", str(dataset)])

    assert code == ExitCode.SUCCESS
    candidate = Candidate.load(dataset / "candidates" / f"pr-{fixture.iid}.json")
    assert candidate["repo"] == "group/sub/project"
    assert candidate["pr_url"] == "https://gitlab.example.com/group/sub/project/-/merge_requests/7"
    assert all(iteration["patch_path"] for iteration in candidate["iterations"]), "every round, the force-pushed one too"
    assert [iteration["comment_ids"] for iteration in candidate["iterations"]] == [[101], [104, 108], []]


def test_capture_verb_refuses_forge_gitlab_on_a_remote_with_no_host(tmp_path: Path) -> None:
    """``--forge gitlab`` on a remote no host can be read from is a usage error, never a guessed gitlab.com."""
    fixture = RepoBuilder.build_gitlab_merge_request_with_force_push(tmp_path)
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(emit_document({"version": "v1", "default": "medium", "rule": []}))

    code = Cli.run(["capture", "7", "--forge", "gitlab", "--clone", str(fixture.clone), "--dataset", str(dataset)])

    assert code == ExitCode.USAGE


def _merged_node_for(fixture: MergeRequestFixture) -> dict[str, Any]:
    """The survey node GitLab would list for the fixture MR: merged onto ``main`` as one commit, approved by its reviewer."""
    _, squash_sha = GitCommandRunner.git(fixture.origin, "rev-parse", "main")
    return {
        "iid": str(fixture.iid),
        "title": "Add scan()",
        "description": "",
        "state": "merged",
        "createdAt": "2026-08-01T09:00:00Z",
        "mergedAt": "2026-08-03T11:00:00Z",
        "closedAt": None,
        "sourceBranch": "feature",
        "targetBranch": "main",
        "diffHeadSha": fixture.round3_tip,
        "mergeCommitSha": squash_sha,
        "squashOnMerge": True,
        "webUrl": f"https://gitlab.example.com/group/sub/project/-/merge_requests/{fixture.iid}",
        "author": {"username": "author-bob"},
        "labels": {"nodes": []},
        "approvedBy": {"nodes": [{"username": "reviewer-alice"}]},
        "reviewers": {"nodes": [{"username": "reviewer-alice", "mergeRequestInteraction": {"reviewState": "APPROVED"}}]},
        "diffRefs": {"baseSha": fixture.base_sha, "headSha": fixture.round3_tip, "startSha": fixture.base_sha},
        "diffStatsSummary": {"additions": 6, "deletions": 0, "fileCount": 1},
    }


def test_survey_verb_fetches_merge_request_heads_with_the_gitlab_refspec(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """``survey --repo`` on a GitLab clone lists MRs with ``glab`` and fetches ``refs/merge-requests/*/head``.

    Catches the GitHub refspec surviving anywhere on the path: GitLab serves no ``refs/pull/*``, so
    the fetch would fail (or, with ``--no-fetch``, every MR would be blocked ``no-pull-head-ref``).
    The fixture changes no test file, so the one MR is blocked ``no-test-change`` — and only that.
    """
    fixture = RepoBuilder.build_gitlab_merge_request_with_force_push(tmp_path)
    _point_clone_at_gitlab(fixture)
    node = _merged_node_for(fixture)
    connection = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [node]}
    monkeypatch.setattr(GitCommandRunner, "gh", staticmethod(lambda *_, **__: pytest.fail("a GitLab survey called gh")))
    monkeypatch.setattr(
        GitCommandRunner,
        "glab",
        staticmethod(lambda argv_tail: (0, json.dumps({"data": {"project": {"mergeRequests": connection}}}), "")),
    )

    code = Cli.run(["--json", "survey", "--clone", str(fixture.clone), "--repo", "group/sub/project"])

    (refusal,) = json.loads(capsys.readouterr().out)["failures"]
    assert code == ExitCode.REFUSAL
    assert refusal["check"] == "no-harvestable-pr"
    assert refusal["offending"].endswith("the most common blocker is no-test-change (1 of 1)"), refusal
    _, head = GitCommandRunner.git(fixture.clone, "rev-parse", f"refs/remotes/pr/{fixture.iid}")
    assert head == fixture.round3_tip
