"""Black-box end-to-end harness for the ``eval-harvest`` CLI.

Every check here drives the installed ``eval-harvest`` console script as a subprocess and asserts
on exit codes, stdout/stderr, and the files the CLI writes — never by importing ``eval_harvest``.
The online verbs are exercised against a real public repository; assertions check *shape* (keys,
types, files, exit codes, the four-field refusal contract), never specific values, so the suite is
immune to the repository's live data drifting. When there is no network or ``gh`` is not
authenticated, the forge-dependent tests skip with a reason rather than fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# tests/e2e/conftest.py -> parents[1] = tests/, parents[2] = repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO = "aws-samples/sample-autonomous-cloud-coding-agents"
REPO_URL = f"https://github.com/{REPO}.git"

#: PRs known to carry inline review comments; the pipeline captures the first one that yields a
#: usable candidate. Values are never asserted, so drift in any single PR only changes which one
#: the pipeline settles on.
CANDIDATE_PRS = (831, 806, 779, 733, 867, 854, 753, 828)

#: The CLI's exit-code contract (eval-harvest(1)): named so assertions read intent, not integers.
EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_REFUSAL = 3
EXIT_VERIFY_FAILURE = 4
EXIT_UNRESOLVED = 5


def forge_env() -> dict[str, str]:
    """Environment with ``GITHUB_TOKEN`` stripped so ``gh`` uses the keyring credential."""
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    return env


def _cli_command() -> list[str]:
    """Resolve the installed console script; fall back to ``uv run`` if the venv is not populated."""
    venv_bin = REPO_ROOT / ".venv" / "bin" / "eval-harvest"
    if venv_bin.exists():
        return [str(venv_bin)]
    return ["uv", "run", "eval-harvest"]


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> object:
        return json.loads(self.stdout)


def run_cli(*args: object, cwd: Path | None = None, input_text: str | None = None, timeout: int = 420) -> CliResult:
    """Invoke ``eval-harvest`` as a subprocess with the forge environment."""
    command = _cli_command() + [str(a) for a in args]
    completed = subprocess.run(
        command,
        cwd=str(cwd or REPO_ROOT),
        env=forge_env(),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CliResult(completed.returncode, completed.stdout, completed.stderr)


def _network_available() -> bool:
    """The target repo is public, so reachability — not authentication — is what gates the suite."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", REPO_URL],
            env=forge_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


NETWORK_AVAILABLE = _network_available()
requires_network = pytest.mark.skipif(not NETWORK_AVAILABLE, reason="github.com unreachable (network required)")


def _seed_dataset_artefacts(clone: Path, dataset: Path) -> None:
    """Run ``init`` and promote the emitted templates to the real rubric/risk-map the verbs read."""
    result = run_cli("init", clone, "--dataset", dataset)
    assert result.returncode == 0, f"init failed: {result.stderr}"
    rubric_template = dataset / "rubric.template.md"
    riskmap_template = dataset / "risk-map.template.toml"
    assert rubric_template.exists() and riskmap_template.exists(), "init did not write the templates"
    shutil.copyfile(rubric_template, dataset / "rubric.md")
    shutil.copyfile(riskmap_template, dataset / "risk-map.toml")


def _rubric_version(dataset: Path) -> str:
    """Read the version the promoted rubric declares, so the candidate can pin the same one."""
    for line in (dataset / "rubric.md").read_text(encoding="utf-8").splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("version:") or stripped.startswith("rubric_version:"):
            return line.split(":", 1)[1].strip()
    return "v1"


def _fill_records(
    candidate: dict[str, Any], defect_ids: list[int], *, with_findings: bool, high: bool, rubric_version: str
) -> str:
    """A JSONL annotate stream: classify every comment, optionally add findings, set risk + rubric."""
    records: list[dict[str, Any]] = []
    for comment in candidate["comments"]:
        classification = "defect" if comment["id"] in defect_ids else "nit"
        records.append({"comment": comment["id"], "classification": classification, "rationale": "e2e fixture classification"})
    if with_findings:
        severity = "high" if high else "medium"
        for defect_id in defect_ids:
            records.append(
                {
                    "finding": {
                        "comment_ids": [defect_id],
                        "statement": "e2e fixture defect statement",
                        "severity": severity,
                        "severity_evidence": "e2e fixture evidence at the referenced line",
                        "severity_rationale": "e2e fixture rubric rule",
                        "reference_only": False,
                    }
                }
            )
    records.append({"risk_classified": "low", "rationale": "e2e fixture risk classification"})
    records.append({"rubric_version": rubric_version})
    return "\n".join(json.dumps(record) for record in records) + "\n"


@dataclass
class Pipeline:
    clone: Path
    pr: int
    pr_url: str
    iteration: int
    defect_ids: list[int]
    raw_candidate: str
    rubric_version: str
    dataset_reject: Path
    reject_task: Path
    has_approved: bool
    approve_task: Path | None = None
    approve_error: str | None = None


def _select_and_capture(clone: Path, workdir: Path) -> tuple[Path, Path, int, int, list[int], bool]:
    """Capture the first candidate PR that yields a usable candidate; return its facts."""
    failures: list[str] = []
    for pr in CANDIDATE_PRS:
        dataset = workdir / f"ds-{pr}"
        dataset.mkdir(parents=True, exist_ok=True)
        _seed_dataset_artefacts(clone, dataset)
        captured = run_cli("capture", pr, "--clone", clone, "--dataset", dataset)
        candidate_path = dataset / "candidates" / f"pr-{pr}.json"
        if captured.returncode != 0 or not candidate_path.exists():
            failures.append(f"pr {pr}: capture rc={captured.returncode}")
            continue
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        located = [
            c for c in candidate["comments"] if c.get("in_diff_iterations") and c.get("path") and c.get("line_start", 0) > 0
        ]
        if not located:
            failures.append(f"pr {pr}: no comment with a located, in-diff line")
            continue
        iteration = min(min(c["in_diff_iterations"]) for c in located)
        defect_ids = [c["id"] for c in located if iteration in c["in_diff_iterations"]][:3]
        if not defect_ids:
            failures.append(f"pr {pr}: no defect comment covers iteration {iteration}")
            continue
        has_approved = any(v.get("state") == "APPROVED" for v in candidate.get("review_verdicts", []))
        return dataset, candidate_path, pr, iteration, defect_ids, has_approved
    pytest.skip("no candidate PR yielded a usable candidate: " + "; ".join(failures))


@pytest.fixture(scope="session")
def clone(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not NETWORK_AVAILABLE:
        pytest.skip("github.com unreachable")
    target = tmp_path_factory.mktemp("clone") / "repo"
    result = subprocess.run(
        ["git", "clone", "--filter=blob:none", REPO_URL, str(target)],
        env=forge_env(),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"clone failed: {result.stderr[-300:]}")
    return target


@pytest.fixture(scope="session")
def pipeline(clone: Path, tmp_path_factory: pytest.TempPathFactory) -> Pipeline:
    """Run the whole capture→fill→emit flow once; tests assert on its artefacts."""
    workdir = tmp_path_factory.mktemp("pipeline")
    dataset_reject, candidate_path, pr, iteration, defect_ids, has_approved = _select_and_capture(clone, workdir)
    raw_candidate = candidate_path.read_text(encoding="utf-8")
    candidate = json.loads(raw_candidate)
    pr_url = candidate.get("pr_url", f"https://github.com/{REPO}/pull/{pr}")
    rubric_version = _rubric_version(dataset_reject)

    # Reject datapoint: findings drawn from comments that cover the emitted iteration.
    reject_stream = _fill_records(candidate, defect_ids, with_findings=True, high=False, rubric_version=rubric_version)
    filled = run_cli("annotate", candidate_path, "--stdin", input_text=reject_stream)
    assert filled.returncode == 0, f"annotate (reject) failed: {filled.stdout}{filled.stderr}"
    emitted = run_cli("emit", candidate_path, "--kind", "reject", "--iteration", iteration, "--clone", clone)
    assert emitted.returncode == 0, f"emit reject failed: {emitted.stdout}{emitted.stderr}"
    reject_tasks = sorted((dataset_reject / "tasks").glob("*reject*"))
    assert reject_tasks, "emit reject wrote no task directory"

    result = Pipeline(
        clone=clone,
        pr=pr,
        pr_url=pr_url,
        iteration=iteration,
        defect_ids=defect_ids,
        raw_candidate=raw_candidate,
        rubric_version=rubric_version,
        dataset_reject=dataset_reject,
        reject_task=reject_tasks[0],
        has_approved=has_approved,
    )

    # Approve datapoint: a fresh dataset, the raw candidate, no findings (nothing to block on).
    if has_approved:
        # Copy the whole captured dataset so the materialized iteration patches come along; then reset
        # the candidate to its raw (unfilled) bytes and fill it for approve with no findings.
        dataset_approve = workdir / "ds-approve"
        shutil.copytree(dataset_reject, dataset_approve)
        approve_candidate = dataset_approve / "candidates" / f"pr-{pr}.json"
        approve_candidate.write_text(raw_candidate, encoding="utf-8")
        approve_stream = _fill_records(candidate, [], with_findings=False, high=False, rubric_version=rubric_version)
        run_cli("annotate", approve_candidate, "--stdin", input_text=approve_stream)
        approve = run_cli("emit", approve_candidate, "--kind", "approve", "--clone", clone)
        approve_tasks = sorted((dataset_approve / "tasks").glob("*approve*"))
        result.approve_task = approve_tasks[0] if approve_tasks else None
        if result.approve_task is None:
            result.approve_error = f"rc={approve.returncode}: {(approve.stdout + approve.stderr)[:500]}"
    return result
