"""The `emit` verb: turn a filled candidate into a self-contained, verified Harbor task directory.

This is the integration point (tech plan §7.1, §7.2). `emit` reads a candidate whose judgment slots
the agent has filled, selects the iteration `--kind` names (a rejected iteration for a reject
datapoint, the approved state for an approve one), sets the expected verdict from `--kind` — never a
severity formula (FR-14) — and assembles the standard Harbor task directory: the sealed
`environment/Dockerfile`, the `change.patch` under review, a uniform `instruction.md`, the
verifier-only `tests/` (the reward-shape `score.py`, its `oracle.json`, the team `rubric.md`, the
pinned `judge.toml`) and `solution/solve.sh`, and `task.toml` carrying provenance and this project's
`[metadata.harvest]` fields.

**Assemble-then-verify-then-promote (§7.2).** The directory is built in a temp location, the real
`verify` (E-2) runs against it, and it is promoted to `tasks/<name>__<kind>/` only when no check
failed — or with an explicit, recorded `--override <check>`. Only hard failures (exit 4 — a *broken*
datapoint) block; a check left *unresolved* for want of a runtime or a clone (exit 5) does not, per
§7.3 ("neither pass nor fail"), so the standalone `verify` command reports it later. A test may inject
a `verify_hook` to stand in for `verify`. A broken datapoint comes back as a refusal, never a silent
artefact.

**Offline and deterministic (NFR-1, NFR-2).** `emit` makes no git or network call: it reads the
candidate's already-materialized patch bytes, not the clone. Two emits from one unchanged candidate
produce a byte-identical directory, including a byte-stable `content_digest` — the digest F-1's
dataset manifest consumes. Serialization goes through `harbor.py`/`tomlw.py`; the reward object's
*shape* (ADR-5) is materialized from `verifier_tpl/` as data the CLI writes but never imports.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from eval_harvest.candidate import Candidate, CandidateDict, CommentDict, FindingDict, IterationDict
from eval_harvest.harbor import (
    CompiledTask,
    EmittedFile,
    Harbor,
    HarvestFields,
    TaskOrigin,
)
from eval_harvest.riskmap import RISK_LEVELS
from eval_harvest.tomlw import TomlValue, emit_document
from eval_harvest.verify import CHANGE_PATCH_RELPATH, Verify

#: The verifier template files `emit` copies into each task's `tests/`, verbatim (data, not imported).
_TEMPLATE_DIR: Final = Path(__file__).parent / "verifier_tpl"

#: The reward object's keys, kept equal to `verifier_tpl/score.py`'s `REWARD_KEYS` so the dataset
#: `metric.py` averages exactly what the verifier writes. Restated here (not imported) because
#: `score.py` is data the CLI must not import (the import scan and the "no LLM" spine, ADR-5).
REWARD_KEYS: Final[tuple[str, ...]] = (
    "coverage_required",
    "coverage_all",
    "precision_strict",
    "precision_adjudicated",
    "credited_required",
    "credited_all",
    "uncredited",
    "oracle_high",
    "oracle_medium",
    "oracle_low",
    "credited_high",
    "credited_medium",
    "credited_low",
)

#: Only high-severity findings block a merge (FR-14); a "substantive" defect is medium or high (a
#: low finding is a nit). A reject datapoint needs at least one substantive finding or it is refused.
_BLOCKING_SEVERITY: Final = "high"
_SUBSTANTIVE_SEVERITIES: Final = ("medium", "high")

#: Pinned container bases. The agent environment needs git for the sealing clone; the verifier needs
#: python for `score.py`. Fixed regardless of host (NFR-8: the emitted task always targets Linux).
#: Pulled from ECR Public (the `docker/library/*` mirror of the Docker Official Images) rather than
#: Docker Hub, so the reference is an internal-registry image and the build does not reach an external
#: repository — same content, same pinned tags, ACAT's non-ECR-image check is satisfied at the source.
_AGENT_BASE_IMAGE: Final = "public.ecr.aws/docker/library/buildpack-deps:bookworm-scm"
_VERIFIER_BASE_IMAGE: Final = "public.ecr.aws/docker/library/python:3.12-slim"
#: The verifier's judge calls Bedrock through boto3, signed with the MicroVM's IAM execution role;
#: the slim base has no AWS SDK, so it is installed into the image. Pinned exactly so a build is
#: reproducible and the Dockerfile bytes (a content-digest input, NFR-1) are stable across emits.
_VERIFIER_BOTO3_PIN: Final = "boto3==1.43.89"

#: The uniform task description and instruction — identical across datapoints so a verdict is never
#: inferable from phrasing (FR-33). v1 declares no per-datapoint instruction slots.
_TASK_DESCRIPTION: Final = (
    "Review the change on this pull-request iteration and report the defects a competent reviewer should catch."
)

#: The dataset-relative rubric the agent authored; copied into each task's `tests/` (FR-31).
_RUBRIC_FILENAME: Final = "rubric.md"
#: The dataset `metric.py` `emit` ships so the aggregation is stated and runnable (§3/§8, F-1 owns it too).
_METRIC_FILENAME: Final = "metric.py"

#: Exit-code classes carried by `EmitRefusalError`, pinned to the tech-plan §9 values (`cli.ExitCode`).
#: Restated as ints rather than importing `cli` — `cli` imports `emit`, so the dependency runs one
#: way; `tests/test_cli.py` locks these same numbers, so a drift is caught there.
_REFUSAL_EXIT_CODE: Final = 3
_VERIFICATION_EXIT_CODE: Final = 4

#: The coherence check: every oracle finding must have a comment inside the emitted iteration's
#: diff, or it cannot be located in the change under review (FR-17, FR-34). Overridable by name via
#: `--override finding-iteration-mismatch` for a deliberate reference-only-heavy datapoint (FR-38).
_MISMATCH_CHECK: Final = "finding-iteration-mismatch"


@dataclass(frozen=True, slots=True)
class VerifyFailure:
    """One `verify` check that failed on the assembled task, in the FR-39 shape `emit` refuses with."""

    check: str
    offending: str
    next_: str


@dataclass(frozen=True, slots=True)
class UnresolvedCheck:
    """One `verify` check that could not run (exit 5): its name and the short reason it stayed unresolved.

    Not a failure and never blocks the write (§7.3). `emit` names it in its report and a `next:`
    command so the agent knows the datapoint is not yet verified — rather than claiming "all checks
    passed" while the git-channel checklist has silently gone unresolved."""

    check: str
    reason: str


#: The short reason a check stays unresolved, keyed by its canonical name (`verify.CHECK_NAMES`). The
#: two checks that can go unresolved are base-and-patch (needs a `--clone`) and git-channel-absence
#: (needs a container runtime to materialize the seal). Any other unresolved check falls back to its
#: raw `offending` text, so a new one is reported truthfully rather than mislabelled.
_UNRESOLVED_REASONS: Final[dict[str, str]] = {
    "base-and-patch": "no clone provided",
    "git-channel-absence": "no container runtime",
}


@dataclass(frozen=True, slots=True)
class HookResult:
    """What the `verify` seam reports back: the checks that passed, that failed, and that are unresolved.

    The hook does not summarise (FR-2/FR-33): it returns all three lists and the caller (`emit`, then
    the CLI) decides how to report. `failures` block the write (exit 4); `unresolved` never do (§7.3);
    `passed` is the set `emit` counts in "N checks ran and passed"."""

    passed: tuple[str, ...]
    failures: tuple[VerifyFailure, ...]
    unresolved: tuple[UnresolvedCheck, ...]


#: The seam to `verify` (E-2): given the assembled task directory, return the full `HookResult` — what
#: passed, what failed, what is unresolved. E-4 wires the default to the real `verify`; the parameter
#: is kept so a test can inject a fixed hook without a clone or a materialized seal.
VerifyHook = Callable[[Path], HookResult]


def _real_verify_hook(clone: Path | None) -> VerifyHook:
    """Build the default hook: run the real `verify` (E-2) and report what passed, failed, and is unresolved.

    Only `VerifyReport.failures` (exit 4 — a *broken* datapoint) block promotion. The *unresolved*
    checks (exit 5) do not: the git-channel checklist needs a materialized sealing container (deferred,
    the Lambda MicroVMs `[TODO]`) and the base/patch checks need a `clone`; per §7.3 a runtime-absent
    check "contributes neither pass nor fail" here, so a datapoint emitted without a clone or a seal is
    still promoted once every check that *could* run passes. The standalone `eval-harvest verify`
    command still reports those as exit 5 so the agent knows to materialize the seal and re-check. The
    content scan — the leak check that needs no runtime — always runs and blocks (S-2, FR-35).

    Both `report.failures` *and* `report.unresolved` are propagated: dropping the latter would let the
    CLI print "all checks passed" over a git-channel check that never ran, so both lists are carried."""

    def hook(task_dir: Path) -> HookResult:
        report = Verify.verify_task(task_dir, clone=clone)
        return HookResult(
            passed=report.passed,
            failures=tuple(VerifyFailure(check=f.check, offending=f.offending, next_=f.next_) for f in report.failures),
            unresolved=tuple(
                UnresolvedCheck(check=u.check, reason=_UNRESOLVED_REASONS.get(u.check, u.offending)) for u in report.unresolved
            ),
        )

    return hook


@dataclass(frozen=True, slots=True)
class EmitResult:
    """A successful emit: the promoted task directory plus the verification status the CLI reports.

    `emit` writes the datapoint and returns *how sound it is*, not just where it landed: `passed` is
    the checks that ran and passed, `unresolved` the checks that could not run (no clone, no runtime).
    A non-empty `unresolved` means the datapoint is not yet verified — the CLI says so and names the
    `verify` command that completes it, rather than a false "all checks passed".
    Failures never reach here; they raise `EmitRefusalError` before the datapoint is promoted.

    `selection` is the human note naming which iteration was built and why (`selected iteration 0 (first
    recoverable, reject)` / `selected iteration 2 (--iteration)`), and `iteration_index` is that
    iteration's index — surfaced so a refusal downstream is diagnosable."""

    path: Path
    passed: tuple[str, ...]
    unresolved: tuple[UnresolvedCheck, ...]
    selection: str
    iteration_index: int


class EmitRefusalError(Exception):
    """A refusal `emit` raises for the CLI to render in the FR-2 four-field shape with its exit code."""

    # Five attributes because FR-2 fixes the four-field shape and each refusal also carries its
    # exit-code class; the CLI maps them straight onto `Cli.refuse`.
    def __init__(self, *, check: str, datapoint: str, offending: str, next_: str, exit_code: int) -> None:
        super().__init__(f"{check}: {offending}")
        self.check = check
        self.datapoint = datapoint
        self.offending = offending
        self.next_ = next_
        self.exit_code = exit_code


class Emit:
    """Assemble, verify, and promote one datapoint's Harbor task directory. Holds no state."""

    # ───────────────────────────── the verb entry point ─────────────────────────────

    # Six parameters because emit's contract needs all of them: the candidate and its `kind` and
    # `dataset_dir`, the recorded `overrides`, the local `clone` handed to `verify`, and the test-only
    # `verify_hook` seam. Collapsing any into an options object would only hide the contract, so the
    # count is suppressed the way `cli.refuse` suppresses its own.
    @classmethod
    def emit_datapoint(  # noqa: PLR0913
        cls,
        candidate: CandidateDict,
        *,
        kind: str,
        dataset_dir: Path,
        overrides: tuple[str, ...] = (),
        clone: Path | None = None,
        iteration_index: int | None = None,
        verify_hook: VerifyHook | None = None,
    ) -> EmitResult:
        """Build the `<name>__<kind>/` task directory and return its `EmitResult`, or raise `EmitRefusalError`.

        Follows §7.2: coherence checks first (a reject needs a substantive finding, an approve carries
        no high finding, and every oracle finding must have a comment inside the selected
        iteration's diff, `finding-iteration-mismatch`), then assemble in a temp dir, then run the real
        `verify` (E-2) over the temp task and the local `clone`, then promote unless a check failed
        (FR-38). `verify` reads local files and the local clone only — no forge call (NFR-2); passing
        `clone=None` leaves the base/patch checks unresolved rather than run (§7.3), and they do not
        block promotion. A test may inject `verify_hook` to stand in for `verify` without a clone or a
        materialized seal.

        `iteration_index` is the `--iteration <n>` override: the index into `candidate["iterations"]`
        to build from, or `None` to keep the default (the first recoverable iteration for `reject`, the
        last for `approve` — ADR-1). It never changes the verdict or the oracle rules, only which patch
        the datapoint is assembled from.
        """
        if verify_hook is None:
            verify_hook = _real_verify_hook(clone)
        datapoint = f"pr-{candidate['pr_number']}/{kind}"
        cls._refuse_unfilled(candidate, datapoint)
        cls._refuse_incoherent_kind(candidate, kind, datapoint)
        selected_index, iteration = cls._select_iteration(candidate, kind, datapoint, iteration_index)
        cls._refuse_finding_iteration_mismatch(candidate, kind, selected_index, datapoint, overrides)
        patch_bytes = cls._read_patch(dataset_dir, iteration, datapoint)
        rubric_bytes = cls._read_rubric(dataset_dir, datapoint)
        oracle_findings = cls._oracle_findings(candidate, kind)
        plan = _TaskPlan(
            candidate=candidate,
            kind=kind,
            iteration=iteration,
            patch_bytes=patch_bytes,
            rubric_bytes=rubric_bytes,
            oracle_findings=oracle_findings,
            overrides=overrides,
        )
        selection = cls._selection_description(selected_index, kind, explicit=iteration_index is not None)
        return cls._assemble_verify_promote(plan, dataset_dir, verify_hook, selection, selected_index)

    # ───────────────────────────── coherence refusals ─────────────────────────────

    @classmethod
    def _refuse_unfilled(cls, candidate: CandidateDict, datapoint: str) -> None:
        """Refuse a candidate whose facts or judgment slots are missing/incoherent (FR-2)."""
        violations = Candidate.validate_facts(candidate) + Candidate.validate_filled(candidate)
        if violations:
            raise EmitRefusalError(
                check="unfilled-candidate",
                datapoint=datapoint,
                offending="; ".join(str(violation) for violation in violations),
                next_="fill every judgment slot coherently (classify each comment, state findings with evidence) and re-run",
                exit_code=_REFUSAL_EXIT_CODE,
            )

    @classmethod
    def _refuse_incoherent_kind(cls, candidate: CandidateDict, kind: str, datapoint: str) -> None:
        """A reject needs ≥1 substantive finding (FR-18); an approve carries no high finding (FR-14)."""
        findings = candidate["findings"]
        if kind == "reject" and not any(cls._is_substantive(finding) for finding in findings):
            raise EmitRefusalError(
                check="empty-oracle",
                datapoint=datapoint,
                offending="a reject datapoint needs a substantive (medium/high) finding; this candidate has none",
                next_="build this PR as `--kind approve`, or add the substantive finding the reviewers raised",
                exit_code=_REFUSAL_EXIT_CODE,
            )
        if kind == "approve":
            high = [f for f in findings if f["severity"] == _BLOCKING_SEVERITY and not f["reference_only"]]
            if high:
                raise EmitRefusalError(
                    check="approve-with-high-finding",
                    datapoint=datapoint,
                    offending=f"{len(high)} high-severity finding(s) on an approve datapoint; the approved state has no blocker",
                    next_="emit this PR as `--kind reject`, or lower the severity if the defect does not block a merge",
                    exit_code=_REFUSAL_EXIT_CODE,
                )

    @staticmethod
    def _is_substantive(finding: FindingDict) -> bool:
        """A substantive finding is a non-reference-only defect of medium or high severity (not a nit)."""
        return not finding["reference_only"] and finding["severity"] in _SUBSTANTIVE_SEVERITIES

    @classmethod
    def _refuse_finding_iteration_mismatch(
        cls, candidate: CandidateDict, kind: str, selected_index: int, datapoint: str, overrides: tuple[str, ...]
    ) -> None:
        """Refuse when an oracle finding has no comment inside the selected iteration's diff (FR-17, FR-34).

        `verify` catches this structurally at the very end of the pipeline as `finding-line-absent`;
        this is the same rule applied at `emit`, before any file is written, where the wording can name
        each offending finding and the iteration that would carry it. Applied for a total mismatch (no
        finding is reachable — the datapoint has no gradeable content) *and* a partial one (some
        findings are reachable, some not), because a partially unreachable oracle silently deflates
        `coverage_all` in `score.py` — a plausible-looking but wrong number, worse than a refusal.

        A finding is reachable if *any* of its comments is inside the selected diff (the "any location
        matches" rule `verify` applies). A finding whose comments are all file-level (empty `path`) has
        no location at all and is graded on neither side, so it is treated as *not* mismatching —
        flagging it would turn every file-level finding into a refusal. `reference_only` findings are
        **not** exempt: `score.py` matches them into `coverage_all` by location overlap, so an
        unreachable one silently deflates every graded agent's coverage (see the task's Notes).
        """
        if _MISMATCH_CHECK in overrides:
            return
        selected = cls._selected_findings(candidate, kind)
        comments_by_id = {comment["id"]: comment for comment in candidate["comments"]}
        infos = [cls._finding_iterations(number, finding, comments_by_id) for number, finding in enumerate(selected, start=1)]
        mismatched = [info for info in infos if info.locatable and selected_index not in info.iterations]
        if not mismatched:
            return
        raise EmitRefusalError(
            check=_MISMATCH_CHECK,
            datapoint=datapoint,
            offending=cls._mismatch_offending(selected_index, len(selected), mismatched),
            next_=cls._mismatch_next(candidate, selected_index, mismatched),
            exit_code=_REFUSAL_EXIT_CODE,
        )

    @staticmethod
    def _selected_findings(candidate: CandidateDict, kind: str) -> list[FindingDict]:
        """The findings that become this kind's oracle: every finding for a reject, the nits only for an approve.

        The single source of the reject/approve finding selection, shared by the pre-write iteration-
        coherence check and :meth:`_oracle_findings`, so the two can never disagree about which
        findings the datapoint is meant to grade against."""
        findings = candidate["findings"]
        if kind == "reject":
            return list(findings)
        return [finding for finding in findings if finding["severity"] == "low" and not finding["reference_only"]]

    @staticmethod
    def _finding_iterations(number: int, finding: FindingDict, comments_by_id: dict[int, CommentDict]) -> _FindingIterations:
        """One finding's location coverage: the recoverable iterations its located comments live in.

        `iterations` is the sorted union of `in_diff_iterations` over the finding's comments that carry
        a file path (`_one_oracle_finding` mines locations from exactly those); `locatable` is whether
        it has any such comment. A finding with only file-level comments is not locatable and never
        counts as a mismatch. `comment_id` is a representative located comment the `next:` hint can
        point `show` at."""
        located = [
            comment
            for comment_id in finding["comment_ids"]
            if (comment := comments_by_id.get(comment_id)) is not None and comment["path"]
        ]
        iterations = sorted({index for comment in located for index in comment["in_diff_iterations"]})
        return _FindingIterations(
            number=number,
            locatable=bool(located),
            iterations=tuple(iterations),
            comment_id=located[0]["id"] if located else None,
        )

    @staticmethod
    def _mismatch_offending(selected_index: int, total: int, mismatched: list[_FindingIterations]) -> str:
        """The FR-2 `offending` line: which findings cannot be located in the selected iteration's diff."""
        details = "; ".join(f"finding {info.number} lives in iterations {list(info.iterations)}" for info in mismatched)
        if len(mismatched) == total:
            return (
                f"iteration {selected_index} is being emitted, but none of the {total} oracle finding(s) "
                f"has a comment inside its diff ({details})"
            )
        return (
            f"iteration {selected_index} is being emitted, but {len(mismatched)} of {total} oracle finding(s) "
            f"cannot be located in its diff ({details})"
        )

    @staticmethod
    def _mismatch_next(candidate: CandidateDict, selected_index: int, mismatched: list[_FindingIterations]) -> str:
        """A *runnable* `next:` hint: a real iteration that carries the findings, or the rewrite path.

        The iteration named is one that actually covers the most mismatched findings (an unactionable
        `<n>` placeholder would just get ignored). When no recoverable diff
        covers any offending finding, `--iteration` cannot help, so only the rewrite path is offered."""
        pr_number = candidate["pr_number"]
        comment_id = next((info.comment_id for info in mismatched if info.comment_id is not None), None)
        show = f"see `show candidates/pr-{pr_number}.json --comment {comment_id}`" if comment_id is not None else ""
        suggested = _suggest_iteration(mismatched)
        rewrite = f"rewrite the findings against iteration {selected_index}'s diff"
        if suggested is not None:
            head = f"emit --iteration {suggested}, or {rewrite}"
        else:
            head = f"{rewrite} so the comments land inside it"
        return f"{head} — {show}" if show else head

    # ───────────────────────────── iteration & inputs ─────────────────────────────

    @classmethod
    def _select_iteration(
        cls, candidate: CandidateDict, kind: str, datapoint: str, iteration_index: int | None
    ) -> tuple[int, IterationDict]:
        """The iteration the datapoint is built from, as `(index, iteration)`, honouring `--iteration`.

        When `iteration_index` is None the default holds (ADR-1): the first recoverable iteration for a
        reject datapoint (the rejected, earliest reviewed state) and the last for an approve one (the
        approved, final state, S-1). An iteration is recoverable when `capture` materialized its patch.
        A candidate with no recoverable iteration cannot yield a change patch and is refused. When
        `iteration_index` is given it must name a recoverable index — otherwise `unrecoverable-iteration`
        fires with the recoverable indices in `next` — and it does not change the verdict or the oracle.
        """
        recoverable = [(index, iteration) for index, iteration in enumerate(candidate["iterations"]) if iteration["patch_path"]]
        if not recoverable:
            raise EmitRefusalError(
                check="unrecoverable-iteration",
                datapoint=datapoint,
                offending="no iteration has a materialized patch; the reviewed states were not recoverable",
                next_="capture a PR whose iteration tips are reachable from refs/pull/<n>/head",
                exit_code=_REFUSAL_EXIT_CODE,
            )
        if iteration_index is None:
            return recoverable[0] if kind == "reject" else recoverable[-1]
        for index, iteration in recoverable:
            if index == iteration_index:
                return index, iteration
        recoverable_indices = [index for index, _ in recoverable]
        raise EmitRefusalError(
            check="unrecoverable-iteration",
            datapoint=datapoint,
            offending=f"iteration {iteration_index} has no materialized patch; the reviewed state was not recoverable",
            next_=f"pass --iteration naming a recoverable index: {recoverable_indices}",
            exit_code=_REFUSAL_EXIT_CODE,
        )

    @staticmethod
    def _selection_description(selected_index: int, kind: str, *, explicit: bool) -> str:
        """The one-line note `emit` prints about which iteration it built from and why.

        Without it the selection is silent, which makes a downstream `finding-line-absent` refusal hard
        to diagnose — the agent cannot see which iteration `emit` built from.
        """
        if explicit:
            return f"selected iteration {selected_index} (--iteration)"
        reason = "first recoverable, reject" if kind == "reject" else "last recoverable, approve"
        return f"selected iteration {selected_index} ({reason})"

    @staticmethod
    def _read_patch(dataset_dir: Path, iteration: IterationDict, datapoint: str) -> bytes:
        """The iteration's diff bytes, read from the path `capture` materialized (no git call, NFR-2)."""
        patch_path = dataset_dir / iteration["patch_path"]
        if not patch_path.is_file():
            raise EmitRefusalError(
                check="missing-patch",
                datapoint=datapoint,
                offending=f"{iteration['patch_path']} is not present under the dataset",
                next_="re-run `eval-harvest capture` so the iteration patches are materialized",
                exit_code=_REFUSAL_EXIT_CODE,
            )
        return patch_path.read_bytes()

    @staticmethod
    def _read_rubric(dataset_dir: Path, datapoint: str) -> bytes:
        """The dataset's authored `rubric.md`, copied into `tests/` as the rubric the judge grades against."""
        rubric_path = dataset_dir / _RUBRIC_FILENAME
        if not rubric_path.is_file():
            raise EmitRefusalError(
                check="missing-rubric",
                datapoint=datapoint,
                offending=f"{_RUBRIC_FILENAME} is not present at the dataset root",
                next_="run `eval-harvest init` and author rubric.md before emitting datapoints",
                exit_code=_REFUSAL_EXIT_CODE,
            )
        return rubric_path.read_bytes()

    # ───────────────────────────── the oracle ─────────────────────────────

    @classmethod
    def _oracle_findings(cls, candidate: CandidateDict, kind: str) -> list[_OracleFinding]:
        """The reference findings for this kind: every finding for a reject, the nits only for an approve.

        A reject datapoint grades against all the defects the reviewers raised (including the
        reference-only late comments, verifier-only). An approve datapoint grades the approved state,
        where the earlier defects were fixed — only the nits (non-blocking) carry over (FR-17).
        """
        selected = cls._selected_findings(candidate, kind)
        comments_by_id = {comment["id"]: comment for comment in candidate["comments"]}
        return [cls._one_oracle_finding(index, finding, comments_by_id) for index, finding in enumerate(selected)]

    @staticmethod
    def _one_oracle_finding(index: int, finding: FindingDict, comments_by_id: dict[int, CommentDict]) -> _OracleFinding:
        """One oracle finding with the file+line spans mined from the comments it groups."""
        locations = sorted(
            {
                (comment["path"], comment["line_start"], comment["line_end"])
                for comment_id in finding["comment_ids"]
                if (comment := comments_by_id.get(comment_id)) is not None and comment["path"]
            }
        )
        return _OracleFinding(
            id=index,
            statement=finding["statement"],
            severity=finding["severity"],
            reference_only=finding["reference_only"],
            locations=tuple(locations),
        )

    # ───────────────────────────── assemble, verify, promote ─────────────────────────────

    @classmethod
    def _assemble_verify_promote(
        cls, plan: _TaskPlan, dataset_dir: Path, verify_hook: VerifyHook, selection: str, selected_index: int
    ) -> EmitResult:
        """Write the task into a temp dir, validate + verify it, and promote it only on a pass (§7.2).

        Returns the promoted path together with the verification status the hook reported — the passed
        and unresolved checks the CLI names in its report — and the `selection` note describing which
        iteration was built and why. Failures raise before promotion."""
        with tempfile.TemporaryDirectory() as staging_root:
            temp_dir = Path(staging_root) / plan.directory_name
            cls._write_task_directory(plan, temp_dir)
            cls._refuse_invalid_layout(temp_dir, plan.datapoint)
            hook_result = cls._run_verify_hook(temp_dir, plan, verify_hook)
            target = dataset_dir / "tasks" / plan.directory_name
            cls._ship_metric_script(dataset_dir)
            promoted = cls._promote(temp_dir, target)
            return EmitResult(
                path=promoted,
                passed=hook_result.passed,
                unresolved=hook_result.unresolved,
                selection=selection,
                iteration_index=selected_index,
            )

    @classmethod
    def _write_task_directory(cls, plan: _TaskPlan, temp_dir: Path) -> None:
        """Write every file of the task, then stamp the content digest into `task.toml`."""
        files = cls._task_files(plan, content_digest="")
        cls._materialize(files, temp_dir)
        digest = cls._content_digest(files)
        # Re-emit task.toml carrying the digest over the just-hashed content (a pure function of the
        # emitted bytes, NFR-1); the digest covers the pre-digest task.toml plus every other file.
        cls._overwrite_task_config(plan, temp_dir, digest)

    @classmethod
    def _task_files(cls, plan: _TaskPlan, *, content_digest: str) -> list[_TaskFile]:
        """The full file list of the task directory, in a stable order (each carries its bytes + mode)."""
        return [
            _TaskFile("task.toml", cls._task_config_bytes(plan, content_digest), executable=False, role="agent"),
            _TaskFile("instruction.md", cls._read_template("instruction.md"), executable=False, role="agent"),
            _TaskFile(CHANGE_PATCH_RELPATH, plan.patch_bytes, executable=False, role="agent"),
            _TaskFile("environment/Dockerfile", cls._sealing_dockerfile(plan), executable=False, role="agent"),
            _TaskFile("tests/Dockerfile", cls._verifier_dockerfile(), executable=False, role="verifier"),
            _TaskFile("tests/test.sh", cls._read_template("test.sh"), executable=True, role="verifier"),
            _TaskFile("tests/score.py", cls._read_template("score.py"), executable=False, role="verifier"),
            _TaskFile("tests/oracle.json", plan.oracle_bytes(), executable=False, role="verifier"),
            _TaskFile("tests/rubric.md", plan.rubric_bytes, executable=False, role="verifier"),
            _TaskFile("tests/judge.toml", cls._read_template("judge.toml"), executable=False, role="verifier"),
            _TaskFile("solution/solve.sh", cls._solution_script(plan), executable=True, role="verifier"),
        ]

    @classmethod
    def _refuse_invalid_layout(cls, temp_dir: Path, datapoint: str) -> None:
        """Refuse if the assembled directory fails Harbor's layout or `task.toml` schema (FR-28, FR-32)."""
        task = cls._compiled_task(temp_dir)
        document = cls._parse_task_config(temp_dir)
        violations = Harbor.validate_task_layout(task) + Harbor.validate_task_config(document)
        if violations:
            raise EmitRefusalError(
                check="task-schema-invalid",
                datapoint=datapoint,
                offending="; ".join(str(violation) for violation in violations),
                next_="this is an emitter defect — report it; the assembled task does not satisfy Harbor's schema",
                exit_code=_VERIFICATION_EXIT_CODE,
            )

    @classmethod
    def _run_verify_hook(cls, temp_dir: Path, plan: _TaskPlan, verify_hook: VerifyHook) -> HookResult:
        """Run `verify` on the assembled task; refuse on any failure not overridden, else return its result (FR-38).

        Failures (exit 4) block the write unless named in `--override`; unresolved checks (exit 5)
        never block (§7.3) — they ride out in the returned `HookResult` for the CLI to report."""
        result = verify_hook(temp_dir)
        unhandled = [failure for failure in result.failures if failure.check not in plan.overrides]
        if unhandled:
            first = unhandled[0]
            raise EmitRefusalError(
                check=first.check,
                datapoint=plan.datapoint,
                offending=first.offending,
                next_=first.next_,
                exit_code=_VERIFICATION_EXIT_CODE,
            )
        return result

    @staticmethod
    def _promote(temp_dir: Path, target: Path) -> Path:
        """Move the verified temp directory into place at `tasks/<name>__<kind>/`, replacing any prior."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(temp_dir), str(target))
        return target

    @classmethod
    def _ship_metric_script(cls, dataset_dir: Path) -> None:
        """Write the dataset `metric.py` so the per-key aggregation is stated and runnable (§3/§8)."""
        dataset_dir.mkdir(parents=True, exist_ok=True)
        (dataset_dir / _METRIC_FILENAME).write_bytes(Harbor.metric_script(REWARD_KEYS))

    # ───────────────────────────── task.toml ─────────────────────────────

    @classmethod
    def _task_config_bytes(cls, plan: _TaskPlan, content_digest: str) -> bytes:
        """The `task.toml` bytes, with the harvest `content_digest` set to `content_digest`."""
        document = cls._task_config_document(plan)
        # `emit` builds the [metadata.harvest] table, so these casts are of a shape we just created,
        # not an untrusted parse; the recursive TomlValue union cannot be narrowed by isinstance.
        metadata = cast("dict[str, TomlValue]", document["metadata"])
        harvest = cast("dict[str, TomlValue]", metadata["harvest"])
        harvest["content_digest"] = content_digest
        return emit_document(document)

    @classmethod
    def _overwrite_task_config(cls, plan: _TaskPlan, temp_dir: Path, digest: str) -> None:
        (temp_dir / "task.toml").write_bytes(cls._task_config_bytes(plan, digest))

    @classmethod
    def _task_config_document(cls, plan: _TaskPlan) -> dict[str, TomlValue]:
        """The §8 `task.toml` value tree for this datapoint (verdict from `--kind`, provenance, harvest)."""
        return Harbor.task_config_document(
            name=plan.task_name,
            description=_TASK_DESCRIPTION,
            origin=plan.origin(),
            harvest=plan.harvest(),
        )

    @classmethod
    def _parse_task_config(cls, temp_dir: Path) -> dict[str, TomlValue]:
        parsed: dict[str, TomlValue] = tomllib.loads((temp_dir / "task.toml").read_text(encoding="utf-8"))
        return parsed

    # ───────────────────────────── dockerfiles ─────────────────────────────

    @classmethod
    def _sealing_dockerfile(cls, plan: _TaskPlan) -> bytes:
        """The `environment/Dockerfile` that seals a clone at the base commit and applies the change (FR-30)."""
        template = cls._read_template("Dockerfile.tmpl").decode("utf-8")
        rendered = (
            template.replace("@BASE_IMAGE@", _AGENT_BASE_IMAGE)
            .replace("@REPO_URL@", plan.repo_url)
            .replace("@BASE_COMMIT@", plan.base_commit)
        )
        return rendered.encode("utf-8")

    @classmethod
    def _solution_script(cls, plan: _TaskPlan) -> bytes:
        """The `solution/solve.sh` that submits the reference findings, inlined at emit time (FR-40).

        Rendered rather than copied, the way :meth:`_sealing_dockerfile` is: the payload is the plan's
        own findings, so the script and `tests/oracle.json` cannot disagree about the answer. `sort_keys`
        and fixed separators keep the bytes a function of the candidate alone (NFR-1) — this file is part
        of the content digest.

        `ensure_ascii=True` is deliberate. The payload is embedded in a shell heredoc inside a generated
        script, and an ASCII-only line is immune to any question about the encoding either the shell or
        the container's `python3` assumes for it; the escaping is lossless either way.
        """
        payload = json.dumps(plan.reference_submission(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        template = cls._read_template("solve.sh").decode("utf-8")
        return template.replace("@REFERENCE_SUBMISSION@", payload).encode("utf-8")

    @staticmethod
    def _verifier_dockerfile() -> bytes:
        """The `tests/Dockerfile` a separate verifier image is built from (self-provides /tests, FR-32).

        In separate mode Harbor builds the verifier image from `tests/` and uploads nothing
        (trial.py:612,686-693), so the hidden tests must be baked in. `--chmod` is explicit so the
        image's layer digests do not depend on a build shell's umask. `boto3` is installed because the
        judge (`score.py`) calls Bedrock signed with the MicroVM's IAM role; the slim base has no
        AWS SDK.
        """
        lines = [
            f"FROM {_VERIFIER_BASE_IMAGE}",
            "",
            "# Generated by eval-harvest emit. In separate-verifier mode Harbor builds this image from",
            "# tests/ as the build context and uploads nothing, so the verifier must be baked in.",
            f"RUN pip install --no-cache-dir {_VERIFIER_BOTO3_PIN}",
            "COPY --chmod=755 test.sh /tests/test.sh",
            "COPY --chmod=755 score.py /tests/score.py",
            "COPY --chmod=644 oracle.json /tests/oracle.json",
            "COPY --chmod=644 rubric.md /tests/rubric.md",
            "COPY --chmod=644 judge.toml /tests/judge.toml",
            "",
        ]
        return "\n".join(lines).encode("utf-8")

    # ───────────────────────────── layout & digest helpers ─────────────────────────────

    @staticmethod
    def _compiled_task(temp_dir: Path) -> CompiledTask:
        """The assembled directory as `Harbor.validate_task_layout` reads it."""
        files = tuple(
            EmittedFile(path=path.relative_to(temp_dir).as_posix()) for path in sorted(temp_dir.rglob("*")) if path.is_file()
        )
        return CompiledTask(name=temp_dir.name, files=files, directory=temp_dir.name, verifier_mode="separate")

    @staticmethod
    def _content_digest(files: list[_TaskFile]) -> str:
        """A `sha256:`-prefixed digest over the sorted (path, bytes) enumeration of the task (NFR-1).

        A pure function of the emitted bytes: paths are sorted and each contributes its relative path
        and its content, so the digest is stable across emits and changes when any file changes. This
        is the digest F-1's dataset manifest records per task; no other task defines it.
        """
        digest = hashlib.sha256()
        for task_file in sorted(files, key=lambda item: item.path):
            digest.update(task_file.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(task_file.data)
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _materialize(files: list[_TaskFile], temp_dir: Path) -> None:
        """Write each task file to disk under `temp_dir`, creating parents and setting the exec bit."""
        for task_file in files:
            path = temp_dir / task_file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(task_file.data)
            if task_file.executable:
                path.chmod(0o755)

    @staticmethod
    def _read_template(name: str) -> bytes:
        """The bytes of a `verifier_tpl/` template, read verbatim (the data the CLI writes, not imports)."""
        return (_TEMPLATE_DIR / name).read_bytes()


@dataclass(frozen=True, slots=True)
class _TaskFile:
    """One emitted file: its task-relative POSIX path, its bytes, whether it is executable, its role."""

    path: str
    data: bytes
    executable: bool
    role: str


@dataclass(frozen=True, slots=True)
class _FindingIterations:
    """One finding's iteration coverage for the mismatch check: the iterations its located comments live in.

    `number` is its 1-based position in the emitted oracle (printed as `finding 1`); `iterations` is
    the sorted union of `in_diff_iterations` over its file-anchored comments; `locatable` is whether it
    has any such comment; `comment_id` is one of them, for the `show` hint in the refusal's `next:`."""

    number: int
    locatable: bool
    iterations: tuple[int, ...]
    comment_id: int | None


def _suggest_iteration(mismatched: list[_FindingIterations]) -> int | None:
    """A recoverable iteration to name in the `next:` hint: the one covering the most mismatched findings.

    Ties break to the lowest index so the hint is deterministic (NFR-1 in spirit — a stable message).
    Returns None when no recoverable diff covers any offending finding, in which case `--iteration`
    cannot recover the datapoint and only the rewrite path is worth offering."""
    coverage_counts: dict[int, int] = {}
    for info in mismatched:
        for index in info.iterations:
            coverage_counts[index] = coverage_counts.get(index, 0) + 1
    if not coverage_counts:
        return None
    return min(coverage_counts, key=lambda index: (-coverage_counts[index], index))


@dataclass(frozen=True, slots=True)
class _OracleFinding:
    """A reference finding as it lands in `tests/oracle.json`: statement, severity, and file+line spans."""

    id: int
    statement: str
    severity: str
    reference_only: bool
    locations: tuple[tuple[str, int, int], ...]

    def as_document(self) -> dict[str, object]:
        """The JSON object for this finding; `blocking` is true only for a high-severity defect (FR-14)."""
        return {
            "id": self.id,
            "statement": self.statement,
            "severity": self.severity,
            "reference_only": self.reference_only,
            "blocking": self.severity == _BLOCKING_SEVERITY,
            "locations": [{"path": path, "line_start": start, "line_end": end} for path, start, end in self.locations],
        }


@dataclass(frozen=True, slots=True)
class _TaskPlan:
    """Everything assembled about one datapoint, computed once so the writers stay pure functions."""

    candidate: CandidateDict
    kind: str
    iteration: IterationDict
    patch_bytes: bytes
    rubric_bytes: bytes
    oracle_findings: list[_OracleFinding]
    overrides: tuple[str, ...]

    @property
    def datapoint(self) -> str:
        return f"pr-{self.candidate['pr_number']}/{self.kind}"

    @property
    def repo(self) -> str:
        return self.candidate["repo"]

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}.git"

    @property
    def base_commit(self) -> str:
        return self.iteration["base_sha"]

    @property
    def expected_verdict(self) -> str:
        """The verdict follows `--kind`, never a severity formula (FR-14): reject⇒block, approve⇒approve."""
        return "block" if self.kind == "reject" else "approve"

    @property
    def task_name(self) -> str:
        """Harbor's `org/name` task id, e.g. `our-org/our-repo__pr1234-reject`."""
        return f"{self.repo}__pr{self.candidate['pr_number']}-{self.kind}"

    @property
    def directory_name(self) -> str:
        """The on-disk directory, the task name with its slug slash flattened (§8)."""
        return f"{self.repo.replace('/', '__')}__pr{self.candidate['pr_number']}-{self.kind}"

    def origin(self) -> TaskOrigin:
        """`[metadata.origin]` provenance (FR-29). The dates are absent from the candidate — `emit`
        makes no git call (NFR-2) so it cannot look them up — and are left empty rather than fabricated."""
        return TaskOrigin(
            repo=self.repo,
            pr_numbers=(self.candidate["pr_number"],),
            base_commit=self.base_commit,
            base_commit_date="",
            merged_at="",
        )

    def harvest(self) -> HarvestFields:
        """`[metadata.harvest]`: verdict from `--kind`, both risk values, disagreement, rubric, overrides."""
        risk = self.candidate["change_risk"]
        return HarvestFields(
            kind=self.kind,
            expected_verdict=self.expected_verdict,
            blocking_severity=_BLOCKING_SEVERITY,
            finding_severities=tuple(f.severity for f in self.oracle_findings if f.severity in RISK_LEVELS),
            change_risk_structural=risk["risk_structural"],
            change_risk_classified=risk["risk_classified"],
            change_risk_disagreement=risk["risk_structural"] != risk["risk_classified"],
            rubric_version=self.candidate["rubric_version"],
            overrides=self.overrides,
        )

    def reference_submission(self) -> list[dict[str, object]]:
        """The known-good submission `solution/solve.sh` makes: one record per (finding, location) pair.

        Exactly the four fields `score.py` consumes — `path`, `line`, `statement`, `severity` — and no
        oracle metadata (ids, `reference_only`, rationales), so the oracle submits only what a real agent
        could have submitted and its perfect score stays meaningful (FR-40). Drawn from the same
        `oracle_findings` :meth:`oracle_bytes` serializes.
        """
        return [
            {"path": path, "line": line_start, "statement": finding.statement, "severity": finding.severity}
            for finding in self.oracle_findings
            for path, line_start, _ in finding.locations
        ]

    def oracle_bytes(self) -> bytes:
        """The `tests/oracle.json` bytes: the reference findings, deterministic and LF-terminated."""
        document = {
            "expected_verdict": self.expected_verdict,
            "blocking_severity": _BLOCKING_SEVERITY,
            "findings": [finding.as_document() for finding in self.oracle_findings],
        }
        return (json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
