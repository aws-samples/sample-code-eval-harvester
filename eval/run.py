"""``mise run eval`` entry point: run the core-bet eval, or skip cleanly when it cannot.

This drives a live model (Claude Code on Bedrock) and the Workstream H Lambda MicroVMs environment,
so it is **excluded from ``mise run check``** and offline CI (see ``mise.toml``). When the Bedrock
credentials the agent needs or the ``harbor`` runner are absent, it prints why and exits 0 — a clean
skip, never a failure — so an offline loop or a credential-less CI job treats the eval as
human-gated rather than red (G-1 What To Build §7; PRD §4/§8/§9).

Run it directly with ``uv run python -m eval.run`` or via ``mise run eval``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from eval.agent import AgentUnderTest, ClaudeCodeAgent, ClaudeCodeReviewerAgent
from eval.catalog import OBJECTIVES_DIR, ObjectiveCatalog
from eval.report import EvalReportStage
from eval.runner import EnvironmentPreflightError, EvalRunner, HarborCliExecutor, preflight_environment_imports

#: The default agent-under-test. ``claude-code`` is the built-in Harbor driver; ``claude-code-reviewer``
#: is the starter with a custom review prompt. A second real agent (Kiro) plugs in here the same way.
_DEFAULT_AGENT = "claude-code"
_AGENTS: dict[str, type[AgentUnderTest]] = {
    "claude-code": ClaudeCodeAgent,
    "claude-code-reviewer": ClaudeCodeReviewerAgent,
}

#: k — trials per objective. LLM agents are nondeterministic, so a single run is a weak signal; the
#: report stage (G-3) rolls the trials up into a pass *rate* over k (G-1 What To Build §5).
_DEFAULT_TRIALS = 3
_DEFAULT_OUTPUT_ROOT = Path("eval") / "runs"
#: Where the G-3 run reports land (one ``<timestamp>.md`` + ``.json`` per run).
_DEFAULT_REPORTS_ROOT = Path("eval") / "reports"


class EvalCommand:
    """The ``mise run eval`` command: parse args, decide runnability, dispatch or skip. Holds no state."""

    @classmethod
    def main(cls, argv: Sequence[str]) -> int:
        """Parse ``argv``, run the eval when it can, or skip cleanly (exit 0) when it cannot."""
        arguments = cls.build_parser().parse_args(argv)
        agent = _AGENTS[arguments.agent]()

        skip_reason = cls._skip_reason(agent, arguments)
        if skip_reason is not None:
            print(f"eval: skipping — {skip_reason}")
            print("      (the eval needs Bedrock credentials, a network, and the Lambda MicroVMs environment;")
            print("       a human or a credentialed CI job runs it. See eval/README.md.)")
            return 0

        # Preflight the environment's imports before any AWS call: a mis-synced `eval` group must
        # fail here, by name, not three frames deep inside Harbor after a build-context upload.
        try:
            preflight_environment_imports()
        except EnvironmentPreflightError as exc:
            print(f"eval: preflight failed — {exc}")
            return 1

        objectives = ObjectiveCatalog.load_all(arguments.objectives)
        executor = HarborCliExecutor(agent, arguments.model)
        manifest = EvalRunner.run_all(
            objectives=objectives,
            agent=agent,
            model=arguments.model,
            output_root=arguments.output,
            trials_per_objective=arguments.trials,
            executor=executor,
            timestamp=cls.timestamp(),
        )
        run_root = arguments.output / manifest.timestamp
        print(f"eval: ran {len(objectives)} objective(s) × {arguments.trials} trial(s) → {run_root}")

        report, report_path = EvalReportStage.generate(manifest, objectives, run_root, arguments.reports)
        print(f"eval: report → {report_path}")
        print(f"eval: {report.go_no_go}")
        return 0

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        """The argument parser: the model, agent, trial count, objectives dir, and output root."""
        parser = argparse.ArgumentParser(prog="eval", description="Run the eval-harvest core-bet eval (Workstream G).")
        parser.add_argument(
            "-m",
            "--model",
            default="",
            help="the Bedrock model id for the agent-under-test (Harbor's -m / ANTHROPIC_MODEL); required to run",
        )
        parser.add_argument(
            "-a", "--agent", default=_DEFAULT_AGENT, choices=sorted(_AGENTS), help="the agent-under-test (default: claude-code)"
        )
        parser.add_argument(
            "-n", "--trials", type=int, default=_DEFAULT_TRIALS, metavar="<k>", help="trials per objective (default: 3)"
        )
        parser.add_argument(
            "--objectives", type=Path, default=OBJECTIVES_DIR, metavar="<dir>", help="the objectives directory to run"
        )
        parser.add_argument(
            "--output",
            type=Path,
            default=_DEFAULT_OUTPUT_ROOT,
            metavar="<dir>",
            help="where run artifacts land (default: eval/runs)",
        )
        parser.add_argument(
            "--reports",
            type=Path,
            default=_DEFAULT_REPORTS_ROOT,
            metavar="<dir>",
            help="where the G-3 run report lands (default: eval/reports)",
        )
        return parser

    @staticmethod
    def _skip_reason(agent: AgentUnderTest, arguments: argparse.Namespace) -> str | None:
        """Why the eval cannot run now, or ``None`` when it can — the clean-skip decision (§7)."""
        missing = agent.missing_env()
        if missing:
            return f"agent {agent.name!r} is missing required environment: {', '.join(missing)}"
        if not arguments.model:
            return "no model id given (pass -m <bedrock-model-id> or set it via Harbor)"
        if shutil.which("harbor") is None:
            return "the 'harbor' runner is not on PATH"
        return None

    @staticmethod
    def timestamp() -> str:
        """A filesystem-safe UTC timestamp for the run directory, e.g. ``20260905T141530Z``."""
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def main() -> int:
    """Console entry point for ``mise run eval`` / ``python -m eval.run``."""
    return EvalCommand.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
