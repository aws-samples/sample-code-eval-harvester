"""Answer-absence checking: the two halves of US-7's leak scan (TP-5, FR-35/36/40, NFR-6).

A leaked answer is the expensive silent failure — if the fix, the review discussion, the PR
number, or a follow-up commit is reachable from anything the evaluated agent can read, every score
looks fine and measures nothing. This module builds the two-halves scan:

- :meth:`Seal.check_repo` — the **git-channel checklist**, the enumeration of the channels a git
  repository leaks its own future through while looking clean.
  It asserts *absence* over genuine git internals, because the checks that matter are for channels
  that leave the visible tree looking correct: a single ``objects/info/alternates`` line makes
  ``git log --all`` still report one commit while ``git show`` prints the answer. Blacklisted paths
  fail open, so the whitelist of allowed ``.git`` entries fails closed alongside them.
- :meth:`Seal.scan_agent_visible` — the **content scanner**, new code (TP-5 is explicit that this
  half is not a port). It greps agent-visible files for the PR number, review text, and any answer
  fragment drawn from the verifier-only tree (``solution/``, ``tests/oracle.json``, ``judge.toml``,
  FR-40), guarded by a planted control token: if the scan does not see the token the driver planted,
  it refuses ``scan-did-not-run`` rather than reporting clean — an empty result and a broken scan
  are otherwise identical (FR-36/NFR-6; CLAUDE.md "a scanner that finds nothing must prove it
  scanned something").

``verify`` (E-2) drives both, plants the control token into the corpus first (§7.2), and maps this
module's :class:`Report` into the FR-2 four-field refusal. Everything here reads local files and the
local ``.git`` only — no network, no forge (NFR-2).

Git is invoked through a self-contained helper here rather than through ``gitcmd.py`` (A-2): E-1's
dependency is A-1 alone, and A-2 had not landed, so a self-contained ``git()`` wrapper lives here.
When ``gitcmd.py`` lands, this helper is the obvious thing to route through it (see the task's
Completion Log).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # nosec B404  # only Seal.git spawns git, always as a fixed argv list, never a shell string
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: Absolute path to git, resolved once. ``path=os.defpath`` preserves the search space the seal
#: uses (the subprocess ``env`` below carries no ``PATH``, so git falls back to ``os.defpath``), and
#: ``or "git"`` keeps the absent-git failure a plain ``FileNotFoundError`` rather than a new error at
#: import time. Resolving the full path is also what keeps bandit's partial-path finding structural
#: rather than suppressed.
_GIT: Final = shutil.which("git", path=os.defpath) or "git"

#: The read-only, offline environment git runs under: prompts disabled, no ambient locks, and a
#: ``HOME`` that resolves nowhere so a developer's ``~/.gitconfig`` cannot alter the verdict. Carries
#: no ``PATH`` on purpose (see ``_GIT``).
_GIT_ENV: Final = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "HOME": "/nonexistent"}


@dataclass(frozen=True, slots=True)
class Finding:
    """One seal assertion and its outcome: a stable check name, whether it held, and why."""

    check: str
    passed: bool
    detail: str
    severity: str = "error"


@dataclass
class Report:
    """The findings of one seal run, plus the ``{ok, repo, checks, findings}`` JSON E-2 maps.

    A small mutable accumulator on purpose: :meth:`add` appends as each check runs, mirroring the
    reference. It carries no shared or long-lived state — one report is built and returned per call.
    """

    repo: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, *, passed: bool, detail: str, severity: str = "error") -> None:
        """Record one finding."""
        self.findings.append(Finding(check, passed, detail, severity))

    @property
    def failures(self) -> list[Finding]:
        """The error-severity findings that did not pass — what a refusal is built from."""
        return [finding for finding in self.findings if not finding.passed and finding.severity == "error"]

    def as_json(self) -> dict[str, object]:
        """The machine-readable shape E-2 maps into the FR-2 refusal."""
        return {
            "ok": not self.failures,
            "repo": self.repo,
            "checks": len(self.findings),
            "findings": [
                {"check": f.check, "passed": f.passed, "detail": f.detail, "severity": f.severity} for f in self.findings
            ],
        }


# Paths inside ``.git`` whose mere existence is a seal failure. Each is a distinct route by which
# objects outside the intended tree become reachable, or by which apparent history differs from real
# history.
FORBIDDEN_PATHS: Final[tuple[tuple[str, str], ...]] = (
    ("objects/info/alternates", "borrows another repository's object database wholesale"),
    ("shallow", "marks a truncated history whose boundary can be extended"),
    ("info/grafts", "rewrites parentage, so apparent history is not real history"),
    ("packed-refs", "carries references absent from the loose ref files"),
    ("FETCH_HEAD", "names commits fetched from a remote"),
    ("ORIG_HEAD", "names the pre-operation commit, often the one being hidden"),
    ("MERGE_HEAD", "names the other side of an in-progress merge"),
    ("CHERRY_PICK_HEAD", "names a commit being replayed"),
    ("REVERT_HEAD", "names a commit being reverted"),
    ("BISECT_START", "bisect state enumerates commits across a range"),
    ("commit-graph", "caches commit topology including pruned commits"),
    ("objects/info/commit-graph", "caches commit topology including pruned commits"),
    ("rr-cache", "recorded conflict resolutions encode diff content"),
    ("worktrees", "links to a repository holding full history"),
    ("modules", "nested submodule repositories with their own history"),
)

# The whitelist of ``.git`` entries a freshly created single-commit repository is allowed to
# contain. Anything else is a finding, including files a git version we have not seen might add —
# the fail-closed half of the checklist.
EXPECTED_GITDIR_FILES: Final = frozenset(
    {
        "HEAD",
        "config",
        "index",
        "COMMIT_EDITMSG",
        "description",
        "objects",
        "objects/info",
        "objects/pack",
        "refs",
        "refs/heads",
        "refs/tags",
        "logs",
        "logs/HEAD",
        "logs/refs",
        "logs/refs/heads",
        "hooks",
        "info",
        "info/exclude",
        "branches",
    }
)

LOOSE_OBJECT: Final = re.compile(r"^objects/[0-9a-f]{2}(/[0-9a-f]{38,62})?$")
PACK_ENTRY: Final = re.compile(r"^objects/pack/pack-[0-9a-f]{40,64}\.(pack|idx|rev|mtimes)$")
SAMPLE_HOOK: Final = re.compile(r"^hooks/[a-z-]+\.sample$")
OBJECT_ID_TOKEN: Final = re.compile(r"\b[0-9a-f]{7,64}\b")

#: The two ``.git`` prefixes a clean single-commit repo may hold branch-shaped paths under: the ref
#: itself and its reflog. Both take the same tail — one or more non-empty, slash-separated segments —
#: which ``_is_branch_shaped_path`` checks by splitting rather than by regex.
_BRANCH_PATH_PREFIXES: Final = ("refs/heads/", "logs/refs/heads/")

#: Config keys that can reintroduce a remote, a hook path, or an alternate object store by
#: inclusion — an included file is not visible in ``.git/config`` itself. From ``seal_assert.py:305``.
_INCLUSION_CONFIG_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("include.path", "an included config file can define remotes or hooks"),
    ("includeif", "conditional includes apply outside this file"),
    ("core.hookspath", "relocates hooks outside .git/hooks, past the hooks check"),
    ("core.alternaterefsprefixes", "exposes refs from alternate object stores"),
    ("extensions.objectformat", "signals a non-default object format"),
)

#: Ref namespaces that each name otherwise-unreachable content. From ``seal_assert.py:380``.
_FORBIDDEN_REF_NAMESPACES: Final[tuple[tuple[str, str], ...]] = (
    ("refs/replace", "replace refs substitute object content"),
    ("refs/notes", "notes attach content to arbitrary commits"),
    ("refs/stash", "stash entries hold otherwise-unreachable commits"),
    ("refs/remotes", "remote-tracking refs name upstream commits"),
    ("refs/original", "filter-branch backups retain pre-rewrite history"),
)

# ── content-scanner corpus rules (the new half) ──

#: Subtrees the evaluated agent never sees; verifier-only. Excluded from the scanned corpus, and (for
#: ``solution/`` / ``tests/``) mined for the answer fragments that must not appear in what the agent
#: *does* see (FR-40).
_VERIFIER_ONLY_PREFIXES: Final = ("solution/", "tests/")

#: Verifier/Harbor config files that are not mounted for the agent and legitimately carry the PR
#: number (``task.toml``'s ``[metadata.origin]``), so they are neither scanned nor mined for answers.
_VERIFIER_ONLY_FILENAMES: Final = ("task.toml", "judge.toml", "dataset.toml")

#: Files whose *content* is the reference answer and must be absent from agent-visible files (FR-40).
_ANSWER_SOURCE_PREFIXES: Final = ("solution/",)
_ANSWER_SOURCE_FILES: Final = ("tests/oracle.json", "judge.toml")

#: The shortest verifier-only line worth treating as an answer fragment. Longer than a shebang or a
#: lone brace, so boilerplate does not manufacture spurious leaks; a real fix line clears it easily.
_MIN_ANSWER_FRAGMENT_LEN: Final = 12

#: Enough located hits to prove a canary leaked; stop scanning past this.
_MAX_REPORTED_HITS: Final = 5


class Seal:
    """The git-channel checklist and the agent-visible content scanner. Holds no state."""

    # ───────────────────────────── git-channel checklist ─────────────────────────────

    @classmethod
    def check_repo(cls, repo: Path, canaries: tuple[str, ...]) -> Report:
        """Assert that ``repo`` is a sealed evaluation tree — every check asserts absence (FR-35).

        Runs the git-channel checklist ported from ``seal_assert.py`` (forbidden paths, the
        fail-closed whitelist, stray objects, refs, config inclusions, commit-message object ids,
        commit/root counts, remotes, the reachability-based reflog check, forbidden ref namespaces,
        active hooks, an alternate object store) plus the tree-level checks (nested/bare repos,
        escaping symlinks) and the ``canaries`` control-token half (FR-36/NFR-6).
        """
        report = Report(repo=str(repo))
        gitdir = repo / ".git"
        if not gitdir.exists():
            report.add("no-git-directory", passed=True, detail="no .git present; exported tree with no metadata", severity="info")
        elif gitdir.is_file():
            report.add(
                "gitdir-is-a-file",
                passed=False,
                detail=f"{gitdir} is a file: a linked worktree pointing elsewhere, and the pointed-to repo holds real history",
            )
            return report
        else:
            cls._check_gitdir(report, repo, gitdir)
        cls._check_tree_level(report, repo)
        cls._check_canaries(report, repo, canaries)
        return report

    @classmethod
    def _check_gitdir(cls, report: Report, repo: Path, gitdir: Path) -> None:
        """The checks that only apply when a ``.git`` directory is present."""
        cls._check_forbidden_paths(report, gitdir)
        cls._check_unexpected_entries(report, gitdir)
        cls._check_stray_objects(report, repo)
        cls._check_branches_and_tags(report, repo)
        cls._check_config_inclusions(report, repo)
        cls._check_commit_message_object_ids(report, repo, gitdir)
        cls._check_commit_and_root_counts(report, repo)
        cls._check_remotes(report, repo)
        cls._check_reflog_reachability(report, repo)
        cls._check_forbidden_ref_namespaces(report, repo)
        cls._check_active_hooks(report, gitdir)
        cls._check_alternate_object_store(report, repo)

    @staticmethod
    def _check_forbidden_paths(report: Report, gitdir: Path) -> None:
        for rel, why in FORBIDDEN_PATHS:
            present = (gitdir / rel).exists()
            report.add(f"absent:{rel}", passed=not present, detail=f"{rel} present ({why})" if present else f"{rel} absent")

    @classmethod
    def _check_unexpected_entries(cls, report: Report, gitdir: Path) -> None:
        # Blacklists fail open; this whitelist fails closed — a future git cache file or a stray drop
        # into .git fails without anyone having predicted it.
        unexpected = sorted(
            rel.as_posix() for path in gitdir.rglob("*") if not cls._is_expected_gitdir_entry(rel := path.relative_to(gitdir))
        )
        report.add(
            "gitdir-contains-only-expected-entries",
            passed=not unexpected,
            detail=f"{len(unexpected)} unexpected .git entries: {unexpected[:6]}" if unexpected else "no unexpected entries",
        )

    @classmethod
    def _check_stray_objects(cls, report: Report, repo: Path) -> None:
        # Dangling and unreachable objects are content the refs do not name; they are readable by digest.
        _, out = cls.git(repo, "fsck", "--unreachable", "--dangling", "--no-progress")
        stray = [line for line in out.splitlines() if line.startswith(("unreachable", "dangling"))]
        report.add(
            "no-unreachable-or-dangling-objects",
            passed=not stray,
            detail=f"{len(stray)} unreachable/dangling object(s): {stray[:3]}" if stray else "none",
        )

    @classmethod
    def _check_branches_and_tags(cls, report: Report, repo: Path) -> None:
        _, out = cls.git(repo, "for-each-ref", "--format=%(refname)", "refs/heads")
        branches = [line for line in out.splitlines() if line]
        report.add("single-branch", passed=len(branches) == 1, detail=f"{len(branches)} branches: {branches}")
        _, out = cls.git(repo, "for-each-ref", "--format=%(refname)", "refs/tags")
        tags = [line for line in out.splitlines() if line]
        report.add("no-tags", passed=not tags, detail=f"{len(tags)} tags: {tags[:5]}" if tags else "none")

    @classmethod
    def _check_config_inclusions(cls, report: Report, repo: Path) -> None:
        for key, why in _INCLUSION_CONFIG_KEYS:
            _, out = cls.git(repo, "config", "--local", "--get-regexp", f"^{key}")
            report.add(
                f"no-config:{key}",
                passed=out == "",
                detail=f"{key} set locally: {out.splitlines()!r} ({why})" if out else "unset",
            )

    @classmethod
    def _check_commit_message_object_ids(cls, report: Report, repo: Path, gitdir: Path) -> None:
        # The commit message itself can name the change it was derived from; an object id is enough
        # to fetch the object wherever a path to the source exists.
        _, subject = cls.git(repo, "log", "-1", "--format=%s%n%b")
        sha_like = cls._looks_like_object_id(subject)
        report.add(
            "commit-message-names-no-object-id",
            passed=not sha_like,
            detail=f"commit message contains object-id-like tokens: {sha_like[:3]}" if sha_like else "clean",
        )
        editmsg = gitdir / "COMMIT_EDITMSG"
        if editmsg.exists():
            found = cls._looks_like_object_id(editmsg.read_text(encoding="utf-8", errors="replace"))
            report.add(
                "commit-editmsg-names-no-object-id",
                passed=not found,
                detail=f"COMMIT_EDITMSG contains object-id-like tokens: {found[:3]}" if found else "clean",
            )

    @classmethod
    def _check_commit_and_root_counts(cls, report: Report, repo: Path) -> None:
        _, out = cls.git(repo, "rev-list", "--all", "--count")
        count = int(out) if out.isdigit() else -1
        report.add("single-commit", passed=count == 1, detail=f"{count} reachable commits, expected exactly 1")
        _, out = cls.git(repo, "rev-list", "--all", "--max-parents=0", "--count")
        roots = int(out) if out.isdigit() else -1
        report.add("single-root", passed=roots == 1, detail=f"{roots} root commits, expected exactly 1")

    @classmethod
    def _check_remotes(cls, report: Report, repo: Path) -> None:
        # A remote turns the network into a history channel.
        _, out = cls.git(repo, "remote")
        report.add("no-remotes", passed=out == "", detail=f"remotes configured: {out!r}" if out else "none")
        _, out = cls.git(repo, "config", "--get-regexp", r"^remote\.")
        detail = f"remote.* config keys present: {out.splitlines()!r}" if out else "none"
        report.add("no-remote-config", passed=out == "", detail=detail)

    @classmethod
    def _check_reflog_reachability(cls, report: Report, repo: Path) -> None:
        # About reachability, not volume: a clean repo has one reflog entry per ref, so a count is a
        # false positive. What matters is a reflog entry naming a commit outside the reachable set —
        # how a superseded value stays retrievable after a reset.
        _, reachable_out = cls.git(repo, "rev-list", "--all")
        reachable = {line for line in reachable_out.splitlines() if line}
        _, out = cls.git(repo, "reflog", "--all", "--format=%H")
        referenced = {line for line in out.splitlines() if line}
        stray = sorted(referenced - reachable)
        report.add(
            "reflog-references-nothing-unreachable",
            passed=not stray,
            detail=f"reflog names {len(stray)} commit(s) outside the reachable set: {stray[:3]}"
            if stray
            else f"{len(referenced)} referenced commit(s), all reachable",
        )

    @classmethod
    def _check_forbidden_ref_namespaces(cls, report: Report, repo: Path) -> None:
        for refspace, why in _FORBIDDEN_REF_NAMESPACES:
            _, out = cls.git(repo, "for-each-ref", refspace)
            entries = [line for line in out.splitlines() if line.strip()]
            report.add(
                f"no-refs:{refspace}",
                passed=not entries,
                detail=f"{len(entries)} refs under {refspace} ({why})" if entries else f"{refspace} empty",
            )

    @staticmethod
    def _check_active_hooks(report: Report, gitdir: Path) -> None:
        # Active (non-sample) hooks can execute or disclose content.
        hooks = gitdir / "hooks"
        active = [p.name for p in hooks.iterdir() if p.is_file() and not p.name.endswith(".sample")] if hooks.is_dir() else []
        report.add("no-active-hooks", passed=not active, detail=f"active hooks: {active}" if active else "none")

    @classmethod
    def _check_alternate_object_store(cls, report: Report, repo: Path) -> None:
        # An alternate object store reported by git is a borrowed database, advisory beyond the file check.
        _, out = cls.git(repo, "count-objects", "-v")
        stats = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
        if stats.get("alternate") is not None:
            detail = f"git reports an alternate object store: {stats['alternate']!r}"
            report.add("count-objects-alternate", passed=False, detail=detail)

    @classmethod
    def _check_tree_level(cls, report: Report, repo: Path) -> None:
        """Checks that apply whether or not a ``.git`` is present: nested/bare repos, escaping symlinks."""
        nested = sorted(str(p.relative_to(repo)) for p in repo.rglob(".git") if p != repo / ".git")
        report.add("no-nested-repositories", passed=not nested, detail=f"nested .git entries: {nested[:5]}" if nested else "none")
        bare = cls._bare_repositories(repo, repo / ".git")
        bare_detail = "none"
        if bare:
            bare_detail = f"gitdir-shaped directories: {bare[:5]} (each usable as --git-dir, so each holds committed history)"
        report.add("no-bare-repositories", passed=not bare, detail=bare_detail)
        escaping = cls._escaping_symlinks(repo)
        escaping_detail = f"escaping symlinks: {escaping[:5]}" if escaping else "none"
        report.add("no-symlinks-escaping-the-tree", passed=not escaping, detail=escaping_detail)

    @staticmethod
    def _check_canaries(report: Report, repo: Path, canaries: tuple[str, ...]) -> None:
        """Assert each canary token appears nowhere under ``repo`` — the ported control-token half."""
        for token in canaries:
            hits = Seal._files_containing(repo, token)
            report.add(f"canary-absent:{token}", passed=not hits, detail=f"canary found in {hits}" if hits else "not found")

    # ───────────────────────────── content scanner (new) ─────────────────────────────

    @classmethod
    def scan_agent_visible(cls, task_dir: Path, tokens: list[str], control_token: str) -> Report:
        """Grep agent-visible files for the PR number, review text, and any answer fragment (FR-35/40).

        The scanned corpus is every file under ``task_dir`` *except* the verifier-only subtrees
        (``solution/``, ``tests/``) and config files (``task.toml``/``judge.toml``/``dataset.toml``),
        which the agent never sees and which legitimately carry the answer. Answer material is the
        explicit ``tokens`` (the PR number and review text the driver supplies) plus fragments mined
        from the verifier-only answer sources (``solution/``, ``tests/oracle.json``, ``judge.toml``),
        so the reference fix leaking into an agent-visible file is caught (FR-40).

        The ``control_token`` is the honesty guard (FR-36/NFR-6): the driver (``verify``, §7.2) plants
        it into the corpus first; if this scan does not *find* it, the corpus was not really read, so
        it refuses ``scan-did-not-run`` rather than reporting clean. Kept as check-and-refuse (not
        self-planting) so the guard can be watched to fail — E-3 removes the token and confirms it
        goes red. Reads files only; issues no subprocess and no network call (NFR-2).
        """
        report = Report(repo=str(task_dir))
        corpus = cls._agent_visible_files(task_dir)
        if not cls._corpus_contains(corpus, task_dir, control_token):
            detail = f"control token {control_token!r} absent from scanned corpus; an empty result is not trusted clean (NFR-6)"
            report.add("scan-did-not-run", passed=False, detail=detail)
            return report
        report.add("content-scan-ran", passed=True, detail=f"control token seen across {len(corpus)} agent-visible file(s)")
        answer_tokens = cls._answer_tokens(task_dir, tokens)
        for token in tokens:
            cls._report_token(report, task_dir, corpus, token)
        for fragment in answer_tokens - set(tokens):
            cls._report_leaked_fragment(report, task_dir, corpus, fragment)
        return report

    @classmethod
    def _report_token(cls, report: Report, task_dir: Path, corpus: list[Path], token: str) -> None:
        """Add a pass/fail finding for one explicit answer token (PR number / review text)."""
        location = cls._first_location(corpus, task_dir, token)
        report.add(
            f"answer-absent:{token}",
            passed=location is None,
            detail=f"{token!r} at {location}" if location else f"{token!r} absent from agent-visible content",
        )

    @classmethod
    def _report_leaked_fragment(cls, report: Report, task_dir: Path, corpus: list[Path], fragment: str) -> None:
        """Add a finding only when a verifier-only answer fragment is *present* in the agent corpus.

        Unlike the explicit tokens, the mined fragments can number in the hundreds (every long line of
        a solution), so recording a passing finding for each would drown the report — only a hit (a
        real FR-40 leak) is worth a finding.
        """
        location = cls._first_location(corpus, task_dir, fragment)
        if location is not None:
            report.add("answer-source-leaked", passed=False, detail=f"verifier-only text {fragment!r} at {location}")

    @classmethod
    def _answer_tokens(cls, task_dir: Path, tokens: list[str]) -> set[str]:
        """The explicit ``tokens`` plus answer fragments mined from the verifier-only sources (FR-40)."""
        mined = set(tokens)
        for source in cls._answer_source_files(task_dir):
            mined |= cls._long_lines(source.read_text(encoding="utf-8", errors="replace"))
        return mined

    @classmethod
    def _answer_source_files(cls, task_dir: Path) -> list[Path]:
        """The existing verifier-only files whose content is the reference answer (FR-40)."""
        found: list[Path] = []
        for prefix in _ANSWER_SOURCE_PREFIXES:
            found.extend(p for p in (task_dir / prefix).rglob("*") if p.is_file())
        found.extend(task_dir / rel for rel in _ANSWER_SOURCE_FILES if (task_dir / rel).is_file())
        return found

    @staticmethod
    def _long_lines(text: str) -> set[str]:
        """The stripped lines long enough to be a real answer fragment (see ``_MIN_ANSWER_FRAGMENT_LEN``)."""
        return {stripped for line in text.splitlines() if len(stripped := line.strip()) >= _MIN_ANSWER_FRAGMENT_LEN}

    @classmethod
    def _agent_visible_files(cls, task_dir: Path) -> list[Path]:
        """Every file under ``task_dir`` the evaluated agent can read — the scanned corpus."""
        files = (p for p in task_dir.rglob("*") if p.is_file())
        return sorted(p for p in files if not cls._is_verifier_only(p.relative_to(task_dir).as_posix()))

    @staticmethod
    def _is_verifier_only(rel: str) -> bool:
        """True when a relative path is verifier-only, so absent from what the agent sees."""
        return rel.startswith(_VERIFIER_ONLY_PREFIXES) or rel in _VERIFIER_ONLY_FILENAMES

    @classmethod
    def _corpus_contains(cls, corpus: list[Path], task_dir: Path, token: str) -> bool:
        """True when ``token`` appears in any file of the corpus (the control-token presence test)."""
        return bool(token) and cls._first_location(corpus, task_dir, token) is not None

    @staticmethod
    def _first_location(corpus: list[Path], task_dir: Path, token: str) -> str | None:
        """The ``<relpath>:<line>`` of the first occurrence of ``token`` in the corpus, or ``None``."""
        for path in corpus:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line_number, line in enumerate(lines, start=1):
                if token in line:
                    return f"{path.relative_to(task_dir).as_posix()}:{line_number}"
        return None

    # ─────────────────────────────────── shared helpers ───────────────────────────────────

    @staticmethod
    def git(repo: Path, *args: str) -> tuple[int, str]:
        """Run a read-only git command in ``repo`` with prompts disabled; return ``(returncode, stdout)``.

        ``argv`` is built here as a list and never passed through a shell, and the environment carries
        no ``PATH`` and a nowhere ``HOME`` — untrusted repo content cannot reach a shell (§3), and no
        remote is configured, so nothing here touches the network (NFR-2).
        """
        # argv[0] is the literal program name; the resolved binary runs via ``executable=`` (_GIT,
        # defpath-hardened above). shell=False, _GIT_ENV carries no PATH and a HOME that resolves
        # nowhere, and every dynamic value is its own argv element — a ref name from the repository is
        # never interpreted as a command. A literal argv[0] also keeps the analyzers reading a constant.
        completed = subprocess.run(  # nosec B607 - literal argv[0], real binary via executable=; B603 skipped in pyproject
            ["git", "-C", str(repo), "--no-pager", *args],
            executable=_GIT,
            capture_output=True,
            text=True,
            env=_GIT_ENV,
            check=False,
        )
        return completed.returncode, completed.stdout.strip()

    @staticmethod
    def _files_containing(root: Path, token: str) -> list[str]:
        """Up to five paths under ``root`` whose bytes contain ``token`` (the canary search)."""
        needle = token.encode()
        hits: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            if needle in blob:
                hits.append(str(path.relative_to(root)))
                if len(hits) >= _MAX_REPORTED_HITS:
                    break
        return hits

    @staticmethod
    def _is_branch_shaped_path(text: str) -> bool:
        """True for ``refs/heads/<name>`` and its reflog, where ``<name>`` is slash-separated segments.

        Written as a prefix strip plus a split rather than as ``[^/]+(/[^/]+)*``: the regex spells the
        rule — no empty segment, so no leading, doubled, or trailing slash — as a nested quantifier,
        which is both the shape a ReDoS scanner flags and the harder of the two to read. The split
        makes one pass over the string and states the rule outright.
        """
        for prefix in _BRANCH_PATH_PREFIXES:
            if text.startswith(prefix):
                return all(segment for segment in text.removeprefix(prefix).split("/"))
        return False

    @classmethod
    def _is_expected_gitdir_entry(cls, rel: Path) -> bool:
        """True when a path inside ``.git`` is one a clean single-commit repo may hold."""
        text = rel.as_posix()
        if text in EXPECTED_GITDIR_FILES:
            return True
        if cls._is_branch_shaped_path(text):
            return True
        return bool(LOOSE_OBJECT.match(text) or PACK_ENTRY.match(text) or SAMPLE_HOOK.match(text))

    @staticmethod
    def _looks_like_object_id(text: str) -> list[str]:
        """Hex tokens long enough to be abbreviated object ids (git's 7-char default), never plain words.

        An object id is enough to fetch the object wherever a path to the source exists, so a sealed
        commit message must name none. Plainly-English short hex words are excluded by the digit/alpha
        filter.
        """
        return [token for token in OBJECT_ID_TOKEN.findall(text) if not token.isdigit() and not token.isalpha()]

    @staticmethod
    def _looks_like_a_gitdir(path: Path) -> bool:
        """True when a directory is a repository metadata directory, named ``*.git`` or bare by shape.

        A bare repository has no ``.git`` entry — the directory itself is the gitdir — so a literal
        ``.git`` search cannot see ``vendor/answerkey.git/``, which can hold the fix's commits in full.
        Detected by shape (``objects/`` plus a ``HEAD`` file, what makes a directory usable as
        ``--git-dir``) as well as by the ``*.git`` name.
        """
        if not path.is_dir():
            return False
        if path.name.endswith(".git"):
            return True
        return (path / "objects").is_dir() and (path / "HEAD").is_file()

    @classmethod
    def _bare_repositories(cls, repo: Path, own: Path) -> list[str]:
        """Gitdir-shaped directories in the tree, excluding the tree's own repository and its internals.

        The companion to the literal ``.git`` search: a bare repo announces itself only by shape.
        Entries below an already-reported entry are dropped as noise. Ported from ``seal_assert.py:203``.
        """
        found: set[str] = set()
        for path in repo.rglob("*"):
            if path in (own, repo) or own in path.parents:
                continue
            if cls._looks_like_a_gitdir(path):
                found.add(path.relative_to(repo).as_posix())
        return sorted(entry for entry in found if not cls._is_below_another(entry, found))

    @staticmethod
    def _is_below_another(entry: str, found: Iterable[str]) -> bool:
        """True when ``entry`` sits inside another reported gitdir (so it is noise, not a new finding)."""
        return any(entry != other and entry.startswith(other + "/") for other in found)

    @staticmethod
    def _escaping_symlinks(repo: Path) -> list[str]:
        """Symlinks under ``repo`` whose resolved target escapes the tree — they read whatever they point at."""
        escaping: list[str] = []
        for path in repo.rglob("*"):
            if not path.is_symlink():
                continue
            try:
                path.resolve().relative_to(repo)
            except (ValueError, OSError):
                escaping.append(f"{path.relative_to(repo)} -> {path.readlink()}")
        return escaping
