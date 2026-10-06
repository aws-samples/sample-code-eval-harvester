"""Which forge a clone came from, and every forge-specific URL and refspec that follows from it.

The forge is read once, from the clone's ``origin`` remote URL: its host and project path name the
project, and the host names the forge (``github.com`` is GitHub; a host containing ``gitlab`` is
GitLab; anything else needs ``--forge``). Everything downstream that used to hard-code GitHub — the
pull-head refspec ``survey`` and ``capture`` fetch, the PR web URL written into the candidate, the
clone URL the verifier Dockerfile uses — is a property of the parsed :class:`ForgeRemote`, so the
forge is never configured in two places that can disagree.

A GitLab project path is kept whole: ``platform/payments/api`` is the project's identity in a nested
group, not noise to trim to two segments. Harbor's task ids are exactly ``org/name``, so the extra
segments are folded into the name only where a Harbor id is built (:meth:`ForgeHost.harbor_org_name`).

**Layer.** Parsing is pure; :meth:`ForgeHost.from_clone` reads the clone's remote through
:class:`~eval_harvest.gitcmd.GitCommandRunner` and makes no network call. ``emit`` recovers the clone
URL from the candidate's recorded ``pr_url`` (:meth:`ForgeHost.clone_url_from_pull_request_url`) so
the offline layer never needs the clone at all (NFR-2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from eval_harvest.gitcmd import GitCommandRunner

#: An scp-like SSH remote, ``[user@]host:path``. The host must contain a dot so a Windows drive path
#: (``C:/work/origin``) is never read as a host named ``C``.
_SCP_LIKE_REMOTE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+\.[^:/]+):(?P<path>[^/].*)$")

#: A URL remote, ``scheme://[user[:secret]@]host[:port]/path``. Credentials and port are matched only
#: to be dropped: neither belongs in a host name written into a candidate.
_URL_REMOTE = re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^:/@]+)(?::\d+)?/(?P<path>.+)$")

#: The separator GitLab puts between a project path and its sub-pages in a web URL.
_GITLAB_WEB_PATH_SEPARATOR = "/-/merge_requests/"

#: A GitHub repo slug is ``owner/name`` — the fallback's two path segments.
_GITHUB_SLUG_SEGMENTS = 2

#: Harbor's task-id shape is exactly one ``org/name`` slash; nested path segments fold with this.
_HARBOR_SEGMENT_JOINER = "__"


class ForgeHostError(ValueError):
    """The clone's remote cannot be read as the forge the operator named."""


class ForgeKind(StrEnum):
    """The forges harvesting supports. The value is the ``--forge`` spelling."""

    GITHUB = "github"
    GITLAB = "gitlab"


@dataclass(frozen=True, slots=True)
class ForgeRemote:
    """One project on one forge: its kind, host, and full project path (``owner/name`` or ``group/sub/name``)."""

    kind: ForgeKind
    host: str
    project_path: str

    @property
    def clone_url(self) -> str:
        """The anonymous HTTPS clone URL the verifier Dockerfile uses."""
        return f"https://{self.host}/{self.project_path}.git"

    @property
    def pull_head_refspec(self) -> str:
        """The bulk refspec that fetches every PR/MR head into ``refs/remotes/pr/*``, the namespace survey reads."""
        if self.kind is ForgeKind.GITLAB:
            return "+refs/merge-requests/*/head:refs/remotes/pr/*"
        return "+refs/pull/*/head:refs/remotes/pr/*"

    def pull_head_ref(self, number: int) -> str:
        """The forge-side ref that holds one PR/MR's head (``refs/pull/<n>/head`` or ``refs/merge-requests/<n>/head``)."""
        if self.kind is ForgeKind.GITLAB:
            return f"refs/merge-requests/{number}/head"
        return f"refs/pull/{number}/head"

    def single_pull_head_refspec(self, number: int) -> str:
        """The refspec that fetches one PR/MR's head into ``refs/remotes/pr/<n>``."""
        return f"+{self.pull_head_ref(number)}:refs/remotes/pr/{number}"

    def pull_request_url(self, number: int) -> str:
        """The PR/MR web URL recorded in the candidate's ``pr_url``."""
        if self.kind is ForgeKind.GITLAB:
            return f"https://{self.host}/{self.project_path}{_GITLAB_WEB_PATH_SEPARATOR}{number}"
        return f"https://{self.host}/{self.project_path}/pull/{number}"


class ForgeHost:
    """Parse remotes into :class:`ForgeRemote` and derive offline URLs from recorded ones. Holds no state."""

    @classmethod
    def from_clone(cls, clone: Path, *, forge: ForgeKind | None = None) -> ForgeRemote:
        """The forge project the clone's ``origin`` remote names (no network call).

        Reads the configured URL, not ``git remote get-url``'s: that one applies ``url.*.insteadOf``
        rewrites, which are local transport aliases (a mirror, an SSH alias) and not the project's name.
        """
        _, url = GitCommandRunner.git(clone, "config", "--get", "remote.origin.url")
        return cls.resolve_remote_url(url, forge=forge)

    @classmethod
    def resolve_remote_url(cls, url: str, *, forge: ForgeKind | None = None) -> ForgeRemote:
        """Parse ``url``; a remote that names no forge host falls back to GitHub, unless GitLab was demanded.

        The GitHub fallback (the last two path segments of a local fixture path) is what every
        existing local-fixture capture relies on, so it stays deterministic and non-empty. Explicit
        ``--forge gitlab`` on such a remote is refused instead: there is no host to query, and a
        silent ``gitlab.com`` default would read some other project's merge requests.
        """
        parsed = cls.parse_remote_url(url, forge=forge)
        if parsed is not None:
            return parsed
        if forge is ForgeKind.GITLAB:
            raise ForgeHostError(
                f"cannot read a GitLab host and project path from the remote {url!r}; "
                "point origin at the GitLab project (git@host:group/project.git)"
            )
        return ForgeRemote(ForgeKind.GITHUB, "github.com", cls._fallback_github_slug(url))

    @classmethod
    def parse_remote_url(cls, url: str, *, forge: ForgeKind | None = None) -> ForgeRemote | None:
        """``(kind, host, project_path)`` for an SSH, scp-like, or HTTP(S) remote, or ``None`` if unrecognised.

        ``forge`` overrides host-name detection — the route for a self-managed GitLab whose host does
        not contain ``gitlab``. Without it, an unrecognised host is ``None`` rather than a guess.
        """
        trimmed = url.strip().removesuffix("/").removesuffix(".git")
        match = _URL_REMOTE.match(trimmed) or _SCP_LIKE_REMOTE.match(trimmed)
        if match is None:
            return None
        host = match.group("host").lower()
        project_path = match.group("path").strip("/")
        kind = forge or cls._kind_from_host(host)
        if kind is None or "/" not in project_path:
            return None
        return ForgeRemote(kind, host, project_path)

    @staticmethod
    def _kind_from_host(host: str) -> ForgeKind | None:
        """GitHub for ``github.com``, GitLab for any host naming ``gitlab``, else unknown."""
        if host == "github.com":
            return ForgeKind.GITHUB
        if "gitlab" in host:
            return ForgeKind.GITLAB
        return None

    @staticmethod
    def _fallback_github_slug(url: str) -> str:
        """The last two path segments of an unrecognised remote (a local fixture path), as ``owner/name``."""
        trimmed = url.strip().removesuffix(".git")
        segments = [segment for segment in trimmed.replace("\\", "/").split("/") if segment][-_GITHUB_SLUG_SEGMENTS:]
        if len(segments) == _GITHUB_SLUG_SEGMENTS:
            return "/".join(segments)
        return segments[-1] if segments else "unknown/unknown"

    @staticmethod
    def clone_url_from_pull_request_url(pr_url: str, repo: str) -> str:
        """The HTTPS clone URL recovered offline from a candidate's recorded ``pr_url``.

        A GitLab MR URL carries its host and full project path before ``/-/merge_requests/``; any
        other (or empty) ``pr_url`` is a GitHub PR, cloned from ``github.com/<repo>`` as before.
        """
        if _GITLAB_WEB_PATH_SEPARATOR in pr_url:
            return pr_url.split(_GITLAB_WEB_PATH_SEPARATOR, 1)[0] + ".git"
        return f"https://github.com/{repo}.git"

    @staticmethod
    def harbor_org_name(project_path: str) -> str:
        """``project_path`` in Harbor's exactly-one-slash ``org/name`` shape: nested segments fold into the name."""
        org, _, name = project_path.partition("/")
        return f"{org}/{name.replace('/', _HARBOR_SEGMENT_JOINER)}" if name else project_path
