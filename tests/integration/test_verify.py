"""Tests for the `verify` verb: structural checks, the absence scans, and the control token (E-2).

`verify` is the guarantee that replaces the absent human, so each precondition it enforces gets a
test that breaks that precondition and watches the specific failure appear (US-7, FR-34-39). The
properties that matter most: every broken datapoint fails its *own* named check with an actionable
`offending`/`next` (S-12/S-17), a leaked answer in a git channel or in agent-visible content fails
`answer-present` naming where it leaked (S-3/S-2), an unplanted control token refuses
`scan-did-not-run` rather than passing an empty scan (S-4, NFR-6), and a check that could not run
(the container-seal variant with no runtime) reports *unresolved* (exit 5) — never a silent pass
(§7.3, S-10).

The scenarios reconstruct the S-1 squash-merge fixture offline, emit a real reject datapoint, then
run `verify` against it with the fixture's clone — the same task/clone pair `emit` will hand it
(E-4). The git-channel half is exercised by building a real sealed fixture repo (a mock would prove
nothing — the whole point is a tree that greps clean while `git show` prints the answer) and passing
it as the materialized seal, because building the sealing container is deferred (task Out Of Scope).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404  # imported to intercept subprocess.run and assert verify shells out only to local git
import tomllib
from pathlib import Path
from typing import Any

from fixtures.repo_builder import RepoBuilder

from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.cli import Cli, ExitCode
from eval_harvest.emit import Emit
from eval_harvest.forge import Forge
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.riskmap import RiskMap
from eval_harvest.tomlw import emit_document
from eval_harvest.verify import CHANGE_PATCH_RELPATH, Verify, VerifyReport

#: A risk map marking the fixture's only file high — enough for `emit` to compute a structural risk.
_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}


def _substantive_findings() -> list[FindingDict]:
    """A reject oracle: one high defect (operator bug, line 2) and one medium (off-by-one, lines 5-7)."""
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


def _fill(candidate: CandidateDict, findings: list[FindingDict]) -> CandidateDict:
    """Classify every comment, set the findings, classify the risk, and pin the rubric version."""
    for comment in candidate["comments"]:
        comment["classification"] = "defect"
        comment["classification_rationale"] = "a substantive problem a reviewer would block on"
    candidate["findings"] = findings
    candidate["change_risk"]["risk_classified"] = "low"
    candidate["change_risk"]["risk_classified_rationale"] = "isolated helper, well covered by tests"
    candidate["rubric_version"] = "v1"
    return candidate


def _emit_reject_task(tmp_path: Path) -> tuple[Path, Path]:
    """Emit a real reject datapoint from the S-1 fixture and return `(task_dir, clone)`.

    Mirrors `capture` then a filled candidate, exactly as `test_emit` does, so `verify` runs against
    a genuine emitted directory (real `change.patch`, `tests/oracle.json`, `task.toml`) and the
    fixture clone whose objects hold the base commit the patch applies at.
    """
    fixture = RepoBuilder.build_squash_merged_pull_request(tmp_path)
    facts = Forge.reconstruct_facts(
        fixture.pr_number,
        fixture.clone,
        fixture.base_ref,
        reviews=fixture.reviews,
        comments=fixture.comments,
        timeline=fixture.timeline,
    )
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "risk-map.toml").write_bytes(emit_document(_RISK_MAP))
    (dataset / "rubric.md").write_text("---\nversion: v1\n---\n# Review rubric\n", encoding="utf-8")
    risk_structural, rule = RiskMap.structural_risk(Candidate.changed_paths(facts), _RISK_MAP)
    Candidate.write_to_dataset(
        facts,
        repo="our-org/our-repo",
        pr_url="https://github.com/our-org/our-repo/pull/1234",
        dataset_dir=dataset,
        risk_structural=risk_structural,
        risk_structural_rule=rule,
    )
    candidate_path = dataset / "candidates" / "pr-1234.json"
    Candidate.dump(_fill(Candidate.load(candidate_path), _substantive_findings()), candidate_path)
    candidate = Candidate.load(candidate_path)
    task_dir = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path
    return task_dir, fixture.clone


def _refusal(report: VerifyReport, check: str) -> Any:
    """The single refusal (failure or unresolved) whose check name is `check`; fail loudly if absent."""
    matches = [refusal for refusal in (*report.failures, *report.unresolved) if refusal.check == check]
    assert matches, f"no refusal named {check!r}; refusals were {[r.check for r in (*report.failures, *report.unresolved)]}"
    return matches[0]


def _set_base_commit(task_dir: Path, base_commit: str) -> None:
    """Rewrite `[metadata.origin].base_commit` in the emitted `task.toml` (to break base-exists)."""
    text = (task_dir / "task.toml").read_text(encoding="utf-8")
    document = tomllib.loads(text)
    real = document["metadata"]["origin"]["base_commit"]
    (task_dir / "task.toml").write_text(text.replace(real, base_commit), encoding="utf-8")


# ───────────────────────────── structural checks (FR-34) ─────────────────────────────


def test_missing_base_commit_specific_failure(tmp_path: Path) -> None:
    """A datapoint whose base commit is absent from the clone fails `base-missing` (S-12, FR-34)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    _set_base_commit(task_dir, "0" * 40)  # a well-formed sha that no object matches

    report = Verify.verify_task(task_dir, clone=clone)
    refusal = _refusal(report, "base-missing")
    assert "0000000000000000000000000000000000000000" in refusal.offending
    assert report.exit_code == ExitCode.VERIFICATION


def test_patch_does_not_apply_specific_failure(tmp_path: Path) -> None:
    """A `change.patch` that will not apply at the base fails `patch-does-not-apply` (S-12, FR-34)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    # Corrupt a context line's content so the hunk no longer matches the base tree.
    patch = (task_dir / CHANGE_PATCH_RELPATH).read_text(encoding="utf-8")
    (task_dir / CHANGE_PATCH_RELPATH).write_text(patch.replace("def add", "def subtract_totally_different"), encoding="utf-8")

    report = Verify.verify_task(task_dir, clone=clone)
    _refusal(report, "patch-does-not-apply")
    assert report.exit_code == ExitCode.VERIFICATION


def test_verify_fails_on_an_unterminated_patch(tmp_path: Path) -> None:
    """A `change.patch` whose last line lost its newline fails `patch-does-not-apply` (FR-34, FR-37).

    Detectable only because `verify` applies the file *as shipped*: a checker that copy-normalized the
    patch first would silently accept this corruption, and then the image build's `RUN git apply` would
    reject the same unterminated final hunk. Applying the stored bytes unmodified keeps the check honest.
    """
    task_dir, clone = _emit_reject_task(tmp_path)
    shipped = (task_dir / CHANGE_PATCH_RELPATH).read_bytes()
    assert shipped.endswith(b"\n"), "the emitted patch should be newline-terminated before we break it"
    (task_dir / CHANGE_PATCH_RELPATH).write_bytes(shipped.rstrip(b"\n"))

    report = Verify.verify_task(task_dir, clone=clone)
    _refusal(report, "patch-does-not-apply")
    assert report.exit_code == ExitCode.VERIFICATION


def test_verify_applies_the_shipped_patch_unmodified(tmp_path: Path, monkeypatch: Any) -> None:
    """`_patch_applies` hands `git apply` the emitted path itself, not a rewritten copy.

    Pins a design boundary: repair of a malformed stored file belongs at the write
    site, and a checker that repairs its input cannot detect a corrupt one. Catches anyone reintroducing
    a normalizing shim — the argument `git apply` receives must be the task directory's own file.
    """
    task_dir, clone = _emit_reject_task(tmp_path)
    applied_paths: list[str] = []
    real_git = GitCommandRunner.git

    def recording_git(cwd: Path, *argv: str) -> tuple[int, str]:
        if argv and argv[0] == "apply":
            applied_paths.append(argv[-1])
        return real_git(cwd, *argv)

    monkeypatch.setattr(GitCommandRunner, "git", staticmethod(recording_git))
    report = Verify.verify_task(task_dir, clone=clone)

    assert not report.failures, f"the baseline datapoint must verify clean; failed {[f.check for f in report.failures]}"
    assert applied_paths == [str(task_dir / CHANGE_PATCH_RELPATH)], (
        "verify must apply the shipped change.patch, not a normalized copy in a temp dir"
    )


def test_finding_on_missing_line_specific_failure(tmp_path: Path) -> None:
    """An oracle finding on a line not in the change fails `finding-line-absent` (S-12, FR-34)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    oracle = json.loads((task_dir / "tests" / "oracle.json").read_text(encoding="utf-8"))
    oracle["findings"][0]["locations"] = [{"path": "code.py", "line_start": 999, "line_end": 999}]
    (task_dir / "tests" / "oracle.json").write_text(json.dumps(oracle, indent=2) + "\n", encoding="utf-8")

    report = Verify.verify_task(task_dir, clone=clone)
    refusal = _refusal(report, "finding-line-absent")
    assert "code.py" in refusal.offending and "999" in refusal.offending
    assert report.exit_code == ExitCode.VERIFICATION


def test_incoherent_rubric_specific_failure(tmp_path: Path) -> None:
    """A blank `tests/rubric.md` fails `rubric-incoherent` — a datapoint graded against nothing (FR-34)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    (task_dir / "tests" / "rubric.md").write_text("", encoding="utf-8")

    report = Verify.verify_task(task_dir, clone=clone)
    _refusal(report, "rubric-incoherent")
    assert report.exit_code == ExitCode.VERIFICATION


# ───────────────────────────── absence scans (FR-35/36) ─────────────────────────────


def test_leak_in_git_channel_fails_answer_present(tmp_path: Path) -> None:
    """A planted `objects/info/alternates` leak in the materialized seal fails `answer-present` (S-3, FR-35).

    The git-channel checklist runs against a materialized sealed tree; here it is a real fixture repo
    with the motivating leak planted, since building the sealing container is deferred (Out Of Scope).
    """
    task_dir, clone = _emit_reject_task(tmp_path)
    sealed = RepoBuilder.build_sealed_repo(tmp_path / "sealed")
    source = RepoBuilder.build_sealed_repo(tmp_path / "source")
    RepoBuilder.plant_alternates(sealed, source / ".git" / "objects")

    report = Verify.verify_task(task_dir, clone=clone, materialized_seal=sealed)
    refusal = _refusal(report, "answer-present")
    assert "alternates" in refusal.offending, "the refusal must name the leaking channel"
    assert report.exit_code == ExitCode.VERIFICATION


def test_content_leak_in_agent_file_fails_answer_present(tmp_path: Path) -> None:
    """The PR number leaking into agent-visible content fails `answer-present` naming file:line (S-2, FR-35)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    (task_dir / "instruction.md").write_text(instruction + "\nSee the original discussion in #1234.\n", encoding="utf-8")

    report = Verify.verify_task(task_dir, clone=clone)
    refusal = _refusal(report, "answer-present")
    assert "instruction.md" in refusal.offending, "the refusal must name the file the answer leaked into"
    assert report.exit_code == ExitCode.VERIFICATION


def test_missing_control_token_refuses_scan_did_not_run(tmp_path: Path) -> None:
    """With the control token not planted, the content scan refuses `scan-did-not-run` (S-4, FR-36/NFR-6).

    The honesty guard: an empty scan result and a broken scan look identical, so an unplanted token
    must refuse rather than report clean. E-3 exercises the same break as a watched-red guard.
    """
    task_dir, clone = _emit_reject_task(tmp_path)

    report = Verify.verify_task(task_dir, clone=clone, plant_control_token=False)
    _refusal(report, "scan-did-not-run")
    assert report.exit_code == ExitCode.VERIFICATION


# ───────────────────────────── base-tree suppression (FR-2/22/23/33) ─────────────────────────────


def _leak_pr_number(task_dir: Path) -> None:
    """Leak the PR number (`#1234`) into an agent-visible file — a content-scan hit on token `#1234`."""
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    (task_dir / "instruction.md").write_text(instruction + "\nSee the original discussion in #1234.\n", encoding="utf-8")


def _base_commit(task_dir: Path) -> str:
    """The `[metadata.origin].base_commit` recorded in the emitted `task.toml`."""
    document = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    base: str = document["metadata"]["origin"]["base_commit"]
    return base


def _has(report: VerifyReport, check: str) -> bool:
    """True when some refusal (failure or unresolved) in the report has the check name `check`."""
    return any(refusal.check == check for refusal in (*report.failures, *report.unresolved))


def test_answer_present_suppressed_when_token_exists_at_base(tmp_path: Path) -> None:
    """A leaked token the oracle reports present at the base commit predates the change, so it is suppressed."""
    task_dir, clone = _emit_reject_task(tmp_path)
    _leak_pr_number(task_dir)

    report = Verify.verify_task(task_dir, clone=clone, base_oracle=lambda _token: True)
    assert not _has(report, "answer-present"), "a token present at the base commit must not fail answer-present"
    assert report.content_scan is not None and len(report.content_scan.suppressed) == 1
    assert report.content_scan.suppressed[0].token == "#1234"  # nosec B105  # PR-number token asserted by the leak-detection test, not a credential


def test_answer_present_fires_when_token_only_in_the_change(tmp_path: Path) -> None:
    """A leaked token the oracle reports absent from the base still fails — suppression is no blanket exemption."""
    task_dir, clone = _emit_reject_task(tmp_path)
    _leak_pr_number(task_dir)

    report = Verify.verify_task(task_dir, clone=clone, base_oracle=lambda _token: False)
    refusal = _refusal(report, "answer-present")
    assert "instruction.md" in refusal.offending
    assert report.content_scan is not None and report.content_scan.suppressed == ()
    assert report.exit_code == ExitCode.VERIFICATION


def test_suppression_is_reported_not_silent(tmp_path: Path, capsys: Any) -> None:
    """A suppressed token and its base commit appear in the output — an invisible suppression hides a real leak."""
    task_dir, clone = _emit_reject_task(tmp_path)
    _leak_pr_number(task_dir)

    report = Verify.verify_task(task_dir, clone=clone, base_oracle=lambda _token: True)
    Cli._report_verify(report, json_mode=False)

    printed = capsys.readouterr().out
    assert "suppressed '#1234'" in printed, "the suppressed token must be named in the output"
    assert _base_commit(task_dir)[:7] in printed, "the base commit the token exists at must be reported"


def test_answer_present_without_clone_still_fails(tmp_path: Path) -> None:
    """With no clone there is no oracle, so a leaked token still fails — guessing it is benign is not allowed."""
    task_dir, _clone = _emit_reject_task(tmp_path)
    _leak_pr_number(task_dir)

    report = Verify.verify_task(task_dir)  # no clone → no base-tree oracle
    assert _has(report, "answer-present"), "without a clone a leaked token must still fail answer-present"


def test_answer_present_next_field_is_runnable(tmp_path: Path) -> None:
    """The no-clone refusal names the real task dir and a `--clone` command — placeholder advice gets ignored."""
    task_dir, _clone = _emit_reject_task(tmp_path)
    _leak_pr_number(task_dir)

    refusal = _refusal(Verify.verify_task(task_dir), "answer-present")
    assert str(task_dir) in refusal.next_, "the next: must name the real task directory, not a placeholder"
    assert "--clone" in refusal.next_ and "--override answer-present" in refusal.next_


def test_control_token_is_never_suppressed(tmp_path: Path) -> None:
    """Even an oracle that clears every token cannot suppress the control token — scan-did-not-run keeps firing (NFR-6)."""
    task_dir, clone = _emit_reject_task(tmp_path)

    report = Verify.verify_task(task_dir, clone=clone, plant_control_token=False, base_oracle=lambda _token: True)
    _refusal(report, "scan-did-not-run")
    assert report.exit_code == ExitCode.VERIFICATION


def test_scan_reports_token_arithmetic(tmp_path: Path) -> None:
    """The scan reports scanned / hit / suppressed counts — a suppression rate nobody can see is a hidden leak."""
    task_dir, clone = _emit_reject_task(tmp_path)

    clean = Verify.verify_task(task_dir, clone=clone)  # no leak: one token scanned, no hits
    assert clean.content_scan is not None
    assert (clean.content_scan.scanned, clean.content_scan.hits, len(clean.content_scan.suppressed)) == (1, 0, 0)

    _leak_pr_number(task_dir)
    suppressed = Verify.verify_task(task_dir, clone=clone, base_oracle=lambda _token: True)
    assert suppressed.content_scan is not None
    assert (suppressed.content_scan.scanned, suppressed.content_scan.hits, len(suppressed.content_scan.suppressed)) == (1, 1, 1)


def test_base_lookup_uses_git_grep_against_the_sha(tmp_path: Path, monkeypatch: Any) -> None:
    """The real oracle greps the base *object* — no worktree checkout, so a dirty clone cannot confuse it."""
    task_dir, clone = _emit_reject_task(tmp_path)
    _leak_pr_number(task_dir)
    base = _base_commit(task_dir)
    recorded: list[tuple[str, ...]] = []
    real_git = GitCommandRunner.git

    def recording_git(cwd: Path, *argv: str) -> tuple[int, str]:
        recorded.append(argv)
        return real_git(cwd, *argv)

    monkeypatch.setattr(GitCommandRunner, "git", staticmethod(recording_git))
    Verify.verify_task(task_dir, clone=clone)  # real oracle: #1234 is not at base, so the hit still fails

    grep_calls = [argv for argv in recorded if argv and argv[0] == "grep"]
    assert grep_calls, "the base-tree oracle must run `git grep`"
    fixed_string_against_base = [argv for argv in grep_calls if "--fixed-strings" in argv and base in argv]
    assert fixed_string_against_base, "grep must be fixed-string against the base sha"
    assert not any("checkout" in argv or "worktree" in argv for argv in recorded), "the lookup needs no worktree checkout"


def test_answer_present_reports_file_and_line(tmp_path: Path) -> None:
    """The refusal names the offending file and line, not just the token — an unchecked refusal is worked around."""
    task_dir, clone = _emit_reject_task(tmp_path)
    _leak_pr_number(task_dir)

    refusal = _refusal(Verify.verify_task(task_dir, clone=clone, base_oracle=lambda _token: False), "answer-present")
    assert "instruction.md:" in refusal.offending, "the offending field must name the file and line the token was found at"


# ───────────────────────────── reporting shape & exit codes (FR-2/39, §7.3) ─────────────────────────────


def test_every_failure_has_four_fields_and_distinct_exit(tmp_path: Path) -> None:
    """Each refusal carries check/datapoint/offending/next, and exit 4 (fail) vs 5 (unresolved) differ (S-17)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    _set_base_commit(task_dir, "0" * 40)

    report = Verify.verify_task(task_dir, clone=clone)
    assert report.failures, "a broken datapoint must produce at least one failure"
    for refusal in report.failures:
        assert refusal.check and refusal.datapoint and refusal.offending and refusal.next_
    assert report.datapoint == "pr-1234/reject"
    assert int(ExitCode.VERIFICATION) != int(ExitCode.RUNTIME_UNAVAILABLE)
    assert int(ExitCode.RUNTIME_UNAVAILABLE) == 5

    payload = report.as_json()
    assert payload["ok"] is False
    failures: list[dict[str, str]] = payload["failures"]  # type: ignore[assignment]
    assert all({"check", "datapoint", "offending", "next"} <= set(failure) for failure in failures)


def test_runtime_absent_reports_unresolved_exit_5(tmp_path: Path) -> None:
    """A clean datapoint with no materialized seal reports the git-channel check unresolved, exit 5 (§7.3).

    "skipped" must never read as "passed": with every hard check green but the container-seal variant
    unable to run, the summary counts it unresolved and the process exits 5, not 0.
    """
    task_dir, clone = _emit_reject_task(tmp_path)

    report = Verify.verify_task(task_dir, clone=clone)  # no materialized_seal → no runtime
    assert report.failures == [], f"the datapoint is otherwise clean: {[(r.check, r.offending) for r in report.failures]}"
    unresolved = _refusal(report, "git-channel-absence")
    assert "skipped: no runtime" in unresolved.offending
    assert report.exit_code == ExitCode.RUNTIME_UNAVAILABLE


def test_clean_datapoint_with_seal_passes_exit_0(tmp_path: Path) -> None:
    """A sound datapoint over a clean materialized seal passes every check and exits 0 (the pass path)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    sealed = RepoBuilder.build_sealed_repo(tmp_path / "sealed")

    report = Verify.verify_task(task_dir, clone=clone, materialized_seal=sealed)
    assert report.ok, f"a sound datapoint must pass: {[(r.check, r.offending) for r in (*report.failures, *report.unresolved)]}"
    assert report.exit_code == ExitCode.SUCCESS


# ───────────────────────────── offline guarantee & CLI end-to-end (NFR-2, FR-4) ─────────────────────────────


def test_verify_makes_no_network_call(tmp_path: Path, monkeypatch: Any) -> None:
    """`verify` issues no `gh` and no network git verb — the sealing path never reaches the forge (NFR-2)."""
    task_dir, clone = _emit_reject_task(tmp_path)
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _recording_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(argv, (list, tuple)):
            calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _recording_run)
    Verify.verify_task(task_dir, clone=clone)

    network_verbs = {"fetch", "clone", "pull", "push", "ls-remote"}
    for argv in calls:
        assert "gh" not in Path(argv[0]).name, f"verify invoked gh: {argv}"
        assert not (network_verbs & set(argv)), f"verify issued a network git verb: {argv}"


def test_cli_verify_end_to_end_exit_5_when_runtime_absent(tmp_path: Path) -> None:
    """`eval-harvest verify <task-dir> --clone <clone>` runs and exits 5 with no runtime (the driven flow)."""
    task_dir, clone = _emit_reject_task(tmp_path)

    code = Cli.run(["verify", str(task_dir), "--clone", str(clone)])
    assert code == ExitCode.RUNTIME_UNAVAILABLE


def test_cli_verify_missing_task_dir_is_usage_error(tmp_path: Path) -> None:
    """A task-dir that is not a directory is a usage error (exit 2), not a verification failure."""
    code = Cli.run(["verify", str(tmp_path / "nope")])
    assert code == ExitCode.USAGE


def test_cli_verify_relative_task_path_does_not_misfire_patch_applies(tmp_path: Path, monkeypatch: Any) -> None:
    """A relative task path must verify the same as an absolute one.

    `patch-applies` runs `git -C <clone> apply <patch>`; a caller-relative patch path resolves against
    the clone, not the CWD, so git cannot open the file and a sound datapoint is wrongly refused
    `patch-does-not-apply` (exit 4). Run from a neutral CWD with a relative task path and an absolute
    clone — the README step-5 shape — and assert the sound datapoint still reaches the no-runtime exit
    (5), never a verification failure.
    """
    task_dir, clone = _emit_reject_task(tmp_path)
    # chdir to the common ancestor so the task path is a *forward* relative path (no `..`) — the real
    # `my-dataset/tasks/…` shape. `git -C <clone>` would resolve that against the clone dir, not the CWD.
    monkeypatch.chdir(tmp_path)
    relative_task = os.path.relpath(task_dir, tmp_path)
    assert not os.path.isabs(relative_task) and not relative_task.startswith(".."), "must be a forward relative path"

    code = Cli.run(["verify", relative_task, "--clone", str(clone.resolve())])
    assert code == ExitCode.RUNTIME_UNAVAILABLE, "a relative task path must not misfire patch-applies"


def test_git_channel_unresolved_guidance_stays_honest(tmp_path: Path) -> None:
    """The unresolved git-channel guidance must not promise `verify` will run the check.

    The `eval-harvest verify` command never materializes a sealing container, so telling the operator to
    "set MICROVM_* or provide Docker and re-run" is a promise the command cannot keep. The exit-5 marker
    ("skipped: no runtime") stays; the actionable guidance must not name an input `verify` does not consume.
    """
    task_dir, clone = _emit_reject_task(tmp_path)
    unresolved = _refusal(Verify.verify_task(task_dir, clone=clone), "git-channel-absence")
    assert "skipped: no runtime" in unresolved.offending, "the exit-5 marker must remain"
    guidance = f"{unresolved.offending}\n{unresolved.next_}".lower()
    assert "provide docker" not in guidance, "guidance must not tell the operator to give `verify` Docker"
    assert "set microvm" not in guidance, "guidance must not tell the operator to set MICROVM_* for `verify`"
