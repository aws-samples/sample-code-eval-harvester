"""Tests for the answer-absence checker: the git-channel checklist and the content scanner (E-1).

A leaked answer is the expensive silent failure — every score looks fine and measures nothing —
so these tests plant real leaks in real fixture repositories and assert each is caught. Two
properties matter most: the git-channel half catches routes that leave the visible tree looking
clean (``objects/info/alternates``, a bare repo smuggled into the tree, an unreachable reflog
entry), and the content scanner refuses ``scan-did-not-run`` when its planted control token is
absent — because an empty result and a broken scan are otherwise identical (NFR-6).
"""

from __future__ import annotations

import subprocess  # nosec B404  # the seal path is asserted to shell out only to local git; see test_seal_makes_no_network_call
from pathlib import Path

import pytest
from fixtures.repo_builder import RepoBuilder

from eval_harvest.seal import Finding, Report, Seal

#: The control token these tests plant in the corpus so the scanner can prove it scanned something
#: (NFR-6). Named rather than repeated as a literal at each call site: it is one value, it has to be
#: the same value in the planted text and in the ``control_token=`` argument, and a bare string in a
#: ``control_token=`` keyword also reads to a secret scanner as a hardcoded credential.
CONTROL_TOKEN = "CONTROL-TOKEN-XYZ"  # nosec B105  # a scan-liveness marker, not a credential


def _finding(report: Report, check: str) -> Finding:
    """The single finding whose check name is ``check`` — fails the test loudly if absent."""
    matches = [f for f in report.findings if f.check == check]
    assert matches, f"no finding named {check!r}; findings were {[f.check for f in report.findings]}"
    return matches[0]


def test_alternates_channel_leak_caught(tmp_path: Path) -> None:
    """``objects/info/alternates`` pointing at the source is flagged even though the tree greps clean.

    Catches a tree that greps clean while ``git show`` prints the answer (the motivating S-3 case).
    """
    repo = RepoBuilder.build_sealed_repo(tmp_path / "sealed")
    source = RepoBuilder.build_sealed_repo(tmp_path / "source")
    RepoBuilder.plant_alternates(repo, source / ".git" / "objects")
    report = Seal.check_repo(repo, canaries=())
    assert not _finding(report, "absent:objects/info/alternates").passed


def test_packed_refs_and_commit_graph_leaks_caught(tmp_path: Path) -> None:
    """packed-refs, commit-graph, refs/replace, an unreachable reflog entry, and include.path each flag.

    Catches every non-obvious git channel ``seal_assert`` exists for (S-3): a bad seal that only
    checks commit counts and tree greps passes all of these.
    """
    packed = RepoBuilder.build_sealed_repo(tmp_path / "packed")
    RepoBuilder.plant_packed_refs(packed)
    assert not _finding(Seal.check_repo(packed, canaries=()), "absent:packed-refs").passed

    graphed = RepoBuilder.build_sealed_repo(tmp_path / "graphed")
    RepoBuilder.plant_commit_graph(graphed)
    assert not _finding(Seal.check_repo(graphed, canaries=()), "absent:objects/info/commit-graph").passed

    replaced = RepoBuilder.build_sealed_repo(tmp_path / "replaced")
    RepoBuilder.plant_replace_ref(replaced)
    assert not _finding(Seal.check_repo(replaced, canaries=()), "no-refs:refs/replace").passed

    reflogged = RepoBuilder.build_sealed_repo(tmp_path / "reflogged")
    RepoBuilder.plant_unreachable_reflog_entry(reflogged)
    assert not _finding(Seal.check_repo(reflogged, canaries=()), "reflog-references-nothing-unreachable").passed

    included = RepoBuilder.build_sealed_repo(tmp_path / "included")
    RepoBuilder.plant_include_path(included)
    assert not _finding(Seal.check_repo(included, canaries=()), "no-config:include.path").passed


def test_bare_repo_in_tree_caught(tmp_path: Path) -> None:
    """A bare ``answerkey.git/`` committed into the tree is flagged by shape, not by name.

    Catches a whole second repository smuggled into the task tree (S-3): a literal ``.git`` search
    cannot see a bare repo, which has no ``.git`` entry — only its ``objects/`` + ``HEAD`` shape does.
    """
    repo = RepoBuilder.build_sealed_repo(tmp_path / "sealed")
    RepoBuilder.plant_bare_repo(repo)
    report = Seal.check_repo(repo, canaries=())
    finding = _finding(report, "no-bare-repositories")
    assert not finding.passed
    assert "answerkey.git" in finding.detail


def test_clean_repo_reports_no_failures(tmp_path: Path) -> None:
    """A freshly built single-commit repo passes every seal check — the baseline leaks deviate from."""
    repo = RepoBuilder.build_sealed_repo(tmp_path / "sealed")
    report = Seal.check_repo(repo, canaries=())
    assert report.failures == [], f"clean repo unexpectedly failed: {[(f.check, f.detail) for f in report.failures]}"


def test_content_scan_finds_pr_number_in_instruction(tmp_path: Path) -> None:
    """ "#1234" in ``instruction.md`` is flagged with the file and line it leaked into.

    Catches the PR number leaking into agent-visible content (S-2): a reviewer could look the PR up.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    body = f"Review this change.\nRelated to #1234 upstream.\n{CONTROL_TOKEN}\n"
    (task_dir / "instruction.md").write_text(body, encoding="utf-8")
    report = Seal.scan_agent_visible(task_dir, tokens=["#1234"], control_token=CONTROL_TOKEN)
    finding = _finding(report, "answer-absent:#1234")
    assert not finding.passed
    assert "instruction.md:2" in finding.detail


def test_solution_text_in_agent_tree_caught(tmp_path: Path) -> None:
    """Verifier-only solution text present in an agent-visible file is flagged (FR-40).

    Catches the reference answer leaking to the evaluated agent: the fix in ``solution/solve.sh`` must
    never appear where the agent reads. The scanner derives the answer from the verifier-only tree.
    """
    task_dir = tmp_path / "task"
    (task_dir / "solution").mkdir(parents=True)
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/sh\nreturn_early_on_null_pointer_guard\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text(
        f"Fix the crash.\nhint: return_early_on_null_pointer_guard\n{CONTROL_TOKEN}\n", encoding="utf-8"
    )
    report = Seal.scan_agent_visible(task_dir, tokens=[], control_token=CONTROL_TOKEN)
    assert report.failures, "solution text leaking into the agent tree was not flagged"
    assert any("instruction.md" in f.detail for f in report.failures)


def test_missing_control_token_refuses_scan_did_not_run(tmp_path: Path) -> None:
    """With no planted control token in the corpus, the scan refuses rather than reporting clean.

    Catches an empty result mistaken for a clean one (S-4): if the scan enumerated nothing, it must
    say so, not pass. This is the honesty guard — E-3 exercises it by removing the token.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("Review this change. Nothing to see here.\n", encoding="utf-8")
    report = Seal.scan_agent_visible(task_dir, tokens=["#1234"], control_token=CONTROL_TOKEN)
    finding = _finding(report, "scan-did-not-run")
    assert not finding.passed


def test_control_token_present_lets_clean_scan_pass(tmp_path: Path) -> None:
    """With the control token planted and no answer tokens present, the scan passes (scan ran, clean)."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text(f"Review this change.\n{CONTROL_TOKEN}\n", encoding="utf-8")
    report = Seal.scan_agent_visible(task_dir, tokens=["#1234"], control_token=CONTROL_TOKEN)
    assert report.failures == [], f"clean scan unexpectedly failed: {[(f.check, f.detail) for f in report.failures]}"


def test_canary_absence_flagged(tmp_path: Path) -> None:
    """A canary token found anywhere under the tree is reported; absent, it passes.

    The ported control-token half of ``check_repo`` (FR-36/NFR-6): a canary that *should* be absent
    is asserted absent, and its presence is a finding.
    """
    repo = RepoBuilder.build_sealed_repo(tmp_path / "sealed")
    (repo / "notes.txt").write_text("leftover SECRET_CANARY_TOKEN here\n", encoding="utf-8")
    report = Seal.check_repo(repo, canaries=("SECRET_CANARY_TOKEN",))
    assert not _finding(report, "canary-absent:SECRET_CANARY_TOKEN").passed


def test_seal_makes_no_network_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither ``check_repo`` nor ``scan_agent_visible`` issues a ``gh`` or a git remote/network call.

    Catches the sealing path reaching the forge (NFR-2): sealing reads local files and the local
    ``.git`` only. Every subprocess argv is captured and asserted to be a local, read-only git verb.
    """
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _recording_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(argv, (list, tuple)):
            calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _recording_run)

    repo = RepoBuilder.build_sealed_repo(tmp_path / "sealed")
    calls.clear()  # ignore the fixture's own build calls; measure only the seal path
    Seal.check_repo(repo, canaries=("X",))
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text(f"clean\n{CONTROL_TOKEN}\n", encoding="utf-8")
    Seal.scan_agent_visible(task_dir, tokens=["#1"], control_token=CONTROL_TOKEN)

    # `git remote` (bare, no subcommand) is a local listing, not a forge call; the network verbs are
    # the ones that actually reach a remote.
    network_verbs = {"fetch", "clone", "pull", "push", "ls-remote"}
    for argv in calls:
        assert "gh" not in Path(argv[0]).name, f"seal path invoked gh: {argv}"
        assert not (network_verbs & set(argv)), f"seal path issued a network git verb: {argv}"


def test_report_json_shape() -> None:
    """The JSON shape E-2 maps to the FR-2 refusal: ``{ok, repo, checks, findings:[...]}``."""
    report = Report(repo="/x")
    report.add("some-check", passed=False, detail="why", severity="error")
    payload = report.as_json()
    assert payload["ok"] is False
    assert payload["repo"] == "/x"
    assert payload["checks"] == 1
    assert payload["findings"] == [{"check": "some-check", "passed": False, "detail": "why", "severity": "error"}]
