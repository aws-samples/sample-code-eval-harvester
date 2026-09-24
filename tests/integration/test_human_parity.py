"""FR-4: a shell invocation produces identical bytes to an agent invocation (task F-2).

The product thesis is one code path: the CLI never asks "am I being run by an agent?" and never
emits different bytes for one. If any verb branched on an agent-harness signal or on the terminal,
a human running it by hand would see something different from what the driving agent sees, and the
methodology the agent learned would not be the methodology the human can reproduce (tech plan §5.1,
scenario S-17). These tests hold the arguments fixed and vary only the ambient environment.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404  # launching our own CLI in a child process is the E2E surface (FR-4)
import sys

import pytest

from eval_harvest.cli import Cli

#: Environment a coding-agent harness might inject that a plain shell would not. If any verb reads
#: one of these, the parity assertions below go red — which is the point.
AGENT_ENV: dict[str, str] = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "COLUMNS": "72",
}

#: Runs the installed CLI in a child process with real argv, the way a user or an agent would.
_CHILD_PROGRAM = "import sys; from eval_harvest.cli import Cli; sys.exit(Cli.run(sys.argv[1:]))"

VERBS_WITH_TOP: tuple[str | None, ...] = (None, "init", "survey", "capture", "annotate", "emit", "verify", "dataset")


def _help_argv(verb: str | None) -> list[str]:
    return ["--help"] if verb is None else [verb, "--help"]


class TestHumanParityInProcess:
    """Rendered help is a pure function of its arguments — independent of the ambient environment."""

    def test_help_identical_across_environments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for verb in VERBS_WITH_TOP:
            plain = Cli.help_for(verb)
            for name, value in AGENT_ENV.items():
                monkeypatch.setenv(name, value)
            under_agent_env = Cli.help_for(verb)
            for name in AGENT_ENV:
                monkeypatch.delenv(name, raising=False)
            assert plain == under_agent_env, f"{verb or 'top-level'} help differs when agent env vars are present"


class TestHumanParitySubprocess:
    """FR-4 end-to-end: a real child process, real argv — shell run == agent run, byte for byte."""

    def _run(self, argv: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env["COLUMNS"] = "80"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(  # nosec B603,B607  # literal argv[0], real interpreter via executable=, no shell
            ["python", "-c", _CHILD_PROGRAM, *argv],
            executable=sys.executable,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_human_parity_identical_bytes(self) -> None:
        for verb in VERBS_WITH_TOP:
            argv = _help_argv(verb)
            shell_run = self._run(argv)
            agent_run = self._run(argv, extra_env=AGENT_ENV)
            assert shell_run.returncode == 0, f"{verb or 'top-level'} --help exited {shell_run.returncode}"
            assert shell_run.returncode == agent_run.returncode
            assert shell_run.stdout == agent_run.stdout, f"{verb or 'top-level'} --help stdout differs by environment"
            assert shell_run.stderr == agent_run.stderr, f"{verb or 'top-level'} --help stderr differs by environment"
