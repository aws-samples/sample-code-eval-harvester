"""NFR-2: the answer-sealing path never reaches the forge (task E-4, S-15, ADR-4).

The offline guarantee is what makes a leaked answer impossible to introduce during sealing, and
ADR-4 chose to enforce it with a *test* rather than an architectural wire between two processes — so
this test is load-bearing, not incidental. It stubs every git *network* verb and the `gh` binary to
raise, then runs `emit` (assembling and verifying a datapoint over a local clone) and the standalone
`verify` (over that clone and a materialized sealed tree) to completion, asserting both succeed. If
either verb reached the forge, the stub would raise and the run would fail.

`test_offline_guard_watched_to_fail` is the guard for the guard (CLAUDE.md: "a guard nobody has
watched fail is decorative"): it proves the stub actually trips on a network verb routed through
either git chokepoint `emit`/`verify` use, so a regression that let the sealing path fetch, clone, or
pull would turn this file red rather than pass unnoticed (FR-37). The stub is scoped to intercept
only network verbs — the local reads `verify` legitimately makes (`cat-file`, `read-tree`, `apply`,
and `seal`'s `fsck`/`for-each-ref`/`rev-list`/…) pass straight through.
"""

from __future__ import annotations

import subprocess  # nosec B404  # imported to intercept subprocess.run and assert the seal path stays offline
from pathlib import Path
from typing import Any

import pytest
from fixtures.repo_builder import RepoBuilder

from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.cli import ExitCode
from eval_harvest.emit import Emit
from eval_harvest.forge import Forge
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.riskmap import RiskMap
from eval_harvest.seal import Seal
from eval_harvest.tomlw import emit_document
from eval_harvest.verify import Verify

#: A risk map marking the fixture's only file high — enough for `emit` to compute a structural risk.
_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}

#: The git verbs that reach a remote. Matches the set `test_verify.test_verify_makes_no_network_call`
#: asserts against, so "offline" means the same thing across the suite. `remote` (listing configured
#: remotes) and `config` are local reads the seal makes and are deliberately not here.
_NETWORK_VERBS = frozenset({"fetch", "clone", "pull", "push", "ls-remote"})


def _substantive_findings() -> list[FindingDict]:
    """A reject oracle: one high defect (operator bug) and one medium (off-by-one), bound to comments."""
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


def _prepare(tmp_path: Path) -> tuple[Path, CandidateDict, Path]:
    """A dataset holding a filled reject candidate and the fixture clone (all git work done up front).

    Fixture construction and the clone are built here, *before* the network stub is installed, so the
    stub only ever intercepts the git the sealing path itself issues.
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
    return dataset, Candidate.load(candidate_path), fixture.clone


def _install_network_stub(monkeypatch: Any) -> None:
    """Replace `subprocess.run` so any git network verb or `gh` invocation raises instead of running.

    Local git verbs pass straight through to the real `subprocess.run`, so the sealing path's genuine
    local reads still work; only a call that would reach the forge trips the stub.
    """
    real_run = subprocess.run

    def _offline_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(argv, (list, tuple)) and argv:
            parts = [str(fragment) for fragment in argv]
            executable = Path(parts[0]).name
            if executable == "gh":
                raise AssertionError(f"offline sealing path invoked gh: {parts}")
            if _NETWORK_VERBS & set(parts):
                raise AssertionError(f"offline sealing path issued a network git verb: {parts}")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _offline_run)


def test_emit_verify_succeed_offline(tmp_path: Path, monkeypatch: Any) -> None:
    """`emit` and `verify` run to success with the network stubbed to fail (NFR-2, S-15, ADR-4)."""
    dataset, candidate, clone = _prepare(tmp_path)
    sealed = RepoBuilder.build_sealed_repo(tmp_path / "sealed")

    _install_network_stub(monkeypatch)

    # emit assembles and verifies the datapoint over the local clone — no forge contact.
    task_dir = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, clone=clone).path
    assert task_dir.is_dir(), "emit must promote a clean datapoint while offline"

    # the standalone verify passes every check over the clone and the materialized seal — still offline.
    report = Verify.verify_task(task_dir, clone=clone, materialized_seal=sealed)
    assert report.ok, f"verify must pass offline: {[(r.check, r.offending) for r in (*report.failures, *report.unresolved)]}"
    assert report.exit_code == ExitCode.SUCCESS


def test_offline_guard_watched_to_fail(tmp_path: Path, monkeypatch: Any) -> None:
    """The offline guard trips on a network verb through either git chokepoint the seal path uses (FR-37).

    Watched-to-fail evidence for `test_emit_verify_succeed_offline`: if a regression let `emit`/`verify`
    fetch, clone, or pull, the stub would raise and this file would go red. Exercises both runners —
    `gitcmd` (the base/patch checks) and `seal.Seal.git` (the git-channel checklist) — so neither path
    can add a network call unnoticed.
    """
    _dataset, _candidate, clone = _prepare(tmp_path)

    _install_network_stub(monkeypatch)

    with pytest.raises(AssertionError, match="network git verb"):
        GitCommandRunner.git(clone, "fetch", "origin")
    with pytest.raises(AssertionError, match="network git verb"):
        Seal.git(clone, "fetch", "origin")

    # A local read the sealing path actually makes must still pass through the guard untouched.
    code, _out = GitCommandRunner.git(clone, "cat-file", "-t", "HEAD")
    assert code == 0, "a local git read must not be blocked by the offline guard"
