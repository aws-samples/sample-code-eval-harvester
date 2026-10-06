"""Tests for how GitLab capture talks to its transports: which git remote it fetches from, and how it
reads a ``glab`` response that is not the JSON it asked for.

Both are failure modes of one merge request that must stay one merge request's failure: a capture
batch contains only :class:`~eval_harvest.forge.ForgeError`, so anything else escaping aborts the
whole batch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_harvest.forge import Forge, ForgeError
from eval_harvest.forge_host import ForgeKind, ForgeRemote
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.gitlab import GitLab

_REMOTE = ForgeRemote(ForgeKind.GITLAB, "gitlab.example.com", "group/proj", git_remote="upstream")


class _StopAfterFetchError(Exception):
    """Raised by the fetch stub so a capture stops before it needs a forge."""


@pytest.mark.parametrize("output", ["<html>sign in</html>", "", '["not", "an", "object"]'])
def test_a_glab_response_that_is_not_a_json_object_is_a_forge_error(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    """A proxy's sign-in page (exit 0, HTML body) becomes a ``ForgeError`` naming the start of the body.

    Catches ``JSONDecodeError`` escaping: survey and batch capture contain only ``ForgeError``, so one
    bad response would abort a whole ``capture --from-survey`` run instead of refusing one MR.
    """
    monkeypatch.setattr(GitCommandRunner, "glab", classmethod(lambda cls, argv: (0, output, "")))

    with pytest.raises(ForgeError, match="not a JSON object"):
        GitLab.fetch_merge_request(_REMOTE, 1)


def test_gitlab_capture_fetches_from_the_resolved_git_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GitLab capture fetches the MR head from the remote the forge was detected on, not a hard-coded ``origin``."""
    fetched_from: list[str] = []

    def record_fetch(clone: Path, refspec: str, *, remote: str = "origin") -> None:
        fetched_from.append(remote)
        raise _StopAfterFetchError

    monkeypatch.setattr(Forge, "fetch_refs", staticmethod(record_fetch))

    with pytest.raises(_StopAfterFetchError):
        GitLab.capture(_REMOTE, 1, tmp_path)
    assert fetched_from == ["upstream"]


def test_github_capture_fetches_from_the_resolved_git_remote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GitHub capture fetches the PR head from the named remote too, so neither forge assumes ``origin``."""
    fetched_from: list[str] = []

    def record_fetch(clone: Path, refspec: str, *, remote: str = "origin") -> None:
        fetched_from.append(remote)
        raise _StopAfterFetchError

    monkeypatch.setattr(Forge, "fetch_refs", staticmethod(record_fetch))

    with pytest.raises(_StopAfterFetchError):
        Forge.capture("owner/name", 1, tmp_path, remote="upstream")
    assert fetched_from == ["upstream"]
