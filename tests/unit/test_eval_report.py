"""Offline tests for the tool-use metrics, the pass rule, and the eval report (Workstream G, G-3).

These exercise G-3's scoring without a live model, a VM, or a network (task How To Verify): the
tool-use metrics are computed from a fixture trajectory and checked against expected counts; the
pass rule is driven with recorded verify/judge seams to prove a trial needs *all three* of
produced + valid + good; the pass rate is a fraction over k trials; and the report records the PRD
§8 bar as open with the straw man rather than inventing a number.

Do NOT add a test that calls a live model or spins a MicroVM here — that is the human-gated run.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.judge.models import CriterionVerdict, Datapoint, PrContext, QualityVerdict
from eval.metrics import ToolUseAnalyzer
from eval.models import (
    AgentTurn,
    Objective,
    RunManifest,
    ToolCall,
    TrialResult,
    TrialTrajectory,
)
from eval.report import (
    PASS_BAR,
    STRAW_MAN,
    EvalScorer,
    PerTrialResult,
    ReportPersistence,
    ReportRenderer,
    ScoringSeams,
    default_pr_context,
)

_FULL_SHA_LENGTH = 40


# ───────────────────────────── fixtures / helpers ─────────────────────────────


def _objective(objective_id: str = "obj-1", kind: str = "reject") -> Objective:
    """A minimal valid objective."""
    verdict = "block" if kind == "reject" else "approve"
    return Objective.model_validate(
        {
            "id": objective_id,
            "repo_url": "https://github.com/pydantic/pydantic",
            "commit_sha": "0" * _FULL_SHA_LENGTH,
            "pr": 13611,
            "kind": kind,
            "expected_properties": {"expected_verdict": verdict, "minimum_findings": 1, "must_mention": ["re.Pattern"]},
        }
    )


def _bash(command: str, exit_code: int | None = 0) -> ToolCall:
    """A bash tool call running ``command`` — the shape the harness records for a shell command."""
    return ToolCall(name="bash", args={"command": command}, result="", exit_code=exit_code)


def _verdict(*, passed: bool) -> QualityVerdict:
    """A quality verdict with one criterion, passing or failing overall."""
    score = 1.0 if passed else 0.0
    criterion = CriterionVerdict(name="oracle-reflects-review", passed=passed, score=score, rationale="x")
    return QualityVerdict(per_criterion=(criterion,), overall_pass=passed, overall_score=score, notes="recorded")


def _datapoint(directory_name: str = "org__repo__pr1-reject") -> Datapoint:
    """A minimal loaded datapoint the judge seam would receive."""
    return Datapoint(
        directory_name=directory_name,
        kind="reject",
        expected_verdict="block",
        blocking_severity="high",
        instruction="Review the change.",
        change_patch="--- a/x\n+++ b/x\n",
    )


def _stub_context(_objective: Objective) -> PrContext:
    """A fixed PR context for the pass-rule tests — the judge seam is recorded, so its content is inert."""
    return PrContext(repo="o/r", pr_number=1, reviewed_state="change-requested", review_summary="s")


def _seams(*, verify: object, judge: object) -> ScoringSeams:
    """Scoring seams wired to recorded verify/judge functions — no model, no VM."""
    return ScoringSeams(
        verify=verify,  # type: ignore[arg-type]
        judge=judge,  # type: ignore[arg-type]
        load_datapoint=lambda task_dir: _datapoint(task_dir.name),
        pr_context_for=_stub_context,
    )


def _write_trial(run_root: Path, objective_id: str, trial_index: int, *, produced: bool) -> None:
    """Write one trial's trajectory.json (and a produced task.toml when ``produced``) under ``run_root``."""
    artifact_dir = run_root / objective_id / f"trial-{trial_index}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    produced_dir = "task" if produced else None
    if produced:
        datapoint_dir = artifact_dir / "task" / "tasks" / f"dp-trial-{trial_index}"
        datapoint_dir.mkdir(parents=True)
        (datapoint_dir / "task.toml").write_text('schema_version = "1.4"\n', encoding="utf-8")
    trajectory = TrialTrajectory(
        objective_id=objective_id,
        trial_index=trial_index,
        agent="recorded",
        model="m",
        tool_calls=(_bash("eval-harvest emit --candidate c", exit_code=0),),
        produced_task_dir=produced_dir,
    )
    (artifact_dir / "trajectory.json").write_text(trajectory.model_dump_json(), encoding="utf-8")


def _manifest(objective_id: str, trials: int, *, produced: bool = True) -> RunManifest:
    """A run manifest whose rows point at ``trials`` trial artifacts (all produced by default)."""
    return RunManifest(
        timestamp="20260905T000000Z",
        agent="recorded",
        model="m",
        trials_per_objective=trials,
        objective_ids=(objective_id,),
        trials=tuple(
            TrialResult(objective_id=objective_id, trial_index=index, trajectory_path="unused", exit_code=0)
            for index in range(trials)
        ),
    )


# ───────────────────────────── tool-use metrics ─────────────────────────────


def test_tool_use_metrics_from_recorded_trajectory() -> None:
    """Metrics from a fixture trace match expected counts: verbs, a repeat, and a refusal then a fix."""
    trajectory = TrialTrajectory(
        objective_id="o",
        trial_index=0,
        agent="recorded",
        model="m",
        turns=(AgentTurn(role="user", text="a"), AgentTurn(role="assistant", text="b"), AgentTurn(role="assistant", text="c")),
        tool_calls=(
            _bash("eval-harvest --help", exit_code=0),
            _bash("eval-harvest survey --repo x", exit_code=0),
            _bash("eval-harvest survey --repo x", exit_code=0),  # byte-identical repeat
            _bash("ls -la", exit_code=0),  # not an eval-harvest call → not a verb
            _bash("eval-harvest emit --candidate c", exit_code=3),  # a refusal
            _bash("eval-harvest emit --candidate c --fix", exit_code=0),  # acted on it
        ),
    )

    metrics = ToolUseAnalyzer.analyze(trajectory)

    assert metrics.verb_sequence == ("--help", "survey", "survey", "emit", "emit")
    assert metrics.distinct_verbs == ("--help", "emit", "survey")
    assert metrics.harvest_calls == 5
    assert metrics.total_tool_calls == 6
    assert metrics.total_turns == 3
    assert metrics.redundant_calls == 1  # only the identical survey repeat
    assert metrics.refusals == 1
    assert metrics.refusals_acted_on == 1
    assert metrics.acted_on_all_refusals is True
    assert metrics.tool_calls_per_turn == 2.0  # 6 calls / 3 turns


def test_tool_use_flags_giveup_after_refusal() -> None:
    """A refusal that is the last eval-harvest call counts as not acted on — the agent gave up."""
    trajectory = TrialTrajectory(
        objective_id="o",
        trial_index=0,
        agent="recorded",
        model="m",
        tool_calls=(
            _bash("eval-harvest survey --repo x", exit_code=0),
            _bash("eval-harvest emit --candidate c", exit_code=3),  # refusal, and the last harvest call
        ),
    )

    metrics = ToolUseAnalyzer.analyze(trajectory)

    assert metrics.refusals == 1
    assert metrics.refusals_acted_on == 0
    assert metrics.acted_on_all_refusals is False
    assert metrics.tool_calls_per_turn == 0.0  # no turns recorded


# ───────────────────────────── the pass rule ─────────────────────────────


def _score_single(tmp_path: Path, *, produced: bool, verify: object, judge: object) -> PerTrialResult:
    """Score one on-disk trial with recorded seams and return its result."""
    run_root = tmp_path / "run"
    _write_trial(run_root, "obj-1", 0, produced=produced)
    manifest = _manifest("obj-1", 1, produced=produced)
    report = EvalScorer.score_run(manifest, (_objective("obj-1"),), run_root, seams=_seams(verify=verify, judge=judge))
    return report.trial_results[0]


def _explode(*_args: object, **_kwargs: object) -> object:
    """A seam that must never be called — proves the pass rule short-circuits before it."""
    raise AssertionError("this seam should not have been called")


def test_pass_rule_requires_all_three(tmp_path: Path) -> None:
    """A produced datapoint that fails verify OR fails quality is not solved; all three gates are needed."""
    # No datapoint produced → not solved, judge never reached.
    no_datapoint = _score_single(tmp_path / "a", produced=False, verify=_explode, judge=_explode)
    assert no_datapoint.task_solved is False
    assert no_datapoint.failure_reason == "no-datapoint"
    assert no_datapoint.quality is None

    # Produced but verify fails → not solved, and the judge is never called (short-circuit).
    failed_verify = _score_single(tmp_path / "b", produced=True, verify=lambda _task_dir: False, judge=_explode)
    assert failed_verify.task_solved is False
    assert failed_verify.failure_reason == "failed-verify"
    assert failed_verify.quality is None

    # Produced and verify passes but the judge fails → not solved.
    failed_quality = _score_single(
        tmp_path / "c", produced=True, verify=lambda _task_dir: True, judge=lambda _dp, _ctx: _verdict(passed=False)
    )
    assert failed_quality.task_solved is False
    assert failed_quality.failure_reason == "failed-quality"
    assert failed_quality.quality is not None and failed_quality.quality.overall_pass is False

    # All three pass → solved.
    solved = _score_single(
        tmp_path / "d", produced=True, verify=lambda _task_dir: True, judge=lambda _dp, _ctx: _verdict(passed=True)
    )
    assert solved.task_solved is True
    assert solved.failure_reason is None


# ───────────────────────────── the pass rate over k ─────────────────────────────


def test_pass_rate_over_k_trials(tmp_path: Path) -> None:
    """2 of 3 solved trials → pass_rate 0.67 for the objective (and 0.67 overall for the one objective)."""
    run_root = tmp_path / "run"
    for trial_index in range(3):
        _write_trial(run_root, "obj-1", trial_index, produced=True)
    manifest = _manifest("obj-1", 3, produced=True)

    # Verify fails only for trial-2 (by its path); the judge passes everything it reaches.
    seams = _seams(verify=lambda task_dir: "trial-2" not in str(task_dir), judge=lambda _dp, _ctx: _verdict(passed=True))
    report = EvalScorer.score_run(manifest, (_objective("obj-1"),), run_root, seams=seams)

    (objective_rate,) = report.per_objective
    assert objective_rate.trials == 3
    assert objective_rate.passed == 2
    assert round(objective_rate.pass_rate, 2) == 0.67
    assert objective_rate.failure_reasons == ("failed-verify",)
    assert round(report.overall_pass_rate, 2) == 0.67


# ───────────────────────────── the §8 pass bar ─────────────────────────────


def test_report_records_open_target_when_unset(tmp_path: Path) -> None:
    """With the §8 bar unset, the report records it as open with the straw man — never an invented number."""
    run_root = tmp_path / "run"
    _write_trial(run_root, "obj-1", 0, produced=True)
    manifest = _manifest("obj-1", 1, produced=True)
    seams = _seams(verify=lambda _task_dir: True, judge=lambda _dp, _ctx: _verdict(passed=True))
    report = EvalScorer.score_run(manifest, (_objective("obj-1"),), run_root, seams=seams)

    assert PASS_BAR.status == "open"
    assert report.pass_bar.status == "open"
    assert report.pass_bar.target is None
    assert report.pass_bar.straw_man == STRAW_MAN
    assert report.go_no_go.startswith("DEFERRED")
    assert STRAW_MAN in report.go_no_go

    markdown = ReportRenderer.render(report)
    assert "**open**" in markdown
    assert STRAW_MAN in markdown
    assert "DEFERRED" in markdown


def test_report_persistence_writes_markdown_and_json(tmp_path: Path) -> None:
    """The report writer lands both a readable <timestamp>.md and a re-scorable <timestamp>.json."""
    run_root = tmp_path / "run"
    _write_trial(run_root, "obj-1", 0, produced=True)
    manifest = _manifest("obj-1", 1, produced=True)
    seams = _seams(verify=lambda _task_dir: True, judge=lambda _dp, _ctx: _verdict(passed=True))
    report = EvalScorer.score_run(manifest, (_objective("obj-1"),), run_root, seams=seams)

    reports_dir = tmp_path / "reports"
    markdown_path = ReportPersistence.write(report, reports_dir)
    assert markdown_path == reports_dir / "20260905T000000Z.md"
    assert markdown_path.is_file()
    json_path = reports_dir / "20260905T000000Z.json"
    assert json.loads(json_path.read_text(encoding="utf-8"))["timestamp"] == "20260905T000000Z"


# ───────────────────────────── the default PR context ─────────────────────────────


def test_default_pr_context_derives_reviewed_state_from_kind() -> None:
    """The offline PR context maps reject→change-requested and approve→approved, and strips the repo host."""
    reject_context = default_pr_context(_objective(kind="reject"))
    assert reject_context.repo == "pydantic/pydantic"
    assert reject_context.reviewed_state == "change-requested"
    assert "re.Pattern" in reject_context.review_summary

    approve_context = default_pr_context(_objective(kind="approve"))
    assert approve_context.reviewed_state == "approved"
