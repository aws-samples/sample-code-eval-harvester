"""Every git the product and its fixtures run must start under Windows' resolution rules (NFR-4).

On Windows ``os.defpath`` is ``.;C:\\bin``, which never holds git, and ``CreateProcess`` runs an
``executable=`` exactly as given: a bare ``"git"`` is never searched for, so it fails with
``FileNotFoundError: [WinError 2]``. A module that resolves git with ``shutil.which("git",
path=os.defpath) or "git"`` and hands git an ``env=`` without ``PATH`` therefore fails there on its
first call. These tests rebuild that condition on any OS: a child interpreter whose ``os.defpath``
names a directory without git imports the module, so its import-time resolution sees what Windows
sees. On POSIX a bare ``executable=`` is then searched on the child env's ``PATH``, falling back to
the same ``os.defpath`` when it has none, so the old resolution fails here with the same error.

Windows also translates every newline a text-mode write emits into CRLF, so a fixture file written
with a plain ``write_text`` commits different bytes, and different SHAs, than on POSIX. A child that
applies the same translation checks the builder writes the bytes it means on every OS.

The child also carries ``SYSTEMROOT`` and a parent ``GIT_DIR``: the first must reach git (Windows
cannot start a process from an ``env=`` without it), the second must not (the seal's verdict and the
fixtures' SHAs must not depend on the caller's environment).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404  # runs a child interpreter on argv lists only, shell=False
import sys
from pathlib import Path

from fixtures.repo_builder import RepoBuilder

from eval_harvest.gitcmd import GIT_EXECUTABLE, GitCommandRunner

#: The tests root, so a child interpreter can import the shared ``fixtures`` package.
_TESTS_ROOT = Path(__file__).resolve().parents[1]

#: Builds a sealed fixture repo with the builder's own git, under the emulated resolution.
_FIXTURE_CHILD = """
import json, os, sys
os.defpath = sys.argv[1]
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from fixtures.repo_builder import _GIT, _ISOLATED_ENV, RepoBuilder
repo = RepoBuilder.build_sealed_repo(Path(sys.argv[3]))
print(json.dumps({"git": _GIT, "head": RepoBuilder._head_sha(repo), "env": _ISOLATED_ENV}))
"""

#: Runs the seal's own git against an existing repo, under the emulated resolution.
_SEAL_CHILD = """
import json, os, sys
os.defpath = sys.argv[1]
from pathlib import Path
from eval_harvest.seal import _GIT, _GIT_ENV, Seal
code, head = Seal.git(Path(sys.argv[2]), "rev-parse", "HEAD")
print(json.dumps({"git": _GIT, "code": code, "head": head, "env": _GIT_ENV}))
"""


#: Builds the squash-merged PR fixture with text-mode writes translated to CRLF, as on Windows.
_CRLF_CHILD = """
import builtins, io, json, sys
real_open = io.open
def windows_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    if "b" not in mode and newline is None and any(flag in mode for flag in "wax+"):
        newline = "\\r\\n"
    return real_open(file, mode, buffering, encoding, errors, newline, closefd, opener)
io.open = builtins.open = windows_open
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from fixtures.repo_builder import RepoBuilder
fixture = RepoBuilder.build_squash_merged_pull_request(Path(sys.argv[3]))
print(json.dumps({"origin": str(fixture.origin), "tips": [fixture.base_sha, fixture.round1_tip, fixture.round2_tip]}))
"""


def _run_child(program: str, *args: str, tmp_path: Path) -> dict[str, object]:
    """Run ``program`` in a child interpreter whose ``os.defpath`` holds no git; return its JSON line."""
    no_git = tmp_path / "defpath-without-git"
    no_git.mkdir()
    env = {**os.environ, "SYSTEMROOT": str(tmp_path / "Windows"), "GIT_DIR": str(tmp_path / "elsewhere.git")}
    completed = subprocess.run(  # nosec B603,B607  # literal argv[0], real interpreter via executable=, no shell
        ["python", "-c", program, str(no_git), *args],
        executable=sys.executable,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, object] = json.loads(completed.stdout)
    return result


def test_fixture_git_runs_when_defpath_holds_no_git(tmp_path: Path) -> None:
    """The fixture builder resolves git on the real PATH and keeps what Windows needs to start it."""
    result = _run_child(_FIXTURE_CHILD, str(_TESTS_ROOT), str(tmp_path / "sealed"), tmp_path=tmp_path)
    assert os.path.isabs(str(result["git"])), "Windows runs executable= as given: it must be an absolute path"
    env = result["env"]
    assert isinstance(env, dict)
    assert env["SYSTEMROOT"] == str(tmp_path / "Windows")
    assert "GIT_DIR" not in env  # a caller's GIT_DIR cannot redirect the build
    head = result["head"]
    assert isinstance(head, str)
    assert len(head) == 40
    assert all(character in "0123456789abcdef" for character in head)


def test_seal_git_runs_when_defpath_holds_no_git(tmp_path: Path) -> None:
    """The seal resolves git on the real PATH, keeps SYSTEMROOT, and still hands git no PATH or GIT_DIR."""
    repo = RepoBuilder.build_sealed_repo(tmp_path / "sealed")
    result = _run_child(_SEAL_CHILD, str(repo), tmp_path=tmp_path)
    assert os.path.isabs(str(result["git"])), "Windows runs executable= as given: it must be an absolute path"
    assert result["code"] == 0
    assert result["head"] == RepoBuilder._head_sha(repo)  # the seal read this repo, not the parent's GIT_DIR
    env = result["env"]
    assert isinstance(env, dict)
    assert env["SYSTEMROOT"] == str(tmp_path / "Windows")
    assert "PATH" not in env
    assert "GIT_DIR" not in env


def test_fixture_commits_lf_bytes_under_windows_text_mode(tmp_path: Path) -> None:
    """Fixture files commit LF bytes, and so the same SHAs, when text-mode writes translate to CRLF."""
    built = _run_child(_CRLF_CHILD, str(_TESTS_ROOT), str(tmp_path / "windows"), tmp_path=tmp_path)
    tips = built["tips"]
    assert isinstance(tips, list)
    for tip in tips:
        blob = subprocess.run(  # nosec B607 - literal argv[0], real binary via executable=; B603 skipped in pyproject
            ["git", "-C", str(built["origin"]), "show", f"{tip}:code.py"],
            executable=GIT_EXECUTABLE,
            capture_output=True,
            env=GitCommandRunner.isolated_git_env(os.environ, inherit=("PATH",)),
            check=True,
        ).stdout
        assert b"\r" not in blob, f"{tip}:code.py was committed with CRLF"
    # Its commits are the very ones the same builder makes with plain LF writes, so SHA goldens hold.
    native = RepoBuilder.build_squash_merged_pull_request(tmp_path / "native")
    assert tips == [native.base_sha, native.round1_tip, native.round2_tip]
