"""Batch mode for `capture` and `emit`: run one unchanged verb over many items, one at a time.

The CLI's verbs are per-PR and the job is ten PRs (PRD US-1), so a driver would otherwise write a
shell loop — and one quoting bug in that loop yields junk invocations, unattributable blocks of
output, and a wrong conclusion about the CLI drawn from a defect in the harness around it. Batch mode
moves the iteration into the CLI so every driver does not have to reinvent it (FR-2, FR-6, FR-10, FR-11).

**Containment is the whole design.** Every item runs inside :meth:`Batch.run`, which contains one
item's refusal *and* one item's unexpected exception so neither aborts the other nine. A refusal is
reported and the batch continues; an interrupt stops the batch and reports what completed, so a
half-finished batch never looks like a finished one. Nothing is rolled back — each written datapoint
is an independent filesystem write (tech plan §6.2), and `dataset` counts what is on disk.

The per-item worker returns an :class:`ItemResult` rather than raising or printing, because
:meth:`Cli.refuse` *returns* an exit code (it does not raise) — so the loop **collects** outcomes.
The one thing the loop catches is the *unexpected* exception: a bug in one item's forge round-trip
must not lose the nine good captures already on disk.

This module holds no verb-specific knowledge: the caller supplies the worker, the item→identity
description, and the progress sink, and reads the counts back off the report. The single-item payload
shape each worker stores in :attr:`ItemResult.payload` is reused verbatim in the batch `--json`
output, so a driver parses one schema across single and batch runs (FR-2).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: ``(index, total, result)`` → the progress sink. The CLI writes one line to **stderr** per item so
#: ``--json`` stdout stays parseable (FR: progress never on stdout).
ProgressSink = Callable[[int, int, "ItemResult"], None]


@dataclass(frozen=True, slots=True)
class ItemRefusal:
    """One item's refusal in the FR-2 four-field shape.

    No ``exit_code`` field: a batch never exits per item. The overall batch exit is a single class
    (refusal, exit 3) whenever any item did not succeed — the per-item exit-code class is not what the
    batch reports, so carrying it here would only invite a caller to switch on it and get the batch
    exit wrong.
    """

    check: str
    datapoint: str
    offending: str
    next_: str


@dataclass(frozen=True, slots=True)
class ItemResult:
    """One item's outcome: its identity, and either a success payload or a refusal.

    ``payload`` is the *single-item* verb's own ``--json`` object (for `capture`: ``candidate`` /
    ``slots`` / ``emittable``; for `emit`: ``task`` / ``iteration`` / ``checks`` …), stored verbatim so
    the batch `--json` reuses one schema (FR-2, "no new per-item fields"). ``detail`` is the short
    human progress note (``→ candidates/pr-523.json``). Exactly one of ``payload`` / ``refusal`` is
    set; ``ok`` says which.
    """

    key: str
    pr_number: int | None
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    refusal: ItemRefusal | None = None

    @classmethod
    def success(cls, key: str, pr_number: int | None, *, payload: dict[str, Any], detail: str) -> ItemResult:
        """A succeeded item carrying the single-item ``--json`` payload and a human progress detail."""
        return cls(key=key, pr_number=pr_number, ok=True, payload=payload, detail=detail)

    @classmethod
    def refuse(cls, key: str, pr_number: int | None, *, check: str, offending: str, next_: str) -> ItemResult:
        """A refused item in the FR-2 four-field shape; its ``datapoint`` is the item's own key."""
        return cls(
            key=key,
            pr_number=pr_number,
            ok=False,
            refusal=ItemRefusal(check=check, datapoint=key, offending=offending, next_=next_),
        )

    @classmethod
    def from_exception(cls, key: str, pr_number: int | None, error: BaseException) -> ItemResult:
        """Contain one item's *unexpected* exception as a refusal, so the other items are unaffected.

        Reported with the exception's type and message (never a swallowed traceback): a bug in one
        item's capture is a defect to inspect, not a reason to lose the batch.
        """
        return cls.refuse(
            key,
            pr_number,
            check="unexpected-error",
            offending=f"{type(error).__name__}: {error}",
            next_="this item raised an unexpected error; the other items were unaffected — inspect it and re-run it alone",
        )

    def as_json(self) -> dict[str, Any]:
        """The per-item ``--json`` object: identity plus the reused single-item payload or refusal shape.

        A success merges the single-item payload verbatim; a refusal reuses ``failures`` — the exact
        shape :meth:`Cli.refuse` emits for a single item — so a driver parses one schema either way.
        """
        base: dict[str, Any] = {"pr_number": self.pr_number, "ok": self.ok}
        if self.ok:
            return {**base, **self.payload}
        refusal = self.require_refusal()
        failure = {
            "check": refusal.check,
            "datapoint": refusal.datapoint,
            "offending": refusal.offending,
            "next": refusal.next_,
        }
        return {**base, "failures": [failure]}

    def require_refusal(self) -> ItemRefusal:
        """The refusal of a non-``ok`` result; a programming error if a succeeded item is asked for one."""
        if self.refusal is None:  # pragma: no cover - constructors keep ok and refusal in lockstep
            raise ValueError("a non-ok ItemResult must carry a refusal")
        return self.refusal


@dataclass(frozen=True, slots=True)
class BatchReport:
    """The result of a whole batch: every item's outcome, the plan size, and whether it was interrupted.

    Counts and the clean/partial verdict are derived, never stored, so they cannot drift from
    ``results``. The CLI maps :attr:`clean` onto an exit code (0 clean, 3 otherwise) — the batch
    itself does not import the exit-code enum, keeping this module free of CLI coupling.
    """

    results: tuple[ItemResult, ...]
    total: int
    interrupted: bool

    @property
    def attempted(self) -> int:
        """How many items actually ran (fewer than ``total`` only when interrupted)."""
        return len(self.results)

    @property
    def succeeded(self) -> int:
        """How many items succeeded."""
        return sum(1 for result in self.results if result.ok)

    @property
    def refused(self) -> int:
        """How many items were refused (including contained unexpected exceptions)."""
        return sum(1 for result in self.results if not result.ok)

    @property
    def clean(self) -> bool:
        """True only when every planned item ran and succeeded — the sole exit-0 condition.

        An interrupt (``attempted < total``) or any refusal makes a batch not clean, so a partial or
        interrupted batch never reports success (FR-11: refusals do not become silent skips).
        """
        return not self.interrupted and self.refused == 0 and self.attempted == self.total

    def refusals(self) -> list[ItemResult]:
        """The refused items, in run order — the source of the summary's per-item refusal lines."""
        return [result for result in self.results if not result.ok]

    def items_json(self) -> list[dict[str, Any]]:
        """Every item's ``--json`` object, in run order — the ``items`` array of the batch payload."""
        return [result.as_json() for result in self.results]

    def summary_json(self) -> dict[str, Any]:
        """The ``summary`` object for batch ``--json``: counts plus one entry per refused item.

        ``interrupted`` is present only when the batch was interrupted, so a clean or fully-refused
        batch keeps the minimal shape a driver expects.
        """
        summary: dict[str, Any] = {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "refused": self.refused,
            "refusals": [{"pr_number": result.pr_number, "check": result.require_refusal().check} for result in self.refusals()],
        }
        if self.interrupted:
            summary["interrupted"] = True
        return summary


class Batch:
    """Run a per-item worker over a sequence of items with per-item containment. Holds no state."""

    @classmethod
    def run[ItemT](
        cls,
        items: Sequence[ItemT],
        worker: Callable[[ItemT], ItemResult],
        describe: Callable[[ItemT], tuple[str, int | None]],
        *,
        on_progress: ProgressSink,
    ) -> BatchReport:
        """Run ``worker`` over ``items`` in order, reporting each as it completes; return the aggregate.

        A refusal is *collected* (the worker returns it, never raises it) and the batch continues. An
        unexpected exception is contained per item via ``describe`` (which yields the item's identity
        without having a result to read it from) and also continues. A :class:`KeyboardInterrupt` stops
        the batch immediately — the completed items are reported, and :attr:`BatchReport.interrupted`
        marks the plan as unfinished so the caller exits non-zero.
        """
        results: list[ItemResult] = []
        total = len(items)
        interrupted = False
        for index, item in enumerate(items, start=1):
            try:
                result = worker(item)
            except KeyboardInterrupt:
                interrupted = True
                break
            except Exception as error:  # noqa: BLE001 - one item's crash must not abort the batch (the point of batch mode)
                key, pr_number = describe(item)
                result = ItemResult.from_exception(key, pr_number, error)
            results.append(result)
            on_progress(index, total, result)
        return BatchReport(results=tuple(results), total=total, interrupted=interrupted)
