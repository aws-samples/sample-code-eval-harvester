"""The candidate file: mechanical facts written by ``capture``, judgment slots left for the agent.

The candidate is the hand-off artefact at the centre of the design (ADR-3). ``capture`` writes it
full of *mechanical facts* — the PR's review iterations, inline comments, verdicts, and the
structural half of its change-risk — with every *judgment slot blank*. The driving agent fills the
slots (classifies each comment, states findings with severities, classifies the change risk, pins a
rubric version); ``emit``/``verify`` read both halves back. Only ``capture`` writes facts; only the
agent writes judgments (FR-9). This module owns the candidate's schema, the scaffold that turns a
:class:`~eval_harvest.forge.PullRequestFacts` bundle into the facts-filled/slots-blank dict, the
byte-stable JSON round-trip, and the two never-raising validators the later verbs call back.

**No pydantic, no third-party validator (ADR-2, NFR-5).** The shape is a plain :class:`TypedDict`
and the validators are explicit functions returning ``list[Violation]`` — the same never-raise
validator shape ``harbor.py``'s ``validate_task_config`` uses, so ``emit``/``verify`` can aggregate
every violation into the FR-2 four-field refusal rather than dying on the first.

**Determinism (FR-10, NFR-1).** Every timestamp in the candidate comes from the forge/commit data,
never the wall clock. The fields are built in a fixed order and serialized with ``sort_keys=False``,
so two ``capture`` runs over the same inputs are byte-identical. ``patch_path`` is a deterministic
function of the PR number and iteration index (:func:`patch_relpath`) — ``capture`` writes the diff
bytes to exactly that path so ``emit`` can assemble ``change.patch`` with no git call (NFR-2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypedDict

from eval_harvest.diffspan import location_in_spans, new_side_spans
from eval_harvest.forge import PullRequestFacts, ReviewComment, ReviewIteration, ReviewVerdict

#: The finding/change-risk severity taxonomy. Kept equal to ``riskmap.RISK_LEVELS`` on purpose —
#: §8 uses the same {low, medium, high} for finding severities and change risk — but restated here
#: so the forge workstream does not depend on the artefacts module to validate its own file.
SEVERITY_LEVELS: Final = ("low", "medium", "high")


class IterationDict(TypedDict):
    """One review iteration's facts: its tip, the base its diff is against, that diff's on-disk
    patch, and the ids of the comments written against it. ``base_sha`` and ``patch_path`` are empty
    for an unrecoverable iteration — a diff is never fabricated for a state git cannot reach (FR-8)."""

    tip_sha: str
    base_sha: str
    patch_path: str
    comment_ids: list[int]


class VerdictDict(TypedDict):
    """One submitted review verdict: state, author, their repo role, and when (FR-7)."""

    state: str
    author: str
    author_role: str
    submitted_at: str


class CommentDict(TypedDict):
    """One inline comment: the six mechanical fields (FR-12) plus the agent's classification slots.

    ``iteration_index`` and ``in_diff_iterations`` answer two different questions and are both facts.
    ``iteration_index`` is chronological — the iteration the comment was *written* against. ``in_diff_
    iterations`` is geometric — every recoverable iteration whose diff actually covers the line range
    the comment points at, which is what decides whether a finding built from that comment can be
    graded (FR-34). They frequently differ, so they are recorded as separate fields rather than conflated.

    ``classification`` and ``classification_rationale`` are the agent's (defect|nit|question|
    approval|bot with its reasoning, FR-13); ``capture`` leaves them empty.
    """

    id: int
    body: str
    path: str
    line_start: int
    line_end: int
    author_role: str
    created_at: str
    iteration_index: int
    in_diff_iterations: list[int]
    classification: str
    classification_rationale: str


class FindingDict(TypedDict):
    """One finding the agent states: the comments it groups, the defect statement, its severity with
    the recorded basis, and whether it is a reference-only late comment (FR-14, FR-15, FR-16). Every
    field is the agent's — ``capture`` seeds no findings."""

    comment_ids: list[int]
    statement: str
    severity: str
    severity_evidence: str
    severity_rationale: str
    reference_only: bool


class ChangeRiskDict(TypedDict):
    """The change's two-part risk. ``risk_structural`` + ``risk_structural_rule`` are CLI-computed
    from the path rules (FR-20); ``risk_classified`` + its rationale are the agent's (FR-19)."""

    risk_structural: str
    risk_structural_rule: str
    risk_classified: str
    risk_classified_rationale: str


class CandidateDict(TypedDict):
    """The whole candidate file (§8): facts filled by ``capture``, judgment slots blank for the agent."""

    repo: str
    pr_number: int
    pr_url: str
    iterations: list[IterationDict]
    review_verdicts: list[VerdictDict]
    comments: list[CommentDict]
    findings: list[FindingDict]
    change_risk: ChangeRiskDict
    rubric_version: str


@dataclass(frozen=True, slots=True)
class Violation:
    """One way a candidate is malformed or incoherent: a stable code, a pointer, and why.

    A structural mirror of ``harbor.py``'s ``SchemaViolation`` (restated here, not imported, so the
    forge workstream stays self-contained). The validators return a *list* of these and never raise,
    so ``emit``/``verify`` report every problem at once (ADR-2)."""

    code: str
    pointer: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code} at {self.pointer}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Emittability:
    """Which iterations ``capture`` recovered, and — per selectable kind — the iteration ``emit`` will
    build from and the comments whose diff span it covers.

    Computed once by :meth:`Candidate.emittability` from the candidate's own recorded facts: an
    iteration's ``patch_path`` *is* the recoverability answer (set only when recoverable, FR-8), and a
    comment's ``in_diff_iterations`` *is* the coverage answer. So the three consumers — ``capture``'s
    report and refusal, ``brief``, and ``emit``'s binding — cannot disagree about which
    iteration a kind uses. The selection mirrors :meth:`Emit._select_iteration`: reject is the first
    recoverable iteration, approve the last (S-1). ``reject_index`` and ``approve_index`` are ``None``
    exactly when no iteration is recoverable — the one case ``capture`` refuses outright (FR-11)."""

    recoverable_indices: list[int]
    reject_index: int | None
    approve_index: int | None
    reject_in_diff_comment_ids: list[int]
    approve_in_diff_comment_ids: list[int]

    def as_json(self) -> dict[str, Any]:
        """The machine-readable shape ``capture --json`` and other consumers branch on.

        Each kind is an object naming its iteration and the in-diff comment ids, or ``null`` when no
        iteration is recoverable — so a driver can decide without parsing prose."""
        return {
            "recoverable_indices": self.recoverable_indices,
            "reject": self._kind_json(self.reject_index, self.reject_in_diff_comment_ids),
            "approve": self._kind_json(self.approve_index, self.approve_in_diff_comment_ids),
        }

    @staticmethod
    def _kind_json(iteration_index: int | None, in_diff_comment_ids: list[int]) -> dict[str, Any] | None:
        """One kind's payload — its iteration and covered comment ids — or ``null`` when it has no iteration."""
        if iteration_index is None:
            return None
        return {"iteration": iteration_index, "in_diff_comment_ids": in_diff_comment_ids}


def patch_relpath(pr_number: int, iteration_index: int) -> str:
    """The dataset-relative path a recoverable iteration's diff is materialized to.

    A pure function of the PR number and iteration index so ``scaffold`` (which records the field)
    and ``capture`` (which writes the bytes) agree without passing paths between them, and so two
    runs produce the identical string (FR-10).
    """
    return f"patches/pr-{pr_number}-iter{iteration_index}.patch"


def _newline_terminated(diff: str) -> bytes:
    """The diff's UTF-8 bytes with a trailing newline, appended only when one is missing.

    A patch is a line-oriented format and ``git apply`` refuses a hunk whose final line is
    unterminated (``corrupt patch at line <last>``). The diff arrives stripped: ``GitCommandRunner``
    returns stripped stdout for every call it makes, diff and non-diff alike, so the newline is
    restored here at the write site rather than by loosening that shared boundary — and it is restored
    *here* rather than in whatever consumes the file, because a checker that repairs its input cannot
    notice this going wrong.

    Appending only when absent keeps ``capture`` a byte-identical function of its inputs (NFR-1): the
    stored tail follows the diff, not the route the diff took to reach us.
    """
    patch_bytes = diff.encode("utf-8")
    if patch_bytes and not patch_bytes.endswith(b"\n"):
        patch_bytes += b"\n"
    return patch_bytes


#: The ``diff --git a/<old> b/<new>`` header git writes once per file in a unified diff. Group 1 is
#: the b-side path — where the file lives after the change, which is what a risk-map prefix matches.
_DIFF_GIT_HEADER = re.compile(r"^diff --git a/.* b/(.*)$")


def _paths_from_diff(diff: str) -> list[str]:
    """The file paths a unified diff touches, read from its ``diff --git`` headers, in file order.

    Git emits one ``diff --git a/… b/…`` header per file; the b-side is taken because it names the
    path after the change (a rename's new name, a normal edit's own path) — the path a rule matches.
    A blank diff (an unrecoverable or no-op iteration) yields nothing."""
    return [match.group(1) for line in diff.splitlines() if (match := _DIFF_GIT_HEADER.match(line))]


def _spans_by_iteration(facts: PullRequestFacts) -> dict[int, dict[str, list[tuple[int, int]]]]:
    """The new-side hunk spans of every *recoverable* iteration's diff, keyed by iteration index.

    Unrecoverable iterations are skipped explicitly rather than left to fall out of an empty diff:
    their ``diff`` is ``""`` by construction (FR-8) and ``emit`` cannot assemble a change patch from
    one, so a comment must never be reported as living in it even if the geometry would allow it.
    """
    spans: dict[int, dict[str, list[tuple[int, int]]]] = {}
    for index, iteration in enumerate(facts.iterations):
        if not iteration.recoverable:
            continue
        spans[index] = new_side_spans(iteration.diff)
    return spans


def _in_diff_iterations(comment: ReviewComment, spans_by_iteration: dict[int, dict[str, list[tuple[int, int]]]]) -> list[int]:
    """The recoverable iterations whose diff covers this comment's line range, as a sorted list.

    Sorted rather than taken in dict order so ``capture``'s bytes do not depend on iteration order
    (FR-10) — the same reason :meth:`Candidate.changed_paths` sorts. An empty list is a legitimate
    answer: a comment can point at a line no recoverable diff touches (an outdated or file-level
    comment), which is a fact about the PR and not a defect in the candidate.
    """
    return sorted(
        index
        for index, spans in spans_by_iteration.items()
        if location_in_spans(comment.path, comment.line_start, comment.line_end, spans)
    )


#: What each classification value is for, printed under the ``classification`` slot. The five values
#: are the taxonomy ``_check_one_comment_classification`` validates against (FR-13); ``bot`` is
#: otherwise learnable only from a validation error, which is the whole reason this text exists.
CLASSIFICATION_GUIDE: Final = (
    "defect: a substantive problem a reviewer would block on   nit: style or preference, does not block",
    "question: asks for information rather than asserting      approval: a sign-off, carries no finding",
    "bot: an automated comment (linter, coverage, dep bot) — not a human review point, never a finding",
)

#: The ``findings[]`` contract as ``capture`` prints it: one line per field of :class:`FindingDict`, in
#: schema order. The agent reads stdout before it reads a man page, so without this list it would have
#: to read this module to learn the shape. Kept here, next to
#: the TypedDict it describes, so the two are edited together; ``man/eval-harvest-capture.md`` carries
#: the same field list and ``tests/test_help_methodology.py`` pins both against
#: ``FindingDict.__annotations__``.
FINDING_FIELD_GUIDE: Final = (
    "comment_ids: [int]        which comment(s) this finding is drawn from",
    "statement: str            what is wrong, in the reviewer's terms",
    "severity: low|medium|high",
    "severity_evidence: str    concrete evidence (path:line, output) — required unless reference_only",
    "severity_rationale: str   which rubric rule assigns this severity",
    "reference_only: bool      kept for coverage, not required to block; still needs a location in the diff",
)

#: The whole fill contract — every judgment slot the agent owns, as lines of stdout — in one place so
#: ``capture``'s :meth:`Candidate.slot_summary` and ``brief``'s "you fill" section render the *same*
#: text and cannot drift into two descriptions of one schema. It composes the classification
#: taxonomy (:data:`CLASSIFICATION_GUIDE`) and the ``findings[]`` shape (:data:`FINDING_FIELD_GUIDE`,
#: which already carries the ``reference_only`` rule) with the change-risk and rubric-version slots.
FILL_CONTRACT_GUIDE: Final = (
    "comments[].classification — classify each comment (defect|nit|question|approval|bot):",
    *(f"    {line}" for line in CLASSIFICATION_GUIDE),
    "findings[] — one per substantive review point:",
    *(f"    {line}" for line in FINDING_FIELD_GUIDE),
    "change_risk.risk_classified — classify the change's risk (structural half already computed)",
    "rubric_version — pin the rubric version this datapoint was built against",
)


class Candidate:
    """Build, serialize, load, and validate the candidate file. Holds no state."""

    # ───────────────────────────── scaffold (facts in, slots blank) ─────────────────────────────

    # scaffold needs repo and pr_url in addition to the forge bundle because §8 requires them and
    # they are mechanical facts the PullRequestFacts bundle does not carry; PLR0913 is the contract.
    @classmethod
    def scaffold(  # noqa: PLR0913
        cls,
        facts: PullRequestFacts,
        *,
        repo: str,
        pr_url: str,
        risk_structural: str,
        risk_structural_rule: str,
    ) -> CandidateDict:
        """Turn a forge fact bundle plus the structural-risk result into a candidate dict.

        Every mechanical fact is filled — iterations (with the deterministic ``patch_path`` a
        recoverable diff will be written to), verdicts, the six per-comment fields, and the
        CLI-computed structural risk. Every judgment slot is *present and empty* — never omitted —
        so the agent sees exactly what to fill: each comment's ``classification`` slots, an empty
        ``findings`` list, ``risk_classified`` and its rationale, and ``rubric_version`` (FR-9).

        The per-iteration hunk spans are parsed *once* for the whole PR and handed to the per-comment
        builder, so a PR with many comments does not re-parse every diff per comment.
        """
        spans_by_iteration = _spans_by_iteration(facts)
        return CandidateDict(
            repo=repo,
            pr_number=facts.pr_number,
            pr_url=pr_url,
            iterations=[cls._iteration_dict(facts.pr_number, index, it) for index, it in enumerate(facts.iterations)],
            review_verdicts=[cls._verdict_dict(verdict) for verdict in facts.review_verdicts],
            comments=[cls._comment_dict(comment, _in_diff_iterations(comment, spans_by_iteration)) for comment in facts.comments],
            findings=[],
            change_risk=ChangeRiskDict(
                risk_structural=risk_structural,
                risk_structural_rule=risk_structural_rule,
                risk_classified="",
                risk_classified_rationale="",
            ),
            rubric_version="",
        )

    @staticmethod
    def _iteration_dict(pr_number: int, index: int, iteration: ReviewIteration) -> IterationDict:
        """One :class:`ReviewIteration` as facts; ``patch_path`` is set only when it is recoverable."""
        patch_path = patch_relpath(pr_number, index) if iteration.recoverable else ""
        return IterationDict(
            tip_sha=iteration.tip_sha,
            base_sha=iteration.base_sha,
            patch_path=patch_path,
            comment_ids=list(iteration.comment_ids),
        )

    @staticmethod
    def _verdict_dict(verdict: ReviewVerdict) -> VerdictDict:
        return VerdictDict(
            state=verdict.state,
            author=verdict.author,
            author_role=verdict.author_role,
            submitted_at=verdict.submitted_at,
        )

    @staticmethod
    def _comment_dict(comment: ReviewComment, in_diff_iterations: list[int]) -> CommentDict:
        """One inline comment's facts, with the classification slots present and empty (FR-9, FR-13).

        ``in_diff_iterations`` is computed once per PR by :func:`_in_diff_iterations` and passed in;
        this builder recomputes nothing and reads no files, so it stays a pure field mapping.
        """
        return CommentDict(
            id=comment.id,
            body=comment.body,
            path=comment.path,
            line_start=comment.line_start,
            line_end=comment.line_end,
            author_role=comment.author_role,
            created_at=comment.created_at,
            iteration_index=comment.iteration_index,
            in_diff_iterations=in_diff_iterations,
            classification="",
            classification_rationale="",
        )

    # ───────────────────────────── changed paths (feeds structural risk) ─────────────────────────────

    @staticmethod
    def changed_paths(facts: PullRequestFacts) -> list[str]:
        """The distinct files the PR touched, read from the recoverable iteration diffs, sorted.

        The structural-risk half is highest-wins across every path the change touches (US-4), so the
        union across iterations is the safe input. Paths are read from each diff's ``diff --git a/… b/…``
        headers (the b-side — where the file lives now, which is what a path rule matches), needing no
        extra git call. Sorted so the input to :meth:`RiskMap.structural_risk` — and thus the recorded
        rule — is deterministic (FR-10)."""
        paths: set[str] = set()
        for iteration in facts.iterations:
            paths.update(_paths_from_diff(iteration.diff))
        return sorted(paths)

    # ───────────────────────────── the capture write (facts to disk) ─────────────────────────────

    # write_to_dataset takes the identity, dataset root, and the pre-computed structural risk on top
    # of the fact bundle because each is a distinct input the schema requires; PLR0913 is the contract.
    @classmethod
    def write_to_dataset(  # noqa: PLR0913
        cls,
        facts: PullRequestFacts,
        *,
        repo: str,
        pr_url: str,
        dataset_dir: Path,
        risk_structural: str,
        risk_structural_rule: str,
    ) -> Path:
        """Materialize each recoverable iteration's diff and write ``candidates/pr-<n>.json``.

        The offline core of ``capture``: it makes no network or git call — the diffs are already in
        ``facts`` (B-2 fetched them). Each recoverable iteration's diff is written to the same
        deterministic ``patches/pr-<n>-iter<i>.patch`` path :meth:`scaffold` records, so ``emit`` can
        assemble ``change.patch`` with no git (NFR-2). Returns the candidate file's path. Two runs
        over the same facts produce byte-identical files (FR-10)."""
        cls._materialize_patches(facts, dataset_dir)
        candidate = cls.scaffold(
            facts, repo=repo, pr_url=pr_url, risk_structural=risk_structural, risk_structural_rule=risk_structural_rule
        )
        candidate_path = dataset_dir / "candidates" / f"pr-{facts.pr_number}.json"
        cls.dump(candidate, candidate_path)
        return candidate_path

    @staticmethod
    def _materialize_patches(facts: PullRequestFacts, dataset_dir: Path) -> None:
        """Write every recoverable iteration's diff bytes to its deterministic patch path."""
        for index, iteration in enumerate(facts.iterations):
            if not iteration.recoverable:
                continue
            patch_path = dataset_dir / patch_relpath(facts.pr_number, index)
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_bytes(_newline_terminated(iteration.diff))

    # ───────────────────────────── slots the agent must fill ─────────────────────────────

    @classmethod
    def slot_summary(cls, candidate: CandidateDict) -> list[str]:
        """A human-readable list of the judgment slots ``capture`` left blank, for stdout (FR-9).

        Names what the agent has to fill so the hand-off is explicit: the comments to classify, that
        findings must be stated, and the change-risk and rubric-version slots. The slot lines come
        from the shared :data:`FILL_CONTRACT_GUIDE`, so ``capture`` and ``brief`` describe the fill
        contract identically. Which comments each selectable kind can carry a finding for is
        reported *separately* by :meth:`emittable_report`, computed once from :meth:`emittability`
        — kept out of this list so the same fact is not printed twice under two headings."""
        comment_count = len(candidate["comments"])
        return [f"you fill ({comment_count} comment(s) to classify):", *FILL_CONTRACT_GUIDE]

    # ───────────────────────────── emittability (facts capture reports and refuses on) ─────────────────────────────

    @classmethod
    def emittability(cls, candidate: CandidateDict) -> Emittability:
        """Which iterations are recoverable and, per kind, the iteration it builds from and covered comments.

        Reads only recorded facts — ``patch_path`` for recoverability (FR-8), ``in_diff_iterations`` for
        coverage — so it makes no git call and stays offline, as ``capture``'s post-write path must
        (NFR-2). The selection matches :meth:`Emit._select_iteration` (reject = first recoverable, approve
        = last); the indices are ``None`` when nothing is recoverable, which is what ``capture`` refuses on."""
        recoverable = [index for index, iteration in enumerate(candidate["iterations"]) if iteration["patch_path"]]
        reject_index = recoverable[0] if recoverable else None
        approve_index = recoverable[-1] if recoverable else None
        return Emittability(
            recoverable_indices=recoverable,
            reject_index=reject_index,
            approve_index=approve_index,
            reject_in_diff_comment_ids=cls._comment_ids_in_diff(candidate, reject_index),
            approve_in_diff_comment_ids=cls._comment_ids_in_diff(candidate, approve_index),
        )

    @staticmethod
    def _comment_ids_in_diff(candidate: CandidateDict, iteration_index: int | None) -> list[int]:
        """The ids of the comments whose ``in_diff_iterations`` names ``iteration_index``, in candidate order.

        Empty when the kind has no iteration (``iteration_index is None``); candidate order is
        deterministic (FR-10), so no sort is needed to keep the bytes stable."""
        if iteration_index is None:
            return []
        return [comment["id"] for comment in candidate["comments"] if iteration_index in comment.get("in_diff_iterations", [])]

    #: The width the kind label is padded to in the emittable report, so ``reject`` and ``approve`` (and
    #: any zero-coverage warning under them) line up in a column. The warning is indented by the same
    #: amount so it sits under the count it qualifies.
    _EMITTABLE_KIND_WIDTH: Final = 9

    #: The warning printed under a kind whose selected iteration covers no comment: a finding built on
    #: one of those comments points at a line the emitted diff never touches, which ``verify`` refuses
    #: with ``finding-line-absent``. A warning, not a refusal — the *other* kind may still be buildable.
    _EMITTABLE_ZERO_WARNING: Final = "warning: a finding built on any of these comments will fail verify's finding-line-absent"

    @classmethod
    def emittable_report(cls, candidate: CandidateDict, emittability: Emittability) -> list[str]:
        """One line per selectable kind — its iteration and in-diff comment count — for ``capture`` stdout.

        A kind whose count is zero gets a second, indented warning line (:data:`_EMITTABLE_ZERO_WARNING`):
        that kind is structurally emittable but no finding built on a comment can pass ``verify``'s
        ``finding-line-absent``. Called only when at least one iteration is recoverable — the empty case
        is a refusal, not a report — so both indices are set."""
        comment_count = len(candidate["comments"])
        kinds = (
            ("reject", emittability.reject_index, emittability.reject_in_diff_comment_ids),
            ("approve", emittability.approve_index, emittability.approve_in_diff_comment_ids),
        )
        return [line for kind, index, ids in kinds for line in cls._emittable_kind_lines(kind, index, ids, comment_count)]

    @classmethod
    def _emittable_kind_lines(
        cls, kind: str, iteration_index: int | None, in_diff_comment_ids: list[int], comment_count: int
    ) -> list[str]:
        """The report line for one kind, plus a warning line when its iteration covers no comment."""
        if iteration_index is None:
            return []
        inside = len(in_diff_comment_ids)
        label = kind.ljust(cls._EMITTABLE_KIND_WIDTH)
        line = f"{label}iteration {iteration_index} — {inside} of {comment_count} comment(s) inside its diff"
        if inside == 0:
            return [line, f"{' ' * cls._EMITTABLE_KIND_WIDTH}{cls._EMITTABLE_ZERO_WARNING}"]
        return [line]

    # ───────────────────────────── byte-stable JSON round-trip ─────────────────────────────

    @staticmethod
    def dumps(candidate: CandidateDict) -> bytes:
        """The candidate's canonical bytes: UTF-8, two-space indent, insertion order, LF-terminated.

        ``sort_keys=False`` keeps the §8 field order (``scaffold`` builds it in a fixed order);
        ``ensure_ascii=False`` keeps non-ASCII review text as UTF-8 rather than ``\\uXXXX``; the
        trailing newline makes the file a well-formed text line. Encoded to bytes with an explicit LF
        so the output is byte-identical on every platform (FR-10, NFR-1)."""
        text = json.dumps(candidate, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
        return text.encode("utf-8")

    @classmethod
    def dump(cls, candidate: CandidateDict, path: Path) -> None:
        """Write the candidate to ``path`` as canonical bytes (see :meth:`dumps`), creating parents."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cls.dumps(candidate))

    @staticmethod
    def load(path: Path) -> CandidateDict:
        """Read a candidate file back into its dict form (stdlib ``json``, no validation here)."""
        loaded: CandidateDict = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    # ───────────────────────────── validation (never raises) ─────────────────────────────

    @classmethod
    def validate_facts(cls, candidate: CandidateDict) -> list[Violation]:
        """Every way the CLI-written *facts* half is malformed, as a list (used right after scaffold).

        Checks the mechanical fields ``capture`` is responsible for — identity, per-iteration facts,
        the six per-comment fields, and the structural-risk pair — not the agent's slots. Returns all
        problems at once and never raises (ADR-2)."""
        violations: list[Violation] = []
        violations += cls._check_identity(candidate)
        violations += cls._check_iterations(candidate)
        violations += cls._check_comment_facts(candidate)
        violations += cls._check_structural_risk(candidate)
        return violations

    @classmethod
    def validate_filled(cls, candidate: CandidateDict) -> list[Violation]:
        """Every unfilled or incoherent judgment slot, as a list (the checks ``emit``/``verify`` call).

        Enforces the §8 coherence constraints on the agent's half: every comment classified (FR-13);
        every non-``reference_only`` finding carrying a real severity with recorded evidence and
        rationale (FR-15); the change risk classified (FR-20); a rubric version pinned (FR-27). A
        ``reference_only`` finding is exempt from the severity-evidence gate (FR-16). Returns all
        problems at once and never raises."""
        violations: list[Violation] = []
        violations += cls._check_classifications(candidate)
        violations += cls._check_findings(candidate)
        violations += cls._check_classified_risk(candidate)
        violations += cls._check_rubric_version(candidate)
        return violations

    # ── facts checks ──

    @staticmethod
    def _check_identity(candidate: CandidateDict) -> list[Violation]:
        violations: list[Violation] = []
        if not candidate.get("repo"):
            violations.append(Violation("candidate.facts-missing", "repo", "`repo` is required (owner/name)"))
        if not isinstance(candidate.get("pr_number"), int) or candidate.get("pr_number", 0) <= 0:
            violations.append(Violation("candidate.facts-missing", "pr_number", "`pr_number` must be a positive integer"))
        if not candidate.get("pr_url"):
            violations.append(Violation("candidate.facts-missing", "pr_url", "`pr_url` is required"))
        return violations

    @classmethod
    def _check_iterations(cls, candidate: CandidateDict) -> list[Violation]:
        violations: list[Violation] = []
        for index, iteration in enumerate(candidate.get("iterations", [])):
            if not iteration.get("tip_sha"):
                violations.append(
                    Violation("candidate.facts-missing", f"iterations[{index}].tip_sha", "iteration tip is required")
                )
            recoverable = bool(iteration.get("base_sha"))
            if recoverable and not iteration.get("patch_path"):
                violations.append(
                    Violation(
                        "candidate.facts-missing",
                        f"iterations[{index}].patch_path",
                        "a recoverable iteration (base present) must record the materialized patch path",
                    )
                )
        return violations

    @classmethod
    def _check_comment_facts(cls, candidate: CandidateDict) -> list[Violation]:
        """Each comment carries the six mechanical fields bound to a real iteration (FR-12)."""
        iteration_count = len(candidate.get("iterations", []))
        violations: list[Violation] = []
        for comment in candidate.get("comments", []):
            pointer = f"comments[id={comment.get('id')}]"
            if not comment.get("path"):
                violations.append(Violation("candidate.facts-missing", f"{pointer}.path", "comment must carry its file path"))
            if not comment.get("created_at"):
                violations.append(
                    Violation("candidate.facts-missing", f"{pointer}.created_at", "comment must carry its timestamp")
                )
            iteration_index = comment.get("iteration_index", -1)
            if not (0 <= iteration_index < iteration_count):
                violations.append(
                    Violation(
                        "candidate.comment-unbound",
                        f"{pointer}.iteration_index",
                        f"comment binds to iteration {iteration_index}, outside the {iteration_count} iterations (FR-12)",
                    )
                )
            violations += cls._check_in_diff_iterations(comment, pointer, iteration_count)
        return violations

    @staticmethod
    def _check_in_diff_iterations(comment: CommentDict, pointer: str, iteration_count: int) -> list[Violation]:
        """``in_diff_iterations`` is present and every entry indexes a real iteration.

        An *empty* list is valid — a comment can legitimately point at a line no recoverable diff
        covers. An *absent* field is not: the candidate is missing a fact ``capture`` must record, and
        the fix is to re-run ``capture`` (cheap and deterministic, FR-10) rather than to guess the binding here.
        """
        in_diff = comment.get("in_diff_iterations")
        if in_diff is None:
            return [
                Violation(
                    "candidate.facts-missing",
                    f"{pointer}.in_diff_iterations",
                    "comment must record which iterations' diffs cover its line range; re-run `capture`",
                )
            ]
        out_of_range = [index for index in in_diff if not (0 <= index < iteration_count)]
        if out_of_range:
            return [
                Violation(
                    "candidate.facts-invalid",
                    f"{pointer}.in_diff_iterations",
                    f"names iteration(s) {out_of_range}, outside the {iteration_count} recorded iterations",
                )
            ]
        return []

    @classmethod
    def _check_structural_risk(cls, candidate: CandidateDict) -> list[Violation]:
        change_risk = candidate.get("change_risk", {})
        violations: list[Violation] = []
        structural = change_risk.get("risk_structural")
        if structural not in SEVERITY_LEVELS:
            violations.append(
                Violation(
                    "candidate.facts-invalid",
                    "change_risk.risk_structural",
                    f"structural risk must be one of {list(SEVERITY_LEVELS)}, not {structural!r}",
                )
            )
        if not change_risk.get("risk_structural_rule"):
            violations.append(
                Violation(
                    "candidate.facts-missing",
                    "change_risk.risk_structural_rule",
                    "the matched path rule must be recorded for the audit",
                )
            )
        return violations

    # ── slot-coherence checks ──

    @staticmethod
    def _check_classifications(candidate: CandidateDict) -> list[Violation]:
        violations: list[Violation] = []
        for comment in candidate.get("comments", []):
            if not comment.get("classification"):
                violations.append(
                    Violation(
                        "candidate.unclassified-comment",
                        f"comments[id={comment.get('id')}].classification",
                        "every comment must be classified (defect|nit|question|approval|bot) before emit (FR-13)",
                    )
                )
        return violations

    @classmethod
    def _check_findings(cls, candidate: CandidateDict) -> list[Violation]:
        violations: list[Violation] = []
        for index, finding in enumerate(candidate.get("findings", [])):
            if finding.get("reference_only"):
                continue
            violations += cls._check_one_finding(index, finding)
        return violations

    @staticmethod
    def _check_one_finding(index: int, finding: FindingDict) -> list[Violation]:
        """A non-``reference_only`` finding must carry a real severity with recorded evidence (FR-15)."""
        violations: list[Violation] = []
        pointer = f"findings[{index}]"
        if finding.get("severity") not in SEVERITY_LEVELS:
            violations.append(
                Violation(
                    "candidate.finding-severity-invalid",
                    f"{pointer}.severity",
                    f"severity must be one of {list(SEVERITY_LEVELS)}, not {finding.get('severity')!r} (FR-15)",
                )
            )
        if not finding.get("severity_evidence"):
            violations.append(
                Violation(
                    "candidate.finding-missing-evidence",
                    f"{pointer}.severity_evidence",
                    "severity asserted with no evidence (FR-15)",
                )
            )
        if not finding.get("severity_rationale"):
            violations.append(
                Violation(
                    "candidate.finding-missing-evidence",
                    f"{pointer}.severity_rationale",
                    "severity asserted with no rationale (FR-15)",
                )
            )
        return violations

    @staticmethod
    def _check_classified_risk(candidate: CandidateDict) -> list[Violation]:
        if not candidate.get("change_risk", {}).get("risk_classified"):
            return [
                Violation(
                    "candidate.risk-unclassified",
                    "change_risk.risk_classified",
                    "the agent must classify the change risk (FR-20)",
                )
            ]
        return []

    @staticmethod
    def _check_rubric_version(candidate: CandidateDict) -> list[Violation]:
        if not candidate.get("rubric_version"):
            return [
                Violation(
                    "candidate.rubric-unpinned", "rubric_version", "the datapoint must pin the rubric version it used (FR-27)"
                )
            ]
        return []
