"""Tests for the `annotate` verb and its pure merge core.

`annotate` is append-only per-comment fill: the agent streams small judgment records (a comment's
classification, a finding, the change-risk) and the CLI merges them into the candidate without the
agent ever reproducing the parts of the file it is not changing. These tests pin the two properties
that make that safe — the write is byte-identical to what `capture`/`Candidate.dump` would produce
(FR-10), and no judgment is ever inferred or defaulted (FR-9) — plus the refusal-before-write
guarantee that a malformed record never leaves a partially-applied candidate behind.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from eval_harvest.annotate import Annotate
from eval_harvest.candidate import Candidate, CandidateDict
from eval_harvest.cli import Cli, ExitCode


def _candidate() -> CandidateDict:
    """A captured-but-unfilled candidate: two comments, one recoverable iteration, blank judgment slots."""
    return {
        "repo": "our-org/our-repo",
        "pr_number": 1,
        "pr_url": "https://github.com/our-org/our-repo/pull/1",
        "iterations": [
            {"tip_sha": "aaaa", "base_sha": "bbbb", "patch_path": "patches/pr-1-iter0.patch", "comment_ids": [101, 102]}
        ],
        "review_verdicts": [],
        "comments": [
            {
                "id": 101,
                "body": "types drift",
                "path": "a.py",
                "line_start": 10,
                "line_end": 12,
                "author_role": "MEMBER",
                "created_at": "2026-01-01T00:00:00Z",
                "iteration_index": 0,
                "in_diff_iterations": [0],
                "classification": "",
                "classification_rationale": "",
            },
            {
                "id": 102,
                "body": "nit: rename",
                "path": "a.py",
                "line_start": 20,
                "line_end": 20,
                "author_role": "MEMBER",
                "created_at": "2026-01-01T00:01:00Z",
                "iteration_index": 0,
                "in_diff_iterations": [0],
                "classification": "",
                "classification_rationale": "",
            },
        ],
        "findings": [],
        "change_risk": {
            "risk_structural": "medium",
            "risk_structural_rule": "default",
            "risk_classified": "",
            "risk_classified_rationale": "",
        },
        "rubric_version": "",
    }


def _filled_candidate() -> CandidateDict:
    """A coherent, fully-filled candidate: every comment classified, the risk and rubric pinned."""
    candidate = _candidate()
    for comment in candidate["comments"]:
        comment["classification"] = "nit"
        comment["classification_rationale"] = "style only"
    candidate["change_risk"]["risk_classified"] = "low"
    candidate["change_risk"]["risk_classified_rationale"] = "docs only"
    candidate["rubric_version"] = "v1"
    return candidate


def _valid_finding(reference_only: bool = False) -> dict[str, Any]:
    """A finding record carrying every field the FR-15 gate requires."""
    return {
        "finding": {
            "comment_ids": [101],
            "statement": "types drift apart",
            "severity": "high",
            "severity_evidence": "a.py:10-12",
            "severity_rationale": "rubric: correctness",
            "reference_only": reference_only,
        }
    }


def _write(tmp_path: Path, candidate: CandidateDict) -> Path:
    """Write a candidate to a dataset-shaped path and return it."""
    path = tmp_path / "candidates" / "pr-1.json"
    Candidate.dump(candidate, path)
    return path


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def _run_stdin(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    records: list[dict[str, Any]],
    *,
    extra: tuple[str, ...] = (),
    json_mode: bool = False,
) -> int:
    """Run `annotate <path> --stdin` feeding `records` as JSONL on stdin, the way an agent would."""
    monkeypatch.setattr("sys.stdin", io.StringIO(_jsonl(records)))
    top = ["--json"] if json_mode else []
    return Cli.run([*top, "annotate", str(path), "--stdin", *extra])


class TestAnnotateMerge:
    """A record sets exactly its slot and nothing else, and the write stays byte-stable (FR-9, FR-10)."""

    def test_annotate_sets_only_the_named_comment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _write(tmp_path, _candidate())
        before = Candidate.load(path)
        code = _run_stdin(monkeypatch, path, [{"comment": 101, "classification": "defect", "rationale": "real bug"}])
        assert code == ExitCode.SUCCESS
        after = Candidate.load(path)
        assert after["comments"][0]["classification"] == "defect"
        assert after["comments"][0]["classification_rationale"] == "real bug"
        # Every other section is byte-for-byte what it was.
        assert after["comments"][1] == before["comments"][1]
        assert after["iterations"] == before["iterations"]
        assert after["findings"] == before["findings"]
        assert after["change_risk"] == before["change_risk"]

    def test_annotate_output_is_byte_identical_to_capture_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        candidate = _candidate()
        path = _write(tmp_path, candidate)
        records = [{"comment": 101, "classification": "defect", "rationale": "real bug"}, _valid_finding()]
        merged, violations = Annotate.apply_records(candidate, records)
        assert not violations
        expected = Candidate.dumps(merged)
        assert _run_stdin(monkeypatch, path, records) == ExitCode.SUCCESS
        assert path.read_bytes() == expected

    def test_annotate_overwrite_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        candidate = _candidate()
        candidate["comments"][0]["classification"] = "nit"
        path = _write(tmp_path, candidate)
        code = _run_stdin(monkeypatch, path, [{"comment": 101, "classification": "defect", "rationale": "re-read"}])
        assert code == ExitCode.SUCCESS
        assert "overwritten" in capsys.readouterr().out
        assert "101" in Candidate.dumps(Candidate.load(path)).decode()  # sanity: still present

    def test_annotate_appends_findings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _write(tmp_path, _candidate())
        first = _valid_finding()
        second = _valid_finding()
        second["finding"]["statement"] = "second finding"
        assert _run_stdin(monkeypatch, path, [first, second]) == ExitCode.SUCCESS
        findings = Candidate.load(path)["findings"]
        assert [f["statement"] for f in findings] == ["types drift apart", "second finding"]

    def test_annotate_sets_risk_and_rubric(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _write(tmp_path, _candidate())
        records: list[dict[str, Any]] = [
            {"risk_classified": "high", "rationale": "cross-package contract"},
            {"rubric_version": "v1"},
        ]
        assert _run_stdin(monkeypatch, path, records) == ExitCode.SUCCESS
        after = Candidate.load(path)
        assert after["change_risk"]["risk_classified"] == "high"
        assert after["change_risk"]["risk_classified_rationale"] == "cross-package contract"
        assert after["rubric_version"] == "v1"

    def test_annotate_replace_findings_clears_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        candidate = _candidate()
        candidate["findings"] = [
            {
                "comment_ids": [102],
                "statement": "old",
                "severity": "low",
                "severity_evidence": "x",
                "severity_rationale": "y",
                "reference_only": False,
            }
        ]
        path = _write(tmp_path, candidate)
        assert _run_stdin(monkeypatch, path, [_valid_finding()], extra=("--replace-findings",)) == ExitCode.SUCCESS
        findings = Candidate.load(path)["findings"]
        assert [f["statement"] for f in findings] == ["types drift apart"]


class TestAnnotateRefusals:
    """A malformed or inapplicable record refuses at exit 3 and leaves the file byte-identical."""

    def test_annotate_refuses_unknown_comment_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, _candidate())
        before = path.read_bytes()
        code = _run_stdin(monkeypatch, path, [{"comment": 999, "classification": "defect", "rationale": "x"}])
        assert code == ExitCode.REFUSAL
        assert "unknown-comment" in capsys.readouterr().err
        assert path.read_bytes() == before

    def test_annotate_refuses_unrecognised_record_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, _candidate())
        before = path.read_bytes()
        code = _run_stdin(monkeypatch, path, [{"not_a_known_key": "value"}])
        assert code == ExitCode.REFUSAL
        err = capsys.readouterr().err
        assert "unrecognised-record" in err
        assert "1" in err  # names the offending record's 1-based position
        assert path.read_bytes() == before

    def test_annotate_writes_nothing_on_violation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _write(tmp_path, _candidate())
        before = path.read_bytes()
        bad = _valid_finding()
        bad["finding"]["severity"] = "critical"  # not in the taxonomy
        assert _run_stdin(monkeypatch, path, [bad]) == ExitCode.REFUSAL
        assert path.read_bytes() == before

    def test_annotate_never_infers_a_judgment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _write(tmp_path, _candidate())
        before = path.read_bytes()
        missing_severity = _valid_finding()
        del missing_severity["finding"]["severity"]
        assert _run_stdin(monkeypatch, path, [missing_severity]) == ExitCode.REFUSAL
        assert path.read_bytes() == before

    def test_annotate_malformed_json_names_the_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, _candidate())
        before = path.read_bytes()
        text = '{"comment": 101, "classification": "defect", "rationale": "ok"}\n{ this is not json\n'
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        code = Cli.run(["annotate", str(path), "--stdin"])
        assert code == ExitCode.REFUSAL
        assert "2" in capsys.readouterr().err  # the 1-based line number of the broken record
        assert path.read_bytes() == before


class TestAnnotateCheck:
    """`--check` validates without writing: exit 3 while slots remain, exit 0 when coherent."""

    def test_annotate_check_reports_remaining_slots_exit_3(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = _write(tmp_path, _candidate())
        before = path.read_bytes()
        code = Cli.run(["annotate", str(path), "--check"])
        assert code == ExitCode.REFUSAL
        assert "remaining slots" in capsys.readouterr().out.lower()
        assert path.read_bytes() == before  # --check writes nothing

    def test_annotate_check_exits_0_when_fully_filled(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _filled_candidate())
        assert Cli.run(["annotate", str(path), "--check"]) == ExitCode.SUCCESS


class TestAnnotateInputSources:
    """Exactly one input source, unless `--check` (which may validate the file as it stands)."""

    def test_neither_source_without_check_is_usage_error(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _candidate())
        assert Cli.run(["annotate", str(path)]) == ExitCode.USAGE

    def test_from_json_array_merges(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _candidate())
        records = [{"comment": 101, "classification": "defect", "rationale": "bug"}]
        source = tmp_path / "records.json"
        source.write_text(json.dumps(records), encoding="utf-8")
        assert Cli.run(["annotate", str(path), "--from-json", str(source)]) == ExitCode.SUCCESS
        assert Candidate.load(path)["comments"][0]["classification"] == "defect"

    def test_json_output_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        path = _write(tmp_path, _candidate())
        code = _run_stdin(monkeypatch, path, [{"comment": 101, "classification": "defect", "rationale": "bug"}], json_mode=True)
        assert code == ExitCode.SUCCESS
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["classified"] == 1
        assert payload["findings_added"] == 0
        assert "remaining_slots" in payload


class TestAnnotatePureCore:
    """`apply_records` is a pure function: it returns a new candidate and never mutates the input."""

    def test_apply_records_does_not_mutate_input(self) -> None:
        candidate = _candidate()
        snapshot = Candidate.dumps(candidate)
        merged, violations = Annotate.apply_records(candidate, [{"comment": 101, "classification": "defect", "rationale": "x"}])
        assert not violations
        assert Candidate.dumps(candidate) == snapshot  # original untouched
        assert merged["comments"][0]["classification"] == "defect"

    def test_remaining_slots_counts_unclassified_comments(self) -> None:
        slots = Annotate.remaining_slots(_candidate())
        assert any("2 of 2" in slot and "unclassified" in slot for slot in slots)
        assert not Annotate.remaining_slots(_filled_candidate())
