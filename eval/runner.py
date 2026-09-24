"""Run the eval's objectives through Harbor and normalize each trial into a trajectory artifact.

The harness has three seams, so its wiring can be exercised offline (G-1 Tests To Write) while the
live half stays out of the gate:

* a :data:`TrialExecutor` — given an objective and a trial index, it runs one trial and returns a
  :class:`HarborRunResult` pointing at where that trial's raw output landed. The live default is
  :class:`HarborCliExecutor` (it shells out to ``harbor run`` against the Workstream H Lambda
  MicroVMs environment); a test injects a fake that writes a synthetic run directory, so no Bedrock
  and no MicroVM are touched.
* :meth:`EvalRunner.normalize_trial` — a reader that turns a raw run directory into a
  :class:`~eval.models.TrialTrajectory`. This *consumes* what Harbor records; it does not build a
  bespoke capture layer.
* persistence under ``eval/runs/<timestamp>/<objective>/trial-<k>/`` — the input G-2 and G-3 read.

**Do not reimplement VM provisioning or the runner** — that is the Workstream H environment's job
(G-1 Notes & Gotchas). :class:`HarborCliExecutor` only *invokes* Harbor.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # argv lists with shell=False only; see HarborCliExecutor. # nosec B404
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from eval.agent import AgentUnderTest
from eval.harbor_task import EvalHarborTask
from eval.models import (
    AgentTurn,
    Objective,
    RunManifest,
    ToolCall,
    TrialResult,
    TrialTrajectory,
)

#: The Workstream H environment's import path — how an unmodified Harbor loads it, no fork (H-3).
#: Points at the `harvest-env` distribution (harvest_env/), a real installed package: the `harbor.*`
#: namespace belongs to the installed Harbor and cannot be overlaid from outside it.
LAMBDA_MICROVMS_IMPORT_PATH = "harvest_env.lambda_microvms:LambdaMicrovmsEnvironment"

#: The modules the MicroVMs environment imports at construction time, each paired with a thunk that
#: imports it by a *literal* name. `mise run eval` syncs the `eval` group, which installs `harvest-env`
#: and its microvms/boto3/dockerfile-parse deps; the name in each pair is what the preflight reports
#: when the group was not synced. The environment module is first so a shadowing or rename regression
#: surfaces here, not three frames deep inside Harbor after an image upload.
#:
#: These are literal `import x` statements behind thunks rather than `importlib.import_module(name)`
#: over a list of strings, so no security scanner sees a dynamic import to flag (`non-literal-import`)
#: — the construct is gone, not suppressed. `find_spec` is not a substitute: it reports a
#: pruned-but-not-empty tree as present, silently defeating this guard. The dotted name is
#: duplicated on purpose — the string is the failure label, the `import` statement is the check. The
#: `# noqa: PLC0415` marks the imports as deliberately local (the eval-group deps are absent from the
#: base env, so they cannot live at module top); the `# noqa: F401` marks them as import-for-effect.


def _import_lambda_microvms_environment() -> None:
    import harvest_env.lambda_microvms  # noqa: F401, PLC0415  # == LAMBDA_MICROVMS_IMPORT_PATH's module


def _import_dockerfile_parse() -> None:
    import dockerfile_parse  # noqa: F401, PLC0415


def _import_microvms() -> None:
    import microvms  # noqa: F401, PLC0415


def _import_boto3() -> None:
    import boto3  # noqa: F401, PLC0415


_ENVIRONMENT_RUNTIME_IMPORTS: tuple[tuple[str, Callable[[], None]], ...] = (
    (LAMBDA_MICROVMS_IMPORT_PATH.split(":", 1)[0], _import_lambda_microvms_environment),
    ("dockerfile_parse", _import_dockerfile_parse),
    ("microvms", _import_microvms),
    ("boto3", _import_boto3),
)


class EnvironmentPreflightError(RuntimeError):
    """The MicroVMs environment's imports do not resolve — names the missing package."""


def preflight_environment_imports(
    modules: Sequence[tuple[str, Callable[[], None]]] = _ENVIRONMENT_RUNTIME_IMPORTS,
) -> None:
    """Import each module the environment needs, failing loudly and by name on the first missing one.

    Called before any AWS call so a mis-synced `eval` group surfaces as "dockerfile_parse missing:
    run uv sync --group eval" rather than a bare ``ModuleNotFoundError`` raised deep inside Harbor
    after the build context has already been uploaded — a free failure instead of one that costs an
    image build. `uv` treats an unknown extra as a warning rather than an error, so a mis-synced
    group is otherwise silent until it fails deep inside a run."""
    for module_name, import_the_module in modules:
        try:
            import_the_module()
        except ImportError as exc:
            raise EnvironmentPreflightError(
                f"{module_name!r} could not be imported ({exc}). The Lambda MicroVMs environment is not "
                "installed: sync the `eval` dependency group with `uv sync --group eval` (or run via "
                "`mise run eval`, which syncs it). That group provides `harvest-env` and its "
                "microvms / boto3 / dockerfile-parse dependencies."
            ) from exc


#: The file a trial's raw run directory carries with the normalized trace. ``HarborCliExecutor``
#: writes it from ``harbor run``'s output; the offline tests write it directly. Confirming the exact
#: field mapping against a real Harbor trace is part of the live run (see eval/README.md).
_TRACE_FILENAME = "trajectory.json"
#: The sub-directory of a raw run directory holding the task directory the agent produced, if any.
_PRODUCED_DIRNAME = "produced"
#: Where the produced task directory is copied under a trial's persisted artifacts.
_ARTIFACT_TASK_DIRNAME = "task"


class HarborRunResult(BaseModel):
    """What a :data:`TrialExecutor` returns: where one trial's raw output landed, and how it exited."""

    model_config = ConfigDict(frozen=True)

    run_dir: Path
    exit_code: int
    wall_time_sec: float


#: One trial's execution: objective + trial index in, a raw run directory out. The seam a test fakes.
TrialExecutor = Callable[[Objective, int], HarborRunResult]


class EvalRunner:
    """Orchestrates objectives × trials, normalizes each trial, and writes the run manifest. Stateless."""

    @classmethod
    def run_all(  # noqa: PLR0913 — each argument is a distinct knob of one run; an options object would only hide them
        cls,
        *,
        objectives: tuple[Objective, ...],
        agent: AgentUnderTest,
        model: str,
        output_root: Path,
        trials_per_objective: int,
        executor: TrialExecutor,
        timestamp: str,
    ) -> RunManifest:
        """Run every objective ``trials_per_objective`` times, capture each trial, and write ``run.json``.

        Returns the :class:`RunManifest` and persists it under ``output_root/<timestamp>/`` alongside
        the per-trial trajectory artifacts.
        """
        run_root = output_root / timestamp
        run_root.mkdir(parents=True, exist_ok=True)
        results = [
            cls._run_one_trial(objective, trial_index, agent, model, run_root, executor)
            for objective in objectives
            for trial_index in range(trials_per_objective)
        ]
        manifest = RunManifest(
            timestamp=timestamp,
            agent=agent.name,
            model=model,
            trials_per_objective=trials_per_objective,
            objective_ids=tuple(objective.id for objective in objectives),
            trials=tuple(results),
        )
        (run_root / "run.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        return manifest

    @classmethod
    def _run_one_trial(  # noqa: PLR0913, PLR0917 — one trial's distinct parts; an options object would only hide the contract
        cls,
        objective: Objective,
        trial_index: int,
        agent: AgentUnderTest,
        model: str,
        run_root: Path,
        executor: TrialExecutor,
    ) -> TrialResult:
        """Execute one trial, normalize + persist its trajectory, and return its manifest row."""
        raw = executor(objective, trial_index)
        artifact_dir = run_root / objective.id / f"trial-{trial_index}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        trajectory = cls.normalize_trial(objective, trial_index, agent.name, model, raw, artifact_dir)
        trajectory_path = artifact_dir / _TRACE_FILENAME
        trajectory_path.write_text(trajectory.model_dump_json(indent=2), encoding="utf-8")
        return TrialResult(
            objective_id=objective.id,
            trial_index=trial_index,
            trajectory_path=str(trajectory_path),
            exit_code=raw.exit_code,
        )

    @classmethod
    def normalize_trial(  # noqa: PLR0913, PLR0917 — the trial's identity plus its raw output; grouping would hide the contract
        cls,
        objective: Objective,
        trial_index: int,
        agent_name: str,
        model: str,
        raw: HarborRunResult,
        artifact_dir: Path,
    ) -> TrialTrajectory:
        """Turn a raw run directory into a :class:`TrialTrajectory`, copying in the produced task dir.

        Reads the normalized trace file for the turns, tool calls, and wall time; falls back to the
        run result's own wall time when the trace omits it. Defensive by design: a missing or partial
        trace yields empty sequences rather than an error, so one broken trial cannot sink a run."""
        trace = cls._read_trace(raw.run_dir)
        produced = cls._copy_produced(raw.run_dir, artifact_dir)
        return TrialTrajectory(
            objective_id=objective.id,
            trial_index=trial_index,
            agent=agent_name,
            model=model,
            turns=cls._parse_turns(trace),
            tool_calls=cls._parse_tool_calls(trace),
            produced_task_dir=produced,
            wall_time_sec=cls._wall_time(trace, raw.wall_time_sec),
            exit_code=raw.exit_code,
        )

    @staticmethod
    def _wall_time(trace: dict[str, object], fallback: float) -> float:
        """The trace's wall time when it recorded a numeric one, else the run result's own."""
        recorded = trace.get("wall_time_sec")
        return float(recorded) if isinstance(recorded, (int, float)) else fallback

    @staticmethod
    def _read_trace(run_dir: Path) -> dict[str, object]:
        """The normalized trace dict from ``run_dir``, or ``{}`` when none was recorded."""
        trace_path = run_dir / _TRACE_FILENAME
        if not trace_path.is_file():
            return {}
        parsed: dict[str, object] = json.loads(trace_path.read_text(encoding="utf-8"))
        return parsed

    @staticmethod
    def _parse_turns(trace: dict[str, object]) -> tuple[AgentTurn, ...]:
        """The agent turns from a trace, validated through :class:`AgentTurn`."""
        raw_turns = trace.get("turns", [])
        if not isinstance(raw_turns, list):
            return ()
        return tuple(AgentTurn.model_validate(turn) for turn in raw_turns)

    @staticmethod
    def _parse_tool_calls(trace: dict[str, object]) -> tuple[ToolCall, ...]:
        """The tool calls from a trace, validated through :class:`ToolCall`."""
        raw_calls = trace.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            return ()
        return tuple(ToolCall.model_validate(call) for call in raw_calls)

    @staticmethod
    def _copy_produced(run_dir: Path, artifact_dir: Path) -> str | None:
        """Copy the produced task directory into the trial's artifacts; return its relative path or None."""
        produced = run_dir / _PRODUCED_DIRNAME
        if not produced.is_dir():
            return None
        destination = artifact_dir / _ARTIFACT_TASK_DIRNAME
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(produced, destination)
        return _ARTIFACT_TASK_DIRNAME


class HarborCliExecutor:
    """The live executor: run one trial via ``harbor run`` against the Lambda MicroVMs environment.

    This is the human-gated half of the harness (it needs Bedrock credentials, a network, and the
    Workstream H environment), so it is never exercised by the offline suite. It materializes the
    objective's eval Harbor task, invokes ``harbor run`` with the environment loaded by import path
    (no fork — H-3), and records the invocation's combined log as a coarse trajectory. Extracting a
    richer per-tool trace from Harbor's native run output is a documented follow-up whose exact field
    mapping is confirmed on the first live run (eval/README.md), the same way H-3's live proving trial
    stays open.
    """

    def __init__(
        self,
        agent: AgentUnderTest,
        model: str,
        *,
        environment_import_path: str = LAMBDA_MICROVMS_IMPORT_PATH,
        harbor_executable: str | None = None,
        task_staging_root: Path | None = None,
    ) -> None:
        self._agent = agent
        self._model = model
        self._environment_import_path = environment_import_path
        self._harbor_executable = harbor_executable or shutil.which("harbor") or "harbor"
        self._task_staging_root = task_staging_root

    def __call__(self, objective: Objective, trial_index: int) -> HarborRunResult:
        """Run one trial and return where its raw output landed."""
        run_dir = self._run_dir_for(objective, trial_index)
        run_dir.mkdir(parents=True, exist_ok=True)
        task_dir = EvalHarborTask.materialize(objective, run_dir / "task-definition")
        started = time.monotonic()
        completed = self._invoke_harbor(task_dir, run_dir)
        wall_time_sec = time.monotonic() - started
        self._write_coarse_trace(run_dir, completed, wall_time_sec)
        return HarborRunResult(run_dir=run_dir, exit_code=completed.returncode, wall_time_sec=wall_time_sec)

    def _run_dir_for(self, objective: Objective, trial_index: int) -> Path:
        root = self._task_staging_root or (Path("eval") / "runs" / "_staging")
        return root / objective.id / f"trial-{trial_index}"

    def _build_argv(self, task_dir: Path) -> list[str]:
        """The ``harbor run`` argv for one local task. A third-party contract; pinned by a test.

        ``-p`` (``--path``), not ``-d``: ``-d`` is ``--dataset`` (``name@version``) and routes a local
        directory into registry resolution, which aborts in ``abort_dataset_resolution`` with "No job was
        started". The path is resolved absolute because
        :meth:`_invoke_harbor` runs with ``cwd=run_dir`` while ``task_dir`` is built under a repo-relative
        staging root, so a relative path would not resolve.

        ``-e`` (``--env``), not ``--environment-import-path``: same value (a ``module.path:ClassName``
        import path), the supported spelling, and no per-invocation deprecation notice.

        ``-n``/``--n-concurrent`` is omitted deliberately: this executor dispatches one task per call, so
        Harbor's default is correct. ``-n 1`` would read as a trial count it does not carry — attempts
        per trial is ``-k``/``--n-attempts`` — and would be silently wrong on a multi-task dataset.
        """
        return [
            "harbor",
            "run",
            "-p",
            str(task_dir.resolve()),
            "-m",
            self._model,
            "-a",
            self._agent.harbor_agent,
            "-e",
            self._environment_import_path,
        ]

    def _invoke_harbor(self, task_dir: Path, run_dir: Path) -> subprocess.CompletedProcess[str]:
        """Invoke ``harbor run`` for one trial, argv-only and shell-free."""
        argv = self._build_argv(task_dir)
        # argv[0] is the literal "harbor"; the resolved binary runs via ``executable=``
        # (self._harbor_executable). Every other element is a value this harness chose itself — model
        # id, agent name, task path — and shell=False, so there is no string-command API to inject into.
        # The list is rebuilt inline (literal argv[0] + the tail) so the call presents a constant
        # command to the analyzer; passing the ``argv`` variable directly would hide argv[0] from it.
        return subprocess.run(  # nosec B607 - literal argv[0], real binary via executable=; B603 skipped in pyproject
            ["harbor", *argv[1:]],
            executable=self._harbor_executable,
            capture_output=True,
            text=True,
            cwd=run_dir,
            check=False,
        )

    @staticmethod
    def _write_coarse_trace(run_dir: Path, completed: subprocess.CompletedProcess[str], wall_time_sec: float) -> None:
        """Record the harbor invocation's combined log as a single-turn trace the normalizer reads.

        A non-zero ``harbor run`` exit is recorded distinguishably: the turn role is
        ``harbor-launch-failure`` and the trace carries ``harbor_launch_failed`` + ``harbor_exit_code``,
        so a reader can tell "Harbor refused to start" — an ``abort_dataset_resolution`` — from "the
        agent ran but produced nothing". Recording the same shape regardless of the return code would
        let a failed invocation be consumed as if it were a zero-scoring trial.
        """
        log = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        launch_failed = completed.returncode != 0
        trace = {
            "turns": [{"role": "harbor-launch-failure" if launch_failed else "harbor", "text": log}],
            "tool_calls": [],
            "wall_time_sec": wall_time_sec,
            "harbor_exit_code": completed.returncode,
            "harbor_launch_failed": launch_failed,
        }
        (run_dir / _TRACE_FILENAME).write_text(json.dumps(trace, indent=2), encoding="utf-8")
