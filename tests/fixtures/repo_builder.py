"""Build throwaway git repositories with planted leaks, for the seal/verify tests (E-1).

The seal checks (``seal.py``) assert *absence* over genuine git internals, so a mock repository
would prove nothing — the whole point is that a tree can grep clean while ``git show`` prints the
answer. Every repo here is a real on-disk repo built via ``git`` so the checks run against real
plumbing. E-1 added the "plant a leak in a named channel" helpers the seal tests need; B-2 extends
it with the PR-iteration support the forge tests need (:meth:`RepoBuilder.build_squash_merged_pull_request`
and :meth:`RepoBuilder.build_pull_request_with_unrecoverable_iteration`).

The forge builders create a real *origin* repo whose PR head lives at ``refs/pull/<n>/head`` and a
*clone* of it — because the recovery behaviour B-2 tests *is* git's behaviour (a normal
``git clone`` never fetches ``refs/pull/*``; only the explicit head fetch recovers a squash-merged,
branch-deleted PR), so faking it would only test the fake (§12). The ``gh`` REST payloads are
loaded from recorded templates under ``tests/fixtures/gh/`` with the real commit SHAs substituted
in, which is the ``--from-json`` replay path the tests use instead of hitting GitHub.

The builder isolates git from the developer's global/system config (``GIT_CONFIG_GLOBAL`` and
``GIT_CONFIG_SYSTEM`` point at the null device) so a stray ``include.path`` or commit-signing
setting in ``~/.gitconfig`` cannot make a "clean" fixture fail the very channels seal checks for.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404  # real git builds the fixture repos; every call below is argv-only, shell=False
from dataclasses import dataclass
from pathlib import Path
from typing import Any  # the recorded gh payloads are arbitrary JSON objects; dict[str, Any] is honest.

_GIT = shutil.which("git", path=os.defpath) or "git"

#: Where the recorded ``gh`` REST payloads live, one subdirectory per scenario. SHAs in these
#: templates are placeholder tokens the builder substitutes with the fixture's real commit SHAs.
_GH_PAYLOAD_DIR = Path(__file__).parent / "gh"

#: The pull-request number the forge fixtures use throughout, matched by the recorded payloads.
_FIXTURE_PR_NUMBER = 1234


@dataclass(frozen=True, slots=True)
class PullRequestFixture:
    """A built PR: its origin/clone paths, the key SHAs, and the recorded reviews/comments/timeline.

    ``clone`` already holds the pull head when the builder fetched it; ``base_sha`` is the mainline
    branch point the iteration diffs are taken against; ``unrecoverable_tip`` is empty unless the
    scenario planted a force-pushed-away round-1 tip.
    """

    origin: Path
    clone: Path
    pr_number: int
    base_ref: str
    base_sha: str
    round1_tip: str
    round2_tip: str
    unrecoverable_tip: str
    reviews: list[dict[str, Any]]
    comments: list[dict[str, Any]]
    timeline: list[dict[str, Any]]


#: Environment that pins git to a fixed identity and no ambient config, so a fixture repo is a
#: function of the builder calls alone — not of whatever the host's ~/.gitconfig happens to set.
_ISOLATED_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Eval Harvest",
    "GIT_AUTHOR_EMAIL": "eval@harvest.test",
    "GIT_COMMITTER_NAME": "Eval Harvest",
    "GIT_COMMITTER_EMAIL": "eval@harvest.test",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": os.devnull,
}


class RepoBuilder:
    """Construct sealed fixture repositories and plant leaks in named channels. Holds no state."""

    @classmethod
    def build_sealed_repo(cls, root: Path) -> Path:
        """A clean single-commit repository at ``root``: the baseline a seal must pass.

        One file, one commit, one branch, no tags, no packed-refs, no remotes — the shape
        ``seal.check_repo`` treats as sealed. Leaks are planted onto this by the helpers below.
        """
        root.mkdir(parents=True, exist_ok=True)
        cls._git(root, "init", "--quiet", "--initial-branch=main")
        (root / "code.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        cls._git(root, "add", "code.py")
        cls._git(root, "commit", "--quiet", "--message", "initial commit")
        return root

    @classmethod
    def plant_alternates(cls, repo: Path, source_objects: Path) -> None:
        """Point ``objects/info/alternates`` at another object store — the motivating leak."""
        alternates = repo / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(f"{source_objects}\n", encoding="utf-8")

    @classmethod
    def plant_packed_refs(cls, repo: Path) -> None:
        """Create a ``packed-refs`` file — a ref channel absent from the loose ref files."""
        head_sha = cls._head_sha(repo)
        (repo / ".git" / "packed-refs").write_text(
            f"# pack-refs with: peeled fully-peeled sorted \n{head_sha} refs/heads/main\n",
            encoding="utf-8",
        )

    @classmethod
    def plant_commit_graph(cls, repo: Path) -> None:
        """Drop a ``objects/info/commit-graph`` cache — it retains pruned commit topology."""
        commit_graph = repo / ".git" / "objects" / "info" / "commit-graph"
        commit_graph.parent.mkdir(parents=True, exist_ok=True)
        commit_graph.write_bytes(b"CGPH\x01\x01\x01\x00")

    @classmethod
    def plant_replace_ref(cls, repo: Path) -> None:
        """Create a ``refs/replace/<sha>`` ref — replace refs substitute object content.

        Replaces one commit with a *different* one (a self-referential replace ref makes git's own
        ``for-each-ref`` fault with "replace depth too high"): commits a second change, resets back,
        then points ``refs/replace/<unreachable>`` at the reachable commit.
        """
        first_sha = cls._head_sha(repo)
        (repo / "code.py").write_text("def add(a: int, b: int) -> int:\n    return a * b\n", encoding="utf-8")
        cls._git(repo, "commit", "--quiet", "--all", "--message", "second commit")
        second_sha = cls._head_sha(repo)
        cls._git(repo, "reset", "--quiet", "--hard", first_sha)
        cls._git(repo, "update-ref", f"refs/replace/{second_sha}", first_sha)

    @classmethod
    def plant_include_path(cls, repo: Path) -> None:
        """Set ``include.path`` locally — an included file can define remotes or hooks unseen.

        The path is deliberately one that does not exist: the seal check keys off the *presence* of
        ``include.path``, never the file it names, and a fixture that pointed at a real world-writable
        temp path would imply it plants a file there. It plants a config key and nothing else.
        """
        cls._git(repo, "config", "--local", "include.path", "/nonexistent/extra.gitconfig")

    @classmethod
    def plant_unreachable_reflog_entry(cls, repo: Path) -> None:
        """Leave a commit in the reflog after resetting away from it — retrievable by reflog.

        Commits a second change, then hard-resets back to the first commit. The second commit is
        no longer reachable from any ref, but the reflog still names it, which is exactly how a
        superseded (rejected) state stays retrievable after a reset.
        """
        first_sha = cls._head_sha(repo)
        (repo / "code.py").write_text("def add(a: int, b: int) -> int:\n    return a - b  # the fix\n", encoding="utf-8")
        cls._git(repo, "commit", "--quiet", "--all", "--message", "second commit")
        cls._git(repo, "reset", "--quiet", "--hard", first_sha)

    @classmethod
    def plant_bare_repo(cls, repo: Path, name: str = "answerkey.git") -> None:
        """Commit a whole bare repository into the tree — recognisable only by shape, not by name."""
        cls._git(repo, "init", "--quiet", "--bare", str(repo / name))

    # ───────────────────────────── PR-iteration fixtures (B-2) ─────────────────────────────

    #: The mainline before-state: the file as it stood before the PR's work.
    _BASE_CODE = "def add(a: int, b: int) -> int:\n    return a + b\n"

    #: Round 1 — the change reviewers requested changes on: a wrong operator and an off-by-one loop.
    _ROUND1_CODE = (
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n"
        "def scan(items):\n"
        "    total = 0\n"
        "    for index in range(len(items) + 1):\n"
        "        total += items[index]\n"
        "    return total\n"
    )

    #: Round 2 — the fix the author pushed and the reviewer approved.
    _ROUND2_CODE = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "def scan(items):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total += item\n"
        "    return total\n"
    )

    @classmethod
    def build_squash_merged_pull_request(cls, root: Path, *, pull_head_fetched: bool = True) -> PullRequestFixture:
        """A squash-merged, branch-deleted PR with two review rounds, recoverable via the pull head.

        Round 1 (commit A) drew change-requesting review with three inline comments; the author
        pushed a fix (commit B) that was approved; the PR was squash-merged into ``main`` and its
        branch deleted. Because ``refs/pull/<n>/head`` still points at B (and A is B's ancestor),
        both iteration tips remain recoverable — the exact chain the north star turns on (S-1). With
        ``pull_head_fetched=False`` the clone is left without the pull ref so a test can drive the
        head-recovery fetch itself.
        """
        origin = cls._init_origin(root)
        base_sha = cls._head_sha(origin)  # main's tip == the branch point, before any PR commit
        cls._git(origin, "checkout", "--quiet", "-b", "feature")
        round1_tip = cls._commit_file(origin, cls._ROUND1_CODE, "PR work: add scan()", date="2026-08-01T09:00:00")
        cls._git(origin, "update-ref", f"refs/pull/{_FIXTURE_PR_NUMBER}/head", round1_tip)
        round2_tip = cls._commit_file(
            origin, cls._ROUND2_CODE, "fix: correct operator and loop bound", date="2026-08-02T09:00:00"
        )
        cls._git(origin, "update-ref", f"refs/pull/{_FIXTURE_PR_NUMBER}/head", round2_tip)
        cls._squash_merge_and_delete_branch(origin, "feature", date="2026-08-03T09:00:00")

        clone = cls._clone(origin, root / "clone")
        if pull_head_fetched:
            cls._fetch_pull_head(clone)
        subs = {"ROUND1_TIP": round1_tip, "ROUND2_TIP": round2_tip}
        return PullRequestFixture(
            origin=origin,
            clone=clone,
            pr_number=_FIXTURE_PR_NUMBER,
            base_ref="main",
            base_sha=base_sha,
            round1_tip=round1_tip,
            round2_tip=round2_tip,
            unrecoverable_tip="",
            reviews=cls._load_gh_payload("squash_merge", "reviews.json", subs),
            comments=cls._load_gh_payload("squash_merge", "comments.json", subs),
            timeline=cls._load_gh_payload("squash_merge", "timeline.json", subs),
        )

    @classmethod
    def build_pull_request_with_unrecoverable_iteration(cls, root: Path) -> PullRequestFixture:
        """A PR whose round-1 tip was force-pushed away and is unfetchable from the pull head.

        Round 1 (commit A_old) drew a change-requesting review, then the author *force-pushed* —
        discarding A_old and re-pushing a different round 1 (A_new) plus the approved fix (B).
        ``refs/pull/<n>/head`` points at B, so A_old is not reachable from it and the clone cannot
        recover it. The round-1 review still names A_old, so its iteration must be reported
        unrecoverable rather than fabricated (FR-8, §4 risk).
        """
        origin = cls._init_origin(root)
        base_sha = cls._head_sha(origin)  # main's tip == the branch point, before any PR commit
        cls._git(origin, "checkout", "--quiet", "-b", "feature")
        unrecoverable_tip = cls._commit_file(origin, cls._ROUND1_CODE, "PR work (force-pushed away)", date="2026-08-01T09:00:00")
        cls._git(origin, "reset", "--quiet", "--hard", "main")  # the force-push: A_old is now dangling
        round1_new_tip = cls._commit_file(origin, cls._ROUND1_CODE, "PR work: add scan() (re-pushed)", date="2026-08-01T10:00:00")
        round2_tip = cls._commit_file(
            origin, cls._ROUND2_CODE, "fix: correct operator and loop bound", date="2026-08-02T09:00:00"
        )
        cls._git(origin, "update-ref", f"refs/pull/{_FIXTURE_PR_NUMBER}/head", round2_tip)
        cls._squash_merge_and_delete_branch(origin, "feature", date="2026-08-03T09:00:00")

        clone = cls._clone(origin, root / "clone")
        cls._fetch_pull_head(clone)
        subs = {"UNRECOVERABLE_TIP": unrecoverable_tip, "ROUND2_TIP": round2_tip}
        return PullRequestFixture(
            origin=origin,
            clone=clone,
            pr_number=_FIXTURE_PR_NUMBER,
            base_ref="main",
            base_sha=base_sha,
            round1_tip=round1_new_tip,
            round2_tip=round2_tip,
            unrecoverable_tip=unrecoverable_tip,
            reviews=cls._load_gh_payload("unrecoverable", "reviews.json", subs),
            comments=cls._load_gh_payload("unrecoverable", "comments.json", subs),
            timeline=cls._load_gh_payload("unrecoverable", "timeline.json", subs),
        )

    @classmethod
    def _init_origin(cls, root: Path) -> Path:
        """Create the origin repo with one mainline commit (the before-state) on ``main``."""
        origin = root / "origin"
        origin.mkdir(parents=True, exist_ok=True)
        cls._git(origin, "init", "--quiet", "--initial-branch=main")
        cls._commit_file(origin, cls._BASE_CODE, "base commit", date="2026-07-31T09:00:00")
        return origin

    @classmethod
    def _squash_merge_and_delete_branch(cls, origin: Path, branch: str, *, date: str) -> None:
        """Squash-merge ``branch`` into ``main`` as a single commit, then delete the branch.

        This is what leaves the pre-merge states recoverable only via ``refs/pull/<n>/head``: the
        squash commit's first parent is the branch point, so the graph alone cannot reconstruct the
        iterations, and the branch is gone.
        """
        cls._git(origin, "checkout", "--quiet", "main")
        cls._git(origin, "merge", "--squash", branch)
        cls._commit_index(origin, f"squash: {branch} (#{_FIXTURE_PR_NUMBER})", date=date)
        cls._git(origin, "branch", "-D", branch)

    @classmethod
    def _commit_file(cls, repo: Path, code: str, message: str, *, date: str) -> str:
        """Write ``code.py``, commit it at ``date``, and return the new commit SHA."""
        (repo / "code.py").write_text(code, encoding="utf-8")
        cls._git(repo, "add", "-A")
        return cls._commit_index(repo, message, date=date)

    @classmethod
    def _commit_index(cls, repo: Path, message: str, *, date: str) -> str:
        """Commit whatever is staged at a fixed author/committer ``date`` and return the SHA."""
        subprocess.run(  # noqa: S603 - literal argv[0], resolved binary via executable=  # nosec B603,B607
            ["git", "-C", str(repo), "commit", "--quiet", "--message", message],
            executable=_GIT,
            capture_output=True,
            text=True,
            env={**_ISOLATED_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
            check=True,
        )
        return cls._head_sha(repo)

    @classmethod
    def _clone(cls, origin: Path, dest: Path) -> Path:
        """Clone ``origin`` into ``dest``, holding ``main`` but no ``refs/pull/*`` and no unreachable objects.

        ``--no-local`` forces the real transport instead of a same-filesystem object copy: a plain
        local clone hardlinks the *entire* object store, which would smuggle in the force-pushed-away
        and pull-head commits and defeat the very recovery this fixture exists to exercise. With
        ``--no-local`` only objects reachable from ``main`` transfer, so the pull head must be fetched.
        """
        subprocess.run(  # noqa: S603 - literal argv[0], resolved binary via executable=  # nosec B603,B607
            ["git", "clone", "--no-local", "--quiet", str(origin), str(dest)],
            executable=_GIT,
            capture_output=True,
            text=True,
            env=_ISOLATED_ENV,
            check=True,
        )
        return dest

    @classmethod
    def _fetch_pull_head(cls, clone: Path) -> None:
        """Fetch the pull head into the clone, simulating the recovery step ``capture`` runs."""
        cls._git(clone, "fetch", "origin", f"+refs/pull/{_FIXTURE_PR_NUMBER}/head:refs/remotes/pr/{_FIXTURE_PR_NUMBER}")

    @staticmethod
    def _load_gh_payload(scenario: str, filename: str, substitutions: dict[str, str]) -> list[dict[str, Any]]:
        """Load a recorded gh payload template and substitute the fixture's real SHAs for its tokens."""
        text = (_GH_PAYLOAD_DIR / scenario / filename).read_text(encoding="utf-8")
        for token, sha in substitutions.items():
            text = text.replace(f"{{{{{token}}}}}", sha)
        loaded: list[dict[str, Any]] = json.loads(text)
        return loaded

    @classmethod
    def _head_sha(cls, repo: Path) -> str:
        completed = subprocess.run(  # noqa: S603 - literal argv[0], resolved binary via executable=  # nosec B603,B607
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            executable=_GIT,
            capture_output=True,
            text=True,
            env=_ISOLATED_ENV,
            check=True,
        )
        return completed.stdout.strip()

    @classmethod
    def _git(cls, repo: Path, *args: str) -> None:
        subprocess.run(  # noqa: S603 - literal argv[0], resolved binary via executable=  # nosec B603,B607
            ["git", "-C", str(repo), *args],
            executable=_GIT,
            capture_output=True,
            text=True,
            env=_ISOLATED_ENV,
            check=True,
        )
