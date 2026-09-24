"""Tests for batch mode of `capture` and `emit`, and the containment engine behind it.

The job is ten PRs, not one, so a driver otherwise writes a shell loop — where a quoting bug can
produce junk `emit` runs and a wrong conclusion
drawn from the harness, not the CLI. Batch mode moves the iteration into the CLI. These tests pin the
properties that make that safe: the loop contains one item's refusal *and* one item's unexpected
exception so neither aborts the rest (FR-2, FR-11), the batch order is the survey payload's order and
a batch is byte-identical to N individual runs (FR-10), a partial batch exits 3 rather than looking
successful, an interrupt is reported rather than swallowed, and `--json` stdout stays parseable while
progress goes to stderr.

The containment tests drive :class:`Batch` with synthetic workers so they exercise the loop and not
argparse, so the containment logic is what is under test. The `capture`/`emit` tests drive the built CLI over the
offline S-1 fixture so the batch path is proven against the real verbs it wraps.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
from fixtures.repo_builder import PullRequestFixture, RepoBuilder

from eval_harvest.batch import Batch, BatchReport, ItemResult
from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.cli import Cli, ExitCode
from eval_harvest.forge import Forge
from eval_harvest.survey import Survey
from eval_harvest.tomlw import emit_document

# ───────────────────────────── the containment engine (synthetic workers) ─────────────────────────────


def _ok(key: str) -> ItemResult:
    """A succeeded item carrying a trivial single-item payload and a progress detail."""
    return ItemResult.success(key, _pr(key), payload={"candidate": f"candidates/{key}.json"}, detail=f"→ {key}.json")


def _refused(key: str, *, check: str = "no-emittable-iteration") -> ItemResult:
    """A refused item in the four-field shape, its datapoint the item's own key."""
    return ItemResult.refuse(key, _pr(key), check=check, offending=f"{key} has no recoverable tip", next_="pick another PR")


def _pr(key: str) -> int:
    """The PR number encoded in a ``pr-<n>`` key."""
    return int(key.removeprefix("pr-"))


def _describe(key: str) -> tuple[str, int | None]:
    """The identity a contained exception is reported under — the item is its own key here."""
    return key, _pr(key)


def _run(worker: object, keys: list[str]) -> tuple[BatchReport, list[tuple[int, int, ItemResult]]]:
    """Run ``worker`` over ``keys`` and also record every progress call, so a test can assert on both."""
    progress: list[tuple[int, int, ItemResult]] = []
    report = Batch.run(keys, worker, _describe, on_progress=lambda i, n, r: progress.append((i, n, r)))  # type: ignore[arg-type]
    return report, progress


class TestBatchContainment:
    """`Batch.run` contains one item's failure so it never aborts the others — the whole point of batch mode."""

    def test_batch_continues_after_one_refusal(self) -> None:
        """A refusal on the second of three items does not stop the third — the failure mode of `set -e`."""
        report, _ = _run(lambda key: _refused(key) if key == "pr-2" else _ok(key), ["pr-1", "pr-2", "pr-3"])
        assert report.attempted == 3, "every item must run even after one refuses"
        assert [r.key for r in report.results] == ["pr-1", "pr-2", "pr-3"]
        assert report.succeeded == 2 and report.refused == 1

    def test_batch_reports_each_refusal_with_its_item(self) -> None:
        """Every refusal names its own PR and check — an unattributable refusal is what made the junk runs unreadable."""

        def worker(key: str) -> ItemResult:
            return _refused(key, check=f"check-{_pr(key)}") if _pr(key) % 2 == 0 else _ok(key)

        report, _ = _run(worker, ["pr-1", "pr-2", "pr-4"])
        refusals = report.refusals()
        seen = [(r.key, r.pr_number, r.require_refusal().check) for r in refusals]
        assert seen == [("pr-2", 2, "check-2"), ("pr-4", 4, "check-4")]

    def test_batch_contains_unexpected_exception_as_a_refusal(self) -> None:
        """A worker that *raises* (a bug, not a refusal path) is contained per item, not left to abort the batch."""

        def worker(key: str) -> ItemResult:
            if key == "pr-2":
                raise RuntimeError("forge round-trip blew up")
            return _ok(key)

        report, _ = _run(worker, ["pr-1", "pr-2", "pr-3"])
        assert report.attempted == 3 and report.succeeded == 2
        contained = report.refusals()[0]
        assert contained.pr_number == 2
        refusal = contained.require_refusal()
        assert refusal.check == "unexpected-error"
        assert "RuntimeError" in refusal.offending and "blew up" in refusal.offending

    def test_batch_interrupt_reports_partial_and_exits_nonzero(self) -> None:
        """Ctrl-C stops the batch, reports what completed, and marks the plan unfinished — never a finished-looking one."""

        def worker(key: str) -> ItemResult:
            if key == "pr-3":
                raise KeyboardInterrupt
            return _ok(key)

        report, progress = _run(worker, ["pr-1", "pr-2", "pr-3", "pr-4"])
        assert report.interrupted is True
        assert report.attempted == 2 and report.total == 4, "the interrupted item and the rest are not attempted"
        assert report.clean is False, "a half-finished batch must not read as a clean one (→ non-zero exit)"
        assert len(progress) == 2, "no progress line is printed for the interrupted item"

    def test_progress_sink_fires_once_per_completed_item_in_order(self) -> None:
        """The sink is called exactly once per completed item, in run order, with (index, total)."""
        _, progress = _run(_ok, ["pr-1", "pr-2", "pr-3"])
        assert [(i, n, r.key) for i, n, r in progress] == [(1, 3, "pr-1"), (2, 3, "pr-2"), (3, 3, "pr-3")]


class TestBatchExitClass:
    """`clean` is the sole exit-0 condition; anything else is a refusal (exit 3), never a new code."""

    def test_batch_all_success_is_clean(self) -> None:
        """A batch where every planned item succeeded is clean → exit 0. A gate that can never be green is useless."""
        report, _ = _run(_ok, ["pr-1", "pr-2"])
        assert report.clean is True

    def test_batch_partial_is_not_clean(self) -> None:
        """One refusal plus one success is not clean → exit 3. A partial batch must not look successful."""
        report, _ = _run(lambda key: _refused(key) if key == "pr-2" else _ok(key), ["pr-1", "pr-2"])
        assert report.clean is False

    def test_batch_all_refused_is_not_clean(self) -> None:
        """Every item refused is also exit 3 — the same refusal class, no separate all-refused code."""
        report, _ = _run(_refused, ["pr-1", "pr-2"])
        assert report.clean is False and report.succeeded == 0


class TestBatchJsonShapes:
    """`--json` reuses the single-item payload per item and adds one `summary` — one schema for a driver."""

    def test_json_items_reuse_single_item_shape(self) -> None:
        """A success item's JSON is its single-item payload verbatim plus identity — no new per-item fields."""
        payload = {"candidate": "candidates/pr-5.json", "emittable": ["reject"], "slots": 3}
        item = ItemResult.success("pr-5", 5, payload=payload, detail="→ candidates/pr-5.json")
        assert item.as_json() == {"pr_number": 5, "ok": True, **payload}

    def test_json_refusal_reuses_the_single_item_failures_shape(self) -> None:
        """A refusal item's JSON carries a `failures` list in the exact shape `Cli.refuse` emits for one PR."""
        item = ItemResult.refuse("pr-6", 6, check="no-emittable-iteration", offending="no recoverable tip", next_="pick another")
        assert item.as_json() == {
            "pr_number": 6,
            "ok": False,
            "failures": [
                {
                    "check": "no-emittable-iteration",
                    "datapoint": "pr-6",
                    "offending": "no recoverable tip",
                    "next": "pick another",
                }
            ],
        }

    def test_summary_json_counts_and_refusals(self) -> None:
        """The summary carries the counts and one entry per refused item; `interrupted` is present only when it happened."""
        report, _ = _run(lambda key: _refused(key, check="boom") if key == "pr-2" else _ok(key), ["pr-1", "pr-2"])
        summary = report.summary_json()
        assert summary == {
            "attempted": 2,
            "succeeded": 1,
            "refused": 1,
            "refusals": [{"pr_number": 2, "check": "boom"}],
        }
        assert "interrupted" not in summary, "a non-interrupted batch keeps the minimal summary shape"


# ───────────────────────────── the survey-driven capture target selection (pure) ─────────────────────────────


def _payload(*rows: dict[str, object]) -> dict[str, object]:
    """A minimal `survey --json` payload carrying just the fields the batch target selection reads."""
    return {"repo": "acme/widgets", "mainline_commits": 1, "prs": list(rows)}


def _row(number: int, *, blockers: list[str]) -> dict[str, object]:
    """One survey row: harvestable iff merged with no blocker."""
    return {"number": number, "state": "MERGED", "blockers": blockers, "review_rounds": 2}


def _write_survey(path: Path, *rows: dict[str, object]) -> Path:
    """Write a survey payload to disk and return its path — the input `capture --from-survey` reads."""
    path.write_text(json.dumps(_payload(*rows)), encoding="utf-8")
    return path


def _targets(payload_path: Path, *, limit: int | None = None, pr_filter: list[int] | None = None) -> list[int] | None:
    """Resolve the capture batch's target PR numbers the way `handle_capture` does, refusals and all."""
    parsed = Survey.load_payload(payload_path)
    args = Namespace(limit=limit, pr_filter=pr_filter or [])
    return Cli._survey_capture_targets(parsed, args, payload_path, json_mode=False)


class TestSurveyCaptureTargets:
    """`capture --from-survey` attempts exactly the harvestable PRs the payload lists, in payload order."""

    def test_capture_from_survey_captures_clean_prs_in_order(self, tmp_path: Path) -> None:
        """Targets follow the payload's order, and `--limit` takes a prefix of it — pinning FR-10 determinism."""
        survey = _write_survey(tmp_path / "s.json", _row(523, blockers=[]), _row(596, blockers=[]), _row(674, blockers=[]))
        assert _targets(survey) == [523, 596, 674]
        assert _targets(survey, limit=2) == [523, 596], "--limit is a prefix of payload order, not an arbitrary subset"

    def test_capture_from_survey_skips_blocked_prs(self, tmp_path: Path) -> None:
        """A blocked PR is never a target — no forge call wasted on a PR `survey` already ruled out."""
        survey = _write_survey(tmp_path / "s.json", _row(1, blockers=["no-test-change"]), _row(2, blockers=[]))
        assert _targets(survey) == [2]

    def test_pr_filter_restricts_and_stays_in_payload_order(self, tmp_path: Path) -> None:
        """`--pr` narrows to the named PRs but keeps them in payload order, not the order they were passed."""
        survey = _write_survey(tmp_path / "s.json", _row(10, blockers=[]), _row(20, blockers=[]), _row(30, blockers=[]))
        assert _targets(survey, pr_filter=[30, 10]) == [10, 30]

    def test_unknown_pr_in_filter_refuses(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A `--pr` naming a number the payload does not list is a typo, refused before any forge round-trip."""
        survey = _write_survey(tmp_path / "s.json", _row(10, blockers=[]))
        assert _targets(survey, pr_filter=[99]) is None
        assert "unknown-pr" in capsys.readouterr().err

    def test_no_harvestable_pr_refuses(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An all-blocked payload refuses `no-clean-pr` rather than running an empty, misleading exit-0 batch."""
        survey = _write_survey(tmp_path / "s.json", _row(1, blockers=["no-test-change"]))
        assert _targets(survey) is None
        assert "no-clean-pr" in capsys.readouterr().err


# ───────────────────────────── capture batch over the offline fixture ─────────────────────────────


_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}


@pytest.fixture
def fixture(tmp_path: Path) -> PullRequestFixture:
    """The S-1 squash-merge PR, reconstructed offline — the same fixture `emit`'s tests build against."""
    return RepoBuilder.build_squash_merged_pull_request(tmp_path)


def _mock_forge_capture(monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture) -> None:
    """Replay `Forge.capture` from the fixture's recorded reviews/comments/timeline for any requested PR number.

    Reconstructing with the *requested* PR number gives each batch item its own `pr-<n>.json` while
    reusing one fixture's commits, so a multi-PR batch is exercised without a forge or network call.
    """

    def replay(repo: str, pr_number: int, clone: Path, base_ref: str = "HEAD") -> object:
        return Forge.reconstruct_facts(
            pr_number,
            fixture.clone,
            fixture.base_ref,
            reviews=fixture.reviews,
            comments=fixture.comments,
            timeline=fixture.timeline,
        )

    monkeypatch.setattr(Forge, "capture", staticmethod(replay))


def _dataset(tmp_path: Path, name: str) -> Path:
    """A dataset directory holding a valid `risk-map.toml`, ready for `capture` to write candidates into."""
    dataset = tmp_path / name
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(emit_document(_RISK_MAP))
    (dataset / "rubric.md").write_text("---\nversion: v1\n---\n# Review rubric\n", encoding="utf-8")
    return dataset


class TestCaptureBatchCli:
    """`capture --from-survey` over the real verb: it captures every clean PR and is byte-identical to N single runs."""

    def test_batch_captures_every_clean_pr_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A three-PR survey writes exactly three candidates; the progress lines name them in payload order."""
        _mock_forge_capture(monkeypatch, fixture)
        dataset = _dataset(tmp_path, "ds")
        survey = _write_survey(tmp_path / "s.json", _row(11, blockers=[]), _row(22, blockers=[]), _row(33, blockers=[]))

        code = Cli.run(["capture", "--from-survey", str(survey), "--clone", str(fixture.clone), "--dataset", str(dataset)])

        assert code == ExitCode.SUCCESS
        assert sorted(p.name for p in (dataset / "candidates").glob("*.json")) == ["pr-11.json", "pr-22.json", "pr-33.json"]
        err = capsys.readouterr().err
        assert err.index("pr-11") < err.index("pr-22") < err.index("pr-33"), "progress lines follow payload order"

    def test_batch_output_matches_individual_invocations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture
    ) -> None:
        """A PR captured in a batch is byte-identical to the same PR captured alone (FR-10) — one code path, not two."""
        _mock_forge_capture(monkeypatch, fixture)
        single_ds = _dataset(tmp_path, "single")
        batch_ds = _dataset(tmp_path, "batch")
        survey = _write_survey(tmp_path / "s.json", _row(11, blockers=[]))

        single_code = Cli.run(["capture", "11", "--clone", str(fixture.clone), "--dataset", str(single_ds)])
        batch_code = Cli.run(["capture", "--from-survey", str(survey), "--clone", str(fixture.clone), "--dataset", str(batch_ds)])

        assert single_code == ExitCode.SUCCESS and batch_code == ExitCode.SUCCESS
        assert _tree(single_ds / "candidates") == _tree(batch_ds / "candidates"), "candidate bytes must match either way"
        assert _tree(single_ds / "patches") == _tree(batch_ds / "patches"), "materialized patch bytes must match either way"

    def test_limit_takes_a_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture) -> None:
        """`--limit 1` over a two-PR survey writes exactly the first candidate — the prefix, deterministically."""
        _mock_forge_capture(monkeypatch, fixture)
        dataset = _dataset(tmp_path, "ds")
        survey = _write_survey(tmp_path / "s.json", _row(11, blockers=[]), _row(22, blockers=[]))

        code = Cli.run(
            ["capture", "--from-survey", str(survey), "--limit", "1", "--clone", str(fixture.clone), "--dataset", str(dataset)]
        )

        assert code == ExitCode.SUCCESS
        assert [p.name for p in (dataset / "candidates").glob("*.json")] == ["pr-11.json"]


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` keyed by POSIX-relative path — the golden set for byte-equality."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _emitted_tasks(dataset: Path) -> list[Path]:
    """The task directories `emit` promoted under `<dataset>/tasks/` — one per written datapoint."""
    tasks = dataset / "tasks"
    return [p for p in tasks.iterdir() if p.is_dir()] if tasks.is_dir() else []


# ───────────────────────────── emit batch over the offline fixture ─────────────────────────────


def _substantive_findings() -> list[FindingDict]:
    """A reject oracle: one high defect and one medium, bound to the fixture's inline comments."""
    return [
        {
            "comment_ids": [9001],
            "statement": "`add` subtracts instead of adds",
            "severity": "high",
            "severity_evidence": "return a - b in the round-1 diff",
            "severity_rationale": "wrong result for every caller",
            "reference_only": False,
        },
        {
            "comment_ids": [9002],
            "statement": "loop runs one index past the end",
            "severity": "medium",
            "severity_evidence": "range(len(items) + 1)",
            "severity_rationale": "IndexError on the last iteration",
            "reference_only": False,
        },
    ]


def _fill(candidate_path: Path, findings: list[FindingDict]) -> None:
    """Classify every comment, set the findings, classify the risk, and pin the rubric — a `reject`-ready candidate."""
    candidate: CandidateDict = Candidate.load(candidate_path)
    for comment in candidate["comments"]:
        comment["classification"] = "defect"
        comment["classification_rationale"] = "a substantive problem a reviewer would block on"
    candidate["findings"] = findings
    candidate["change_risk"]["risk_classified"] = "low"
    candidate["change_risk"]["risk_classified_rationale"] = "isolated helper, well covered by tests"
    candidate["rubric_version"] = "v1"
    Candidate.dump(candidate, candidate_path)


def _dataset_with_filled_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: PullRequestFixture,
    pr_numbers: list[int],
    *,
    break_last: bool = False,
) -> Path:
    """A dataset with one filled `reject`-ready candidate per PR number (the last left empty if ``break_last``).

    Captures each PR through the mocked forge, then fills its judgment slots. A broken last candidate
    (empty findings) makes `emit --all --kind reject` refuse exactly one item — the partial-batch case.
    """
    _mock_forge_capture(monkeypatch, fixture)
    dataset = _dataset(tmp_path, "ds")
    survey = _write_survey(tmp_path / "s.json", *[_row(n, blockers=[]) for n in pr_numbers])
    assert (
        Cli.run(["capture", "--from-survey", str(survey), "--clone", str(fixture.clone), "--dataset", str(dataset)])
        == ExitCode.SUCCESS
    )
    for index, pr_number in enumerate(pr_numbers):
        is_last = index == len(pr_numbers) - 1
        findings: list[FindingDict] = [] if (break_last and is_last) else _substantive_findings()
        _fill(dataset / "candidates" / f"pr-{pr_number}.json", findings)
    return dataset


class TestEmitBatchCli:
    """`emit --all` over the real verb: every candidate is emitted, the summary matches disk, and exit codes are honest."""

    def test_emit_all_emits_every_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture
    ) -> None:
        """Every candidate under `candidates/` is emitted — a batch must not stop at the first alphabetical gap."""
        dataset = _dataset_with_filled_candidates(tmp_path, monkeypatch, fixture, [11, 22, 33])

        code = Cli.run(["emit", "--all", "--kind", "reject", "--dataset", str(dataset)])

        assert code == ExitCode.SUCCESS
        assert len(_emitted_tasks(dataset)) == 3, "one task directory per candidate"

    def test_batch_all_success_exits_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture
    ) -> None:
        """A batch where every candidate is written exits 0."""
        dataset = _dataset_with_filled_candidates(tmp_path, monkeypatch, fixture, [11, 22])
        assert Cli.run(["emit", "--all", "--kind", "reject", "--dataset", str(dataset)]) == ExitCode.SUCCESS

    def test_batch_partial_exits_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture) -> None:
        """One refusal plus one success exits 3 — a partial batch that looks successful loses the refusal."""
        dataset = _dataset_with_filled_candidates(tmp_path, monkeypatch, fixture, [11, 22], break_last=True)

        code = Cli.run(["emit", "--all", "--kind", "reject", "--dataset", str(dataset)])

        assert code == ExitCode.REFUSAL
        assert len(_emitted_tasks(dataset)) == 1, "the succeeded item still landed; nothing rolled back"

    def test_batch_summary_counts_match_files_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The summary's counts equal what is on disk — a summary that reports intent rather than outcome hides drift."""
        dataset = _dataset_with_filled_candidates(tmp_path, monkeypatch, fixture, [11, 22, 33], break_last=True)

        Cli.run(["emit", "--all", "--kind", "reject", "--dataset", str(dataset)])

        out = capsys.readouterr().out
        written = len(_emitted_tasks(dataset))
        assert f"3 attempted · {written} written · 1 refused" in out
        assert written == 2, "two candidates were fillable; the third was broken"

    def test_batch_json_items_reuse_single_item_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A batch item's JSON is the single-candidate `emit --json` payload plus identity — one schema for a driver."""
        single_ds = _dataset_with_filled_candidates(tmp_path, monkeypatch, fixture, [11])
        capsys.readouterr()  # drop the capture step's stdout so only the emit --json payload remains
        Cli.run(["--json", "emit", str(single_ds / "candidates" / "pr-11.json"), "--kind", "reject"])
        single_payload = json.loads(capsys.readouterr().out)

        batch_root = tmp_path / "b"
        batch_root.mkdir()
        batch_ds = _dataset_with_filled_candidates(batch_root, monkeypatch, fixture, [11])
        capsys.readouterr()
        Cli.run(["--json", "emit", "--all", "--kind", "reject", "--dataset", str(batch_ds)])
        batch = json.loads(capsys.readouterr().out)

        assert set(batch) == {"items", "summary"}
        item = batch["items"][0]
        # The item reuses the single-item payload verbatim; the task path differs only by dataset root.
        assert set(item) == {"pr_number", *single_payload.keys()}
        assert item["ok"] == single_payload["ok"] and item["checks"] == single_payload["checks"]

    def test_batch_progress_goes_to_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: PullRequestFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--json` stdout parses cleanly as one object; the per-item progress lines are on stderr only."""
        dataset = _dataset_with_filled_candidates(tmp_path, monkeypatch, fixture, [11, 22])
        capsys.readouterr()  # drop the capture step's output so stdout holds only the emit --json payload

        Cli.run(["--json", "emit", "--all", "--kind", "reject", "--dataset", str(dataset)])

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)  # would raise if a progress line corrupted stdout
        assert parsed["summary"]["succeeded"] == 2
        assert "1/2" in captured.err and "2/2" in captured.err, "progress belongs on stderr"
