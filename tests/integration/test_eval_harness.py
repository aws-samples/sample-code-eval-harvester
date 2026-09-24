"""Offline tests for the eval harness (Workstream G, G-1).

These exercise the harness wiring without a live Bedrock model or a MicroVM (G-1 Tests To Write):
the objective-spec model and its validation, the trajectory artifact's JSON round-trip, the
agent-under-test interface with a recorded/fake agent, and the end-to-end wiring
objective → run → captured artifact via a fake executor. They also assert the isolation S-18 rests
on — `eval/` imports `eval_harvest`, never the reverse — and that the eval Harbor task is
Harbor-valid with no methodology leaked into the agent's instruction.

Do NOT add a test that asserts a live agent's behaviour or spins a real MicroVM here (G-1): that is
the human-gated live run, not this suite.
"""

from __future__ import annotations

import ast
import json
import shlex
from pathlib import Path

import pytest
from pydantic import ValidationError

from eval.agent import ClaudeCodeAgent, ClaudeCodeReviewerAgent, RecordedAgent
from eval.catalog import ObjectiveCatalog
from eval.harbor_task import EvalHarborTask
from eval.models import AgentTurn, ExpectedProperties, Objective, ToolCall, TrialTrajectory
from eval.run import _AGENTS, EvalCommand
from eval.runner import EvalRunner, HarborRunResult

# The reviewer subclasses Harbor's ClaudeCode, so it only imports under the `eval` group. Guard it
# the way test_harbor_loader_contract.py guards Harbor: skip with a named reason, never error at
# collection, so `mise run check` (which does not sync `eval`) stays green and legible.
try:
    from eval.agents.claude_code_reviewer import REVIEW_SYSTEM_PROMPT, ClaudeCodeReviewer
except ImportError:
    HARBOR_IS_INSTALLED = False
else:
    HARBOR_IS_INSTALLED = True

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "eval_harvest"
_FULL_SHA_LENGTH = 40


def _objective(objective_id: str = "obj-1", kind: str = "reject") -> Objective:
    """A minimal valid objective for wiring tests."""
    verdict = "block" if kind == "reject" else "approve"
    return Objective.model_validate(
        {
            "id": objective_id,
            "repo_url": "https://github.com/pydantic/pydantic",
            "commit_sha": "0" * _FULL_SHA_LENGTH,
            "pr": 13611,
            "kind": kind,
            "expected_properties": {"expected_verdict": verdict, "minimum_findings": 1},
        }
    )


def _write_fake_run_dir(run_dir: Path, *, produced: bool = True) -> Path:
    """A synthetic Harbor run directory: a normalized trace, and (optionally) a produced task dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "turns": [
                    {"role": "user", "text": "build datapoints"},
                    {"role": "assistant", "text": "ran eval-harvest --help"},
                ],
                "tool_calls": [
                    {"name": "bash", "args": {"command": "eval-harvest survey --help"}, "result": "ok", "exit_code": 0}
                ],
                "wall_time_sec": 2.5,
            }
        ),
        encoding="utf-8",
    )
    if produced:
        task = run_dir / "produced" / "tasks" / "pydantic__pydantic__pr13611-reject"
        task.mkdir(parents=True)
        (task / "task.toml").write_text('schema_version = "1.4"\n', encoding="utf-8")
    return run_dir


# ───────────────────────────── objective model ─────────────────────────────


def test_catalog_loads_two_objectives_one_reject_one_approve() -> None:
    """The shipped catalog is exactly the 2 objectives G-1 requires, one of each kind, pinned by SHA."""
    objectives = ObjectiveCatalog.load_all()
    assert len(objectives) == 2
    assert {objective.kind for objective in objectives} == {"reject", "approve"}
    for objective in objectives:
        assert objective.repo_url.startswith("https://github.com/")
        assert len(objective.commit_sha) == _FULL_SHA_LENGTH


def test_objective_rejects_unknown_kind() -> None:
    """A kind outside {reject, approve} fails validation rather than reaching a trial."""
    with pytest.raises(ValidationError):
        Objective.model_validate(
            {
                "id": "x",
                "repo_url": "https://github.com/a/b",
                "commit_sha": "0" * _FULL_SHA_LENGTH,
                "pr": 1,
                "kind": "maybe",
                "expected_properties": {"expected_verdict": "block", "minimum_findings": 1},
            }
        )


def test_expected_properties_rejects_negative_minimum_findings() -> None:
    """`minimum_findings` is a count; a negative value is a malformed objective, caught at load."""
    with pytest.raises(ValidationError):
        ExpectedProperties.model_validate({"expected_verdict": "approve", "minimum_findings": -1})


# ───────────────────────────── trajectory (de)serialization ─────────────────────────────


def test_trajectory_round_trips_through_json() -> None:
    """A TrialTrajectory survives model_dump_json → model_validate_json unchanged (G-2/G-3 read it back)."""
    trajectory = TrialTrajectory(
        objective_id="o",
        trial_index=1,
        agent="claude-code",
        model="anthropic.claude-x",
        turns=(AgentTurn(role="user", text="hi"),),
        tool_calls=(ToolCall(name="bash", args={"command": "ls"}, result="ok", exit_code=0),),
        produced_task_dir="task",
        wall_time_sec=3.0,
        exit_code=0,
    )
    restored = TrialTrajectory.model_validate_json(trajectory.model_dump_json())
    assert restored == trajectory


# ───────────────────────────── agent-under-test interface ─────────────────────────────


def test_recorded_agent_is_available_without_credentials() -> None:
    """The fake agent needs no env, so the harness wiring can be driven offline."""
    agent = RecordedAgent()
    assert agent.required_env() == ()
    assert agent.is_available({}) is True


def test_claude_code_agent_reports_missing_bedrock_env() -> None:
    """The Claude Code agent names the Bedrock env it needs and is available once it is present."""
    agent = ClaudeCodeAgent()
    assert agent.harbor_agent == "claude-code"
    assert agent.missing_env({}) == ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION")
    assert agent.is_available({"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"}) is True


def test_claude_code_reviewer_agent_selects_the_custom_import_path() -> None:
    """The starter reviewer is passed to `harbor run -a` as its own import path, not a built-in name.

    Harbor's AgentFactory loads any `-a` value containing ':' as `module:Class`, so returning the
    import path is what makes the custom agent swap in for `nop`/`oracle` without a Harbor fork.
    """
    agent = ClaudeCodeReviewerAgent()
    assert agent.name == "claude-code-reviewer"
    assert agent.harbor_agent == "eval.agents.claude_code_reviewer:ClaudeCodeReviewer"
    # It reuses Claude Code on Bedrock, so it needs the same environment gate.
    assert agent.missing_env({}) == ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION")


def test_claude_code_reviewer_is_registered_and_selectable() -> None:
    """`mise run eval -a claude-code-reviewer` resolves: the short name is in the selectable set."""
    assert "claude-code-reviewer" in _AGENTS
    assert _AGENTS["claude-code-reviewer"] is ClaudeCodeReviewerAgent


@pytest.mark.skipif(not HARBOR_IS_INSTALLED, reason="the reviewer subclasses Harbor's ClaudeCode; needs the eval group")
def test_claude_code_reviewer_injects_the_review_prompt_as_one_shell_token(tmp_path: Path) -> None:
    """The custom review prompt reaches Claude Code intact as a single `--append-system-prompt` arg.

    Harbor builds the CLI flag string with no shell quoting (`f"{cli} {value}"`), so a multi-word
    prompt must be pre-quoted or it shatters the command. This guard fails loudly if either the
    quoting or Harbor's flag wiring regresses — an empty/split prompt is worse than none.
    """
    assert ClaudeCodeReviewer.name() == "claude-code-reviewer"
    reviewer = ClaudeCodeReviewer(logs_dir=tmp_path)
    tokens = shlex.split(reviewer.build_cli_flags())
    assert "--append-system-prompt" in tokens
    assert tokens[tokens.index("--append-system-prompt") + 1] == REVIEW_SYSTEM_PROMPT


# ───────────────────────────── the harness wiring ─────────────────────────────


def test_harness_wires_objective_run_and_captures_artifact(tmp_path: Path) -> None:
    """objective → run → captured artifact: a fake executor stands in for Bedrock + the MicroVM."""
    objective = _objective(objective_id="obj-1")
    agent = RecordedAgent()
    raw_root = tmp_path / "raw"

    def fake_executor(target: Objective, trial_index: int) -> HarborRunResult:
        run_dir = _write_fake_run_dir(raw_root / f"{target.id}-{trial_index}")
        return HarborRunResult(run_dir=run_dir, exit_code=0, wall_time_sec=2.5)

    manifest = EvalRunner.run_all(
        objectives=(objective,),
        agent=agent,
        model="anthropic.claude-x",
        output_root=tmp_path / "out",
        trials_per_objective=2,
        executor=fake_executor,
        timestamp="20260101T000000Z",
    )

    assert manifest.trials_per_objective == 2
    assert len(manifest.trials) == 2
    run_root = tmp_path / "out" / "20260101T000000Z"
    assert (run_root / "run.json").is_file()

    for trial_index in (0, 1):
        trajectory_path = run_root / "obj-1" / f"trial-{trial_index}" / "trajectory.json"
        assert trajectory_path.is_file()
        trajectory = TrialTrajectory.model_validate_json(trajectory_path.read_text(encoding="utf-8"))
        assert trajectory.objective_id == "obj-1"
        assert trajectory.trial_index == trial_index
        assert trajectory.agent == "recorded"
        assert trajectory.model == "anthropic.claude-x"
        assert len(trajectory.turns) == 2
        assert trajectory.tool_calls[0].name == "bash"
        assert trajectory.wall_time_sec == 2.5
        assert trajectory.produced_task_dir == "task"
        copied = (
            run_root / "obj-1" / f"trial-{trial_index}" / "task" / "tasks" / "pydantic__pydantic__pr13611-reject" / "task.toml"
        )
        assert copied.is_file()


def test_trial_without_produced_task_dir_records_none(tmp_path: Path) -> None:
    """A trial that produced no datapoint records produced_task_dir=None and keeps its exit code."""
    run_dir = _write_fake_run_dir(tmp_path / "raw", produced=False)
    raw = HarborRunResult(run_dir=run_dir, exit_code=5, wall_time_sec=1.0)
    trajectory = EvalRunner.normalize_trial(_objective(), 0, "recorded", "m", raw, tmp_path / "art")
    assert trajectory.produced_task_dir is None
    assert trajectory.exit_code == 5


# ───────────────────────────── the eval Harbor task ─────────────────────────────


def test_eval_harbor_task_is_harbor_valid_and_leaks_no_methodology(tmp_path: Path) -> None:
    """Each objective materializes to a Harbor-valid task whose instruction names no verdict/kind."""
    for objective in ObjectiveCatalog.load_all():
        # materialize() validates with Harbor's own validators and raises on any violation.
        destination = EvalHarborTask.materialize(objective, tmp_path / objective.id)
        assert (destination / "task.toml").is_file()

        instruction = (destination / "instruction.md").read_text(encoding="utf-8").lower()
        assert objective.repo_url.lower() in instruction
        assert f"#{objective.pr}" in instruction
        # The agent's only guidance is the repo + PR + "read the CLI's help": no methodology, no
        # datapoint kind, no verdict — naming any would test the prompt, not the CLI's docs (PRD §4).
        for banned in ("reject", "approve", "block", "request changes", "severity", "defect"):
            assert banned not in instruction

        dockerfile = (destination / "environment" / "Dockerfile").read_text(encoding="utf-8")
        assert objective.commit_sha in dockerfile
        assert "uv pip install --system -e" in dockerfile
        assert (destination / "environment" / "eval-harvest" / "pyproject.toml").is_file()


# ───────────────────────────── isolation (S-18 direction) ─────────────────────────────


def test_package_does_not_import_the_eval_harness() -> None:
    """Nothing under src/eval_harvest/ imports `eval` — the dependency runs one way (tech plan §3)."""
    offenders: dict[Path, set[str]] = {}
    for module_path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "verifier_tpl" in module_path.parts:
            continue
        imported: set[str] = set()
        for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imported.add(node.module)
        # `eval_harvest` starts with "eval" but is not the `eval` package; match only the package.
        eval_imports = {name for name in imported if name == "eval" or name.startswith("eval.")}
        if eval_imports:
            offenders[module_path] = eval_imports
    assert offenders == {}, f"package modules importing the eval harness: {offenders}"


# ───────────────────────────── clean skip (out of the gate) ─────────────────────────────


def test_eval_command_skips_cleanly_without_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no Bedrock credentials, `mise run eval` skips cleanly (exit 0), never a failure (§7)."""
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    exit_code = EvalCommand.main([])
    assert exit_code == 0
    assert "skipping" in capsys.readouterr().out
