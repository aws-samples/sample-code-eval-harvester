"""``python -m eval.alignment.run_alignment``: measure judge↔human agreement and record it.

Offline (the default), it replays each labeled datapoint's pinned reply — reproducible, no model,
green in the gate — and writes ``alignment_result.json``, the record the gate reads to license the
judge. Pass ``--model <id>`` to refresh the number against a live model instead (the human-gated
step: it needs credentials and a network, so it is never run in CI), which records the real model id.

Run it whenever the rubric or prompt changes (bump ``JUDGE_VERSION``): a stale alignment for an older
rubric no longer licenses the judge, so the gate forces the re-run.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from eval.alignment.labeled_set import LabeledDatapoint, LabeledSet
from eval.alignment.measure import RECORDED_FIXTURE_MODEL, AlignmentMeasurement, AlignmentReport
from eval.judge.client import LiveModelClient, ModelClient
from eval.judge.gate import DEFAULT_ALIGNMENT_PATH


class RunAlignmentCommand:
    """Parse args, run the measurement, print the report, and write the recorded result. Holds no state."""

    @classmethod
    def main(cls, argv: Sequence[str]) -> int:
        """Measure agreement over the labeled set and persist the :class:`AlignmentResult`."""
        arguments = cls.build_parser().parse_args(argv)
        labeled = LabeledSet.load_all()
        report = cls._measure(labeled, arguments.model)
        cls._write_result(report, arguments.output)
        cls._print_report(report, arguments.output)
        return 0

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        """The argument parser: the optional live model id and the output path."""
        parser = argparse.ArgumentParser(
            prog="run_alignment", description="Measure and record judge↔human alignment (Workstream G, G-2)."
        )
        parser.add_argument(
            "-m",
            "--model",
            default="",
            help="a live model id to refresh the number against (human-gated); omit to replay the pinned replies",
        )
        parser.add_argument(
            "--output", type=Path, default=DEFAULT_ALIGNMENT_PATH, metavar="<path>", help="where to write alignment_result.json"
        )
        return parser

    @classmethod
    def _measure(cls, labeled: Sequence[LabeledDatapoint], model: str) -> AlignmentReport:
        """Run the measurement — replaying pinned replies offline, or against a live model when asked."""
        if not model:
            return AlignmentMeasurement.run(labeled)
        live_client: ModelClient = LiveModelClient.from_env({"EVAL_JUDGE_MODEL": model})

        def client_for(_case: LabeledDatapoint) -> ModelClient:
            return live_client

        return AlignmentMeasurement.run(labeled, model=model, client_for=client_for)

    @staticmethod
    def _write_result(report: AlignmentReport, output: Path) -> None:
        """Persist the recorded alignment result — the record the gate reads to license the judge."""
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.result.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _print_report(report: AlignmentReport, output: Path) -> None:
        """Print the headline agreement, the bar, and any judge↔human disagreements for the operator."""
        result = report.result
        verdict = "PASS" if result.passed else "FAIL"
        source = "recorded fixtures" if result.model == RECORDED_FIXTURE_MODEL else result.model
        print(f"alignment: {verdict} — agreement {result.agreement:.3f} vs bar {result.bar:.3f} over {result.n_labeled} labeled")
        print(f"           judge {result.judge_version}, model {source} → {output}")
        if report.disagreements:
            print(f"           judge↔human disagreements: {', '.join(report.disagreements)}")


def main() -> int:
    """Console entry point for ``python -m eval.alignment.run_alignment``."""
    return RunAlignmentCommand.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
