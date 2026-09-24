"""The `verify` verb: the guarantee that replaces the absent human (US-7, FR-34-39).

`verify` decides, per datapoint, whether it is sound enough to score and whether its answer is absent
from everywhere the evaluated agent can read. It runs four *structural* checks against the emitted
task directory and the local clone — the base commit exists, `change.patch` applies at it, every
oracle finding references a line the change actually touches, and the rubric is coherent — plus the
two-halves *absence* scan from `seal.py`: the content scan over agent-visible files (guarded by a
planted control token) and the git-channel checklist over a materialized sealed tree. Every failure
is reported in the FR-2 four-field shape (`check`, `datapoint`, `offending`, `next`) so the
unsupervised agent can fix and retry (FR-39). This is the verb `emit` calls before writing (wired in
E-4) and the `verify <task-dir>` command the agent runs directly.

**Offline (NFR-2, ADR-4).** `verify` reads the task directory and the local clone only — no `gh`, no
`git fetch`, no remote op. The base-exists and patch-applies checks use the clone's objects through a
throwaway index (`GIT_INDEX_FILE`) so the clone's own index and working tree are never touched. The
content scan runs over a *copy* of the task directory so the emitted datapoint is never mutated (§9:
`verify` writes nothing).

**Runtime-absent is unresolved, never a pass (§7.3).** The git-channel checklist needs a *materialized*
sealed tree — the sealing container built from the datapoint's Dockerfile. Building that container is
deferred (the Lambda MicroVMs credential `[TODO]` gates it; task Out Of Scope), so unless a sealed
tree is supplied, that half reports `skipped: no runtime` and counts *unresolved*: the process exits
5, and a reader can never mistake "skipped" for "passed" (the single most dangerous misread, §4 risk
row 6). Exit codes: 0 pass · 4 a check failed · 5 a check was unresolved.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eval_harvest.diffspan import location_in_spans, new_side_spans
from eval_harvest.gitcmd import GitCommandRunner
from eval_harvest.seal import Finding, Report, Seal

#: Exit-code classes, pinned to the tech-plan §9 values (`cli.ExitCode`). Restated as ints rather
#: than importing `cli` — `cli` imports `verify`, so the dependency runs one way; `tests/test_cli.py`
#: and the `ExitCode` enum lock these same numbers, so a drift is caught there (mirrors `emit.py`).
_SUCCESS_EXIT_CODE: Final = 0
_VERIFICATION_EXIT_CODE: Final = 4
_UNRESOLVED_EXIT_CODE: Final = 5

#: Where `change.patch` lives inside the emitted task: *inside* `environment/`, the Docker build
#: context, so the sealing Dockerfile's `COPY change.patch` resolves. `emit` writes it here and
#: `verify` reads it here; a single constant keeps the two from spelling the path independently. A
#: patch at the task root instead would fall outside the build context, where the image build cannot
#: reach it. `emit` imports this name (the dependency runs emit→verify), so there is one source of truth.
CHANGE_PATCH_RELPATH: Final = "environment/change.patch"

#: The honesty guard's control token (FR-36/NFR-6). `verify` plants it into a copy of the scanned
#: corpus; if `seal.scan_agent_visible` does not find it, the scan did not really read the corpus, so
#: it refuses `scan-did-not-run` rather than trusting an empty result. Kept as plant-then-check (not
#: self-planting inside the scanner) so E-3 can remove the plant and watch the guard go red.
_CONTROL_TOKEN: Final = "EVAL-HARVEST-SCAN-CONTROL-TOKEN"

#: The agent-visible file `verify` writes the control token into, inside the throwaway corpus copy.
_CONTROL_FILENAME: Final = ".eval-harvest-scan-control"

#: The message that marks the git-channel check unresolved when no materialized seal is available; the
#: substring "skipped: no runtime" is what tells a reader (and E-4) this is exit 5, not a pass (§7.3).
#: The standalone `verify` command never materializes a seal (it is built when the datapoint runs in
#: its sealing container), so the message must not promise that giving *this command* a runtime resolves
#: the check — that guidance is a promise the command cannot keep.
_NO_RUNTIME: Final = (
    "skipped: no runtime — the git-channel checklist runs against a materialized sealing container, "
    "which is built when the datapoint is executed, not by the standalone `verify` command (§7.3)"
)


@dataclass(frozen=True, slots=True)
class VerifyRefusal:
    """One `verify` verdict in the FR-2 four-field shape, plus whether it is unresolved (exit 5) or a fail.

    `unresolved=True` marks a check that could not run (the container-seal variant with no runtime),
    which counts toward exit 5, never a pass; `unresolved=False` is a hard failure, which counts
    toward exit 4. Both carry all four actionable fields (FR-39).
    """

    check: str
    datapoint: str
    offending: str
    next_: str
    unresolved: bool = False


#: A base-tree oracle: given an answer token found in the agent-visible corpus, report whether
#: it already exists in the repository *at the base commit* — `True` (it predates the change, so its
#: presence carries no information about the review and is not a leak), `False` (introduced at or after
#: the change, so it is a real hit), or `None` (the base object is missing from the clone — a shallow
#: fetch — so the question cannot be answered and the token must **not** be suppressed). Built from a
#: `--clone`; `None` (the callable itself) when no clone is available.
BaseTreeOracle = Callable[[str], bool | None]


@dataclass(frozen=True, slots=True)
class SuppressedToken:
    """One answer token the base-tree oracle cleared: it exists at the base commit, so it is not a leak.

    Recorded and reported (never silent, §CLAUDE.md) so a suppression is always visible — a scan that
    suppresses most of its tokens is a signal, and an invisible suppression is how a real leak hides
    behind a "clean" run."""

    token: str
    location: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class ContentScanSummary:
    """The answer-absence scan's arithmetic: tokens scanned, corpus hits, and hits suppressed as pre-existing.

    `scanned` is the number of answer tokens the content scan looked for; `hits` is how many were
    found in the agent-visible corpus; `suppressed` are the hits the base-tree oracle cleared. A hit
    that is not suppressed is a failure; `hits - len(suppressed)` is the number that fired."""

    scanned: int
    hits: int
    suppressed: tuple[SuppressedToken, ...]


#: The canonical checks `verify_task` runs, in order — the names a reader (and `emit`'s report) uses
#: to say which checks ran and passed. One name per `_check_*` helper: each helper is one check that
#: either passes (no refusal), fails (exit 4), or is unresolved (exit 5). Stated as data so `emit`'s
#: honest "N checks ran and passed" line and the `--json` `checks.passed` list have a fixed set to
#: name, without inferring it from the ad-hoc refusal names a failing check happens to emit.
CHECK_NAMES: Final = (
    "base-and-patch",
    "finding-lines-present",
    "rubric-coherent",
    "content-absence",
    "git-channel-absence",
)


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """The outcome of one `verify` run: the datapoint it checked, every refusal, and the checks that passed."""

    datapoint: str
    refusals: tuple[VerifyRefusal, ...]
    #: The names (from `CHECK_NAMES`) of the checks that ran and passed — no failure, nothing
    #: unresolved. Defaulted so a `VerifyReport` built without it (older callers, tests) is still valid;
    #: `verify_task` always supplies it. This is the passed set `emit` reports and `--json` lists.
    passed: tuple[str, ...] = ()
    #: The answer-absence scan's arithmetic: tokens scanned, hits, and hits suppressed as
    #: pre-existing. Defaulted to `None` so a `VerifyReport` built without it stays valid;
    #: `verify_task` always supplies it. The CLI prints it so a suppression is never silent.
    content_scan: ContentScanSummary | None = None

    @property
    def failures(self) -> list[VerifyRefusal]:
        """The hard failures — a broken datapoint (exit 4)."""
        return [refusal for refusal in self.refusals if not refusal.unresolved]

    @property
    def unresolved(self) -> list[VerifyRefusal]:
        """The checks that could not run — unresolved, never a pass (exit 5)."""
        return [refusal for refusal in self.refusals if refusal.unresolved]

    @property
    def ok(self) -> bool:
        """True only when every check ran and passed — no failure and nothing unresolved (exit 0)."""
        return not self.refusals

    @property
    def exit_code(self) -> int:
        """0 pass · 4 a check failed · 5 a check was unresolved. A failure outranks an unresolved check."""
        if self.failures:
            return _VERIFICATION_EXIT_CODE
        if self.unresolved:
            return _UNRESOLVED_EXIT_CODE
        return _SUCCESS_EXIT_CODE

    def as_json(self) -> dict[str, object]:
        """The `{ok, failures:[...], unresolved:[...]}` shape `--json` emits (§9); each entry four-field.

        Carries the answer-absence arithmetic under `answer_absence` when a content scan ran, so
        a driver can see how many tokens were suppressed as pre-existing rather than treating a clean
        scan and a heavily-suppressed one as the same result."""
        payload: dict[str, object] = {
            "ok": self.ok,
            "failures": [self._as_fields(refusal) for refusal in self.failures],
            "unresolved": [self._as_fields(refusal) for refusal in self.unresolved],
        }
        if self.content_scan is not None:
            payload["answer_absence"] = {
                "scanned": self.content_scan.scanned,
                "hits": self.content_scan.hits,
                "suppressed": [
                    {"token": token.token, "location": token.location, "base_commit": token.base_commit}
                    for token in self.content_scan.suppressed
                ],
            }
        return payload

    @staticmethod
    def _as_fields(refusal: VerifyRefusal) -> dict[str, str]:
        return {"check": refusal.check, "datapoint": refusal.datapoint, "offending": refusal.offending, "next": refusal.next_}


class Verify:
    """Structural checks, the two absence scans, and the FR-2 reporting for one datapoint. Holds no state."""

    # Six parameters because `verify` needs each: the task and the two optional inputs (`clone`,
    # `materialized_seal`), the E-3 control-token toggle, and the `base_oracle` seam. Collapsing
    # any into an options object would hide the contract, so the count is suppressed like `emit`'s.
    @classmethod
    def verify_task(  # noqa: PLR0913
        cls,
        task_dir: Path,
        *,
        clone: Path | None = None,
        materialized_seal: Path | None = None,
        plant_control_token: bool = True,
        base_oracle: BaseTreeOracle | None = None,
    ) -> VerifyReport:
        """Verify the emitted `task_dir` and return every refusal, aggregated (FR-39: never stop at the first).

        `clone` is a local clone whose objects hold the base commit — needed for the base-exists and
        patch-applies checks; absent, those two report unresolved. `materialized_seal` is a sealed tree
        for the git-channel checklist; absent, that half reports unresolved (§7.3, no runtime).
        `plant_control_token=False` skips planting the control token, so the honesty guard can be
        watched to fail (E-3). Reads local files and the local clone only — no network (NFR-2).

        `base_oracle` answers whether an answer token found in the corpus already existed at the
        base commit; such a token predates the change and is not a leak, so the content scan suppresses
        it. When `None` and a `clone` is available, the real `git grep`-against-the-sha oracle is built
        here; a test may inject a fixed one. Without an oracle a corpus hit still fails `answer-present`
        — guessing that an unresolvable token is benign is not an option (the control token is never
        suppressible either way, so `scan-did-not-run` keeps firing regardless of the oracle).
        """
        origin, harvest = cls._read_task_config(task_dir)
        datapoint = cls._datapoint(origin, harvest, task_dir)
        if base_oracle is None and clone is not None:
            base_oracle = cls._build_base_tree_oracle(task_dir, clone, origin)
        content_refusals, content_scan = cls._check_content_absence(
            task_dir, origin, datapoint, base_oracle=base_oracle, plant=plant_control_token
        )
        # One (name, refusals) entry per canonical check, in `CHECK_NAMES` order. A check with no
        # refusal ran and passed; one with any refusal failed (exit 4) or is unresolved (exit 5). The
        # names let `emit` report which checks ran and passed without decoding the ad-hoc refusal names.
        by_check: list[tuple[str, list[VerifyRefusal]]] = [
            ("base-and-patch", cls._check_base_and_patch(task_dir, clone, origin, datapoint)),
            ("finding-lines-present", cls._check_findings_reference_real_lines(task_dir, datapoint)),
            ("rubric-coherent", cls._check_rubric_coherent(task_dir, harvest, datapoint)),
            ("content-absence", content_refusals),
            ("git-channel-absence", cls._check_git_channels(materialized_seal, datapoint)),
        ]
        refusals = tuple(refusal for _, group in by_check for refusal in group)
        passed = tuple(name for name, group in by_check if not group)
        return VerifyReport(datapoint=datapoint, refusals=refusals, passed=passed, content_scan=content_scan)

    # ───────────────────────────── task-config reads ─────────────────────────────

    @staticmethod
    def _read_task_config(task_dir: Path) -> tuple[dict[str, object], dict[str, object]]:
        """The `[metadata.origin]` and `[metadata.harvest]` tables of the emitted `task.toml`."""
        document = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        metadata = document.get("metadata", {})
        origin: dict[str, object] = metadata.get("origin", {})
        harvest: dict[str, object] = metadata.get("harvest", {})
        return origin, harvest

    @staticmethod
    def _datapoint(origin: dict[str, object], harvest: dict[str, object], task_dir: Path) -> str:
        """`pr-<n>/<kind>` from provenance (the §7.2 shape), falling back to the directory name."""
        pr_numbers = origin.get("pr_numbers")
        kind = harvest.get("kind")
        if isinstance(pr_numbers, list) and pr_numbers and isinstance(kind, str) and kind:
            return f"pr-{pr_numbers[0]}/{kind}"
        return task_dir.name

    @staticmethod
    def _pr_number_tokens(origin: dict[str, object]) -> list[str]:
        """The answer tokens the content scan greps for: the `#<n>` form of each PR number (§7.2)."""
        pr_numbers = origin.get("pr_numbers")
        if not isinstance(pr_numbers, list):
            return []
        return [f"#{number}" for number in pr_numbers if isinstance(number, int)]

    # ───────────────────────────── base-exists + patch-applies (FR-34) ─────────────────────────────

    @classmethod
    def _check_base_and_patch(
        cls, task_dir: Path, clone: Path | None, origin: dict[str, object], datapoint: str
    ) -> list[VerifyRefusal]:
        """The base commit exists in the clone, and `change.patch` applies cleanly at it (FR-34)."""
        base_commit = origin.get("base_commit")
        if not isinstance(base_commit, str) or not base_commit:
            return [
                cls._fail(
                    "base-missing",
                    datapoint,
                    "task.toml records no base_commit",
                    "re-run `emit` from a candidate whose iteration has a base",
                )
            ]
        if clone is None:
            return [
                cls._unresolved(
                    "base-and-patch",
                    datapoint,
                    "no clone provided; the base and patch cannot be checked",
                    "pass `--clone <dir>` pointing at a local clone of the repo",
                )
            ]
        if not cls._base_exists(clone, base_commit):
            return [
                cls._fail(
                    "base-missing",
                    datapoint,
                    f"base commit {base_commit} is absent from the clone {clone}",
                    "clone the repo and fetch the base commit, or re-capture the PR",
                )
            ]
        if not cls._patch_applies(clone, base_commit, task_dir / CHANGE_PATCH_RELPATH):
            return [
                cls._fail(
                    "patch-does-not-apply",
                    datapoint,
                    f"change.patch does not apply cleanly at base commit {base_commit}",
                    "re-capture the PR so the diff matches its base, or fix change.patch",
                )
            ]
        return []

    @staticmethod
    def _base_exists(clone: Path, base_commit: str) -> bool:
        """True when the base commit object is present in the local clone (`git cat-file -e`)."""
        code, _ = GitCommandRunner.git(clone, "cat-file", "-e", f"{base_commit}^{{commit}}")
        return code == 0

    @classmethod
    def _patch_applies(cls, clone: Path, base_commit: str, patch_path: Path) -> bool:
        """True when `change.patch` applies to the base tree, checked against a throwaway index.

        Seeds a temporary index from the base commit's tree (`read-tree`) and runs `apply --cached
        --check` against it, so the check answers "does this patch apply *at the base*" without ever
        touching the clone's real index or working tree — the read-only guarantee `verify` needs.

        **The file is applied exactly as shipped.** The check deliberately does not normalize the patch
        before applying it — no copying to a temp dir, no appending a trailing newline. `git apply`
        rejects an unterminated final hunk, and the artefact the image build consumes is the stored file
        itself; if this check repaired the patch first it could go green while the image build's `RUN git
        apply` failed on the same corrupt bytes. The terminating newline belongs to the stored bytes
        (`candidate._newline_terminated`), so this check is allowed to see — and reject — a malformed patch.
        """
        # The patch path is passed *absolute*. `GitCommandRunner.git` shells `git -C <clone>`, so git's
        # working directory is the clone; a caller-relative `patch_path` (e.g. the `my-dataset/tasks/…`
        # the operator typed) would resolve against the clone, not the caller's CWD, and git would report
        # "can't open patch … No such file" — surfacing as a false `patch-does-not-apply` about the diff
        # rather than a missing file. Resolving here closes that at the one place a caller path reaches
        # `git -C <clone>`.
        absolute_patch_path = patch_path.resolve()
        with tempfile.TemporaryDirectory() as staging, cls._git_index(Path(staging) / "index"):
            read_code, _ = GitCommandRunner.git(clone, "read-tree", base_commit)
            if read_code != 0:
                return False
            apply_code, _ = GitCommandRunner.git(clone, "apply", "--cached", "--check", str(absolute_patch_path))
            return apply_code == 0

    @staticmethod
    @contextmanager
    def _git_index(index_path: Path) -> Iterator[None]:
        """Point `GIT_INDEX_FILE` at a throwaway index for the duration, then restore the prior value.

        `GitCommandRunner.git` derives its child env from `os.environ`, so setting the variable here is
        how the temporary index reaches the git child without a bespoke env-passing entry point (A-2's
        chokepoint stays the single place git is spawned)."""
        previous = os.environ.get("GIT_INDEX_FILE")
        os.environ["GIT_INDEX_FILE"] = str(index_path)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("GIT_INDEX_FILE", None)
            else:
                os.environ["GIT_INDEX_FILE"] = previous

    # ───────────────────────────── findings reference real lines (FR-34) ─────────────────────────────

    @classmethod
    def _check_findings_reference_real_lines(cls, task_dir: Path, datapoint: str) -> list[VerifyRefusal]:
        """Every oracle finding location points at a file+line the change actually touches (FR-34).

        A finding on a line the change does not contain is ungradeable — the evaluated agent never sees
        that line, so it cannot be credited or missed against it. Cross-checks each location against the
        new-side line spans parsed from `change.patch`.
        """
        oracle = json.loads((task_dir / "tests" / "oracle.json").read_text(encoding="utf-8"))
        changed = new_side_spans((task_dir / CHANGE_PATCH_RELPATH).read_text(encoding="utf-8", errors="replace"))
        refusals: list[VerifyRefusal] = []
        for finding in oracle.get("findings", []):
            refusals += cls._check_one_finding_locations(finding, changed, datapoint)
        return refusals

    @classmethod
    def _check_one_finding_locations(
        cls, finding: dict[str, object], changed: dict[str, list[tuple[int, int]]], datapoint: str
    ) -> list[VerifyRefusal]:
        """A finding whose location falls outside the change's touched lines fails `finding-line-absent`."""
        locations = finding.get("locations")
        if not isinstance(locations, list):
            return []
        refusals: list[VerifyRefusal] = []
        for location in locations:
            path = str(location.get("path", ""))
            start = int(location.get("line_start", 0))
            end = int(location.get("line_end", 0) or start)
            if not location_in_spans(path, start, end, changed):
                refusals.append(
                    cls._fail(
                        "finding-line-absent",
                        datapoint,
                        f"finding {finding.get('id')} references {path}:{start}-{end}, not present in the change",
                        "point the finding at a line the change touches, or drop it if it is unrelated to this diff",
                    )
                )
        return refusals

    # ───────────────────────────── rubric coherence (FR-34) ─────────────────────────────

    @classmethod
    def _check_rubric_coherent(cls, task_dir: Path, harvest: dict[str, object], datapoint: str) -> list[VerifyRefusal]:
        """The rubric is present and non-empty in `tests/`, and the datapoint pins a rubric version (FR-34)."""
        rubric = task_dir / "tests" / "rubric.md"
        version = harvest.get("rubric_version")
        if not rubric.is_file() or not rubric.read_text(encoding="utf-8").strip():
            return [
                cls._fail(
                    "rubric-incoherent",
                    datapoint,
                    "tests/rubric.md is missing or empty — the judge grades against nothing",
                    "author rubric.md and re-emit so the task carries the rubric the judge uses",
                )
            ]
        if not isinstance(version, str) or not version:
            return [
                cls._fail(
                    "rubric-incoherent",
                    datapoint,
                    "task.toml records no rubric_version — the datapoint is not tied to a rubric",
                    "pin rubric_version in the candidate and re-emit",
                )
            ]
        return []

    # ───────────────────────────── base-tree oracle ─────────────────────────────

    @classmethod
    def _build_base_tree_oracle(cls, task_dir: Path, clone: Path, origin: dict[str, object]) -> BaseTreeOracle | None:
        """The real base-tree oracle: `git grep` a token against the base commit's tree in the clone.

        Returns `None` (no oracle) when `task.toml` records no base commit, so a hit still fails rather
        than being cleared by a lookup that could not run. The lookup is scoped to the paths the change
        touches — the files whose base content actually bleeds into the agent-visible corpus — so a
        token that exists at the base only inside an untouched file the agent never sees is not counted
        as pre-existing (see the task's Notes). It runs `git grep` against the commit *object*, needing
        no worktree checkout, so an untracked or dirty file in the clone cannot confuse it (NFR-2: no
        network — `git grep` is a local read).
        """
        base_commit = origin.get("base_commit")
        if not isinstance(base_commit, str) or not base_commit:
            return None
        paths = cls._changed_paths(task_dir)

        def present_at_base(token: str) -> bool | None:
            argv = ["grep", "--fixed-strings", "--quiet", "-e", token, base_commit]
            if paths:
                argv += ["--", *paths]
            code, _ = GitCommandRunner.git(clone, *argv)
            if code == 0:
                return True  # the token is in the base tree — it predates the change, not a leak
            if code == 1:
                return False  # a clean "no match" — the token was introduced at or after the change
            return None  # git errored (e.g. the base object is missing from a shallow clone) — cannot answer

        return present_at_base

    @staticmethod
    def _changed_paths(task_dir: Path) -> tuple[str, ...]:
        """The new-side paths `change.patch` touches — what the base-tree grep is scoped to."""
        patch_text = (task_dir / CHANGE_PATCH_RELPATH).read_text(encoding="utf-8", errors="replace")
        return tuple(sorted(new_side_spans(patch_text)))

    # ───────────────────────────── content absence scan (FR-35/36) ─────────────────────────────

    @classmethod
    def _check_content_absence(
        cls, task_dir: Path, origin: dict[str, object], datapoint: str, *, base_oracle: BaseTreeOracle | None, plant: bool
    ) -> tuple[list[VerifyRefusal], ContentScanSummary]:
        """Grep the agent-visible corpus for the PR number and any leaked answer fragment (FR-35/40).

        Scans a *copy* of the task directory so the emitted datapoint is never mutated (§9). Plants the
        control token into that copy first (unless `plant` is false, the E-3 break); if the scan does
        not see it, it refuses `scan-did-not-run` rather than trusting an empty result (FR-36/NFR-6). A
        hit whose token `base_oracle` reports present at the base commit predates the change and is
        suppressed rather than failed. Returns the refusals and the scan's arithmetic.
        """
        tokens = cls._pr_number_tokens(origin)
        base_commit = origin.get("base_commit")
        base_commit_str = base_commit if isinstance(base_commit, str) and base_commit else None
        with tempfile.TemporaryDirectory() as staging:
            corpus = Path(staging) / task_dir.name
            shutil.copytree(task_dir, corpus)
            if plant:
                (corpus / _CONTROL_FILENAME).write_text(f"{_CONTROL_TOKEN}\n", encoding="utf-8")
            report = Seal.scan_agent_visible(corpus, tokens=tokens, control_token=_CONTROL_TOKEN)
        return cls._map_content_report(
            report, datapoint, task_dir, len(tokens), base_oracle=base_oracle, base_commit=base_commit_str
        )

    # Six parameters because the mapping needs each: the seal report and datapoint, the `task_dir` and
    # scanned-token count for the arithmetic and the `next:` command, and the oracle + base commit.
    @classmethod
    def _map_content_report(  # noqa: PLR0913
        cls,
        report: Report,
        datapoint: str,
        task_dir: Path,
        scanned: int,
        *,
        base_oracle: BaseTreeOracle | None,
        base_commit: str | None,
    ) -> tuple[list[VerifyRefusal], ContentScanSummary]:
        """Map the seal content report into refusals, suppressing pre-existing tokens.

        `scan-did-not-run` short-circuits *before* the oracle is consulted, so the control token can
        never be suppressed and the honesty guard keeps firing regardless of the base tree (NFR-6). A
        `answer-absent:<token>` hit is cleared when `base_oracle` reports the token present at the base
        commit (recorded as a suppression, never silent); otherwise it fails `answer-present` with an
        actionable location and `next:`. A leaked verifier-only *fragment* (the reference fix itself) is
        always a hard leak — it is the answer, not repository prose, so it is never suppressible.
        """
        empty = ContentScanSummary(scanned=scanned, hits=0, suppressed=())
        if any(failure.check == "scan-did-not-run" for failure in report.failures):
            return [cls._scan_did_not_run(datapoint)], empty
        refusals: list[VerifyRefusal] = []
        suppressed: list[SuppressedToken] = []
        hits = 0
        for failure in report.failures:
            if not failure.check.startswith("answer-absent:"):
                refusals.append(cls._answer_present(failure, datapoint, "agent-visible content"))
                continue
            hits += 1
            outcome = cls._classify_hit(failure, datapoint, task_dir, base_commit, base_oracle)
            if isinstance(outcome, SuppressedToken):
                suppressed.append(outcome)
            else:
                refusals.append(outcome)
        return refusals, ContentScanSummary(scanned=scanned, hits=hits, suppressed=tuple(suppressed))

    @classmethod
    def _classify_hit(
        cls,
        failure: Finding,
        datapoint: str,
        task_dir: Path,
        base_commit: str | None,
        base_oracle: BaseTreeOracle | None,
    ) -> VerifyRefusal | SuppressedToken:
        """Classify one `answer-absent:<token>` hit: a suppression (predates the change) or an `answer-present` refusal.

        The control token is never handed to the oracle (`verdict` stays `None`), so it can never be
        suppressed — a defence-in-depth pairing with the `scan-did-not-run` short-circuit above, since
        the control token is not in the scanned token set to begin with."""
        token = failure.check.split(":", 1)[1]
        location = cls._hit_location(failure)
        verdict = base_oracle(token) if base_oracle is not None and token != _CONTROL_TOKEN else None
        if verdict is True and base_commit is not None:
            return SuppressedToken(token=token, location=location, base_commit=base_commit)
        return cls._content_leak_refusal(
            datapoint, token, location, task_dir, base_commit, oracle_ran=base_oracle is not None, undetermined=verdict is None
        )

    @staticmethod
    def _hit_location(failure: Finding) -> str:
        """The `<relpath>:<line>` half of a seal `answer-absent` hit detail (`'<token>' at <relpath>:<line>`)."""
        marker = " at "
        return failure.detail.split(marker, 1)[1] if marker in failure.detail else failure.detail

    @classmethod
    def _scan_did_not_run(cls, datapoint: str) -> VerifyRefusal:
        """The honesty-guard refusal when the planted control token was not seen (FR-36/NFR-6)."""
        return cls._fail(
            "scan-did-not-run",
            datapoint,
            "the content scan returned without finding its planted control token — an empty result is not trusted clean",
            "re-run verify; if it recurs the scanned corpus is unreadable — this is a verifier defect, report it",
        )

    # Six parameters because an actionable answer-present refusal (FR-2) needs each: the datapoint, the
    # offending token and its location, the task dir for a runnable `next:`, and — to pick which of the
    # three messages — the base commit plus whether the oracle ran and whether it could answer.
    @classmethod
    def _content_leak_refusal(  # noqa: PLR0913
        cls,
        datapoint: str,
        token: str,
        location: str,
        task_dir: Path,
        base_commit: str | None,
        *,
        oracle_ran: bool,
        undetermined: bool,
    ) -> VerifyRefusal:
        """An `answer-present` content-hit refusal, worded for whether the base tree could be consulted.

        Three cases: no clone (cannot tell if it predates the change — point at `verify --clone`); a
        clone whose base object is missing (a shallow fetch — fetch it, then re-run); or a clone that
        answered "not at base" (a real leak — remove it). Every message names the offending file and
        line and gives a runnable `next:` — an unactionable "remove the answer" invites a by-hand
        workaround that defeats the check."""
        if not oracle_ran:
            offending = f"token {token!r} found at {location}; cannot tell whether it predates the change without a clone"
            next_ = (
                f"eval-harvest verify {task_dir} --clone <clone-dir> to resolve it; "
                "or, if it is genuinely pre-existing, re-emit with --override answer-present"
            )
        elif undetermined:
            offending = (
                f"token {token!r} found at {location}; base commit {base_commit} is missing from the clone, "
                "so whether it predates the change cannot be checked"
            )
            next_ = (
                f"fetch the base commit into the clone and re-run eval-harvest verify {task_dir} --clone <clone-dir>; "
                "or re-emit with --override answer-present if it is genuinely pre-existing"
            )
        else:
            offending = f"token {token!r} found at {location}; it does not exist at the base commit, so it is not pre-existing"
            next_ = "remove the leaked reference (or re-run capture); do not --override a real answer leak"
        return VerifyRefusal(check="answer-present", datapoint=datapoint, offending=offending, next_=next_)

    # ───────────────────────────── git-channel checklist (FR-35, §7.3) ─────────────────────────────

    @classmethod
    def _check_git_channels(cls, materialized_seal: Path | None, datapoint: str) -> list[VerifyRefusal]:
        """Run the git-channel checklist on the materialized seal, or report it unresolved (§7.3)."""
        if materialized_seal is None:
            guidance = (
                "run the datapoint through the eval harness (it materializes the seal via the Lambda "
                "MicroVMs setup); the standalone `verify` command reports this check unresolved by design"
            )
            return [cls._unresolved("git-channel-absence", datapoint, _NO_RUNTIME, guidance)]
        report = Seal.check_repo(materialized_seal, canaries=(_CONTROL_TOKEN,))
        return [cls._answer_present(failure, datapoint, "the sealed git tree") for failure in report.failures]

    # ───────────────────────────── refusal constructors ─────────────────────────────

    @staticmethod
    def _answer_present(failure: Finding, datapoint: str, where: str) -> VerifyRefusal:
        """One seal failure (a git channel or a content hit) rendered as an `answer-present` refusal."""
        return VerifyRefusal(
            check="answer-present",
            datapoint=datapoint,
            offending=f"{failure.check} in {where}: {failure.detail}",
            next_="remove the leaked reference (or re-run capture); do not --override a real answer leak",
        )

    @staticmethod
    def _fail(check: str, datapoint: str, offending: str, next_: str) -> VerifyRefusal:
        """A hard failure (exit 4) in the four-field shape."""
        return VerifyRefusal(check=check, datapoint=datapoint, offending=offending, next_=next_)

    @staticmethod
    def _unresolved(check: str, datapoint: str, offending: str, next_: str) -> VerifyRefusal:
        """An unresolved check (exit 5) — never a pass — in the four-field shape."""
        return VerifyRefusal(check=check, datapoint=datapoint, offending=offending, next_=next_, unresolved=True)
