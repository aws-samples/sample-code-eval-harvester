"""Tests for the cross-platform git/gh subprocess chokepoint (task A-2).

The env-shape and resolution tests are pure (data in / data out) so they run identically
on every platform and need neither a network nor gh installed. The two git tests drive a
real, throwaway repository — they are the only place a subprocess actually runs.
"""

from __future__ import annotations

from pathlib import Path

from eval_harvest.gitcmd import GitCommandRunner


def _make_repo_with_one_commit(repo: Path) -> None:
    """Init a repo and land one commit, passing identity per-call so no global config is needed."""
    assert GitCommandRunner.git(repo, "init")[0] == 0
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    assert GitCommandRunner.git(repo, "add", "-A")[0] == 0
    committed = GitCommandRunner.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init")
    assert committed[0] == 0, committed


class TestGitInvocation:
    """git() returns the code callers branch on and never routes an argument through a shell."""

    def test_git_returns_code_and_stdout(self, tmp_path: Path) -> None:
        _make_repo_with_one_commit(tmp_path)
        code, head_sha = GitCommandRunner.git(tmp_path, "rev-parse", "HEAD")
        assert code == 0
        assert len(head_sha) == 40
        assert all(character in "0123456789abcdef" for character in head_sha)

    def test_no_shell_string_accepted(self, tmp_path: Path) -> None:
        # A shell would run the trailing command and create sentinel.txt; shell=False must not.
        _make_repo_with_one_commit(tmp_path)
        sentinel = tmp_path / "sentinel.txt"
        code, _ = GitCommandRunner.git(tmp_path, "rev-parse", f"HEAD; touch {sentinel}")
        assert code != 0  # git rejects the injected token as an unknown revision
        assert not sentinel.exists()


class TestChildEnvironment:
    """The child env strips the ambient token from gh and disables prompts/locks for git."""

    def test_github_token_stripped_from_child(self) -> None:
        parent = {"GITHUB_TOKEN": "ghp_should_not_leak", "PATH": "/usr/bin", "HOME": "/home/x"}
        child = GitCommandRunner.github_child_env(parent)
        assert "GITHUB_TOKEN" not in child
        assert child["PATH"] == "/usr/bin"  # everything else is preserved

    def test_prompts_disabled(self) -> None:
        parent = {"HOME": "/home/x", "PATH": "/usr/bin"}
        child = GitCommandRunner.git_child_env(parent)
        assert child["GIT_TERMINAL_PROMPT"] == "0"
        assert child["GIT_OPTIONAL_LOCKS"] == "0"
        assert child["HOME"] == "/home/x"  # a valid home survives (Windows needs one)


class TestExecutableResolution:
    """git resolution prefers os.defpath but falls back to the real PATH (Windows: os.defpath misses)."""

    def test_git_resolution_has_windows_fallback(self) -> None:
        def fake_which(command: str, *, path: str | None = None) -> str | None:
            # Emulate Windows: os.defpath (".;") finds nothing; the real PATH resolves git.exe.
            if path is not None:
                return None
            return r"C:\Program Files\Git\cmd\git.exe"

        assert GitCommandRunner.resolve_git(fake_which) == r"C:\Program Files\Git\cmd\git.exe"

    def test_git_resolution_prefers_defpath(self) -> None:
        def fake_which(command: str, *, path: str | None = None) -> str | None:
            return "/usr/bin/git" if path is not None else "/some/venv/shim/git"

        # The os.defpath hit wins, so a venv shim on the real PATH cannot shadow the system git.
        assert GitCommandRunner.resolve_git(fake_which) == "/usr/bin/git"
