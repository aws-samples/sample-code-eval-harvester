"""Tests for reading which forge a clone came from, and the URLs and refspecs that follow from it.

The forge is never configured twice: the clone's ``origin`` URL names the host and project path, and
everything forge-specific downstream — the pull-head refspec, the PR's web URL written into the
candidate, the clone URL the verifier Dockerfile uses — is derived from that one parse. The
properties that matter: a nested GitLab group path survives intact (it is the project's identity, not
noise to trim to two segments), a self-managed GitLab host is recognised, and a remote this cannot
parse still yields the GitHub fallback the existing local-fixture tests rely on.
"""

from __future__ import annotations

import pytest

from eval_harvest.forge_host import ForgeHost, ForgeHostError, ForgeKind, ForgeRemote


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:owner/name.git", ForgeRemote(ForgeKind.GITHUB, "github.com", "owner/name")),
        ("https://github.com/owner/name.git", ForgeRemote(ForgeKind.GITHUB, "github.com", "owner/name")),
        ("https://github.com/owner/name", ForgeRemote(ForgeKind.GITHUB, "github.com", "owner/name")),
        ("git@gitlab.com:gitlab-org/cli.git", ForgeRemote(ForgeKind.GITLAB, "gitlab.com", "gitlab-org/cli")),
        (
            "https://gitlab.example.com/platform/payments/api.git",
            ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "platform/payments/api"),
        ),
        (
            "ssh://git@gitlab.example.com:2222/platform/payments/api.git",
            ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "platform/payments/api"),
        ),
        (
            "https://oauth2:secret@gitlab.example.com/platform/api.git",
            ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "platform/api"),
        ),
    ],
)
def test_remote_url_names_the_forge_host_and_full_project_path(url: str, expected: ForgeRemote) -> None:
    """GitHub and GitLab remotes in SSH, scp-like, and HTTPS form parse to (kind, host, full path).

    Catches a nested GitLab group being cut to its last two segments — which would make capture
    query a project that does not exist — and credentials embedded in an HTTPS remote leaking into
    the host.
    """
    assert ForgeHost.parse_remote_url(url) == expected


def test_explicit_forge_overrides_host_name_detection() -> None:
    """A self-managed GitLab whose host does not contain "gitlab" is still GitLab when the operator says so.

    Catches auto-detection being the only route: ``code.example.com`` would otherwise parse as an
    unknown host and capture would fall back to GitHub's API against a GitLab server.
    """
    assert ForgeHost.parse_remote_url("git@code.example.com:team/svc.git") is None
    assert ForgeHost.parse_remote_url("git@code.example.com:team/svc.git", forge=ForgeKind.GITLAB) == ForgeRemote(
        ForgeKind.GITLAB, "code.example.com", "team/svc"
    )


def test_unparseable_remote_falls_back_to_github_with_the_last_two_segments() -> None:
    """A local fixture path remote resolves to a deterministic GitHub ``owner/name`` slug.

    Catches the GitLab work changing what every existing local-fixture capture writes as its repo.
    """
    assert ForgeHost.resolve_remote_url("/tmp/work/origin") == ForgeRemote(ForgeKind.GITHUB, "github.com", "work/origin")


def test_explicit_gitlab_forge_on_an_unparseable_remote_is_refused() -> None:
    """``--forge gitlab`` with a remote that names no host fails loudly rather than guessing gitlab.com.

    Catches a silent default host: capture would read some other project's merge requests.
    """
    with pytest.raises(ForgeHostError, match="cannot read a GitLab host"):
        ForgeHost.resolve_remote_url("/tmp/work/origin", forge=ForgeKind.GITLAB)


def test_gitlab_urls_and_refspecs_follow_from_the_parsed_remote() -> None:
    """A GitLab remote yields the merge-request web URL, an HTTPS clone URL, and the MR head refspec."""
    remote = ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "platform/payments/api")

    assert remote.pull_request_url(12) == "https://gitlab.example.com/platform/payments/api/-/merge_requests/12"
    assert remote.clone_url == "https://gitlab.example.com/platform/payments/api.git"
    assert remote.pull_head_refspec == "+refs/merge-requests/*/head:refs/remotes/pr/*"
    assert remote.single_pull_head_refspec(12) == "+refs/merge-requests/12/head:refs/remotes/pr/12"


def test_github_urls_and_refspecs_are_unchanged() -> None:
    """A GitHub remote yields exactly the URL and refspec shapes the code wrote before GitLab existed."""
    remote = ForgeRemote(ForgeKind.GITHUB, "github.com", "owner/name")

    assert remote.pull_request_url(1234) == "https://github.com/owner/name/pull/1234"
    assert remote.clone_url == "https://github.com/owner/name.git"
    assert remote.pull_head_refspec == "+refs/pull/*/head:refs/remotes/pr/*"
    assert remote.single_pull_head_refspec(1234) == "+refs/pull/1234/head:refs/remotes/pr/1234"


@pytest.mark.parametrize(
    ("pr_url", "repo", "expected"),
    [
        ("https://github.com/owner/name/pull/7", "owner/name", "https://github.com/owner/name.git"),
        (
            "https://gitlab.example.com/platform/payments/api/-/merge_requests/12",
            "platform/payments/api",
            "https://gitlab.example.com/platform/payments/api.git",
        ),
        ("", "owner/name", "https://github.com/owner/name.git"),
    ],
)
def test_clone_url_is_recovered_offline_from_a_candidates_pr_url(pr_url: str, repo: str, expected: str) -> None:
    """``emit`` derives the clone URL from the candidate's recorded ``pr_url``, never from a forge call.

    Catches a GitLab datapoint's verifier Dockerfile cloning ``github.com/<gitlab path>`` — a repo
    that does not exist — because emit hard-coded the GitHub host.
    """
    assert ForgeHost.clone_url_from_pull_request_url(pr_url, repo) == expected


def test_task_slug_folds_nested_group_segments_into_harbors_org_name_shape() -> None:
    """Harbor task names are exactly ``org/name``; a nested GitLab path folds its extra segments into the name."""
    assert ForgeHost.harbor_org_name("owner/name") == "owner/name"
    assert ForgeHost.harbor_org_name("platform/payments/api") == "platform/payments__api"
