"""The oracle/nop gradeability guard: dispatch one emitted task twice and prove it grades.

A whole class of defects can make every emitted datapoint ungradeable while a fully green
``mise run check`` sees nothing wrong: the emitter suite tests the emitter against its own port of
Harbor's rules, and that port never runs a task. Such a defect surfaces only by *executing* one
datapoint on real AWS MicroVMs. This module is the standing guard so one cannot hide: it runs the two
model-free agents Harbor ships and asserts the one thing the suite cannot see.

* ``--agent oracle`` copies ``solution/`` in and runs ``solve.sh``, submitting the reference
  findings. It must score **non-zero coverage** and its agent must exit **0**. If it does not, the
  datapoint is broken — a patch that will not apply, a solution that cannot run, a ``score.py`` that
  miscredits.
* ``--agent nop`` does nothing. It must **credit nothing**. If it scores above zero, the eval is
  gameable or the answer is leaking.

Neither needs a model or API spend — only AWS MicroVM time (~5-10 minutes, one image build plus two
short trials). This is **not** part of ``mise run check``: it needs AWS credentials, applied
Terraform, and minutes of wall time. It is an explicit, on-demand acceptance gate — run it before any
change to ``emit.py``, ``verifier_tpl/``, or ``candidate.py``'s patch writing.

The seam is deliberate (CODING_STANDARDS §8): :class:`SmokeContract` is the pure reward logic the
unit tests drive with synthetic reward objects, no AWS; :class:`SmokeDatapoint` is the thin
subprocess/filesystem edge that dispatches Harbor and reads what it wrote.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # argv lists with shell=False only; see SmokeDatapoint._dispatch. # nosec B404
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from eval.runner import (
    LAMBDA_MICROVMS_IMPORT_PATH,
    EnvironmentPreflightError,
    preflight_environment_imports,
)

#: A Harbor reward object: the keys :data:`eval_harvest.verifier_tpl.score.REWARD_KEYS` names, each a
#: float. Coverage/precision are ratios in [0, 1]; the ``credited_*``/``uncredited``/``oracle_*`` keys
#: are per-trial tallies.
RewardObject = dict[str, float]

#: Reference-set sizes, not agent scores: ``score.py`` sets ``oracle_<sev>`` to the *count of oracle
#: findings* of that severity, so they are non-zero for **any** agent, ``nop`` included — a ``nop``
#: reward still carries e.g. ``oracle_high: 2.0``. Reading those as the ``nop`` scoring above zero is
#: a mistake, so they are excluded from the nop-must-be-zero contract and instead asserted *present*
#: (a task with an empty oracle cannot distinguish anything).
_REFERENCE_SIZE_KEYS: frozenset[str] = frozenset({"oracle_high", "oracle_medium", "oracle_low"})

#: ``coverage_required = credited_required / required``. ``nop`` credits nothing, so this is ``0.0``
#: when the task has a required (high-severity) finding — but ``score.py``'s coverage helper returns
#: ``1.0`` over an *empty* required set (an agent cannot miss a finding that does not exist). So it is
#: held to zero only when ``oracle_high > 0``; on a task with no high findings, ``1.0`` is correct.
_CONDITIONAL_COVERAGE_KEY = "coverage_required"

#: The infrastructure the dispatch needs, by name. ``scripts/smoke-datapoint.sh`` loads these from
#: ``terraform output`` and exports them; when one is absent the guard exits non-zero naming it — it
#: does **not** skip (``../tech-plan.md:138``: a runtime check that "did not run" must never read as
#: "passed").
_REQUIRED_ENV_VARS: tuple[str, ...] = (
    "MICROVM_BUCKET",
    "MICROVM_BUILD_ROLE_ARN",
    "MICROVM_EXECUTION_ROLE_ARN",
)

#: Exit codes, distinct so "did not run" is never read as "passed" or as "the contract failed".
_EXIT_OK = 0
_EXIT_CONTRACT_FAILED = 1
_EXIT_PREREQUISITE_MISSING = 2

#: How many lines of each diagnosis source to print on failure. A gradeability defect is diagnosed
#: from ``agent/oracle.txt`` (the oracle traceback) and the build-log tail, so both are printed.
_DIAGNOSIS_LINES = 40


class SmokeContract:
    """The pure gradeability contract: reward objects in, human-readable failures out. Holds no state.

    Every method returns a list of failure messages (empty means the assertion passed), so the four
    checks compose into one report and the unit tests can drive each with synthetic rewards — no AWS.
    """

    @classmethod
    def evaluate(
        cls,
        oracle_reward: Mapping[str, float],
        nop_reward: Mapping[str, float],
        oracle_exit_code: int | None,
        oracle_trial_errored: bool,
    ) -> list[str]:
        """Every failing assertion for one oracle/nop pair; an empty list means the datapoint grades.

        Composes the four checks a gradeable datapoint must pass: the oracle credited a known-good
        submission and its agent did not crash, the oracle carries a reference set at all, nop
        credited nothing, and the two rewards are not identical.
        """
        return [
            *cls.reference_set_present_failures(oracle_reward),
            *cls.oracle_failures(oracle_reward, oracle_exit_code, oracle_trial_errored),
            *cls.nop_failures(nop_reward),
            *cls.rewards_differ_failures(oracle_reward, nop_reward),
        ]

    @staticmethod
    def reference_set_present_failures(oracle_reward: Mapping[str, float]) -> list[str]:
        """Fail when the oracle has no findings — a task with an empty oracle cannot grade anything."""
        reference_total = sum(oracle_reward.get(key, 0.0) for key in _REFERENCE_SIZE_KEYS)
        if reference_total > 0:
            return []
        message = (
            f"oracle reference set is empty (oracle_high/medium/low sum to {reference_total}) — the "
            "task has nothing to grade against, so oracle and nop cannot differ"
        )
        return [message]

    @staticmethod
    def oracle_failures(
        oracle_reward: Mapping[str, float], oracle_exit_code: int | None, oracle_trial_errored: bool
    ) -> list[str]:
        """Fail when the oracle's agent crashed, or its known-good submission scored no coverage.

        The agent's health is asserted, not only the reward (What To Build §2): an agent can exit
        non-zero while Harbor still reports a scored trial, so "the agent crashed but the number
        looked fine" — the exact shape this guard exists to reject — would otherwise pass. Two independent
        crash signals are honoured, because different environments record different ones: a recorded
        non-zero ``agent/exit-code.txt`` (when the environment writes it), and ``result.json``'s
        ``exception_info`` (which the Lambda MicroVMs environment records instead — it writes no
        ``exit-code.txt``). A *missing* exit code is not itself a failure: it means this environment did
        not record one, and a crash would still surface through ``exception_info`` and, failing that,
        through zero coverage below.
        """
        failures: list[str] = []
        if oracle_trial_errored:
            failures.append(
                "oracle trial raised an exception (result.json exception_info is set) — the agent did not run cleanly"
            )
        if oracle_exit_code is not None and oracle_exit_code != 0:
            failures.append(
                f"oracle agent exited {oracle_exit_code}, expected 0 — the agent crashed, so any non-zero "
                "reward it scored is accidental"
            )
        coverage_all = oracle_reward.get("coverage_all")
        if not isinstance(coverage_all, (int, float)) or coverage_all <= 0:
            failures.append(
                f"oracle coverage_all={coverage_all!r}, expected > 0 — the reference submission was not "
                "credited, so the datapoint does not grade a known-good answer"
            )
        return failures

    @classmethod
    def nop_failures(cls, nop_reward: Mapping[str, float]) -> list[str]:
        """Fail when an agent that did nothing was credited anything — the eval would be gameable.

        Every reward key is asserted zero *except* the reference-set sizes (:data:`_REFERENCE_SIZE_KEYS`,
        which are task constants) and ``coverage_required`` on a task with no required findings (where
        ``1.0`` is correct). A coverage-only assertion would pass a nop that scored
        ``coverage_all = 0.0`` while reporting ``precision_strict = 1.0`` — so the whole object is
        checked.
        """
        return [failure for key, value in sorted(nop_reward.items()) for failure in cls._nop_key_failures(nop_reward, key, value)]

    @staticmethod
    def _nop_key_failures(nop_reward: Mapping[str, float], key: str, value: float) -> list[str]:
        """The failure, if any, for one nop reward key — the loop body of :meth:`nop_failures`."""
        if key in _REFERENCE_SIZE_KEYS:
            return []
        if key == _CONDITIONAL_COVERAGE_KEY and nop_reward.get("oracle_high", 0.0) <= 0:
            return []  # no required findings on this task, so coverage_required = 1.0 is correct, not a leak
        if value == 0:
            return []
        return [f"nop {key}={value}, expected 0 — an agent that did nothing was credited, so the eval is gameable"]

    @staticmethod
    def rewards_differ_failures(oracle_reward: Mapping[str, float], nop_reward: Mapping[str, float]) -> list[str]:
        """Fail when oracle and nop produced identical rewards — the datapoint cannot distinguish them.

        The cheapest strong assertion available: it catches the whole failure class in one comparison,
        without knowing which key should be what.
        """
        if dict(oracle_reward) != dict(nop_reward):
            return []
        return ["oracle and nop produced identical rewards — the eval cannot tell a perfect reviewer from silence"]


class SmokeDatapoint:
    """The runnable guard: dispatch oracle + nop against one task, read the rewards, assert, diagnose.

    The subprocess/filesystem edge (CODING_STANDARDS §8); :class:`SmokeContract` holds the logic. Holds
    no state.
    """

    @staticmethod
    def missing_prerequisite(environment: Mapping[str, str], harbor_executable: str | None) -> str | None:
        """The first missing prerequisite named, or ``None`` when the guard can dispatch.

        A missing prerequisite is a non-zero exit with a named reason, never a skip: an on-demand
        acceptance gate that silently does nothing is the ``../tech-plan.md:138`` failure mode this
        guard exists to close.
        """
        for variable in _REQUIRED_ENV_VARS:
            if not environment.get(variable):
                return (
                    f"{variable} is not set — the Lambda MicroVMs infrastructure is not wired in. Run via "
                    "`mise run smoke-datapoint` (it loads Terraform outputs), or `mise run infra-apply` to "
                    "provision it first."
                )
        if harbor_executable is None:
            return "the 'harbor' runner is not on PATH — sync the eval group with `uv sync --group eval`."
        return None

    @classmethod
    def main(cls, argv: Sequence[str]) -> int:
        """Dispatch both agents against ``argv[0]`` (a task dir), assert the contract, and return the exit code."""
        if len(argv) != 1:
            print("usage: python -m eval.smoke <emitted-task-dir>", file=sys.stderr)
            return _EXIT_PREREQUISITE_MISSING
        task_dir = Path(argv[0])
        if not (task_dir / "task.toml").is_file():
            print(f"error: {task_dir} is not an emitted task directory (no task.toml)", file=sys.stderr)
            return _EXIT_PREREQUISITE_MISSING

        missing = cls.missing_prerequisite(os.environ, shutil.which("harbor"))
        if missing is not None:
            print(f"smoke-datapoint: cannot run — {missing}", file=sys.stderr)
            return _EXIT_PREREQUISITE_MISSING
        try:
            preflight_environment_imports()
        except EnvironmentPreflightError as exc:
            print(f"smoke-datapoint: preflight failed — {exc}", file=sys.stderr)
            return _EXIT_PREREQUISITE_MISSING

        return cls._dispatch_and_assert(task_dir)

    @classmethod
    def _dispatch_and_assert(cls, task_dir: Path) -> int:
        """Run oracle and nop against one task, then assert the contract and diagnose on failure."""
        harbor = shutil.which("harbor") or "harbor"
        jobs_root = task_dir.resolve().parent / "_smoke-jobs"
        print(f"smoke-datapoint: dispatching oracle and nop against {task_dir} (no model, AWS MicroVM time only)")
        oracle = cls._dispatch(harbor, task_dir, "oracle", jobs_root / "oracle")
        nop = cls._dispatch(harbor, task_dir, "nop", jobs_root / "nop")

        oracle_reward = cls._read_reward(oracle.task_dir)
        nop_reward = cls._read_reward(nop.task_dir)
        missing_rewards = cls._missing_reward_failures(oracle_reward, nop_reward)
        if missing_rewards:
            cls._print_failures(missing_rewards)
            cls._print_diagnosis(oracle)
            return _EXIT_CONTRACT_FAILED

        assert oracle_reward is not None and nop_reward is not None  # noqa: S101 — narrowed by the guard above  # nosec B101
        failures = SmokeContract.evaluate(
            oracle_reward,
            nop_reward,
            cls._read_exit_code(oracle.task_dir),
            cls._read_trial_errored(oracle.task_dir),
        )
        if failures:
            cls._print_failures(failures)
            cls._print_diagnosis(oracle)
            return _EXIT_CONTRACT_FAILED
        cls._print_pass(oracle_reward, nop_reward)
        return _EXIT_OK

    class _AgentRun:
        """Where one agent's dispatch landed: the located ``task__*`` dir and the captured harbor log."""

        def __init__(self, task_dir: Path | None, harbor_log: str) -> None:
            self.task_dir = task_dir
            self.harbor_log = harbor_log

    @classmethod
    def _dispatch(cls, harbor: str, task_dir: Path, agent: str, jobs_dir: Path) -> _AgentRun:
        """Invoke ``harbor run`` for one model-free agent and locate the ``task__*`` dir it produced."""
        jobs_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            "harbor",
            "run",
            "-p",
            str(task_dir.resolve()),
            "-a",
            agent,
            "-e",
            LAMBDA_MICROVMS_IMPORT_PATH,
            "-o",
            str(jobs_dir.resolve()),
            "-y",
        ]
        # argv[0] is the literal "harbor"; the resolved binary runs via ``executable=`` (``harbor`` is the
        # shutil.which() path chosen above). Every other element is a value this guard chose itself — the
        # agent name, the task dir — and shell=False, so there is no string-command API to inject into. The
        # literal argv[0] is also what keeps Slingshot's Semgrep quiet (inline # nosemgrep is disabled there):
        # `dangerous-subprocess-use-audit` fires on a command that is not a static string, so passing the
        # which()-resolved path as argv[0] would match while presenting the same constant command here does not.
        completed = subprocess.run(  # nosec B607 - literal argv[0], real binary via executable=; B603 skipped in pyproject
            ["harbor", *argv[1:]], executable=harbor, capture_output=True, text=True, check=False
        )
        harbor_log = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        return cls._AgentRun(cls._locate_task_dir(jobs_dir), harbor_log)

    @staticmethod
    def _locate_task_dir(jobs_dir: Path) -> Path | None:
        """The newest per-task directory Harbor wrote under ``jobs_dir``, or ``None`` if none.

        Harbor lays a job out as ``<jobs_dir>/<timestamp>/<task-slug>__<hash>/`` with ``agent/`` and
        ``verifier/`` under it (e.g. ``.../<timestamp>/aws-samples__…__qiM4MCZ/``). A glob for
        ``task__*`` would never match — the prefix is the task's slug, not the literal ``task`` — and
        would mis-diagnose a scored trial as "no reward.json / build failed". The directory is found by
        its shape (a two-level child that carries ``agent/`` or ``verifier/``), not by a name guess.
        """
        task_dirs = [
            path for path in jobs_dir.glob("*/*") if path.is_dir() and ((path / "verifier").is_dir() or (path / "agent").is_dir())
        ]
        return max(task_dirs, key=lambda path: path.stat().st_mtime) if task_dirs else None

    @staticmethod
    def _read_trial_errored(task_dir: Path | None) -> bool:
        """True when ``result.json`` records an exception for the trial — the Lambda env's crash signal.

        This environment writes no ``agent/exit-code.txt``; ``result.json``'s ``exception_info`` is the
        signal that the trial raised. Absent or ``null`` means it ran without raising (not that it
        graded correctly — that is coverage's job).
        """
        if task_dir is None:
            return False
        result_path = task_dir / "result.json"
        if not result_path.is_file():
            return False
        parsed: object = json.loads(result_path.read_text(encoding="utf-8"))
        return isinstance(parsed, dict) and parsed.get("exception_info") is not None

    @staticmethod
    def _read_reward(task_dir: Path | None) -> RewardObject | None:
        """The parsed ``verifier/reward.json`` under ``task_dir``, or ``None`` when it is absent/unreadable.

        Absent is the normal shape of a build failure: the image never builds, so no reward is
        ever written. That is a guard failure, diagnosed from the harbor log, not a crash here.
        """
        if task_dir is None:
            return None
        reward_path = task_dir / "verifier" / "reward.json"
        if not reward_path.is_file():
            return None
        parsed: object = json.loads(reward_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            return None
        return {str(key): float(value) for key, value in parsed.items() if isinstance(value, (int, float))}

    @staticmethod
    def _read_exit_code(task_dir: Path | None) -> int | None:
        """The integer in ``agent/exit-code.txt`` under ``task_dir``, or ``None`` when it is absent."""
        if task_dir is None:
            return None
        exit_code_path = task_dir / "agent" / "exit-code.txt"
        if not exit_code_path.is_file():
            return None
        text = exit_code_path.read_text(encoding="utf-8").strip()
        return int(text) if text.lstrip("-").isdigit() else None

    @staticmethod
    def _missing_reward_failures(oracle_reward: RewardObject | None, nop_reward: RewardObject | None) -> list[str]:
        """A failure for each agent whose reward is missing — the build-failure shape (no image, no reward)."""
        failures: list[str] = []
        if oracle_reward is None:
            failures.append("oracle produced no reward.json — the verifier image likely failed to build (see diagnosis below)")
        if nop_reward is None:
            failures.append("nop produced no reward.json — the verifier image likely failed to build (see diagnosis below)")
        return failures

    @staticmethod
    def _print_failures(failures: Sequence[str]) -> None:
        """Print every failed assertion, one per line, under a clear FAILED banner."""
        print("smoke-datapoint: FAILED — the datapoint does not grade end to end:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)

    @classmethod
    def _print_diagnosis(cls, oracle: _AgentRun) -> None:
        """Print the oracle traceback and the build-log tail — where a gradeability defect is diagnosed.

        A guard that says only "expected > 0, got 0" sends the next person back through the same
        multi-hour investigation (What To Build §3).
        """
        print("\nsmoke-datapoint: diagnosis --------------------------------------------------", file=sys.stderr)
        cls._print_oracle_txt(oracle.task_dir)
        cls._print_log_tail("harbor run output (build log is streamed here; fuller detail in CloudWatch)", oracle.harbor_log)

    @classmethod
    def _print_oracle_txt(cls, task_dir: Path | None) -> None:
        """Print the head of ``agent/oracle.txt`` — the oracle's own stdout, e.g. its traceback."""
        oracle_txt = None if task_dir is None else task_dir / "agent" / "oracle.txt"
        if oracle_txt is None or not oracle_txt.is_file():
            print("  (no agent/oracle.txt — the oracle agent never ran; the image build failed before it)", file=sys.stderr)
            return
        cls._print_log_head("agent/oracle.txt (oracle stdout)", oracle_txt.read_text(encoding="utf-8"))

    @staticmethod
    def _print_log_head(label: str, text: str) -> None:
        """Print the first :data:`_DIAGNOSIS_LINES` lines of ``text`` under ``label``."""
        lines = text.splitlines()
        print(f"  --- {label} (first {_DIAGNOSIS_LINES} lines) ---", file=sys.stderr)
        for line in lines[:_DIAGNOSIS_LINES]:
            print(f"  {line}", file=sys.stderr)

    @staticmethod
    def _print_log_tail(label: str, text: str) -> None:
        """Print the last :data:`_DIAGNOSIS_LINES` lines of ``text`` under ``label``."""
        lines = text.splitlines()
        print(f"  --- {label} (last {_DIAGNOSIS_LINES} lines) ---", file=sys.stderr)
        for line in lines[-_DIAGNOSIS_LINES:]:
            print(f"  {line}", file=sys.stderr)

    @staticmethod
    def _print_pass(oracle_reward: RewardObject, nop_reward: RewardObject) -> None:
        """Print the PASS banner with the values that mattered, so drift stays visible in the log."""
        print("smoke-datapoint: PASSED — the datapoint grades end to end.")
        print(f"  oracle coverage_all = {oracle_reward.get('coverage_all')} (credited the reference submission)")
        print("  oracle agent exit    = 0")
        print(
            f"  nop credited nothing (credited_all = {nop_reward.get('credited_all')}, "
            f"precision_strict = {nop_reward.get('precision_strict')})"
        )
        print("  oracle and nop rewards differ.")


def main() -> int:
    """Console entry point for ``python -m eval.smoke <task-dir>`` / ``mise run smoke-datapoint``."""
    return SmokeDatapoint.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
