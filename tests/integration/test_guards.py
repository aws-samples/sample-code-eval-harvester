"""Guard-verification suite: every US-7 check watched to go red, then green (E-3, FR-37, NFR-6).

"A guard nobody has watched fail is decorative" (CLAUDE.md). E-1/E-2/D-3 *build* the checks; this
suite proves each one actually fires. For every US-7 invariant (S-2, S-3, S-4, S-8, S-9, S-12) a test
constructs the exact violation, asserts the specific check goes **red**, then asserts a corrected
input goes **green** — a test that only asserts green has not watched the guard fail, which is the
whole point of FR-37. The guards, their invariants, and the test that exercises each are declared in
``tests/guard_registry.py``; :func:`test_guard_report_covers_every_us7_check` asserts that registry is
complete, that every named test exists and asserts both red and green, and that the committed
``guard-report.md`` is regenerated from it and cannot silently drift.

The setup mirrors the rest of the suite: a real S-1 squash-merge fixture reconstructed offline, a
filled candidate, a genuinely emitted task directory, and — for the git-channel half — real fixture
repos with a leak planted in a named channel (a mock would prove nothing; the point is a tree that
greps clean while ``git show`` prints the answer). Each break is applied to a *copy* of the emitted
task so the clean original stays available for the paired green assertion.

Each guard test is labelled with ``# RED:`` at its violation assertion and ``# GREEN:`` at its
correction assertion; the coverage test enforces that both labels are present, so the break-then-fix
structure cannot quietly decay into a green-only test.
"""

from __future__ import annotations

import inspect
import json
import shutil
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fixtures.repo_builder import RepoBuilder
from guard_registry import GUARDS, REPORT_RELATIVE_PATH, render_guard_report, repo_root

from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.emit import Emit, EmitRefusalError
from eval_harvest.forge import Forge
from eval_harvest.riskmap import RiskMap
from eval_harvest.tomlw import emit_document
from eval_harvest.verify import CHANGE_PATCH_RELPATH, Verify, VerifyRefusal, VerifyReport

#: A risk map marking the fixture's only file high, so `risk_structural` is `high`; paired with a
#: `low` classification it makes the two disagree (S-9), and with a `high` one it aligns them.
_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}

#: The canonical US-7 guard set, stated independently of the registry so an edit to `GUARDS` that
#: drops or invents a guard fails the coverage test rather than silently changing what is covered.
_CANONICAL_GUARD_IDS = frozenset(
    {
        "content-leak",
        "git-channel-leak",
        "missing-control-token",
        "verdict-coherence",
        "risk-disagreement",
        "precondition-base",
        "precondition-patch",
        "precondition-finding-line",
        "precondition-rubric",
    }
)


# ───────────────────────────── candidate & task fixtures ─────────────────────────────


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


def _nits_only_findings() -> list[FindingDict]:
    """An approve oracle: a single low-severity nit, non-blocking (FR-17)."""
    return [
        {
            "comment_ids": [9002],
            "statement": "prefer a for-each loop for readability",
            "severity": "low",
            "severity_evidence": "range(len(items))",
            "severity_rationale": "stylistic, does not block a merge",
            "reference_only": False,
        }
    ]


def _fill(candidate: CandidateDict, findings: list[FindingDict], risk_classified: str) -> CandidateDict:
    """Classify every comment, set the findings, classify the risk, and pin the rubric version."""
    for comment in candidate["comments"]:
        comment["classification"] = "defect"
        comment["classification_rationale"] = "a substantive problem a reviewer would block on"
    candidate["findings"] = findings
    candidate["change_risk"]["risk_classified"] = risk_classified
    candidate["change_risk"]["risk_classified_rationale"] = "isolated helper, well covered by tests"
    candidate["rubric_version"] = "v1"
    return candidate


def _prepare(tmp_path: Path, findings: list[FindingDict], *, risk_classified: str = "low") -> tuple[Path, CandidateDict]:
    """A dataset holding a filled candidate and its rubric, from the S-1 squash-merge fixture (offline)."""
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
    candidate = _fill(Candidate.load(candidate_path), findings, risk_classified)
    Candidate.dump(candidate, candidate_path)
    return dataset, candidate


def _emit_reject_task(tmp_path: Path) -> tuple[Path, Path]:
    """Emit a real reject datapoint from the S-1 fixture and return `(task_dir, clone)`."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    fixture_clone = tmp_path / "clone"
    task_dir = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path
    return task_dir, fixture_clone


# ───────────────────────────── small assertion / mutation helpers ─────────────────────────────


def _refusals(report: VerifyReport) -> tuple[VerifyRefusal, ...]:
    """Every refusal in a report — hard failures and unresolved checks alike."""
    return (*report.failures, *report.unresolved)


def _has_refusal(report: VerifyReport, check: str) -> bool:
    """True when some refusal in the report has the check name `check`."""
    return any(refusal.check == check for refusal in _refusals(report))


def _refusal(report: VerifyReport, check: str) -> VerifyRefusal:
    """The single refusal named `check`; fail loudly if absent, listing what was there."""
    matches = [refusal for refusal in _refusals(report) if refusal.check == check]
    assert matches, f"no refusal named {check!r}; refusals were {[r.check for r in _refusals(report)]}"
    return matches[0]


def _harvest(task_dir: Path) -> dict[str, Any]:
    """The parsed `[metadata.harvest]` table of the emitted task."""
    harvest: dict[str, Any] = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))["metadata"]["harvest"]
    return harvest


def _copy_task(task_dir: Path, destination: Path) -> Path:
    """Copy an emitted task directory so a break can be applied without disturbing the clean original."""
    shutil.copytree(task_dir, destination)
    return destination


def _set_base_commit(task_dir: Path, base_commit: str) -> None:
    """Rewrite `[metadata.origin].base_commit` in the emitted `task.toml` (to break base-exists)."""
    text = (task_dir / "task.toml").read_text(encoding="utf-8")
    document = tomllib.loads(text)
    real = document["metadata"]["origin"]["base_commit"]
    (task_dir / "task.toml").write_text(text.replace(real, base_commit), encoding="utf-8")


# ───────────────────────────── S-2: content leak (answer-present) ─────────────────────────────


def test_guard_content_leak(tmp_path: Path) -> None:
    """The PR number leaking into an agent-visible file fires `answer-present`; removed, it passes (S-2)."""
    task_dir, clone = _emit_reject_task(tmp_path)

    # RED: the PR number in instruction.md is an answer the evaluated agent could look up.
    leaked = _copy_task(task_dir, tmp_path / "leaked")
    instruction = (leaked / "instruction.md").read_text(encoding="utf-8")
    (leaked / "instruction.md").write_text(instruction + "\nSee the original discussion in #1234.\n", encoding="utf-8")
    red = Verify.verify_task(leaked, clone=clone)
    assert _has_refusal(red, "answer-present"), "the content leak did not fire answer-present"
    assert "instruction.md" in _refusal(red, "answer-present").offending, "the refusal must name the leaking file"

    # GREEN: the untouched task carries no PR number in agent-visible content, so nothing leaks.
    green = Verify.verify_task(task_dir, clone=clone)
    assert not _has_refusal(green, "answer-present"), "a clean task must not fire answer-present"


# ───────────────────────────── base-tree suppression is narrow ─────────────────────────────


def test_guard_base_tree_suppression_is_not_a_blanket_exemption(tmp_path: Path) -> None:
    """A base-existing token is suppressed, but a change-introduced one still fires — the narrow rule watched both ways.

    The danger the suppression introduces is a blanket exemption that silently disables the most
    important leak check. This breaks the invariant deliberately: the *same* leaked token fails when
    the oracle says it is new (the check keeps its bite) and only clears when the oracle says it
    predates the change. A `# RED`/`# GREEN` pair over one token, so the exemption cannot widen unseen.
    """
    task_dir, clone = _emit_reject_task(tmp_path)
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    (task_dir / "instruction.md").write_text(instruction + "\nSee the original discussion in #1234.\n", encoding="utf-8")

    # RED: the token was introduced by the change (not at base), so it is a real leak and must fire.
    red = Verify.verify_task(task_dir, clone=clone, base_oracle=lambda _token: False)
    assert _has_refusal(red, "answer-present"), "a change-introduced token must still fail answer-present"

    # GREEN: the same token already exists at the base commit, so it predates the change and is suppressed.
    green = Verify.verify_task(task_dir, clone=clone, base_oracle=lambda _token: True)
    assert not _has_refusal(green, "answer-present"), "a base-existing token must be suppressed, not fired"
    assert green.content_scan is not None and len(green.content_scan.suppressed) == 1


# ───────────────────────────── S-3: git-channel leak (answer-present) ─────────────────────────────


def _git_channel_planters(tmp_path: Path) -> dict[str, Callable[[Path], None]]:
    """One planter per non-obvious git channel a leak can hide in — each makes `check_repo` fail (S-3)."""
    alternate_source = RepoBuilder.build_sealed_repo(tmp_path / "alt-source")
    return {
        "alternates": lambda repo: RepoBuilder.plant_alternates(repo, alternate_source / ".git" / "objects"),
        "packed-refs": RepoBuilder.plant_packed_refs,
        "commit-graph": RepoBuilder.plant_commit_graph,
        "replace-ref": RepoBuilder.plant_replace_ref,
        "include-path": RepoBuilder.plant_include_path,
        "reflog": RepoBuilder.plant_unreachable_reflog_entry,
        "bare-repo": RepoBuilder.plant_bare_repo,
    }


def test_guard_git_channel_leak(tmp_path: Path) -> None:
    """Each git channel leak in the materialized seal fires `answer-present`; a clean seal passes (S-3)."""
    task_dir, clone = _emit_reject_task(tmp_path)

    # RED: every non-obvious channel (alternates, packed-refs, commit-graph, refs/replace, include.path,
    # an unreachable reflog entry, a bare repo) leaves the tree greppable-clean yet reaches hidden
    # content — each must fire answer-present naming the sealed git tree.
    for channel, plant in _git_channel_planters(tmp_path).items():
        sealed = RepoBuilder.build_sealed_repo(tmp_path / f"sealed-{channel}")
        plant(sealed)
        report = Verify.verify_task(task_dir, clone=clone, materialized_seal=sealed)
        assert _has_refusal(report, "answer-present"), f"the {channel} channel leak did not fire answer-present"
        assert "sealed git tree" in _refusal(report, "answer-present").offending, f"{channel}: refusal must name the tree"

    # GREEN: a clean sealed tree over a sound datapoint passes every check.
    clean_seal = RepoBuilder.build_sealed_repo(tmp_path / "sealed-clean")
    green = Verify.verify_task(task_dir, clone=clone, materialized_seal=clean_seal)
    assert green.ok, f"a clean seal must pass: {[(r.check, r.offending) for r in _refusals(green)]}"


# ───────────────────────────── S-4: scanner honesty (scan-did-not-run) ─────────────────────────────


def test_guard_missing_control_token(tmp_path: Path) -> None:
    """With the control token not planted, the scan refuses `scan-did-not-run`; planted, it passes (S-4)."""
    task_dir, clone = _emit_reject_task(tmp_path)

    # RED: an unplanted control token means the scan cannot prove it read the corpus — an empty result
    # and a broken scan are otherwise identical, so it must refuse rather than report clean (NFR-6).
    red = Verify.verify_task(task_dir, clone=clone, plant_control_token=False)
    assert _has_refusal(red, "scan-did-not-run"), "an unplanted control token must refuse scan-did-not-run"

    # GREEN: with the token planted (the default), the scan proves it ran and reports clean.
    green = Verify.verify_task(task_dir, clone=clone, plant_control_token=True)
    assert not _has_refusal(green, "scan-did-not-run"), "a planted control token must let a clean scan pass"


# ───────────────────────────── S-8: verdict coherence (approve-with-high-finding) ─────────────────────────────


def test_guard_verdict_coherence(tmp_path: Path) -> None:
    """An approve datapoint carrying a high finding is refused; without it, approve emits (S-8, FR-14)."""
    # RED: an approved state that still carries a high-severity finding is incoherent — refuse it.
    dataset_high, candidate_high = _prepare(tmp_path / "high", _substantive_findings())
    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate_high, kind="approve", dataset_dir=dataset_high)
    assert refusal.value.check == "approve-with-high-finding", "an approve carrying a high finding must be refused"

    # GREEN: drop the high finding (nits only) and the same approve emits a task.
    dataset_nits, candidate_nits = _prepare(tmp_path / "nits", _nits_only_findings())
    task = Emit.emit_datapoint(candidate_nits, kind="approve", dataset_dir=dataset_nits).path
    assert task.is_dir(), "a nits-only approve datapoint must emit"
    assert _harvest(task)["expected_verdict"] == "approve"


# ───────────────────────────── S-9: risk disagreement recorded, not resolved ─────────────────────────────


def test_guard_risk_disagreement(tmp_path: Path) -> None:
    """A structural/classified risk disagreement is recorded with both values; aligned, the marker clears (S-9)."""
    # RED: structural (high) disagrees with the agent's classification (low) — both values and the
    # disagreement marker must survive, never collapse to one value (FR-20/21).
    dataset_dis, candidate_dis = _prepare(tmp_path / "disagree", _substantive_findings(), risk_classified="low")
    disagree = _harvest(Emit.emit_datapoint(candidate_dis, kind="reject", dataset_dir=dataset_dis).path)
    assert disagree["change_risk_structural"] == "high"
    assert disagree["change_risk_classified"] == "low"
    assert disagree["change_risk_disagreement"] is True, "a genuine disagreement must be recorded"

    # GREEN: align the classification (high) with the structural risk and the marker clears.
    dataset_agree, candidate_agree = _prepare(tmp_path / "agree", _substantive_findings(), risk_classified="high")
    agree = _harvest(Emit.emit_datapoint(candidate_agree, kind="reject", dataset_dir=dataset_agree).path)
    assert agree["change_risk_disagreement"] is False, "aligned risks must clear the disagreement marker"
    assert agree["change_risk_structural"] == agree["change_risk_classified"]


# ───────────────────────────── S-12: well-formedness preconditions ─────────────────────────────


def test_guard_preconditions(tmp_path: Path) -> None:
    """Each of the four broken candidates fires its own specific check; the sound one fires none (S-12)."""
    task_dir, clone = _emit_reject_task(tmp_path)

    # GREEN: the sound datapoint trips none of the four precondition checks.
    green = Verify.verify_task(task_dir, clone=clone)
    for check in ("base-missing", "patch-does-not-apply", "finding-line-absent", "rubric-incoherent"):
        assert not _has_refusal(green, check), f"{check} fired on a sound datapoint"

    # RED: a base commit absent from the clone fails base-missing.
    base_broken = _copy_task(task_dir, tmp_path / "broken-base")
    _set_base_commit(base_broken, "0" * 40)
    assert _has_refusal(Verify.verify_task(base_broken, clone=clone), "base-missing"), "missing base must fail base-missing"

    # RED: a change.patch that will not apply at the base fails patch-does-not-apply.
    patch_broken = _copy_task(task_dir, tmp_path / "broken-patch")
    patch = (patch_broken / CHANGE_PATCH_RELPATH).read_text(encoding="utf-8")
    (patch_broken / CHANGE_PATCH_RELPATH).write_text(patch.replace("def add", "def subtract_totally_different"), encoding="utf-8")
    assert _has_refusal(Verify.verify_task(patch_broken, clone=clone), "patch-does-not-apply"), "bad patch must fail"

    # RED: an oracle finding on a line the change does not touch fails finding-line-absent.
    line_broken = _copy_task(task_dir, tmp_path / "broken-line")
    oracle = json.loads((line_broken / "tests" / "oracle.json").read_text(encoding="utf-8"))
    oracle["findings"][0]["locations"] = [{"path": "code.py", "line_start": 999, "line_end": 999}]
    (line_broken / "tests" / "oracle.json").write_text(json.dumps(oracle, indent=2) + "\n", encoding="utf-8")
    assert _has_refusal(Verify.verify_task(line_broken, clone=clone), "finding-line-absent"), "off-diff finding must fail"

    # RED: a blank rubric — a datapoint graded against nothing — fails rubric-incoherent.
    rubric_broken = _copy_task(task_dir, tmp_path / "broken-rubric")
    (rubric_broken / "tests" / "rubric.md").write_text("", encoding="utf-8")
    assert _has_refusal(Verify.verify_task(rubric_broken, clone=clone), "rubric-incoherent"), "blank rubric must fail"


# ───────────────────────────── the coverage & report guard (FR-37) ─────────────────────────────


def test_guard_report_covers_every_us7_check() -> None:
    """The registry covers every US-7 check, each test asserts red+green, and the report cannot drift (FR-37)."""
    assert {guard.guard_id for guard in GUARDS} == _CANONICAL_GUARD_IDS, "the guard registry omits or invents a check"

    module = sys.modules[__name__]
    for guard in GUARDS:
        test_fn = getattr(module, guard.test, None)
        assert callable(test_fn), f"guard {guard.guard_id} names a missing test {guard.test!r}"
        source = inspect.getsource(test_fn)
        assert "# RED:" in source, f"{guard.test} has no watched-red assertion (FR-37 needs both red and green)"
        assert "# GREEN:" in source, f"{guard.test} has no watched-green assertion (FR-37 needs both red and green)"

    committed = (repo_root() / REPORT_RELATIVE_PATH).read_text(encoding="utf-8")
    assert committed == render_guard_report(), "guard-report.md is stale — regenerate with `uv run scripts/gen_guard_report.py`"
