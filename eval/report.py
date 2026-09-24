"""Combine verify + the quality judge + tool-use metrics into per-objective pass rates and a report.

This is the eval's **answer** (G-3): for each objective × trial, did the agent *solve the task* — and
how well did it use the tool getting there? A trial is `task_solved` only when all three hold:

1. it **produced** a datapoint,
2. that datapoint **passes `eval-harvest verify`** (structural validity + the leak scan), and
3. the human-aligned **quality judge** passes it (a valid-but-wrong datapoint is not a pass).

The three are ANDed on purpose (task Notes): a structurally-valid datapoint about the wrong PR must
not count. The headline is a **pass rate over k trials**, not a single run — a stochastic agent needs
the fraction, and the report shows the spread rather than hiding it.

The scoring is pure over its inputs and its I/O is behind :class:`ScoringSeams` (verify, judge, the
datapoint loader, and the PR-context builder), so the whole pass rule is exercised offline with
recorded trajectories and a recorded judge — no live model, no VM (task How To Verify). The live
defaults (:meth:`ScoringSeams.live`) run the real `verify` and the alignment-gated judge; running
those is the human-gated step, the same posture as G-1's live dispatch (eval/README.md).

**The §8 pass bar is not invented here.** :data:`PASS_BAR` records it as *open* with the straw man
from PRD §8 — a real go/no-go threshold is set by a human once the eval has run, and the report says
so rather than manufacturing a number (memory; task What To Build §5).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eval.judge import DatapointLoader, judge
from eval.judge.models import Datapoint, PrContext, QualityVerdict
from eval.metrics import ToolUseAnalyzer, ToolUseMetrics
from eval.models import Objective, RunManifest, TrialTrajectory
from eval_harvest.verify import Verify

#: The failure reasons a trial can carry — the diagnostic the report reads (task What To Build §2).
FailureReason = Literal["no-datapoint", "failed-verify", "failed-quality"]

#: The PRD §8 straw man, recorded verbatim so the report can state it without inventing a target.
STRAW_MAN = "80% of trials build a valid, good datapoint on first attempt; 100% after the agent acts on the CLI's feedback"


class PassBar(BaseModel):
    """The PRD §8 success bar: either a set target pass rate, or *open* with the straw man.

    ``status='open'`` (with ``target=None``) is the honest v1 state — the bar is a go/no-go threshold
    a human sets once the eval has run, not a number the CLI manufactures (PRD §8). When
    a human sets it, ``status='set'`` and ``target`` carries the fraction."""

    model_config = ConfigDict(frozen=True)

    status: Literal["set", "open"]
    target: float | None = None
    straw_man: str = STRAW_MAN
    rationale: str = ""


#: The shipped bar: **open**. Set deliberately by a human after reading the eval's recorded result;
#: until then the report records the achieved rate and defers the go/no-go (PRD §8, task What To Build §5).
PASS_BAR = PassBar(
    status="open",
    target=None,
    straw_man=STRAW_MAN,
    rationale=(
        "Left open: the go/no-go threshold is set by a human once the eval has actually run against a live "
        "agent, not invented here. The straw man from PRD §8 is recorded so the report states the bar it is "
        "measured against, and a failed bar is a legitimate v1 outcome (ship a working builder, document that "
        "its findings are not yet trusted)."
    ),
)


class PerTrialResult(BaseModel):
    """One trial's full score: whether it produced/verified/passed-quality, its tool use, the verdict.

    ``quality`` is ``None`` when the judge did not run — there was no datapoint, or `verify` failed, so
    scoring short-circuits before spending a model call. ``task_solved`` requires all three of
    produced + verified + quality-pass; ``failure_reason`` names the first gate that failed."""

    model_config = ConfigDict(frozen=True)

    objective_id: str
    trial_index: int
    produced_datapoint: bool
    verify_passed: bool
    quality: QualityVerdict | None
    tool_use: ToolUseMetrics
    task_solved: bool
    failure_reason: FailureReason | None


class ObjectivePassRate(BaseModel):
    """The pass rate for one objective over its k trials, plus the per-trial failure reasons."""

    model_config = ConfigDict(frozen=True)

    objective_id: str
    trials: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    failure_reasons: tuple[FailureReason, ...] = ()


class EvalReport(BaseModel):
    """The eval's headline: per-objective + overall pass rates, the §8 bar, and the go/no-go read.

    Persisted as JSON beside the human-readable markdown, and round-trips like the rest of the eval
    models so a recorded run can be re-scored or diffed."""

    model_config = ConfigDict(frozen=True)

    timestamp: str
    agent: str
    model: str
    trials_per_objective: int
    per_objective: tuple[ObjectivePassRate, ...]
    overall_pass_rate: float = Field(ge=0.0, le=1.0)
    pass_bar: PassBar
    go_no_go: str
    trial_results: tuple[PerTrialResult, ...]


#: The datapoint's structural/leak gate: a task directory in, "did every check run and pass?" out.
VerifyFn = Callable[[Path], bool]
#: The quality judge: a loaded datapoint + its PR context in, a :class:`QualityVerdict` out.
JudgeFn = Callable[[Datapoint, PrContext], QualityVerdict]
#: Load a built datapoint directory into a gradeable :class:`Datapoint`.
LoadDatapointFn = Callable[[Path], Datapoint]
#: The PR context a datapoint is graded against, derived from its objective.
PrContextFn = Callable[[Objective], PrContext]


def default_verify(task_dir: Path) -> bool:
    """The live verify seam: a datapoint passes when every `eval-harvest verify` check ran and passed.

    Runs the shipped structural + leak checks over the produced directory (E-2). The base-exists and
    patch-applies checks need a local clone of the source repo, which the live report must supply for
    them to run rather than report unresolved — extracting that clone from the trial is a live-run
    wiring item, the same honest posture as the richer-trace extraction note in eval/README.md."""
    return Verify.verify_task(task_dir).ok


def default_pr_context(objective: Objective) -> PrContext:
    """Derive the PR context a datapoint is graded against from its objective record.

    The objective does not carry the human review's prose, so this builds a coarse context from what
    it does carry — repo, PR number, and the expected properties — enough for the judge to compare the
    datapoint against the reviewed state it claims. A live run may substitute a richer context built
    from the captured candidate; this is the offline-derivable default."""
    repo = objective.repo_url.removeprefix("https://github.com/").removesuffix("/")
    reviewed_state = "change-requested" if objective.kind == "reject" else "approved"
    must_mention = ", ".join(objective.expected_properties.must_mention)
    mention_clause = f" the review flagged: {must_mention}." if must_mention else ""
    review_summary = (
        f"expected verdict {objective.expected_properties.expected_verdict}, "
        f"at least {objective.expected_properties.minimum_findings} finding(s).{mention_clause}"
    )
    return PrContext(repo=repo, pr_number=objective.pr, reviewed_state=reviewed_state, review_summary=review_summary)


@dataclass(frozen=True, slots=True)
class ScoringSeams:
    """The injectable I/O boundary of scoring: verify, judge, datapoint loader, PR-context builder.

    Grouped so the scorer's signatures stay short and a test replaces the whole boundary at once with
    recorded/fake implementations — no live model, no VM. :meth:`live` returns the real defaults the
    human-gated eval run uses."""

    verify: VerifyFn
    judge: JudgeFn
    load_datapoint: LoadDatapointFn
    pr_context_for: PrContextFn

    @classmethod
    def live(cls) -> ScoringSeams:
        """The live defaults: the shipped `verify`, the alignment-gated judge, and the offline PR context."""
        return cls(
            verify=default_verify,
            judge=judge,
            load_datapoint=DatapointLoader.load,
            pr_context_for=default_pr_context,
        )


class EvalScorer:
    """Scores a run's trajectories into per-trial results and rolls them up into an :class:`EvalReport`.

    Holds no state. The pass rule and the roll-up are pure; all I/O (verify, judge, loading a datapoint,
    building a PR context) is behind :class:`ScoringSeams`, so the whole thing runs offline in a test."""

    @classmethod
    def score_run(
        cls,
        manifest: RunManifest,
        objectives: tuple[Objective, ...],
        run_root: Path,
        *,
        seams: ScoringSeams,
    ) -> EvalReport:
        """Score every trial in ``manifest`` and combine them into the eval's report."""
        objective_by_id = {objective.id: objective for objective in objectives}
        results = tuple(
            cls._score_manifest_trial(row.objective_id, row.trial_index, objective_by_id, run_root, seams)
            for row in manifest.trials
        )
        per_objective = cls._per_objective_rates(manifest.objective_ids, results)
        overall = cls._overall_rate(results)
        return EvalReport(
            timestamp=manifest.timestamp,
            agent=manifest.agent,
            model=manifest.model,
            trials_per_objective=manifest.trials_per_objective,
            per_objective=per_objective,
            overall_pass_rate=overall,
            pass_bar=PASS_BAR,
            go_no_go=cls._go_no_go(overall, per_objective),
            trial_results=results,
        )

    @classmethod
    def _score_manifest_trial(
        cls,
        objective_id: str,
        trial_index: int,
        objective_by_id: dict[str, Objective],
        run_root: Path,
        seams: ScoringSeams,
    ) -> PerTrialResult:
        """Load one trial's trajectory from ``run_root`` and score it against its objective."""
        artifact_dir = run_root / objective_id / f"trial-{trial_index}"
        trajectory = TrialTrajectory.model_validate_json((artifact_dir / "trajectory.json").read_text(encoding="utf-8"))
        objective = objective_by_id[objective_id]
        return cls.score_trial(trajectory, artifact_dir, objective, seams=seams)

    @classmethod
    def score_trial(
        cls,
        trajectory: TrialTrajectory,
        artifact_dir: Path,
        objective: Objective,
        *,
        seams: ScoringSeams,
    ) -> PerTrialResult:
        """Score one trial: apply the produced→verify→quality pass rule and compute its tool-use metrics.

        Short-circuits: no datapoint stops before verify; a failed verify stops before the judge, so a
        model call is never spent on a datapoint that already cannot pass."""
        tool_use = ToolUseAnalyzer.analyze(trajectory)
        task_dir = cls._locate_datapoint(artifact_dir, trajectory)
        if task_dir is None:
            return cls._result(objective, trajectory, tool_use, produced=False, verify_passed=False, quality=None)
        verify_passed = seams.verify(task_dir)
        if not verify_passed:
            return cls._result(objective, trajectory, tool_use, produced=True, verify_passed=False, quality=None)
        datapoint = seams.load_datapoint(task_dir)
        quality = seams.judge(datapoint, seams.pr_context_for(objective))
        return cls._result(objective, trajectory, tool_use, produced=True, verify_passed=True, quality=quality)

    @staticmethod
    def _result(  # noqa: PLR0913 — one trial's identity plus its three gate outcomes; grouping would hide the contract
        objective: Objective,
        trajectory: TrialTrajectory,
        tool_use: ToolUseMetrics,
        *,
        produced: bool,
        verify_passed: bool,
        quality: QualityVerdict | None,
    ) -> PerTrialResult:
        """Assemble one trial's result, deriving ``task_solved`` and its failure reason from the three gates."""
        failure_reason = _failure_reason(produced=produced, verify_passed=verify_passed, quality=quality)
        return PerTrialResult(
            objective_id=objective.id,
            trial_index=trajectory.trial_index,
            produced_datapoint=produced,
            verify_passed=verify_passed,
            quality=quality,
            tool_use=tool_use,
            task_solved=failure_reason is None,
            failure_reason=failure_reason,
        )

    @staticmethod
    def _locate_datapoint(artifact_dir: Path, trajectory: TrialTrajectory) -> Path | None:
        """The produced datapoint directory (the one holding ``task.toml``), or ``None`` when none was built.

        The runner copies the produced tree under ``artifact_dir/<produced_task_dir>``; the datapoint
        itself is nested (``…/tasks/<name>/``). Find the single ``task.toml`` under it and return its
        directory. Absent ``produced_task_dir`` — or a copied tree with no ``task.toml`` — is no datapoint."""
        if trajectory.produced_task_dir is None:
            return None
        copied_root = artifact_dir / trajectory.produced_task_dir
        task_configs = sorted(copied_root.rglob("task.toml"))
        return task_configs[0].parent if task_configs else None

    @staticmethod
    def _per_objective_rates(
        objective_ids: tuple[str, ...], results: tuple[PerTrialResult, ...]
    ) -> tuple[ObjectivePassRate, ...]:
        """The pass rate per objective, in the manifest's objective order."""
        return tuple(_objective_rate(objective_id, results) for objective_id in objective_ids)

    @staticmethod
    def _overall_rate(results: tuple[PerTrialResult, ...]) -> float:
        """The fraction of all trials that were solved — the eval's single headline number."""
        if not results:
            return 0.0
        return sum(1 for result in results if result.task_solved) / len(results)

    @staticmethod
    def _go_no_go(overall: float, per_objective: tuple[ObjectivePassRate, ...]) -> str:
        """The go/no-go read: deferred while the §8 bar is open, else a pass/fail against the set target."""
        spread = ", ".join(f"{rate.objective_id} {rate.pass_rate:.0%}" for rate in per_objective)
        if PASS_BAR.status == "open" or PASS_BAR.target is None:
            return (
                f"DEFERRED — overall pass rate {overall:.0%} ({spread}). The PRD §8 bar is not set "
                f"(straw man: {PASS_BAR.straw_man}); a human sets the go/no-go threshold after reading this run."
            )
        verdict = "GO" if overall >= PASS_BAR.target else "NO-GO"
        return f"{verdict} — overall pass rate {overall:.0%} vs bar {PASS_BAR.target:.0%} ({spread})."


class ReportRenderer:
    """Renders an :class:`EvalReport` to the human-readable markdown the operator reads. Holds no state."""

    @classmethod
    def render(cls, report: EvalReport) -> str:
        """The full markdown report: objectives, pass rates, tool-use findings, quality notes, go/no-go."""
        sections = [
            cls._header(report),
            cls._pass_rate_table(report),
            cls._pass_bar_section(report),
            cls._go_no_go_section(report),
            cls._per_trial_section(report),
        ]
        return "\n\n".join(sections) + "\n"

    @staticmethod
    def _header(report: EvalReport) -> str:
        """The run's identity: when, which agent + model, and the shape of the run."""
        return (
            f"# Eval report — {report.timestamp}\n\n"
            f"- **Agent:** {report.agent}\n"
            f"- **Model:** {report.model or '(unset)'}\n"
            f"- **Objectives × trials:** {len(report.per_objective)} × {report.trials_per_objective}\n"
            f"- **Overall pass rate:** {report.overall_pass_rate:.0%} "
            f"({sum(rate.passed for rate in report.per_objective)}/"
            f"{sum(rate.trials for rate in report.per_objective)} trials solved)"
        )

    @staticmethod
    def _pass_rate_table(report: EvalReport) -> str:
        """A row per objective: trials, passed, rate, and the failure reasons behind any misses."""
        lines = [
            "## Pass rates",
            "",
            "| Objective | Trials | Passed | Pass rate | Failure reasons |",
            "|-----------|--------|--------|-----------|-----------------|",
        ]
        for rate in report.per_objective:
            reasons = ", ".join(rate.failure_reasons) if rate.failure_reasons else "—"
            lines.append(f"| {rate.objective_id} | {rate.trials} | {rate.passed} | {rate.pass_rate:.0%} | {reasons} |")
        return "\n".join(lines)

    @staticmethod
    def _pass_bar_section(report: EvalReport) -> str:
        """The §8 bar: set or open, always with the straw man and the reason it stands where it does."""
        bar = report.pass_bar
        status = f"**set** at {bar.target:.0%}" if bar.status == "set" and bar.target is not None else "**open**"
        return f"## PRD §8 pass bar\n\n- **Status:** {status}\n- **Straw man:** {bar.straw_man}\n- **Rationale:** {bar.rationale}"

    @staticmethod
    def _go_no_go_section(report: EvalReport) -> str:
        """The go/no-go read — the sentence the whole eval exists to produce."""
        return f"## Go / no-go\n\n{report.go_no_go}"

    @classmethod
    def _per_trial_section(cls, report: EvalReport) -> str:
        """One block per trial: the three gates, the tool-use summary, and the judge's quality notes."""
        blocks = ["## Per-trial detail"]
        for result in report.trial_results:
            blocks.append(cls._trial_block(result))
        return "\n\n".join(blocks)

    @classmethod
    def _trial_block(cls, result: PerTrialResult) -> str:
        """One trial's detail: solved/failed, the gate outcomes, tool use, and quality notes."""
        verdict = "solved" if result.task_solved else f"failed ({result.failure_reason})"
        tool = result.tool_use
        verbs = " → ".join(tool.verb_sequence) if tool.verb_sequence else "(no eval-harvest calls)"
        quality_notes = result.quality.notes if result.quality is not None else "(judge not run)"
        return (
            f"### {result.objective_id} — trial {result.trial_index}: {verdict}\n"
            f"- produced datapoint: {result.produced_datapoint} · verify: {result.verify_passed} · "
            f"quality: {cls._quality_summary(result)}\n"
            f"- verbs: {verbs}\n"
            f"- redundant calls: {tool.redundant_calls} · refusals: {tool.refusals} "
            f"(acted on {tool.refusals_acted_on}) · tool calls/turn: {tool.tool_calls_per_turn:.2f}\n"
            f"- quality notes: {quality_notes}"
        )

    @staticmethod
    def _quality_summary(result: PerTrialResult) -> str:
        """The judge's roll-up for a trial, or a note that the judge did not run."""
        if result.quality is None:
            return "not judged"
        return f"{'pass' if result.quality.overall_pass else 'fail'} ({result.quality.overall_score:.2f})"


class ReportPersistence:
    """Writes the report to disk — the markdown the operator reads and the JSON G-3 can re-score. Stateless."""

    @classmethod
    def write(cls, report: EvalReport, reports_dir: Path) -> Path:
        """Write ``<timestamp>.md`` (and ``<timestamp>.json`` beside it) under ``reports_dir``; return the md path."""
        reports_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = reports_dir / f"{report.timestamp}.md"
        markdown_path.write_text(ReportRenderer.render(report), encoding="utf-8")
        (reports_dir / f"{report.timestamp}.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return markdown_path


class EvalReportStage:
    """The G-3 stage of ``mise run eval``: score the run G-1 captured and write the report. Holds no state."""

    @classmethod
    def generate(
        cls,
        manifest: RunManifest,
        objectives: tuple[Objective, ...],
        run_root: Path,
        reports_dir: Path,
        *,
        seams: ScoringSeams | None = None,
    ) -> tuple[EvalReport, Path]:
        """Score ``manifest`` (defaulting to the live seams) and persist the report; return both."""
        report = EvalScorer.score_run(manifest, objectives, run_root, seams=seams or ScoringSeams.live())
        report_path = ReportPersistence.write(report, reports_dir)
        return report, report_path


def _failure_reason(*, produced: bool, verify_passed: bool, quality: QualityVerdict | None) -> FailureReason | None:
    """The first gate that failed — ``None`` when produced, verified, and quality-passed (a solved trial)."""
    if not produced:
        return "no-datapoint"
    if not verify_passed:
        return "failed-verify"
    if quality is None or not quality.overall_pass:
        return "failed-quality"
    return None


def _objective_rate(objective_id: str, results: tuple[PerTrialResult, ...]) -> ObjectivePassRate:
    """One objective's pass rate over the trials that belong to it."""
    trials = [result for result in results if result.objective_id == objective_id]
    passed = sum(1 for result in trials if result.task_solved)
    pass_rate = passed / len(trials) if trials else 0.0
    reasons = tuple(result.failure_reason for result in trials if result.failure_reason is not None)
    return ObjectivePassRate(
        objective_id=objective_id,
        trials=len(trials),
        passed=passed,
        pass_rate=pass_rate,
        failure_reasons=reasons,
    )
