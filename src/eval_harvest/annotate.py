"""The `annotate` verb's core: parse per-comment fill records and merge them into a candidate.

The candidate is both what the agent reads to decide and what it writes the decision back into.
Without record-level merging, every fill and every correction is a whole-file rewrite — tens of KB
read, judged, and written back per PR, and again after each `emit` refusal. `annotate`
takes the judgments as a stream of small records (one comment's classification, one finding, the
change risk) and merges each into the candidate, so the agent never reproduces the bytes it is not
changing (tech plan §5.3).

The merge is a *pure* function (:meth:`Annotate.apply_records`): a candidate dict in, a new candidate
dict plus every :class:`~eval_harvest.candidate.Violation` out — no filesystem access, so the
abort-on-violation rule the CLI enforces is one branch rather than a cleanup path, and the whole
thing is trivially testable. Only the CLI's ``handle_annotate`` reads stdin and writes the file, and
it writes through :meth:`~eval_harvest.candidate.Candidate.dump` so the bytes stay byte-identical to
what ``capture`` would produce for the same content (FR-10).

**The CLI never writes a judgment (FR-9).** ``annotate`` records exactly what the agent said and
infers nothing: it does not guess a severity, default ``reference_only``, or fill ``rubric_version``.
A finding record that omits a judgment is a violation the agent must resolve, not a slot the CLI
fills for it.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Final

from eval_harvest.candidate import CandidateDict, CommentDict, FindingDict, Violation


class AnnotateParseError(Exception):
    """A record stream that is not valid JSON, carrying the 1-based line the parse failed on.

    Distinct from a :class:`~eval_harvest.candidate.Violation`: a syntactically broken stream yields
    no records at all, so it is reported before the merge rather than aggregated with the coherence
    violations. ``handle_annotate`` turns it into the FR-2 ``malformed-record`` refusal.
    """

    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line
        self.message = message


@dataclass(frozen=True, slots=True)
class AnnotateStats:
    """What a merge did, for the run's stdout: comments classified, ids overwritten, findings added."""

    classified: int
    overwritten: list[int]
    findings_added: int


class Annotate:
    """Parse fill records and merge them into a candidate. Holds no state."""

    #: The discriminators that select a record's kind. A record carrying none of them is unrecognised
    #: and refuses (never a silent skip): a dropped judgment is worse than a loud refusal.
    _COMMENT_KEY: Final = "comment"
    _FINDING_KEY: Final = "finding"
    _RISK_KEY: Final = "risk_classified"
    _RUBRIC_KEY: Final = "rubric_version"

    # ───────────────────────────── parse (one place for both input forms) ─────────────────────────────

    @classmethod
    def parse_records(cls, text: str, *, jsonl: bool) -> list[dict[str, object]]:
        """Parse the record stream: JSONL (one object per line) when ``jsonl``, else a JSON array.

        A malformed record raises :class:`AnnotateParseError` naming its 1-based line so a broken
        record in a long stream is locatable, rather than a bare ``json`` traceback.
        """
        if jsonl:
            return cls._parse_jsonl(text)
        return cls._parse_json_array(text)

    @staticmethod
    def _parse_jsonl(text: str) -> list[dict[str, object]]:
        """One JSON object per non-blank line; blank lines are skipped without shifting line numbers."""
        records: list[dict[str, object]] = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise AnnotateParseError(line_number, f"line {line_number} is not valid JSON: {error.msg}") from error
            if not isinstance(record, dict):
                raise AnnotateParseError(line_number, f"line {line_number} is not a JSON object")
            records.append(record)
        return records

    @staticmethod
    def _parse_json_array(text: str) -> list[dict[str, object]]:
        """A single JSON array of objects; the element index doubles as the reported line number."""
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as error:
            raise AnnotateParseError(error.lineno, f"line {error.lineno} is not valid JSON: {error.msg}") from error
        if not isinstance(loaded, list):
            raise AnnotateParseError(1, "the --from-json file must contain a JSON array of records")
        for index, record in enumerate(loaded, start=1):
            if not isinstance(record, dict):
                raise AnnotateParseError(index, f"record {index} is not a JSON object")
        records: list[dict[str, object]] = loaded
        return records

    # ───────────────────────────── merge (pure: dict in, new dict + violations out) ─────────────────────────────

    @classmethod
    def apply_records(
        cls, candidate: CandidateDict, records: list[dict[str, object]], *, replace_findings: bool = False
    ) -> tuple[CandidateDict, list[Violation]]:
        """Merge ``records`` into a *copy* of ``candidate``; return the copy and every apply-time violation.

        Pure: the input candidate is never mutated (a deep copy is merged), so the caller can compare
        the before and after, and abort the write on any violation without a rollback. The returned
        violations are the ones only ``annotate`` can see — an unknown comment id, an unrecognised
        record shape, a finding that omits ``reference_only``. Coherence of the *values* (a finding's
        severity and evidence, FR-15) is left to :meth:`Candidate.validate_filled`, which the CLI runs
        on the merged result, so ``annotate`` never carries a second copy of those rules (ADR-2).

        With ``replace_findings`` the finding list is cleared before applying, so a rescoping run can
        drop the previous set in one command instead of hand-editing the file.
        """
        merged = copy.deepcopy(candidate)
        if replace_findings:
            merged["findings"] = []
        comments_by_id: dict[int, CommentDict] = {comment["id"]: comment for comment in merged["comments"]}
        violations: list[Violation] = []
        for position, record in enumerate(records, start=1):
            violations += cls._apply_one_record(merged, comments_by_id, position, record)
        return merged, violations

    @classmethod
    def _apply_one_record(
        cls, merged: CandidateDict, comments_by_id: dict[int, CommentDict], position: int, record: dict[str, object]
    ) -> list[Violation]:
        """Apply one record by its discriminator; an unrecognised shape is a violation, never a skip."""
        if cls._COMMENT_KEY in record:
            return cls._apply_comment_record(comments_by_id, position, record)
        if cls._FINDING_KEY in record:
            return cls._apply_finding_record(merged, position, record)
        if cls._RISK_KEY in record or cls._RUBRIC_KEY in record:
            cls._apply_scalar_record(merged, record)
            return []
        return [
            Violation(
                "annotate.unrecognised-record",
                f"records[{position}]",
                f"record {position} carries none of: {cls._COMMENT_KEY}, {cls._FINDING_KEY}, {cls._RISK_KEY}, {cls._RUBRIC_KEY}",
            )
        ]

    @staticmethod
    def _apply_comment_record(
        comments_by_id: dict[int, CommentDict], position: int, record: dict[str, object]
    ) -> list[Violation]:
        """Set one comment's classification slots from a comment record; an unknown id refuses.

        Mutates the existing comment dict's values in place rather than rebuilding it, so the §8 field
        order is preserved (``sort_keys=False`` makes insertion order the file's order, FR-10).
        """
        comment_id = record[Annotate._COMMENT_KEY]
        comment = comments_by_id.get(comment_id) if isinstance(comment_id, int) else None
        if comment is None:
            return [
                Violation(
                    "annotate.unknown-comment",
                    f"records[{position}].comment",
                    f"no comment with id {comment_id!r} in the candidate",
                )
            ]
        comment["classification"] = str(record.get("classification", ""))
        comment["classification_rationale"] = str(record.get("rationale", ""))
        return []

    @staticmethod
    def _apply_finding_record(merged: CandidateDict, position: int, record: dict[str, object]) -> list[Violation]:
        """Append one finding; ``reference_only`` must be stated explicitly (FR-9 — never defaulted).

        The finding dict is built in the §8 field order so its serialized form matches what ``capture``
        would write. The severity/evidence/rationale rules are *not* checked here: the CLI runs
        :meth:`Candidate.validate_filled` on the merged candidate, which owns FR-15 for every finding.
        """
        finding = record[Annotate._FINDING_KEY]
        if not isinstance(finding, dict):
            return [Violation("annotate.unrecognised-record", f"records[{position}].finding", "finding must be a JSON object")]
        if "reference_only" not in finding:
            return [
                Violation(
                    "annotate.finding-missing-field",
                    f"records[{position}].finding.reference_only",
                    "reference_only must be stated explicitly (true|false); the CLI never defaults a block/no-block call (FR-9)",
                )
            ]
        merged["findings"].append(
            FindingDict(
                comment_ids=list(finding.get("comment_ids", [])),
                statement=str(finding.get("statement", "")),
                severity=str(finding.get("severity", "")),
                severity_evidence=str(finding.get("severity_evidence", "")),
                severity_rationale=str(finding.get("severity_rationale", "")),
                reference_only=bool(finding["reference_only"]),
            )
        )
        return []

    @staticmethod
    def _apply_scalar_record(merged: CandidateDict, record: dict[str, object]) -> None:
        """Set the change-risk classification and/or the rubric version from a scalar record.

        Both live on existing keys (``capture`` seeds them empty), so the values are overwritten in
        place and the field order is untouched. Neither value is inferred: a missing rationale is
        recorded as the empty string, not guessed.
        """
        if Annotate._RISK_KEY in record:
            merged["change_risk"]["risk_classified"] = str(record[Annotate._RISK_KEY])
            merged["change_risk"]["risk_classified_rationale"] = str(record.get("rationale", ""))
        if Annotate._RUBRIC_KEY in record:
            merged["rubric_version"] = str(record[Annotate._RUBRIC_KEY])

    # ───────────────────────────── report helpers (pure) ─────────────────────────────

    @classmethod
    def summarize(cls, original: CandidateDict, records: list[dict[str, object]]) -> AnnotateStats:
        """What a clean merge of ``records`` did, measured against the pre-merge ``original``.

        ``overwritten`` is the ids whose classification was *already* set before this run — reclassifying
        after re-reading a comment is normal, but a silent overwrite the agent cannot see is not, so the
        run reports it. Only reached on a run with no violations, so every comment id is known.
        """
        already_classified = {comment["id"] for comment in original["comments"] if comment["classification"]}
        classified = 0
        overwritten: list[int] = []
        findings_added = 0
        for record in records:
            if cls._COMMENT_KEY in record:
                classified += 1
                comment_id = record[cls._COMMENT_KEY]
                if isinstance(comment_id, int) and comment_id in already_classified:
                    overwritten.append(comment_id)
            elif cls._FINDING_KEY in record:
                findings_added += 1
        return AnnotateStats(classified=classified, overwritten=overwritten, findings_added=findings_added)

    @classmethod
    def remaining_slots(cls, candidate: CandidateDict) -> list[str]:
        """The judgment slots still unfilled after a merge, one compact line each, for stdout.

        Rendered from the candidate itself so a driver can loop ``annotate`` until this is empty. The
        countable required slots only: unclassified comments, the change-risk classification, and the
        rubric version. Findings are not listed — a PR of only nits legitimately states none.
        """
        slots: list[str] = []
        comments = candidate["comments"]
        unclassified = sum(1 for comment in comments if not comment["classification"])
        if unclassified:
            slots.append(f"comments[].classification — {unclassified} of {len(comments)} still unclassified")
        if not candidate["change_risk"]["risk_classified"]:
            slots.append("change_risk.risk_classified — the change's risk is unclassified")
        if not candidate["rubric_version"]:
            slots.append("rubric_version — no rubric version pinned")
        return slots
