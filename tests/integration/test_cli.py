"""Tests for the eval-harvest CLI dispatcher, refusal helper, and exit codes (task A-1)."""

import json
from pathlib import Path

import pytest

from eval_harvest.cli import Cli, ExitCode


class TestCliDispatch:
    """The top-level parser lists the verbs and rejects an unknown one distinctly."""

    def test_help_lists_every_verb(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            Cli.run(["--help"])
        assert exit_info.value.code == ExitCode.SUCCESS
        help_text = capsys.readouterr().out
        for verb in ("init", "survey", "capture", "show", "emit", "verify", "dataset"):
            assert verb in help_text

    def test_unknown_verb_exits_2(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            Cli.run(["not-a-verb"])
        assert exit_info.value.code == ExitCode.USAGE


class TestRefusalHelper:
    """refuse() emits the FR-2 four-field shape in both human and --json renderings."""

    def test_refuse_prints_four_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = Cli.refuse(
            check="no-review-iteration",
            datapoint="pr-42",
            offending="0 review rounds",
            next_="pick a PR that went through review",
            exit_code=ExitCode.REFUSAL,
        )
        assert exit_code == ExitCode.REFUSAL
        stderr = capsys.readouterr().err
        assert "no-review-iteration" in stderr
        assert "pr-42" in stderr
        assert "0 review rounds" in stderr
        assert "pick a PR that went through review" in stderr

    def test_refuse_json_shape(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = Cli.refuse(
            check="empty-oracle",
            datapoint="pr-7",
            offending="no defect findings",
            next_="use --kind approve or add a finding",
            exit_code=ExitCode.REFUSAL,
            json_mode=True,
        )
        assert exit_code == ExitCode.REFUSAL
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "ok": False,
            "failures": [
                {
                    "check": "empty-oracle",
                    "datapoint": "pr-7",
                    "offending": "no defect findings",
                    "next": "use --kind approve or add a finding",
                }
            ],
        }


class TestBatchMutualExclusion:
    """Batch and single inputs are mutually exclusive; both or neither is a usage error (exit 2).

    An ambiguous invocation silently preferring one input is the class of bug that makes a shell loop
    hard to reason about; the CLI refuses it before any forge or filesystem work.
    """

    def test_capture_rejects_pr_number_with_from_survey(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """`capture <pr> --from-survey <file>` names two inputs at once — a usage error, not a silent choice."""
        survey = tmp_path / "s.json"
        survey.write_text('{"prs": []}', encoding="utf-8")
        code = Cli.run(["capture", "42", "--from-survey", str(survey), "--clone", str(tmp_path)])
        assert code == ExitCode.USAGE
        assert "exactly one" in capsys.readouterr().err

    def test_capture_rejects_neither_pr_nor_from_survey(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """`capture` with neither a PR nor `--from-survey` is a usage error — there is nothing to capture."""
        code = Cli.run(["capture", "--clone", str(tmp_path)])
        assert code == ExitCode.USAGE
        assert "exactly one" in capsys.readouterr().err

    def test_emit_rejects_candidate_with_all(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """`emit <candidate> --all` names two inputs at once — a usage error, not a silent choice."""
        candidate = tmp_path / "pr-1.json"
        candidate.write_text("{}", encoding="utf-8")
        code = Cli.run(["emit", str(candidate), "--all", "--kind", "reject"])
        assert code == ExitCode.USAGE
        assert "exactly one" in capsys.readouterr().err

    def test_emit_rejects_neither_candidate_nor_all(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`emit` with neither a candidate nor `--all` is a usage error — there is nothing to emit."""
        code = Cli.run(["emit", "--kind", "reject"])
        assert code == ExitCode.USAGE
        assert "exactly one" in capsys.readouterr().err


class TestExitCodes:
    """The exit-code classes are distinct and returned exactly as declared (tech plan §9)."""

    def test_exit_codes_distinct_per_class(self) -> None:
        codes = [
            ExitCode.SUCCESS,
            ExitCode.USAGE,
            ExitCode.REFUSAL,
            ExitCode.VERIFICATION,
            ExitCode.RUNTIME_UNAVAILABLE,
        ]
        assert [int(code) for code in codes] == [0, 2, 3, 4, 5]
        assert len(set(codes)) == len(codes)
        for code in (ExitCode.REFUSAL, ExitCode.VERIFICATION, ExitCode.RUNTIME_UNAVAILABLE):
            assert Cli.refuse("check", "datapoint", "offending", "next", exit_code=code) == code
