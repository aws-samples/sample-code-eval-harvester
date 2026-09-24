"""Tests for the ``survey`` verb (task B-1): mechanical PR triage, determinism, and the FR-11 refusal.

``survey.py`` is a self-contained build, so its output is pinned against a committed golden payload
rather than compared to any external tool. The golden is byte-stable because the fixture's commit
SHAs are deterministic: :class:`RepoBuilder` pins the author/committer identity, the
author/committer dates, *and* the timezone (``TZ=UTC``, since a commit date is stored with its zone
offset), so a squash-merged, branch-deleted PR built from the same builder calls hashes to the same
SHAs on every run and every machine. That is what lets a raw-bytes golden-file test (NFR-1) stand in
for the old independent-tool comparison and still catch a reordered key, a changed range, or a lost
field.

The recovery behaviour the survey reads *is* git's behaviour, so the clone is a real squash-merged,
branch-deleted PR built by :class:`RepoBuilder` (a mock would only test the mock). The ``gh pr
list`` payloads are replayed from recorded templates under ``tests/fixtures/gh/survey/`` with the
fixture's real SHAs substituted in — the same ``--from-json`` replay path the verb uses in the
field, so no test touches GitHub.

To regenerate the golden after an intended change to the payload shape: run the verb over the
reviewed fixture with ``--json`` and write its stdout to ``fixtures/gh/survey/expected.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures.repo_builder import RepoBuilder

from eval_harvest.cli import Cli, ExitCode
from eval_harvest.forge import Forge
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.survey import PULL_HEAD_REFSPEC, Survey

#: The recorded ``gh pr list`` payload templates (tokens substituted with the fixture's real SHAs).
_SURVEY_PAYLOAD_DIR = Path(__file__).parents[1] / "fixtures" / "gh" / "survey"

#: The frozen expected ``--json`` payload for the reviewed fixture — the golden the verb is pinned to.
_GOLDEN_PAYLOAD = _SURVEY_PAYLOAD_DIR / "expected.json"

#: Judgment fields a survey must never carry — every output is a mechanical fact, not a label (FR-5).
_QUALITY_LABEL_KEYS = frozenset(
    {"quality", "good", "bad", "score", "label", "verdict", "recommendation", "rating", "grade", "harvestable"}
)


def _write_payload(template_name: str, clone: Path, fixture: object, tmp_path: Path) -> Path:
    """Materialize a recorded payload with the fixture's real merge/head/base SHAs substituted in."""
    merge_sha = GitCommandRunner.git(clone, "rev-parse", "main")[1]
    substitutions = {
        "MERGE_SHA": merge_sha,
        "ROUND2_TIP": fixture.round2_tip,  # type: ignore[attr-defined]
        "BASE_SHA": fixture.base_sha,  # type: ignore[attr-defined]
    }
    text = (_SURVEY_PAYLOAD_DIR / template_name).read_text(encoding="utf-8")
    for token, sha in substitutions.items():
        text = text.replace(f"{{{{{token}}}}}", sha)
    destination = tmp_path / template_name
    destination.write_text(text, encoding="utf-8")
    return destination


def _run_survey_verb(clone: Path, payload: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Run ``eval-harvest survey --json`` in-process and return its captured stdout."""
    code = Cli.run(["--json", "survey", "--clone", str(clone), "--from-json", str(payload)])
    assert code == ExitCode.SUCCESS
    return capsys.readouterr().out


def test_survey_matches_golden(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`survey --json` output is byte-identical to the committed golden on the reviewed fixture.

    The regression check (S-13, FR-5): the payload is pinned to a frozen expected output, so this is
    what catches the triage logic drifting — a reordered key, a different range, a lost field. It
    holds byte-for-byte because the fixture SHAs are deterministic (fixed identity, dates, and TZ).
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = _write_payload("reviewed.json", fixture.clone, fixture, tmp_path)

    verb_output = _run_survey_verb(fixture.clone, payload, capsys)

    assert verb_output == _GOLDEN_PAYLOAD.read_text(encoding="utf-8")


def test_survey_deterministic_twice(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two `survey --json` runs on fixed inputs are byte-identical (FR-10, NFR-1).

    Catches nondeterministic dict/set ordering — the kind of drift that breaks the downstream content
    hash even while every field is individually correct.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = _write_payload("reviewed.json", fixture.clone, fixture, tmp_path)

    first = _run_survey_verb(fixture.clone, payload, capsys)
    second = _run_survey_verb(fixture.clone, payload, capsys)

    assert first == second


def test_refuse_no_review_iteration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A payload where no PR went through review refuses `no-review-iteration` with the count, exit 3.

    Catches a worthless empty dataset being shipped instead of an honest refusal (S-5, FR-11): a repo
    whose PRs merge with no review iteration has no human verdict to grade an agent against.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = _write_payload("never_reviewed.json", fixture.clone, fixture, tmp_path)

    code = Cli.run(["survey", "--clone", str(fixture.clone), "--from-json", str(payload)])

    assert code == ExitCode.REFUSAL
    stderr = capsys.readouterr().err
    assert "no-review-iteration" in stderr
    assert "0 of 1 merged PRs had review rounds" in stderr
    assert "pick a repo whose PRs go through review" in stderr


def test_survey_reports_no_quality_label(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No output field is a quality label — every per-PR field is a mechanical fact (FR-5).

    Catches judgment being smuggled into a script where no provenance attaches to it. The blockers are
    the only derived field and they are derived from facts (no integration commit, no changed path),
    never from taste.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = _write_payload("reviewed.json", fixture.clone, fixture, tmp_path)

    output = _run_survey_verb(fixture.clone, payload, capsys)
    parsed = json.loads(output)

    assert parsed["prs"], "the fixture PR must be surveyed"
    for row in parsed["prs"]:
        offending = _QUALITY_LABEL_KEYS & row.keys()
        assert not offending, f"survey row carries a quality label: {sorted(offending)}"


# ───────────────────────────── pull-head fetch, histogram, and the no-harvestable refusal ─────────────────────────────


def _reviewed_prs(clone: Path, fixture: object, tmp_path: Path) -> list[dict[str, object]]:
    """The reviewed `gh pr list` payload as a Python list, SHAs substituted — the online-path input."""
    loaded: list[dict[str, object]] = json.loads(
        _write_payload("reviewed.json", clone, fixture, tmp_path).read_text(encoding="utf-8")
    )
    return loaded


def _row(number: int, *, blockers: list[str], state: str = "MERGED", review_rounds: int = 2) -> dict[str, object]:
    """A minimal per-PR survey row carrying every field the refusal, histogram, and table renderer read."""
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "blockers": blockers,
        "review_rounds": review_rounds,
        "n_changed": 1,
        "additions": 1,
        "deletions": 0,
        "branch_commits": 1,
        "reverted_by": [],
        "followup_fixes": [],
        "integration_is_true_merge": False,
    }


def _replay_gh(monkeypatch: pytest.MonkeyPatch, fixture: object, tmp_path: Path) -> None:
    """Patch the online ``gh pr list`` call to replay the reviewed payload — drives the `--repo` path offline."""
    prs = _reviewed_prs(fixture.clone, fixture, tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(Survey, "fetch_pull_requests", staticmethod(lambda *a, **k: prs))


def test_survey_fetches_pull_head_refs_on_online_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`survey --repo` fetches `+refs/pull/*/head:refs/remotes/pr/*` once, before enumerating.

    A plain clone holds no pull-head refs, so without the fetch every PR blocks
    `no-pull-head-ref` and the table is empty. The fixture clone
    has a real `origin` and no pull refs, so the bulk fetch is exercised for real (no network).
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path, pull_head_fetched=False)
    _replay_gh(monkeypatch, fixture, tmp_path)
    fetched: list[str] = []
    real_fetch = Forge.fetch_refs

    def spy_fetch(clone: Path, refspec: str, *, remote: str = "origin") -> None:
        fetched.append(refspec)
        real_fetch(clone, refspec, remote=remote)

    monkeypatch.setattr(Forge, "fetch_refs", staticmethod(spy_fetch))

    before = GitCommandRunner.git(fixture.clone, "rev-parse", "--verify", "--quiet", "refs/remotes/pr/1234")[0]
    assert before != 0, "the plain clone must not already hold the pull ref (else the test proves nothing)"

    Cli.run(["survey", "--repo", "acme/widgets", "--clone", str(fixture.clone)])

    assert fetched == [PULL_HEAD_REFSPEC], "exactly one bulk pull-head fetch, before enumeration"
    after = GitCommandRunner.git(fixture.clone, "rev-parse", "--verify", "--quiet", "refs/remotes/pr/1234")[0]
    assert after == 0, "the fetch must populate refs/remotes/pr/*"


def test_survey_from_json_never_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The offline `--from-json` path issues no fetch — re-rendering a saved payload stays offline (NFR-2).

    Catches a re-render silently going online. Spies both the forge fetch primitive and every git
    invocation for a `fetch` argv, and asserts neither fires.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = _write_payload("reviewed.json", fixture.clone, fixture, tmp_path)
    fetched: list[str] = []

    def spy_fetch(*args: object, **kwargs: object) -> None:
        fetched.append("fetch")

    monkeypatch.setattr(Forge, "fetch_refs", staticmethod(spy_fetch))
    real_git_with_stderr = GitCommandRunner.git_with_stderr
    git_fetches: list[str] = []

    def spy_git(repo: Path, *args: str) -> tuple[int, str, str]:
        if args and args[0] == "fetch":
            git_fetches.append(" ".join(args))
        return real_git_with_stderr(repo, *args)

    monkeypatch.setattr(GitCommandRunner, "git_with_stderr", staticmethod(spy_git))

    code = Cli.run(["survey", "--clone", str(fixture.clone), "--from-json", str(payload)])

    assert code == ExitCode.SUCCESS, "a from-json survey with a reviewed PR still renders (exit 0)"
    assert fetched == [], "the offline path must not call the forge fetch"
    assert git_fetches == [], "the offline path must issue no `git fetch`"


def test_survey_no_fetch_flag_skips_the_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--no-fetch` suppresses the pull-head fetch on the online path — for a mirror or pre-fetched clone."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path, pull_head_fetched=False)
    _replay_gh(monkeypatch, fixture, tmp_path)
    fetched: list[str] = []

    def spy_fetch(*args: object, **kwargs: object) -> None:
        fetched.append("fetch")

    monkeypatch.setattr(Forge, "fetch_refs", staticmethod(spy_fetch))

    Cli.run(["survey", "--repo", "acme/widgets", "--clone", str(fixture.clone), "--no-fetch"])

    assert fetched == [], "--no-fetch must suppress the fetch"


def test_survey_refuses_when_every_pr_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An online survey where every PR is blocked refuses `no-harvestable-pr` (exit 3), not exit 0 on silence.

    The silence that could be mistaken for "no review history". The reviewed fixture's one PR is
    blocked by `no-test-change`, so after the (successful) fetch every PR is still blocked.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    _replay_gh(monkeypatch, fixture, tmp_path)

    code = Cli.run(["survey", "--repo", "acme/widgets", "--clone", str(fixture.clone), "--no-fetch"])

    assert code == ExitCode.REFUSAL
    err = capsys.readouterr().err
    assert "no-harvestable-pr" in err
    assert "the most common blocker is no-test-change (1 of 1)" in err
    assert "no test files" in err, "next: must name the dominant blocker's remedy"


def test_survey_exits_zero_with_one_clean_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A survey with even one harvestable PR keeps exit 0 — no over-refusal on a mostly-blocked repo."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = {
        "repo": "acme/widgets",
        "mainline_commits": 1,
        "prs": [_row(111, blockers=[]), _row(222, blockers=["no-test-change"])],
    }
    monkeypatch.setattr(Survey, "fetch_pull_requests", staticmethod(lambda *a, **k: [{}]))
    monkeypatch.setattr(Survey, "build_payload", classmethod(lambda cls, *a, **k: payload))

    code = Cli.run(["survey", "--repo", "acme/widgets", "--clone", str(fixture.clone), "--no-fetch"])

    assert code == ExitCode.SUCCESS


def test_survey_prints_blocker_histogram_with_remedies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The human summary prints a `blocked (N)` histogram with per-blocker counts and remedies when any PR is blocked."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = _write_payload("reviewed.json", fixture.clone, fixture, tmp_path)

    Cli.run(["survey", "--clone", str(fixture.clone), "--from-json", str(payload), "--summary"])

    out = capsys.readouterr().out
    assert "blocked (1)" in out
    assert "no-test-change" in out
    assert "no test files" in out, "each histogram row names a remedy"


def test_no_pull_head_ref_remedy_is_the_real_command() -> None:
    """The `no-pull-head-ref` remedy is a runnable, single-quoted `git fetch` with the real remote."""
    remedy = Survey.fetch_remedy("upstream")
    assert remedy == "git fetch upstream '+refs/pull/*/head:refs/remotes/pr/*'"
    assert "'" in remedy, "the glob refspec must be single-quoted so zsh does not expand it"


def test_survey_refuses_when_the_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pull-head fetch that cannot run refuses with git's stderr and the `--no-fetch` escape, not a traceback."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path, pull_head_fetched=False)
    GitCommandRunner.git(fixture.clone, "remote", "remove", "origin")  # so the fetch has no remote to reach
    _replay_gh(monkeypatch, fixture, tmp_path)

    code = Cli.run(["survey", "--repo", "acme/widgets", "--clone", str(fixture.clone)])

    assert code == ExitCode.REFUSAL, "a failed fetch refuses, it does not raise"
    err = capsys.readouterr().err
    assert "pull-head-fetch-failed" in err
    # `fatal:` is git's own stderr, not the ForgeError template (which says "from origin"), so this
    # only passes when git's real reason reaches the refusal — the point of routing through git_with_stderr.
    assert "fatal:" in err, "the refusal must carry git's own stderr, not just the command that failed"
    assert "--no-fetch" in err and PULL_HEAD_REFSPEC in err, "next: names the escape and the manual command"


def test_survey_json_carries_blocker_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The `--json` payload carries the documented `blockers` object: name → {count, remedy}."""
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    payload = _write_payload("reviewed.json", fixture.clone, fixture, tmp_path)

    Cli.run(["--json", "survey", "--clone", str(fixture.clone), "--from-json", str(payload)])

    parsed = json.loads(capsys.readouterr().out)
    assert parsed["blockers"]["no-test-change"]["count"] == 1
    assert "no test files" in parsed["blockers"]["no-test-change"]["remedy"]


def test_survey_clean_rows_only_in_table(capsys: pytest.CaptureFixture[str]) -> None:
    """The summary table lists only unblocked, merged PRs — blocked ones appear in the histogram, not the table."""
    payload = {"repo": "", "mainline_commits": 1, "prs": [_row(111, blockers=[]), _row(222, blockers=["no-test-change"])]}

    print(Survey.render_summary(payload, branch="main", remote="origin"), end="")

    out = capsys.readouterr().out
    table = out.split("\nblocked (")[0]
    assert "111" in table, "the clean PR is listed in the table"
    assert "222" not in table, "the blocked PR is not in the table (only in the histogram)"
