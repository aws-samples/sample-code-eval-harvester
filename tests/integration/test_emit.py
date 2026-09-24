"""Tests for the `emit` verb and the verifier template (task D-3).

`emit` is the integration point: a filled candidate becomes a self-contained Harbor task directory.
These scenarios pin the properties that make that task trustworthy and non-cheatable — the verdict
follows `--kind` and never a severity formula (S-8), a reject with no substantive finding refuses
rather than shipping an empty oracle (S-6), an approve carrying a high finding refuses (S-8), the
change-risk disagreement is kept not collapsed (S-9), the task is `separate`-mode/Linux with
provenance and a verifier-only `solution/` (S-10), the instruction leaks nothing through phrasing
(S-11), and emission is byte-identical twice including a stable content digest (S-14). The reward
object's shape is exercised against a recorded judge (S-19) that replays pinned decisions — because
the live judge needs a model the tree does not carry.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import shutil
import subprocess  # nosec B404 - runs the emitted solve.sh to prove its inlined payload survives the shell
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fixtures.repo_builder import RepoBuilder

import eval_harvest
from eval_harvest.candidate import Candidate, CandidateDict, FindingDict
from eval_harvest.cli import Cli, ExitCode
from eval_harvest.emit import REWARD_KEYS, Emit, EmitRefusalError, EmitResult, HookResult, UnresolvedCheck, VerifyFailure
from eval_harvest.forge import Forge
from eval_harvest.riskmap import RiskMap
from eval_harvest.seal import Seal
from eval_harvest.tomlw import emit_document
from eval_harvest.verify import CHANGE_PATCH_RELPATH, Verify

#: A scan-liveness marker planted in the leaked corpus and passed as ``control_token=`` — not a
#: credential. Held in a constant (not an inline literal) so Bandit's B106 has no literal funcarg to
#: read as a hardcoded password, the same way ``test_seal.py`` carries its ``CONTROL_TOKEN``.
CONTROL_TOKEN = "EVAL-HARVEST-CONTROL-TOKEN"  # nosec B105 - a scan-liveness marker, not a credential

#: A risk map marking the fixture's only file high, so `risk_structural` is `high` and (with the
#: agent's `low` classification below) the two disagree — the S-9 signal we assert is preserved.
_RISK_MAP: dict[str, Any] = {"version": "v1", "default": "medium", "rule": [{"prefix": "code.py", "risk": "high"}]}

_SHA256_FORM = re.compile(r"^sha256:[a-f0-9]{64}$")


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


def _prepare(tmp_path: Path, findings: list[FindingDict], *, risk_classified: str = "low") -> tuple[Path, CandidateDict]:
    """A dataset holding a filled candidate (facts materialized, judgment slots filled) and its rubric.

    Reconstructs the S-1 squash-merge fixture offline, materializes the candidate and its patches the
    way `capture` would, then fills the judgment slots so `emit` has a coherent candidate to read.
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
    candidate = _fill(Candidate.load(candidate_path), findings, risk_classified)
    Candidate.dump(candidate, candidate_path)
    return dataset, candidate


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


def _harvest(task_dir: Path) -> dict[str, Any]:
    """The parsed `[metadata.harvest]` table of the emitted task."""
    harvest: dict[str, Any] = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))["metadata"]["harvest"]
    return harvest


def _read_tree(root: Path) -> dict[str, bytes]:
    """Every file under `root`, keyed by its POSIX-relative path — the golden set for byte-equality."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


# ───────────────────────────── verdict, coherence, refusals ─────────────────────────────


def test_verdict_follows_kind_not_severity(tmp_path: Path) -> None:
    """`--kind` sets the verdict (reject⇒block, approve⇒approve), never a severity formula (FR-14, S-8)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())

    reject = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path
    assert _harvest(reject)["expected_verdict"] == "block"
    assert _harvest(reject)["kind"] == "reject"

    dataset_a, candidate_a = _prepare(tmp_path / "approve", _nits_only_findings())
    approve = Emit.emit_datapoint(candidate_a, kind="approve", dataset_dir=dataset_a).path
    assert _harvest(approve)["expected_verdict"] == "approve"


def test_approve_with_high_finding_refused(tmp_path: Path) -> None:
    """`--kind approve` on a candidate carrying a high finding refuses — verdict and findings agree (S-8)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())  # carries a high finding

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="approve", dataset_dir=dataset)
    assert refusal.value.check == "approve-with-high-finding"
    assert refusal.value.exit_code == ExitCode.REFUSAL


def test_reject_with_no_finding_refuses_empty_oracle(tmp_path: Path) -> None:
    """`--kind reject` with no substantive finding refuses `empty-oracle` (exit 3), never ships empty (S-6)."""
    dataset, candidate = _prepare(tmp_path, [])

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)
    assert refusal.value.check == "empty-oracle"
    assert refusal.value.exit_code == ExitCode.REFUSAL
    assert not (dataset / "tasks").exists() or not any((dataset / "tasks").iterdir()), "no task may be written on a refusal"


def test_emit_still_refuses_unrecoverable_iteration(tmp_path: Path) -> None:
    """`emit`'s own `unrecoverable-iteration` guard fires even though `capture` refuses earlier.

    A candidate can reach `emit` with no recoverable iteration from a hand-edited file, so `emit` must
    stay safe on its own. Blanking every iteration's base and patch (the recoverability facts) leaves
    `_select_iteration` nothing to build from. The check name stays distinct from capture's
    `no-emittable-iteration` so a driver can tell which verb spoke.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    for iteration in candidate["iterations"]:
        iteration["base_sha"] = ""
        iteration["patch_path"] = ""

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)
    assert refusal.value.check == "unrecoverable-iteration"
    assert refusal.value.exit_code == ExitCode.REFUSAL


def test_nits_only_emits_approve_nonblocking(tmp_path: Path) -> None:
    """A nits-only PR emits an approve datapoint with the nit recorded non-blocking (FR-17, S-6)."""
    dataset, candidate = _prepare(tmp_path, _nits_only_findings())

    # Reject refuses (no substantive finding); approve succeeds with the nit non-blocking.
    with pytest.raises(EmitRefusalError):
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)

    task = Emit.emit_datapoint(candidate, kind="approve", dataset_dir=dataset).path
    oracle = _read_json(task / "tests" / "oracle.json")
    assert oracle["findings"], "the nit is recorded as a reference finding in tests/"
    assert all(not finding["blocking"] for finding in oracle["findings"]), "a nit is never a blocking oracle finding"
    assert _harvest(task)["expected_verdict"] == "approve"


def test_risk_disagreement_recorded(tmp_path: Path) -> None:
    """When structural≠classified, both values and `change_risk_disagreement=true` survive (FR-19/21, S-9)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings(), risk_classified="low")  # structural is high

    harvest = _harvest(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path)
    assert harvest["change_risk_structural"] == "high"
    assert harvest["change_risk_classified"] == "low"
    assert harvest["change_risk_disagreement"] is True


# ───────────────────────────── layout, provenance, sealing ─────────────────────────────


def test_task_toml_separate_mode_linux(tmp_path: Path) -> None:
    """The task is `separate` mode, Linux, with `[metadata.origin]` provenance (FR-32, NFR-8, S-10)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path
    document = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))

    assert document["verifier"]["environment_mode"] == "separate"
    assert document["environment"]["os"] == "linux"
    assert document["verifier"]["environment"]["os"] == "linux"
    origin = document["metadata"]["origin"]
    assert origin["repo"] == "our-org/our-repo"
    assert origin["pr_numbers"] == [1234]
    assert origin["base_commit"], "the base commit is recorded provenance"
    assert document["task"]["name"] == "our-org/our-repo__pr1234-reject"


def test_sealing_dockerfile_has_every_step(tmp_path: Path) -> None:
    """The environment Dockerfile carries each FR-30 sealing step (clone, reset, deref, expire, prune)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path
    dockerfile = (task / "environment" / "Dockerfile").read_text(encoding="utf-8")

    for step in ("git clone --depth 1", "git reset --hard", "git remote remove origin", "reflog expire", "gc --prune=now"):
        assert step in dockerfile, f"the sealing Dockerfile is missing the step: {step!r}"
    assert "git apply" in dockerfile, "the change under review must be applied over the base"


def test_solution_excluded_from_agent_env(tmp_path: Path) -> None:
    """`solution/solve.sh` is present but verifier-only — absent from the agent-visible tree (FR-40, S-10)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    assert (task / "solution" / "solve.sh").is_file()
    agent_visible = {path.relative_to(task).as_posix() for path in Seal._agent_visible_files(task)}
    assert "solution/solve.sh" not in agent_visible, "the oracle must never be in the agent-visible corpus"
    assert "instruction.md" in agent_visible and "environment/change.patch" in agent_visible, "the agent does see these"


# ───────────────────────────── the build context (FR-28) ─────────────────────────────


def _copy_sources(dockerfile_text: str) -> list[str]:
    """The source arguments of every `COPY` in a Dockerfile, ignoring `--flag` options and the dest."""
    sources: list[str] = []
    for line in dockerfile_text.splitlines():
        if line.strip().startswith("COPY "):
            arguments = [token for token in line.split()[1:] if not token.startswith("--")]
            sources.extend(arguments[:-1])  # every argument but the destination
    return sources


def test_every_dockerfile_copy_source_exists_in_context(tmp_path: Path) -> None:
    """Each `COPY` source in `environment/Dockerfile` resolves to a file inside `environment/` (FR-28).

    Every `COPY` source must resolve inside the build context: a sealing Dockerfile that did
    `COPY change.patch` while the file lived at the task root, outside the build context
    (`environment/`), could build no image. Parsing the sources rather than hard-coding `change.patch`
    covers any future `COPY` too.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path
    environment = task / "environment"

    sources = _copy_sources((environment / "Dockerfile").read_text(encoding="utf-8"))
    assert sources, "the sealing Dockerfile has no COPY to check"
    for source in sources:
        assert (environment / source).is_file(), f"COPY {source} resolves outside the build context {environment}"


def test_change_patch_is_emitted_inside_environment(tmp_path: Path) -> None:
    """The patch is at `environment/change.patch` and *not* at the task root — moved, not duplicated."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    assert (task / "environment" / "change.patch").is_file(), "the patch must ship inside the build context"
    assert not (task / "change.patch").exists(), "the patch must not also sit at the task root (no duplication)"


def test_change_patch_stays_in_the_agent_visible_corpus(tmp_path: Path) -> None:
    """The patch is agent-visible content the FR-35 scan covers, even living inside `environment/`."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    agent_visible = {path.relative_to(task).as_posix() for path in Seal._agent_visible_files(task)}
    assert "environment/change.patch" in agent_visible, "the patch is applied into the agent's image; the scan must see it"


# ───────────────────────────── the oracle solution (FR-40) ─────────────────────────────


def _hostile_findings() -> list[FindingDict]:
    """One finding whose statement carries every character that could break a generated shell script."""
    return [
        {
            "comment_ids": [9001],
            "statement": "don't use `$HOME` here: \"$(rm -rf /)\" and 'PY' and a\nnewline — 100% wrong \\ backslash",
            "severity": "high",
            "severity_evidence": "return a - b in the round-1 diff",
            "severity_rationale": "wrong result for every caller",
            "reference_only": False,
        }
    ]


def _submission_from_oracle(task_dir: Path) -> list[dict[str, Any]]:
    """The submission `solve.sh` ought to make, derived independently from the emitted `oracle.json`."""
    oracle = json.loads((task_dir / "tests" / "oracle.json").read_text(encoding="utf-8"))
    return [
        {
            "path": location["path"],
            "line": location["line_start"],
            "statement": finding["statement"],
            "severity": finding["severity"],
        }
        for finding in oracle["findings"]
        for location in finding["locations"]
    ]


def _run_solve_script(task_dir: Path, tmp_path: Path) -> list[dict[str, Any]]:
    """Run the emitted `solve.sh` and return the submission it wrote — the real shell-safety test."""
    findings_path = tmp_path / "agent-run" / "findings.json"
    completed = subprocess.run(  # nosec B603 - runs the sh script this test just emitted, with a fixed argv
        ["/bin/sh", str(task_dir / "solution" / "solve.sh")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "EVAL_HARVEST_AGENT_FINDINGS": str(findings_path)},
        check=False,
    )
    assert completed.returncode == 0, f"solve.sh exited {completed.returncode}: {completed.stderr}"
    submission: list[dict[str, Any]] = json.loads(findings_path.read_text(encoding="utf-8"))
    return submission


def test_solve_sh_does_not_read_tests_directory(tmp_path: Path) -> None:
    """The emitted `solve.sh` reads no path under `/tests/` (FR-40).

    Reading `/tests/oracle.json` would die `FileNotFoundError`: in `separate` mode `tests/` is baked
    into the *verifier* image and is absent from the agent environment, where this script runs, so the
    known-good submission would never be made.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    # Comment lines are exempt: the script should *say* why it does not read /tests/, and the header
    # explaining that keeps a reader from assuming it does.
    script = (task / "solution" / "solve.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "/tests/" not in code, "solve.sh must not read the verifier image's tests/ — it is absent where it runs"
    assert "oracle.json" not in code, "the findings are inlined at emit time; nothing is read back"
    assert "separate" in script and "tech-plan.md:489" in script, "the header must state why the findings are inlined"


def test_solve_sh_inlines_every_oracle_location(tmp_path: Path) -> None:
    """`solve.sh` submits one record per `(finding, location)` pair, equal to what `oracle.json` implies.

    Catches a payload that drifts from the oracle it mirrors — a divergence would make the oracle agent
    score below 1.0 for a reason that has nothing to do with the reward stack.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    assert _run_solve_script(task, tmp_path) == _submission_from_oracle(task)


def test_solve_sh_payload_is_shell_safe(tmp_path: Path) -> None:
    """A statement carrying quotes, `$`, backticks, a newline and the heredoc terminator survives verbatim.

    Finding statements are reviewer prose. Rather than reasoning about the quoting, this runs the
    generated script and compares what it wrote against the oracle — the only check that would notice
    the shell expanding `$(…)` or a payload line closing the heredoc early.
    """
    dataset, candidate = _prepare(tmp_path, _hostile_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    submission = _run_solve_script(task, tmp_path)
    assert submission == _submission_from_oracle(task)
    assert submission[0]["statement"] == _hostile_findings()[0]["statement"], "the statement must survive byte-for-byte"


def test_solve_sh_is_byte_identical_across_emits(tmp_path: Path) -> None:
    """The rendered `solve.sh` is a byte-identical function of the candidate (NFR-1).

    It is now part of the content digest's input, so dict or set iteration order leaking into the
    payload would make every task's digest unstable.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())

    first = (Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path / "solution" / "solve.sh").read_bytes()
    second = (Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path / "solution" / "solve.sh").read_bytes()
    assert first == second, "the inlined payload must be deterministic"


def test_solution_is_absent_from_the_agent_build_context(tmp_path: Path) -> None:
    """`solution/` is outside the agent image's build context and is never copied into it (FR-35, FR-40).

    Inlining the findings puts the answer in `solve.sh`'s bytes, so the reason that is safe — the graded
    agent's image is built from `environment/` and cannot reach `solution/` — has to be checked, not
    assumed. Catches the unsafe alternative fix of shipping the answer into the agent environment.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    context = {path.relative_to(task / "environment").as_posix() for path in (task / "environment").rglob("*")}
    assert not any(entry.startswith("solution") for entry in context), f"solution/ is inside the build context: {context}"
    dockerfile = (task / "environment" / "Dockerfile").read_text(encoding="utf-8")
    assert "solution" not in dockerfile, "the agent Dockerfile must not reference solution/"


def test_the_scan_mines_the_inlined_payload_as_answer_material(tmp_path: Path) -> None:
    """The FR-35 scan reads `solve.sh`'s *contents*, so the inlined payload leaking is caught (FR-40).

    The reference findings live in `solve.sh`'s bytes, so "the scan covers solve.sh" has to mean its
    contents and not merely its path. Planting the payload line into an agent-visible file must go red.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path
    assert (task / "solution" / "solve.sh") in Seal._answer_source_files(task), "solve.sh must be an answer source"

    script_lines = (task / "solution" / "solve.sh").read_text(encoding="utf-8").splitlines()
    payload_line = max((line.strip() for line in script_lines), key=len)
    assert _substantive_findings()[0]["statement"] in payload_line, "the longest line should be the inlined payload"
    leaked = tmp_path / "leaked"
    shutil.copytree(task, leaked)
    with (leaked / "instruction.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n{payload_line}\n")
    (leaked / "control.txt").write_text(f"{CONTROL_TOKEN}\n", encoding="utf-8")

    report = Seal.scan_agent_visible(leaked, tokens=[], control_token=CONTROL_TOKEN)
    assert any(finding.check == "answer-source-leaked" for finding in report.failures), (
        f"the inlined payload in an agent-visible file must fail the scan; got {[f.check for f in report.failures]}"
    )


def _solve_sh_findings_default(script: str) -> str:
    """The path `solve.sh` writes to when `EVAL_HARVEST_AGENT_FINDINGS` is unset — its `:-<default>` fallback."""
    match = re.search(r"EVAL_HARVEST_AGENT_FINDINGS:-([^}\"]+)", script)
    assert match is not None, "solve.sh must define a default findings path"
    return match.group(1)


def test_agent_findings_path_reaches_the_separate_verifier(tmp_path: Path) -> None:
    """The submission path is one Harbor carries into the separate verifier, and all three files agree.

    In `separate` mode the verifier runs in its own environment and Harbor transfers only the agent
    environment's artifacts dir (`/logs/artifacts`) into it — never `/logs/agent` (trial.py:706-712
    `upload_artifacts`, and `with_convention_entry` always injects the `/logs/artifacts` entry). An
    oracle that submitted the reference findings to `/logs/agent/findings.json` would score
    `coverage_all = 0`, because `score.py` would read an empty submission from a path that never
    crossed the boundary — indistinguishable from silence. The three files that name this path —
    `instruction.md` (the graded agent), `solve.sh` (the oracle), `score.py` (the reader) — must agree
    on a path under the transferred convention dir, or a correct submission scores zero.
    """
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    task = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    transferred = "/logs/artifacts/findings.json"
    unreachable = "/logs/agent/findings.json"  # written by the agent, never seen by the verifier

    score_default = str(_load_score_module()._DEFAULT_AGENT_FINDINGS)
    solve_default = _solve_sh_findings_default((task / "solution" / "solve.sh").read_text(encoding="utf-8"))
    instruction = (task / "instruction.md").read_text(encoding="utf-8")

    assert score_default == transferred, f"score.py reads {score_default!r}, not the transferred artifacts dir"
    assert solve_default == transferred, f"solve.sh writes {solve_default!r}, not the transferred artifacts dir"
    assert transferred in instruction, "instruction.md must name the transferred path the graded agent writes to"

    for name, value in (("score.py", score_default), ("solve.sh", solve_default), ("instruction.md", instruction)):
        assert unreachable not in value, f"{name} still targets /logs/agent — a submission there never reaches the verifier"


# ───────────────────────────── uniformity & determinism ─────────────────────────────


def test_instruction_uniform_modulo_slots(tmp_path: Path) -> None:
    """Two datapoints' `instruction.md` are byte-identical — no verdict inferable from phrasing (FR-33, S-11)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    reject = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path

    dataset_a, candidate_a = _prepare(tmp_path / "other", _nits_only_findings())
    approve = Emit.emit_datapoint(candidate_a, kind="approve", dataset_dir=dataset_a).path

    assert (reject / "instruction.md").read_bytes() == (approve / "instruction.md").read_bytes()


def test_emit_byte_identical_twice(tmp_path: Path) -> None:
    """Two emits from one unchanged candidate yield a byte-identical directory (golden, NFR-1, S-14)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())

    first = _read_tree(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path)
    second = _read_tree(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path)
    assert first == second, "emission must be a byte-identical function of the candidate (NFR-1)"


def test_content_digest_recorded_and_stable(tmp_path: Path) -> None:
    """`content_digest` is `sha256:`-form, stable across emits, and changes when the content does (NFR-1)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())

    first = _harvest(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path)["content_digest"]
    assert _SHA256_FORM.match(first), f"digest {first!r} is not sha256:<64 hex>"
    again = _harvest(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path)["content_digest"]
    assert first == again, "the digest is byte-stable across emits from an unchanged candidate"

    candidate["findings"][0]["statement"] = "a different defect entirely"
    changed = _harvest(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset).path)["content_digest"]
    assert changed != first, "the digest must change when the emitted content changes"


# ───────────────────────────── verify hook: refuse before write, override ─────────────────────────────


def _failing_hook(task_dir: Path) -> HookResult:  # noqa: ARG001 - a fixed failure regardless of the dir
    """A stand-in for the real `verify` (E-4) that always reports one leak."""
    return HookResult(
        passed=(),
        failures=(VerifyFailure(check="answer-present", offending="#1234 at instruction.md:6", next_="remove the leak"),),
        unresolved=(),
    )


def _unresolved_hook(task_dir: Path) -> HookResult:  # noqa: ARG001 - a fixed result regardless of the dir
    """A stand-in that passes three checks and leaves two unresolved — the no-runtime, no-clone shape."""
    return HookResult(
        passed=("finding-lines-present", "rubric-coherent", "content-absence"),
        failures=(),
        unresolved=(
            UnresolvedCheck(check="base-and-patch", reason="no clone provided"),
            UnresolvedCheck(check="git-channel-absence", reason="no container runtime"),
        ),
    )


def _clean_hook(task_dir: Path) -> HookResult:  # noqa: ARG001 - a fixed all-passed result regardless of the dir
    """A stand-in where every check ran and passed — the machine-with-a-runtime shape."""
    return HookResult(passed=tuple(f"check-{index}" for index in range(10)), failures=(), unresolved=())


def test_verify_failure_refuses_before_write(tmp_path: Path) -> None:
    """`emit` runs `verify` and refuses (exit 4) on a failure, writing nothing (FR-38, §7.2)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, verify_hook=_failing_hook)
    assert refusal.value.check == "answer-present"
    assert refusal.value.exit_code == ExitCode.VERIFICATION
    assert not (dataset / "tasks").exists() or not any((dataset / "tasks").iterdir()), "a failing datapoint is not written"


def test_override_records_and_proceeds(tmp_path: Path) -> None:
    """An explicit `--override <check>` lets a failed check through and records it in `task.toml` (FR-38)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())

    task = Emit.emit_datapoint(
        candidate, kind="reject", dataset_dir=dataset, overrides=("answer-present",), verify_hook=_failing_hook
    ).path
    assert _harvest(task)["overrides"] == ["answer-present"]


# ─────────── emit reports which checks ran, passed, and stayed unresolved ───────────


def _emit_result_with_unresolved() -> EmitResult:
    """The shape `emit` returns on this machine: three checks passed, two unresolved (no clone, no runtime)."""
    return EmitResult(
        path=Path("tasks/our-org__our-repo__pr1234-reject"),
        passed=("finding-lines-present", "rubric-coherent", "content-absence"),
        unresolved=(
            UnresolvedCheck(check="base-and-patch", reason="no clone provided"),
            UnresolvedCheck(check="git-channel-absence", reason="no container runtime"),
        ),
        selection="selected iteration 0 (first recoverable, reject)",
        iteration_index=0,
    )


def test_emit_does_not_claim_all_checks_passed_when_unresolved(capsys: pytest.CaptureFixture[str]) -> None:
    """The false "all checks passed" line must never appear while a check is unresolved."""
    Cli._report_emit(_emit_result_with_unresolved(), json_mode=False)
    assert "all checks passed" not in capsys.readouterr().out


def test_emit_names_each_unresolved_check_and_reason(capsys: pytest.CaptureFixture[str]) -> None:
    """Every unresolved check appears with its reason and a runnable `next:` command — not a bare count."""
    Cli._report_emit(_emit_result_with_unresolved(), json_mode=False)
    out = capsys.readouterr().out
    assert "base-and-patch" in out and "no clone provided" in out
    assert "git-channel-absence" in out and "no container runtime" in out
    assert "next: eval-harvest verify tasks/our-org__our-repo__pr1234-reject" in out


def test_emit_reports_all_passed_when_nothing_unresolved(capsys: pytest.CaptureFixture[str]) -> None:
    """With nothing unresolved the clean message names the count, so a green run is unambiguous."""
    result = EmitResult(
        path=Path("tasks/x"),
        passed=tuple(f"check-{index}" for index in range(10)),
        unresolved=(),
        selection="selected iteration 0 (first recoverable, reject)",
        iteration_index=0,
    )
    Cli._report_emit(result, json_mode=False)
    assert "10 check(s) ran and passed; none unresolved" in capsys.readouterr().out


def test_emit_json_checks_payload(capsys: pytest.CaptureFixture[str]) -> None:
    """The `--json` payload carries the documented `checks` object so a driver can gate on verification status."""
    Cli._report_emit(_emit_result_with_unresolved(), json_mode=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["passed"] == ["finding-lines-present", "rubric-coherent", "content-absence"]
    assert payload["checks"]["unresolved"] == [
        {"check": "base-and-patch", "reason": "no clone provided"},
        {"check": "git-channel-absence", "reason": "no container runtime"},
    ]


def test_emit_still_refuses_on_failures(tmp_path: Path) -> None:
    """A hook returning a failure still refuses (exit 4) and writes nothing — unresolved must not swallow it."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, verify_hook=_failing_hook)
    assert refusal.value.exit_code == ExitCode.VERIFICATION
    assert not (dataset / "tasks").exists() or not any((dataset / "tasks").iterdir())


def test_emit_exit_code_zero_with_unresolved(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """On this machine (no clone, no runtime) `emit` exits 0 with unresolved checks — never forced to 4/5."""
    dataset, _ = _prepare(tmp_path, _substantive_findings())
    candidate_path = dataset / "candidates" / "pr-1234.json"

    code = Cli.run(["emit", str(candidate_path), "--kind", "reject"])
    out = capsys.readouterr().out

    assert code == ExitCode.SUCCESS, "unresolved checks must not change emit's exit code"
    assert "all checks passed" not in out, "the git-channel/base checks could not run here"
    assert "base-and-patch" in out, "the unresolved checks are named so the driver can act"


def test_emit_hook_result_carries_unresolved(tmp_path: Path) -> None:
    """The real hook propagates `report.unresolved` rather than dropping it at the hook boundary."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    result = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)  # no clone, no seal
    names = {check.check for check in result.unresolved}
    assert "base-and-patch" in names, "no clone → base/patch unresolved, propagated not dropped"
    assert "git-channel-absence" in names, "no runtime → git-channel unresolved, propagated not dropped"


def test_emit_without_clone_marks_clone_checks_unresolved(tmp_path: Path) -> None:
    """A missing `--clone` is `unresolved` with reason `no clone provided` — not a failure that would refuse."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    result = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)
    base = [check for check in result.unresolved if check.check == "base-and-patch"]
    assert base and base[0].reason == "no clone provided"


def test_emit_clone_flag_is_passed_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--clone <dir>` reaches `emit_datapoint`'s `clone` parameter — a param the core had but the CLI never fed."""
    dataset, _ = _prepare(tmp_path, _substantive_findings())
    candidate_path = dataset / "candidates" / "pr-1234.json"
    a_clone = tmp_path / "a-clone"
    a_clone.mkdir()
    captured: dict[str, Any] = {}

    def _spy(candidate: Any, **kwargs: Any) -> EmitResult:  # noqa: ARG001 - the candidate is irrelevant to the passthrough check
        captured.update(kwargs)
        return EmitResult(
            path=tmp_path / "spied",
            passed=(),
            unresolved=(),
            selection="selected iteration 0 (first recoverable, reject)",
            iteration_index=0,
        )

    monkeypatch.setattr(Emit, "emit_datapoint", staticmethod(_spy))
    code = Cli.run(["emit", str(candidate_path), "--kind", "reject", "--clone", str(a_clone)])

    assert code == ExitCode.SUCCESS
    assert captured["clone"] == a_clone


def test_emit_and_verify_agree_on_unresolved_set(tmp_path: Path) -> None:
    """`emit` and `verify` name the same unresolved checks for the same task dir — one story, not two."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    result = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)

    emit_unresolved = {check.check for check in result.unresolved}
    verify_unresolved = {refusal.check for refusal in Verify.verify_task(result.path, clone=None).unresolved}
    assert emit_unresolved == verify_unresolved, "the two verbs must agree about what is unresolved"


# ─────────── emit binds the oracle to the emitted iteration and can choose it ───────────


def _set_in_diff(candidate: CandidateDict, coverage: dict[int, list[int]]) -> None:
    """Overwrite `in_diff_iterations` for the named comment ids — the diff geometry, forced for a scenario.

    Lets a test say exactly which iterations' diffs a comment lives in, so the `emit` iteration-
    mismatch decision can be exercised without hand-building a diff whose spans land just so."""
    for comment in candidate["comments"]:
        if comment["id"] in coverage:
            comment["in_diff_iterations"] = coverage[comment["id"]]


def test_emit_refuses_when_no_finding_is_in_the_selected_diff(tmp_path: Path) -> None:
    """A reject built from iteration 0 whose every finding lives only in a later round refuses (FR-17).

    The `finding-line-absent` case is caught at `emit` before a byte is written instead of by `verify`
    at the end of the pipeline — with each offending finding named."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    _set_in_diff(candidate, {9001: [1], 9002: [1]})  # both findings live only in iteration 1

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)

    value = refusal.value
    assert value.check == "finding-iteration-mismatch"
    assert value.exit_code == ExitCode.REFUSAL
    assert value.datapoint == "pr-1234/reject"
    assert "none of the 2 oracle finding(s)" in value.offending
    assert "finding 1 lives in iterations [1]" in value.offending
    assert "finding 2 lives in iterations [1]" in value.offending
    assert "emit --iteration 1" in value.next_
    assert "show candidates/pr-1234.json --comment 9001" in value.next_


def test_emit_refuses_when_only_some_findings_mismatch(tmp_path: Path) -> None:
    """A partial mismatch refuses too, naming only the offenders — a partly unreachable oracle silently
    deflates `coverage_all`, a plausible-looking wrong number that is worse than a refusal."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    _set_in_diff(candidate, {9001: [0, 1], 9002: [1]})  # finding 1 reachable in iter 0, finding 2 only in iter 1

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)

    value = refusal.value
    assert value.check == "finding-iteration-mismatch"
    assert "1 of 2 oracle finding(s) cannot be located in its diff" in value.offending
    assert "finding 2 lives in iterations [1]" in value.offending
    assert "finding 1 lives" not in value.offending  # the reachable finding is not named an offender
    assert "emit --iteration 1" in value.next_
    assert "show candidates/pr-1234.json --comment 9002" in value.next_


def test_emit_writes_nothing_on_mismatch(tmp_path: Path) -> None:
    """The mismatch is caught *before* assembly — no half-written task directory is left for `dataset` to count.

    Asserting the check name (not just that *something* raised) is deliberate: `verify`'s structural
    `finding-line-absent` backstop would also refuse this candidate, but only after the task directory
    was assembled and torn down. This pins the pre-write guard, the whole point of moving it to `emit`."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    _set_in_diff(candidate, {9001: [1], 9002: [1]})

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)

    assert refusal.value.check == "finding-iteration-mismatch", "the pre-write guard must fire, not verify's later backstop"
    assert not (dataset / "tasks").exists() or not any((dataset / "tasks").iterdir()), "no task may be written on a refusal"


def test_file_level_finding_does_not_mismatch(tmp_path: Path) -> None:
    """A finding whose comments are all file-level (empty `path`) has no location and never mismatches.

    `_one_oracle_finding` mines no location from such a comment, so it is graded on neither side;
    flagging it would turn every file-level finding into a spurious refusal. `capture` requires a path
    on every inline comment (`_refuse_unfilled` rejects an empty one before this check), so the
    property is pinned directly on `_refuse_finding_iteration_mismatch` — the only place it can arise."""
    _, candidate = _prepare(tmp_path, _substantive_findings())
    # Finding 1 (9001) is reachable in iteration 0; finding 2 (9002) is file-level — no path, so no
    # location — and must not be counted a mismatch even though iteration 0 does not "cover" it.
    _set_in_diff(candidate, {9001: [0, 1], 9002: []})
    for comment in candidate["comments"]:
        if comment["id"] == 9002:
            comment["path"] = ""

    # No raise: the file-level finding is not locatable, so it is neither a match nor a mismatch.
    Emit._refuse_finding_iteration_mismatch(candidate, "reject", 0, "pr-1234/reject", ())


def test_emit_default_selection_unchanged(tmp_path: Path) -> None:
    """Omitting `--iteration` follows ADR-1 exactly: reject⇒first recoverable, approve⇒last recoverable."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    reject = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)
    assert reject.iteration_index == 0
    assert reject.selection == "selected iteration 0 (first recoverable, reject)"

    dataset_a, candidate_a = _prepare(tmp_path / "approve", _nits_only_findings())
    approve = Emit.emit_datapoint(candidate_a, kind="approve", dataset_dir=dataset_a)
    assert approve.iteration_index == 1
    assert approve.selection == "selected iteration 1 (last recoverable, approve)"


def test_emit_iteration_flag_overrides_default(tmp_path: Path) -> None:
    """`--iteration 1` builds a reject from iteration 1's patch bytes, not the default iteration 0's."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())  # findings reachable in both iterations

    result = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, iteration_index=1)

    assert result.iteration_index == 1
    assert result.selection == "selected iteration 1 (--iteration)"
    emitted_patch = (result.path / CHANGE_PATCH_RELPATH).read_bytes()
    assert emitted_patch == (dataset / candidate["iterations"][1]["patch_path"]).read_bytes()
    assert emitted_patch != (dataset / candidate["iterations"][0]["patch_path"]).read_bytes(), "the flag must change the bytes"


def test_emit_iteration_flag_refuses_unrecoverable_index(tmp_path: Path) -> None:
    """Naming an iteration with no materialized patch refuses `unrecoverable-iteration`, listing the recoverable ones."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())  # iterations 0 and 1 are recoverable

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, iteration_index=5)

    value = refusal.value
    assert value.check == "unrecoverable-iteration"
    assert value.exit_code == ExitCode.REFUSAL
    assert "iteration 5" in value.offending
    assert "[0, 1]" in value.next_


def test_emit_reports_selected_iteration_and_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`emit`'s stdout names the iteration and whether it was defaulted or flagged.

    A `finding-line-absent` refusal is hard to diagnose when `emit` never says which iteration it built
    from; the human report leads with it and `--json` carries it too."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())

    Cli._report_emit(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset), json_mode=False)
    assert "selected iteration 0 (first recoverable, reject)" in capsys.readouterr().out

    Cli._report_emit(Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, iteration_index=1), json_mode=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["iteration"] == 1
    assert payload["selection"] == "selected iteration 1 (--iteration)"


def test_emit_override_allows_mismatch_and_records_it(tmp_path: Path) -> None:
    """`--override finding-iteration-mismatch` emits despite the mismatch and records the override in `task.toml` (FR-38)."""
    dataset, candidate = _prepare(tmp_path, _substantive_findings())
    _set_in_diff(candidate, {9001: [1], 9002: [1]})  # would refuse without the override

    result = Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset, overrides=("finding-iteration-mismatch",))
    assert result.iteration_index == 0
    assert "finding-iteration-mismatch" in _harvest(result.path)["overrides"]


def test_reference_only_finding_still_needs_a_reachable_location(tmp_path: Path) -> None:
    """A `reference_only` finding out of the emitted diff still refuses — it is matched into `coverage_all`
    by location, so an unreachable one silently deflates the score (not exempt)."""
    findings: list[FindingDict] = [
        {
            "comment_ids": [9001],
            "statement": "`add` subtracts instead of adds",
            "severity": "high",
            "severity_evidence": "return a - b",
            "severity_rationale": "wrong result for every caller",
            "reference_only": False,
        },
        {
            "comment_ids": [9002],
            "statement": "a late reviewer note kept only for coverage",
            "severity": "medium",
            "severity_evidence": "",
            "severity_rationale": "",
            "reference_only": True,
        },
    ]
    dataset, candidate = _prepare(tmp_path, findings)
    # The substantive finding stays reachable in iteration 0; the reference-only one lives only in iter 1.
    _set_in_diff(candidate, {9001: [0, 1], 9002: [1]})

    with pytest.raises(EmitRefusalError) as refusal:
        Emit.emit_datapoint(candidate, kind="reject", dataset_dir=dataset)
    assert refusal.value.check == "finding-iteration-mismatch"
    assert "finding 2 lives in iterations [1]" in refusal.value.offending


# ───────────────────────────── the reward object (recorded judge) ─────────────────────────────


def _load_score_module() -> ModuleType:
    """Load `verifier_tpl/score.py` by path — it is data the CLI never imports, so a test loads it directly."""
    path = Path(eval_harvest.__file__).parent / "verifier_tpl" / "score.py"
    spec = importlib.util.spec_from_file_location("eval_harvest_score_tpl", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the template's dataclasses resolve their annotations against
    # sys.modules[__name__] (dataclasses.py:814), which is absent otherwise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reward_shape_uncredited_not_false() -> None:
    """`score.py` counts an unmatched agent finding as *uncredited*, never false (ADR-5, S-19).

    Oracle: two high findings and one medium. The agent matches one high and the medium and raises a
    third, novel finding. Coverage/precision reflect two of three credited; the novel finding lowers
    precision but is counted `uncredited`, and there is no `false`/false-positive key at all.
    """
    score = _load_score_module()
    oracle = [
        score.OracleFinding(0, "operator bug", "high", False, (score.Location("code.py", 2, 2),)),
        score.OracleFinding(1, "missing null check", "high", False, (score.Location("code.py", 10, 10),)),
        score.OracleFinding(2, "off-by-one loop", "medium", False, (score.Location("code.py", 5, 7),)),
    ]
    agent = [
        score.AgentFinding(0, "code.py", 2, "subtracts rather than adds", "high"),
        score.AgentFinding(1, "code.py", 6, "loop overruns the list", "medium"),
        score.AgentFinding(2, "code.py", 99, "a finding the reviewers never raised", "low"),
    ]
    judge = score.RecordedJudge({(0, 0): True, (2, 1): True})

    reward = score.Score.compute_reward(oracle, agent, judge)

    assert reward["coverage_all"] == 2 / 3
    assert reward["coverage_required"] == 1 / 2  # one of two high findings credited
    assert reward["precision_strict"] == 2 / 3  # two of three submitted credited
    assert reward["uncredited"] == 1.0
    assert "false" not in reward and "false_positives" not in reward, "a human-missed finding is never scored false"
    assert reward["credited_high"] == 1.0
    assert reward["credited_medium"] == 1.0
    assert reward["credited_all"] == 2.0
    assert set(reward) == set(REWARD_KEYS), "the reward keys the metric averages must match the emitter's set"


def _oracle_for_empty_submission_cases(score: ModuleType) -> list[Any]:
    """A small non-empty oracle: one high (required) and one medium finding, both locatable."""
    return [
        score.OracleFinding(0, "operator bug", "high", False, (score.Location("code.py", 2, 2),)),
        score.OracleFinding(1, "off-by-one loop", "medium", False, (score.Location("code.py", 5, 7),)),
    ]


def test_empty_submission_scores_zero_precision() -> None:
    """An agent that submits nothing has demonstrated no precision, so both precision keys are `0.0`.

    `1.0` would make silence the dominant strategy: Harbor averages every reward key with `Mean()`,
    so a systematically silent agent would collect a full 1.0 on precision across the whole dataset
    and outrank an agent that reviews well but imperfectly.
    """
    score = _load_score_module()

    reward = score.Score.compute_reward(_oracle_for_empty_submission_cases(score), [], score.RecordedJudge({}))

    assert reward["precision_strict"] == 0.0, "declining to review is not precision"
    assert reward["precision_adjudicated"] == 0.0, "declining to review is not precision"
    assert reward["coverage_all"] == 0.0
    assert reward["uncredited"] == 0.0


def test_empty_required_set_still_scores_full_coverage() -> None:
    """With no required (high) findings there is nothing to miss, so `coverage_required` stays `1.0`.

    Pins the asymmetry the empty-submission rule rests on: the empty-denominator answer differs between
    coverage and precision, so a change that flips the shared `_ratio` breaks the case it was right about.
    """
    score = _load_score_module()
    oracle = [score.OracleFinding(0, "cosmetic naming", "low", False, (score.Location("code.py", 2, 2),))]
    agent = [score.AgentFinding(0, "code.py", 2, "name is misleading", "low")]

    reward = score.Score.compute_reward(oracle, agent, score.RecordedJudge({(0, 0): True}))

    assert reward["coverage_required"] == 1.0, "an agent cannot miss a required finding that does not exist"
    assert reward["precision_strict"] == 1.0


def test_empty_oracle_still_scores_full_coverage_all() -> None:
    """With an empty oracle there is nothing to cover, so `coverage_all` stays `1.0` (the other coverage key)."""
    score = _load_score_module()
    agent = [score.AgentFinding(0, "code.py", 2, "a finding the reviewers never raised", "low")]

    reward = score.Score.compute_reward([], agent, score.RecordedJudge({}))

    assert reward["coverage_all"] == 1.0, "an agent cannot miss a finding that does not exist"
    assert reward["coverage_required"] == 1.0
    assert reward["precision_strict"] == 0.0, "one submitted, none credited — ordinary division"
    assert reward["uncredited"] == 1.0


def test_unmatched_findings_are_uncredited_not_false() -> None:
    """Ten submitted and none matched: precision `0.0` by ordinary division, `uncredited == 10`, no false key.

    Guards ADR-5 against a precision fix that smuggles in a false-positive penalty — the human review
    is an incomplete reference, so an unmatched agent finding lowers precision but is never wrong.
    """
    score = _load_score_module()
    oracle = _oracle_for_empty_submission_cases(score)
    agent = [score.AgentFinding(index, "other.py", 40 + index, f"finding {index}", "low") for index in range(10)]

    reward = score.Score.compute_reward(oracle, agent, score.RecordedJudge({}))

    assert reward["precision_strict"] == 0.0
    assert reward["uncredited"] == 10.0
    assert "false" not in reward and "false_positives" not in reward, "a human-missed finding is never scored false"
    assert set(reward) == set(REWARD_KEYS)


def test_nop_and_oracle_rewards_differ() -> None:
    """A silent agent and a perfect one must not score identically.

    If `--agent nop` and `--agent oracle` produced byte-identical `reward.json` — both
    `coverage_all = 0.0`, `precision_strict = 1.0` — the eval could not tell them apart. Given a
    non-empty oracle the two must differ.
    """
    score = _load_score_module()
    oracle = _oracle_for_empty_submission_cases(score)
    perfect_submission = [
        score.AgentFinding(0, "code.py", 2, "subtracts rather than adds", "high"),
        score.AgentFinding(1, "code.py", 6, "loop overruns the list", "medium"),
    ]

    nop_reward = score.Score.compute_reward(oracle, [], score.RecordedJudge({}))
    oracle_reward = score.Score.compute_reward(oracle, perfect_submission, score.RecordedJudge({(0, 0): True, (1, 1): True}))

    assert nop_reward != oracle_reward, "the eval cannot tell a perfect reviewer from a program that did nothing"
    assert oracle_reward["coverage_all"] == 1.0
    assert oracle_reward["precision_strict"] == 1.0
    assert nop_reward["coverage_all"] == 0.0
    assert nop_reward["precision_strict"] == 0.0


def test_reward_keys_match_between_emitter_and_template() -> None:
    """`emit.REWARD_KEYS` equals `score.REWARD_KEYS` — the metric averages exactly what the verifier writes."""
    score = _load_score_module()
    assert set(score.REWARD_KEYS) == set(REWARD_KEYS)


def test_strict_and_adjudicated_precision_can_differ() -> None:
    """The two precision keys are distinct measurements: strict is judge-free, adjudicated is judge-gated.

    Computing both keys as `credited_count / len(agent)` from one expression would make them agree on
    every trial, showing a robustness column that was never computed. Here the agent's finding overlaps
    the oracle location but the judge rejects it as a different defect — strict credits it (a location
    match), adjudicated does not (the judge gates it out), so the keys diverge.
    """
    score = _load_score_module()
    oracle = [score.OracleFinding(0, "operator bug", "high", False, (score.Location("code.py", 2, 2),))]
    agent = [score.AgentFinding(0, "code.py", 2, "a different problem on the same line", "high")]

    reward = score.Score.compute_reward(oracle, agent, score.RecordedJudge({(0, 0): False}))

    assert reward["precision_strict"] == 1.0, "the finding matches the oracle by location, so the strict tier credits it"
    assert reward["precision_adjudicated"] == 0.0, "the judge rejected it as a different defect, so adjudication does not"
    # Coverage and credited_* follow the adjudicated set (ADR-5, S-19) — unchanged by the strict tier.
    assert reward["coverage_all"] == 0.0
    assert reward["credited_all"] == 0.0


def test_strict_precision_makes_no_judge_call() -> None:
    """The strict tier is computed by `_location_only_match`, which takes no judge — it cannot consult one.

    A "strict" tier that quietly still called the judge would defeat the point with more code. The
    method's signature is the guarantee: it credits a location overlap whose judge decision is `False`.
    """
    score = _load_score_module()
    signature = inspect.signature(score.Score._location_only_match)
    assert "judge" not in signature.parameters, "the strict matcher must not take a judge — it is judge-free by construction"

    oracle = [score.OracleFinding(0, "operator bug", "high", False, (score.Location("code.py", 2, 2),))]
    agent = [score.AgentFinding(0, "code.py", 2, "same line, judge would say no", "high")]
    matched = score.Score._location_only_match(oracle, agent)
    assert matched == {0: 0}, "location overlap alone credits the finding, with no judge in the loop"


def test_both_precision_keys_zero_on_empty_submission() -> None:
    """The empty-submission semantics hold for *both* precision tiers, not just the adjudicated one."""
    score = _load_score_module()

    reward = score.Score.compute_reward(_oracle_for_empty_submission_cases(score), [], score.RecordedJudge({}))

    assert reward["precision_strict"] == 0.0
    assert reward["precision_adjudicated"] == 0.0


def test_coverage_still_uses_the_adjudicated_match() -> None:
    """`coverage_all` and `credited_*` are computed from the judge-adjudicated set, never the strict one.

    Guards against an Option-A refactor that narrows coverage to the looser strict set — a silent
    scoring regression. Here strict would credit two location matches but the judge only accepts one.
    """
    score = _load_score_module()
    oracle = [
        score.OracleFinding(0, "operator bug", "high", False, (score.Location("code.py", 2, 2),)),
        score.OracleFinding(1, "off-by-one loop", "medium", False, (score.Location("code.py", 5, 7),)),
    ]
    agent = [
        score.AgentFinding(0, "code.py", 2, "subtracts rather than adds", "high"),
        score.AgentFinding(1, "code.py", 6, "an unrelated remark on that line", "medium"),
    ]

    reward = score.Score.compute_reward(oracle, agent, score.RecordedJudge({(0, 0): True}))  # only the first pair accepted

    assert reward["precision_strict"] == 2 / 2, "both submissions overlap an oracle location"
    assert reward["precision_adjudicated"] == 1 / 2, "the judge credits only one"
    assert reward["coverage_all"] == 1 / 2, "coverage follows the adjudicated set, not the strict one"
    assert reward["credited_all"] == 1.0
    assert reward["credited_high"] == 1.0 and reward["credited_medium"] == 0.0


# ───────────────────────────── end-to-end via the CLI ─────────────────────────────


def test_cli_emit_end_to_end(tmp_path: Path) -> None:
    """`eval-harvest emit <candidate> --kind reject` writes the task and exits 0 (the driven flow)."""
    dataset, _ = _prepare(tmp_path, _substantive_findings())
    candidate_path = dataset / "candidates" / "pr-1234.json"

    code = Cli.run(["emit", str(candidate_path), "--kind", "reject"])
    assert code == ExitCode.SUCCESS
    assert (dataset / "tasks" / "our-org__our-repo__pr1234-reject" / "task.toml").is_file()
    assert (dataset / "metric.py").is_file(), "emit ships the dataset metric.py so aggregation is stated (§3/§8)"


def _read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON file."""
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


# ──────────────────────── the verifier's judge reaches Bedrock over https and nothing else ────────────────────────


def test_live_judge_refuses_a_non_https_endpoint() -> None:
    """`LiveJudge` refuses a non-`https://` Bedrock endpoint before `boto3` is ever reached.

    The endpoint is derived from `AWS_REGION`, so a crafted region must not be able to downgrade the
    judge call to a plaintext or `file://` transport whose answer an agent could control. The judge is
    constructed and *called*, so the guard is proven to fire inside `__call__` before the `import boto3`
    on the line below it — `boto3` is not even installed in the dev tree. Watched fail: deleting the
    `require_transport_security` call turns this red (an `ImportError`/no-raise instead of the `ValueError`).
    """
    score = _load_score_module()
    oracle = score.OracleFinding(0, "operator bug", "high", False, (score.Location("code.py", 2, 2),))
    agent = score.AgentFinding(0, "code.py", 2, "subtracts rather than adds", "high")

    for endpoint in ("file:///tests/oracle.json", "http://169.254.169.254/latest/meta-data/", "ftp://host/x"):
        judge = score.LiveJudge(model="m", temperature=0.0, seed=0, region="us-east-1", endpoint=endpoint)
        with pytest.raises(ValueError, match="must start with 'https://'") as raised:
            judge(oracle, agent)
        assert endpoint in str(raised.value), "the refusal names the endpoint it rejected"


def test_live_judge_accepts_an_https_bedrock_endpoint() -> None:
    """An `https://` Bedrock endpoint passes the scheme guard — the guard rejects a scheme, not a hostname.

    The pair to the test above: a guard that refused everything would pass that one for the wrong
    reason. Nothing here calls Bedrock; the assertion is that the guard returns for an https endpoint.
    """
    score = _load_score_module()
    score.LiveJudge.require_transport_security("https://bedrock-runtime.us-east-1.amazonaws.com")
    score.LiveJudge.require_transport_security("https://bedrock-runtime.eu-west-1.amazonaws.com")


def test_live_judge_from_config_resolves_region_and_bedrock_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`from_config` reads the pinned model from `judge.toml` and the region from the env.

    The endpoint is not configurable in `judge.toml` — it is `bedrock-runtime.<region>`, so no
    `judge.toml` value can point the judge at a host the agent controls. With no region set, it refuses
    rather than guessing one.
    """
    score = _load_score_module()
    config = tmp_path / "judge.toml"
    config.write_text('[judge]\nprovider = "bedrock"\nmodel = "a.model.id"\ntemperature = 0.0\nseed = 0\n', encoding="utf-8")

    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    judge = score.LiveJudge.from_config(config)
    assert judge.model == "a.model.id"
    assert judge.region == "us-west-2"
    assert judge.endpoint == "https://bedrock-runtime.us-west-2.amazonaws.com"

    monkeypatch.delenv("AWS_REGION", raising=False)
    with pytest.raises(ValueError, match="AWS region"):
        score.LiveJudge.from_config(config)
