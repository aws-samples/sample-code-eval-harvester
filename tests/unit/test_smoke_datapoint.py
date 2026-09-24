"""Offline tests for the oracle/nop gradeability guard's pure contract logic.

The guard's live half needs AWS, applied Terraform, and minutes of wall time, so it cannot live in
`mise run check`. But its *assertion* logic is a pure function over reward objects — the gradeability
defects it guards would each trip it — so it is unit-tested here with synthetic rewards, no AWS. The
fifth test drives the prerequisite check, which decides "run" vs. "exit non-zero, named" and must
never decide "skip".

The reward objects below match the shape `verifier_tpl/score.py` writes. Note the `oracle_*` keys are
non-zero even for `nop`: they are the *size of the reference set*, not an agent score — a subtlety it
is easy to get wrong, pinned by `test_contract_passes_a_healthy_oracle_nop_pair`.
"""

from __future__ import annotations

from pathlib import Path

from eval.smoke import SmokeContract, SmokeDatapoint

#: A reference set with 2 high / 5 medium / 3 low findings — the `oracle_*` sizes are task constants
#: that appear in both agents' rewards. Reused across the reward builders below.
_REFERENCE_SIZES = {"oracle_high": 2.0, "oracle_medium": 5.0, "oracle_low": 3.0}


def _nop_reward(**overrides: float) -> dict[str, float]:
    """A correct `nop` reward: it credited nothing, but still carries the reference-set sizes."""
    reward = {
        "coverage_required": 0.0,
        "coverage_all": 0.0,
        "precision_strict": 0.0,
        "precision_adjudicated": 0.0,
        "credited_required": 0.0,
        "credited_all": 0.0,
        "uncredited": 0.0,
        "credited_high": 0.0,
        "credited_medium": 0.0,
        "credited_low": 0.0,
        **_REFERENCE_SIZES,
    }
    reward.update(overrides)
    return reward


def _oracle_reward(**overrides: float) -> dict[str, float]:
    """A healthy `oracle` reward: it credited the whole reference set, so coverage is 1.0."""
    reward = {
        "coverage_required": 1.0,
        "coverage_all": 1.0,
        "precision_strict": 1.0,
        "precision_adjudicated": 1.0,
        "credited_required": 2.0,
        "credited_all": 10.0,
        "uncredited": 0.0,
        "credited_high": 2.0,
        "credited_medium": 5.0,
        "credited_low": 3.0,
        **_REFERENCE_SIZES,
    }
    reward.update(overrides)
    return reward


def test_contract_passes_a_healthy_oracle_nop_pair() -> None:
    """The positive control: a good oracle and a zero-credit nop pass — and the non-zero `oracle_*`
    reference counts must NOT be read as the nop scoring above zero."""
    failures = SmokeContract.evaluate(_oracle_reward(), _nop_reward(), oracle_exit_code=0, oracle_trial_errored=False)
    assert failures == []


def test_contract_passes_when_environment_records_no_exit_code() -> None:
    """A missing exit code (the Lambda env writes none) is not itself a failure when the trial did not
    raise — the crash signal is `exception_info`, and coverage still guards a silently broken oracle."""
    failures = SmokeContract.evaluate(_oracle_reward(), _nop_reward(), oracle_exit_code=None, oracle_trial_errored=False)
    assert failures == []


def test_smoke_script_rejects_errored_oracle_trial() -> None:
    """An oracle trial that raised (result.json exception_info set) fails even with a perfect reward —
    the crashing-agent-that-scored case, in the Lambda MicroVMs environment."""
    failures = SmokeContract.oracle_failures(_oracle_reward(), oracle_exit_code=None, oracle_trial_errored=True)
    assert any("exception" in failure for failure in failures)


def test_smoke_script_rejects_zero_coverage_oracle() -> None:
    """An oracle that scored no coverage fails — a broken datapoint."""
    failures = SmokeContract.evaluate(
        _oracle_reward(coverage_all=0.0), _nop_reward(), oracle_exit_code=0, oracle_trial_errored=False
    )
    assert any("coverage_all" in failure for failure in failures)


def test_smoke_script_rejects_nonzero_nop() -> None:
    """A `nop` scoring `precision_strict = 1.0` fails, caught on the whole object.

    A coverage-only assertion would miss this: a nop can score `coverage_all = 0.0` yet
    `precision_strict = 1.0`.
    """
    gameable_nop = _nop_reward(precision_strict=1.0, precision_adjudicated=1.0)
    failures = SmokeContract.nop_failures(gameable_nop)
    assert any("precision_strict" in failure for failure in failures)


def test_smoke_script_rejects_identical_rewards() -> None:
    """Byte-identical oracle and nop rewards fail — caught in one compare."""
    identical = _nop_reward()  # coverage 0, precision 0 — what both agents produced when nothing graded
    failures = SmokeContract.evaluate(identical, dict(identical), oracle_exit_code=0, oracle_trial_errored=False)
    assert any("identical" in failure for failure in failures)


def test_smoke_script_fails_on_nonzero_agent_exit() -> None:
    """An oracle that exited 1 fails even when its reward looks perfect — a crashing agent that scored.

    Harbor can report `n_errored_trials: 0` for an oracle that exited 1, so the reward alone cannot
    be trusted; the exit code is asserted separately.
    """
    failures = SmokeContract.oracle_failures(_oracle_reward(), oracle_exit_code=1, oracle_trial_errored=False)
    assert any("exited 1" in failure for failure in failures)


def test_smoke_script_fails_when_prerequisites_absent() -> None:
    """A missing credential yields a non-zero-exit reason, never a skip (the tech-plan:138 failure mode)."""
    environment = {"MICROVM_BUILD_ROLE_ARN": "arn:...", "MICROVM_EXECUTION_ROLE_ARN": "arn:..."}
    reason = SmokeDatapoint.missing_prerequisite(environment, harbor_executable="/usr/bin/harbor")
    assert reason is not None
    assert "MICROVM_BUCKET" in reason


def test_prerequisite_check_passes_when_everything_present() -> None:
    """With every env var set and harbor on PATH, the guard proceeds (returns no missing reason)."""
    environment = {name: "set" for name in ("MICROVM_BUCKET", "MICROVM_BUILD_ROLE_ARN", "MICROVM_EXECUTION_ROLE_ARN")}
    assert SmokeDatapoint.missing_prerequisite(environment, harbor_executable="/usr/bin/harbor") is None


def test_missing_harbor_runner_is_a_named_prerequisite() -> None:
    """A synced infra but no `harbor` on PATH is a named non-zero reason, not a skip."""
    environment = {name: "set" for name in ("MICROVM_BUCKET", "MICROVM_BUILD_ROLE_ARN", "MICROVM_EXECUTION_ROLE_ARN")}
    reason = SmokeDatapoint.missing_prerequisite(environment, harbor_executable=None)
    assert reason is not None
    assert "harbor" in reason


def test_coverage_required_may_be_one_when_task_has_no_required_findings() -> None:
    """On a task with no high-severity findings, nop's `coverage_required = 1.0` is correct, not a leak.

    `score.py`'s coverage helper returns 1.0 over an empty required set (nothing to miss); the guard
    must only hold `coverage_required` to zero when `oracle_high > 0`.
    """
    no_required_nop = _nop_reward(coverage_required=1.0, oracle_high=0.0, credited_high=0.0)
    assert SmokeContract.nop_failures(no_required_nop) == []


def test_locate_task_dir_matches_harbors_real_layout(tmp_path: Path) -> None:
    """`_locate_task_dir` finds Harbor's `<timestamp>/<task-slug>__<hash>/` dir, not a literal `task__*`.

    Pins the live-run bug: the per-task dir is named after the task slug (e.g.
    `aws-samples__…__qiM4MCZ`), so the old `*/task__*` glob matched nothing and mis-diagnosed a scored
    trial as a build failure.
    """
    task_dir = tmp_path / "2026-09-08__08-02-30" / "aws-samples__sample-autonomous-c__qiM4MCZ"
    (task_dir / "verifier").mkdir(parents=True)
    (task_dir / "agent").mkdir()
    (tmp_path / "2026-09-08__08-02-30" / "result.json").write_text("{}", encoding="utf-8")
    assert SmokeDatapoint._locate_task_dir(tmp_path) == task_dir


def test_trial_errored_reads_result_json_exception_info(tmp_path: Path) -> None:
    """`_read_trial_errored` is the Lambda env's crash signal: true iff `result.json` sets exception_info."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "result.json").write_text('{"exception_info": null}', encoding="utf-8")
    assert SmokeDatapoint._read_trial_errored(task_dir) is False
    (task_dir / "result.json").write_text('{"exception_info": {"type": "RuntimeError"}}', encoding="utf-8")
    assert SmokeDatapoint._read_trial_errored(task_dir) is True


def test_empty_reference_set_is_a_failure() -> None:
    """A task whose oracle carries no findings cannot grade — the guard rejects it."""
    empty_oracle = _oracle_reward(oracle_high=0.0, oracle_medium=0.0, oracle_low=0.0)
    failures = SmokeContract.reference_set_present_failures(empty_oracle)
    assert any("reference set is empty" in failure for failure in failures)
