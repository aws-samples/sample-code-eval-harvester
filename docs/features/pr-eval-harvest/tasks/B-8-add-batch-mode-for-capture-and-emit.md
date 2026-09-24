---
id: B-8
title: "[Forge] Batch mode for `capture` and `emit`"
feature: pr-eval-harvest
workstream: Forge capture
status: done
complexity: M
implements: [FR-2, FR-6, FR-10, FR-11]
user_story: US-1
blocked_by: [B-3, D-3]
blocks: []
---

# B-8: [Forge] Batch mode for `capture` and `emit`

## Context

The CLI's verbs are per-PR and the job is ten PRs (US-1), so without batch mode a driver writes a
shell loop — and a shell loop is a second, un-tested harness around the CLI: one quoting bug turns ten
clean invocations into junk output and a wrong conclusion drawn from a bug in the loop rather than the
tool. Batch mode moves the iteration into the CLI so every driver does not reinvent it, and so a
partial or interrupted run can never be mistaken for a finished one.

**Containment is the whole design.** Every item runs inside `Batch.run`, which contains one item's
refusal *and* one item's unexpected exception so neither aborts the other nine: a refusal is reported
and the batch continues, an interrupt stops the batch and reports what completed. Nothing is rolled
back — each written datapoint is an independent filesystem write (tech plan §6.2), and `dataset`
counts what is on disk. The per-item worker returns a result rather than raising or printing (because
the refusal helper *returns* an exit code), so the loop **collects** outcomes. The module holds no
verb-specific knowledge: the caller supplies the worker, the item→identity description, and the
progress sink, and the single-item `--json` payload each worker stores is reused verbatim in the batch
output, so a driver parses one schema across single and batch runs (FR-2).

**North star:** get Harbor review-eval datapoints from a repo's own PR history without hand-labelling.
**Implements:** FR-2 (one `--json` schema across single and batch; per-item refusals in the four-field shape), FR-6 (`capture` enumerates candidate PRs), FR-10 (a batch's candidates are byte-identical to individual captures), FR-11 (a partial/refused batch never reports success)  ·  **User story:** US-1

## Design References

- **Tech plan:** `../tech-plan.md` §5.1, §6.2 — filesystem-as-bus; each written datapoint is independent, nothing to roll back
- **Tech plan:** §9 — the shared verb conventions and exit-code classes
- **B-1** — `survey --json`, whose harvestable-PR list drives batch `capture`
- **B-3** — single-PR `capture` (the worker batch reuses) and its `no-emittable-iteration` per-item refusal
- **D-3** — single-candidate `emit` (the worker batch reuses) and its refusals

## What To Build

1. Create `src/eval_harvest/batch.py` — verb-agnostic per-item iteration with containment:
   - `Batch.run(items, worker, describe, *, on_progress) -> BatchReport` — run `worker` over `items` in
     order, collecting each `ItemResult`; contain an unexpected exception per item via `describe`
     (which yields the item's identity without a result to read it from); a `KeyboardInterrupt` stops
     the batch, reports completed items, and marks the report `interrupted`.
   - `ItemResult` — `success(key, pr_number, *, payload, detail)` carrying the single-item verb's own
     `--json` object verbatim, or `refuse(...)` in the FR-2 four-field shape, or `from_exception(...)`
     which contains an unexpected error as a refusal (`unexpected-error`) with the type + message.
     `as_json()` merges the single-item payload on success, or emits `failures: [{check, datapoint,
     offending, next}]` — the exact shape the single-item refusal helper emits — on failure.
   - `BatchReport` — `attempted`/`succeeded`/`refused` counts and `clean` (true only when every planned
     item ran and succeeded; an interrupt or any refusal makes it not clean), `items_json`, and
     `summary_json`. Counts are derived, never stored, so they cannot drift from `results`.
2. Wire batch mode into `capture`: `--from-survey <file>` reads a saved `survey --json` payload and
   captures every harvestable PR it lists; it is mutually exclusive with the positional `<pr>` (both or
   neither is a usage error, exit 2). `--limit N` takes a prefix of that harvestable list; `--pr <n>`
   (repeatable) restricts the batch to named PRs, validated against the payload (an unknown PR refuses
   `unknown-pr`). The per-item worker is the offline, deterministic single-PR capture core (B-3), so a
   batch's candidate files are byte-identical to individual captures (FR-10). An empty batch (no clean
   PR to capture) refuses `no-clean-pr` rather than run an exit-0 empty batch. A per-item forge failure
   or `no-emittable-iteration` is a per-item refusal, not a crash.
3. Wire batch mode into `emit`: `--all` emits every candidate under `<dataset>/candidates/` for the
   chosen `--kind` (mutually exclusive with a positional `<candidate>`), in sorted order; a per-item
   `emit` refusal is contained and the batch continues.
4. Exit and reporting: a fully clean batch exits 0; a partial (interrupted) or any-refused batch exits
   3 (the refusals are real and must not be invisible, FR-11). Progress goes one line per item to
   **stderr** so `--json` stdout stays parseable; the batch `--json` payload is `{items: [...],
   summary: {...}}` reusing the single-item shape.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/batch.py` | Create | `Batch.run`, `ItemResult`, `ItemRefusal`, `BatchReport`, `ProgressSink`; no CLI/verb coupling |
| `src/eval_harvest/cli.py` | Modify | `capture --from-survey/--limit/--pr`, `emit --all`; the batch workers, `no-clean-pr`/`unknown-pr`, the batch reporter; progress to stderr |
| `tests/test_batch.py` | Create | The unit cases below plus the `capture`/`emit` batch integration cases |

## Schemas & Contracts

**Batch `--json`:** `{items: [ <single-item payload | {failures:[…]}> … ], summary: {attempted,
succeeded, refused, refusals:[{pr_number, check}], interrupted?}}`. Each item's object is the verb's
own single-item payload (`capture`: `candidate`/`slots`/`emittable`; `emit`: `task`/`iteration`/
`checks`) or the single-item `failures` shape — one schema across single and batch runs (FR-2).

**Migration:** none. **Backward compatibility:** new flags on existing verbs; single-PR/single-candidate
invocations are unchanged.

## How To Verify

```bash
mise run test -- tests/test_batch.py
mise run lint
mise run typecheck
```

Then by hand: `survey --json > s.json`, then `capture --from-survey s.json …` and confirm the
harvestable PRs are captured in payload order, blocked PRs are skipped, a per-item failure is reported
and the batch continues, and the batch's candidate files are byte-identical to individual captures.
Then `emit --all --kind reject --dataset …` and confirm every candidate is emitted, a partial batch
exits 3, and progress prints to stderr while `--json` stdout stays parseable.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_batch_continues_after_one_refusal` | a refused item does not stop the batch | One bad PR losing nine good ones |
| `test_batch_reports_each_refusal_with_its_item` | each refusal names its own item | Unattributable failures |
| `test_batch_contains_unexpected_exception_as_a_refusal` | an item's crash is contained as `unexpected-error` | A single crash aborting the batch |
| `test_batch_interrupt_reports_partial_and_exits_nonzero` | an interrupt reports completed items and marks the plan unfinished | A half-batch read as a full one |
| `test_progress_sink_fires_once_per_completed_item_in_order` | progress fires once per item, in order | Missing or duplicated progress lines |
| `test_batch_all_success_is_clean` / `test_batch_partial_is_not_clean` / `test_batch_all_refused_is_not_clean` | `clean` is true only when every planned item succeeded | A partial batch reporting success (FR-11) |
| `test_json_items_reuse_single_item_shape` | a success item's `--json` is the single-item payload | Two schemas a driver must branch on |
| `test_json_refusal_reuses_the_single_item_failures_shape` | a refusal item reuses the `failures` shape | A batch refusal a single-item consumer cannot parse |
| `test_summary_json_counts_and_refusals` | the summary carries counts + per-item refusal checks | Counts drifting from the item list |
| `test_capture_from_survey_captures_clean_prs_in_order` / `test_capture_from_survey_skips_blocked_prs` | `--from-survey` captures harvestable PRs in payload order, skips blocked | A batch capturing unbuildable PRs or reordering them |
| `test_pr_filter_restricts_and_stays_in_payload_order` / `test_unknown_pr_in_filter_refuses` | `--pr` restricts and validates against the payload | A silent skip of a mistyped PR |
| `test_limit_takes_a_prefix` | `--limit` takes a prefix of the harvestable list | An off-by-one or wrong slice |
| `test_batch_output_matches_individual_invocations` | batch candidates are byte-identical to individual captures | Batch introducing nondeterminism (FR-10) |
| `test_emit_all_emits_every_candidate` / `test_batch_all_success_exits_0` / `test_batch_partial_exits_3` | `emit --all` emits every candidate; exit 0 clean, 3 partial | A partial emit batch exiting 0 |
| `test_batch_summary_counts_match_files_written` | the summary counts match the datapoints on disk | A summary that over- or under-counts |
| `test_batch_progress_goes_to_stderr` | progress is on stderr, not stdout | Progress corrupting `--json` stdout |

## Acceptance Criteria

- [ ] `Batch.run` contains a per-item refusal and a per-item unexpected exception; an interrupt reports completed items and marks the batch unfinished
- [ ] `capture --from-survey <file>` captures every harvestable PR from a `survey --json` payload in payload order; `--limit`/`--pr` restrict it; `<pr>` and `--from-survey` are mutually exclusive (exit 2); an empty batch refuses `no-clean-pr`
- [ ] `emit --all` emits every candidate under the dataset for `--kind`, in sorted order
- [ ] A clean batch exits 0; a partial or any-refused batch exits 3 (FR-11)
- [ ] The batch `--json` reuses the single-item payload/`failures` shape (FR-2); progress goes to stderr
- [ ] A batch's candidate files are byte-identical to individual captures (FR-10)
- [ ] All commands in "How To Verify" pass; no regressions

## Out Of Scope

- Any change to what single-PR `capture` or single-candidate `emit` decide — batch reuses their workers verbatim (B-3, D-3).
- Rolling back written datapoints on a partial batch — each write is independent; `dataset` counts what is on disk.
- Parallelism — items run one at a time; correctness first.

## Notes & Gotchas

- **Collect, do not raise.** The refusal helper returns an exit code, so the worker returns an `ItemResult`; the only thing the loop catches is the *unexpected* exception.
- **`clean` is derived** from `interrupted`, `refused`, and `attempted == total` — so a partial or interrupted batch can never report success (FR-11).
- **One schema (FR-2).** Store the single-item `--json` payload verbatim in `ItemResult.payload`; a batch adds no per-item fields a single-item consumer would not know.

## Dependencies

**Blocked by:** [B-3](B-3-build-candidate-py-and-wire-capture-verb.md) (single-PR `capture` worker), [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md) (single-candidate `emit` worker)
**Blocks:** nothing
