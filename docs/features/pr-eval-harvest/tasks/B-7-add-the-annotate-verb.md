---
id: B-7
title: "[Forge] Add the `annotate` verb: append-only per-comment fill"
feature: pr-eval-harvest
workstream: Forge capture
status: done
complexity: M
implements: [FR-1, FR-9, FR-10, FR-13, FR-15]
user_story: US-3
blocked_by: [B-3, B-4, F-2]
blocks: []
---

# B-7: [Forge] Add the `annotate` verb: append-only per-comment fill

## Context

The candidate is both what the agent reads to decide and what it writes the decision back into.
Filling it by rewriting the whole JSON file — and rewriting it again after every correction — means
the agent reproduces tens of kilobytes it is not changing on every edit. `annotate` takes the
judgments as a stream of small records (one comment's classification, one finding, the change risk,
the rubric version) and merges each into the candidate, so the agent never reproduces the bytes it is
not changing (tech plan §5.3).

The merge is a *pure* function: a candidate dict in, a new candidate dict plus every violation out, no
filesystem access — so abort-on-violation is one branch, not a cleanup path, and the whole thing is
trivially testable. Only the CLI's handler reads stdin and writes the file, through the candidate's
`dump` so the bytes stay byte-identical to what `capture` would produce for the same content (FR-10).
**The CLI never writes a judgment (FR-9):** `annotate` records exactly what the agent said and infers
nothing — it does not guess a severity, default `reference_only`, or fill `rubric_version`. This task
also pins down the fill contract the agent writes against — the `comments[].classification` vocabulary
(including `bot`) and the `findings[]` field shape — so the schema the agent fills is documented, not
reverse-engineered from the source.

**North star:** get Harbor review-eval datapoints from a repo's own PR history without hand-labelling.
**Implements:** FR-1 (help teaches the fill contract), FR-9 (records judgments, infers none), FR-10 (byte-identical merges), FR-13 (per-comment classification), FR-15 (findings carry severity + evidence, validated)  ·  **User story:** US-3

## Design References

- **Tech plan:** `../tech-plan.md` §5.3 — `annotate` is the append-only fill step in the sequence
- **Tech plan:** §8 (Data Model) — the candidate `comments[]`/`findings[]`/`change_risk`/`rubric_version` slots the records fill
- **Tech plan:** §9 — the shared verb conventions
- **B-3** — `CandidateDict`/`FindingDict`/`Violation` and `validate_filled` (which owns the value-coherence rules `annotate` defers to)
- **B-4** — the shared fill-contract constants (`FILL_CONTRACT_GUIDE`, `CLASSIFICATION_GUIDE`, `FINDING_FIELD_GUIDE`)
- **F-2** — the man/`--help` methodology surface this fill contract is documented in

## What To Build

1. Create `src/eval_harvest/annotate.py` with a stateless `Annotate` namespace class, an
   `AnnotateParseError` (carrying the 1-based line a parse failed on), and an `AnnotateStats`
   (comments classified, ids overwritten, findings added):
   - `parse_records(text, *, jsonl)` — JSONL (one object per non-blank line) when `jsonl`, else a JSON
     array; a malformed record raises `AnnotateParseError` naming its line, not a bare `json` traceback.
   - `apply_records(candidate, records, *, replace_findings=False) -> (CandidateDict, list[Violation])`
     — merge into a *deep copy*, never mutating the input; return the copy and every apply-time
     violation (unknown comment id, unrecognised record shape, a finding omitting `reference_only`).
     Value coherence (a finding's severity + evidence, FR-15) is left to `Candidate.validate_filled`,
     which the CLI runs on the merged result — one copy of those rules, not two (ADR-2). With
     `replace_findings`, clear `findings[]` before applying so a rescoping run drops the previous set
     in one command.
   - A record is dispatched by its discriminator key (`comment`/`finding`/`risk_classified`/
     `rubric_version`); a record carrying none is a violation, never a silent skip. A comment record
     sets `classification`/`classification_rationale` in place (preserving field order for FR-10); a
     finding record appends a `FindingDict` in §8 field order and requires `reference_only` stated
     explicitly; a scalar record sets the risk classification and/or rubric version in place.
   - `summarize` (what a clean merge did, including ids whose classification was already set —
     reported, never silently overwritten) and `remaining_slots` (the still-unfilled required slots,
     one compact line each, so a driver can loop until empty).
2. Wire `annotate <candidate> [--stdin | --from-json <file>] [--check] [--replace-findings]
   [--dataset <dir>]`. `--stdin` reads JSONL; `--from-json` reads a JSON array; the two are mutually
   exclusive; `--check` runs with no input and reports the remaining slots (exit 3 when any remain,
   exit 0 when fully filled). Records apply in order, last writer wins.
3. Refusals (FR-2 four fields): `malformed-record` (a broken stream, naming the line), and — as
   violations aggregated from `apply_records` + `validate_filled` — `annotate.unknown-comment`,
   `annotate.unrecognised-record`, and `annotate.finding-missing-field`. **Write nothing on any
   violation.** Neither source without `--check` is a usage error (exit 2).
4. Document the fill contract so the agent does not read `candidate.py` to learn it: the
   `classification` vocabulary `defect|nit|question|approval|bot`, and the `findings[]` field shape
   with `severity`/`severity_evidence`/`severity_rationale`/`reference_only`. Keep it in the shared
   guide constants and the `annotate` man page, and pin the documented finding fields against
   `FindingDict.__annotations__` so the docs cannot decay.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/annotate.py` | Create | The `Annotate` namespace class, `AnnotateParseError`, `AnnotateStats`; a pure merge |
| `src/eval_harvest/cli.py` | Modify | Register `annotate`, its flags, `handle_annotate`, the refusals; reads stdin/file and writes through `Candidate.dump` |
| `man/eval-harvest-annotate.md` | Create | The record shapes, the `classification` vocabulary (incl. `bot`), the `findings[]` field contract |
| `tests/test_annotate.py` | Create | The cases below |
| `tests/test_help_methodology.py` | Modify | Pin the `annotate` methodology + the documented-fields-vs-`FindingDict` check |

## Schemas & Contracts

**Record stream (JSONL on `--stdin`, or a JSON array via `--from-json`), one object per judgment:**
```json
{"comment": 101, "classification": "defect", "rationale": "off-by-one on the new-side bound"}
{"finding": {"comment_ids": [101], "statement": "…", "severity": "high",
             "severity_evidence": "src/pay.py:41", "severity_rationale": "…", "reference_only": false}}
{"risk_classified": "low", "rationale": "config-only change"}
{"rubric_version": "v1"}
```
`classification ∈ {defect, nit, question, approval, bot}`. `reference_only` must be stated explicitly
on every finding — the CLI never defaults a block/no-block call (FR-9). Coherence of a finding's
severity/evidence is enforced by `validate_filled` on the merged candidate (FR-15).

**`--json` output (a clean merge, stdout, exit 0):** `{candidate, classified, overwritten, findings_added,
remaining_slots, ok}` — what the merge did plus the still-unfilled slots. `--check` emits `{candidate,
ok, violations, remaining_slots}` and writes nothing.

**Migration:** none. **Backward compatibility:** new verb; edits the B-3/B-4 candidate in place, byte-stably.

## How To Verify

```bash
mise run test -- tests/test_annotate.py tests/test_help_methodology.py
mise run lint
mise run typecheck
```

Then by hand: `capture` a PR, pipe a few JSONL records through `annotate --stdin`, and confirm only
the named slots changed and the file is byte-identical to a `capture` of the same content; feed an
unknown comment id / a record with no discriminator / a finding missing `reference_only` and confirm
each refuses and writes nothing; run `annotate --check` and confirm it lists the remaining slots
(exit 3) then exits 0 once filled.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_annotate_sets_only_the_named_comment` | a comment record touches only that comment's slots | A merge clobbering unrelated fields |
| `test_annotate_output_is_byte_identical_to_capture_dump` | the merged file matches a `capture` dump of the same content | Field-order/whitespace drift breaking FR-10 |
| `test_annotate_overwrite_is_reported` | re-classifying an already-set comment is reported, not silent | A silent overwrite the agent cannot see |
| `test_annotate_appends_findings` | a finding record appends a well-formed `FindingDict` | A finding dropped or malformed |
| `test_annotate_sets_risk_and_rubric` | scalar records set risk classification and rubric version | The change-risk/rubric slots unfillable via annotate |
| `test_annotate_replace_findings_clears_first` | `--replace-findings` drops the prior set before applying | A rescope leaving stale findings behind |
| `test_annotate_refuses_unknown_comment_id` | an unknown id refuses `annotate.unknown-comment` | A judgment silently attached to nothing |
| `test_annotate_refuses_unrecognised_record_shape` | a record with no discriminator refuses `annotate.unrecognised-record` | A dropped judgment worse than a loud refusal |
| `test_annotate_writes_nothing_on_violation` | any violation leaves the file untouched | A partial write on a rejected stream |
| `test_annotate_never_infers_a_judgment` | a finding missing `reference_only` refuses rather than defaulting | The CLI guessing a block/no-block call (FR-9) |
| `test_annotate_malformed_json_names_the_line` | a broken stream refuses `malformed-record` with the line | A bare `json` traceback |
| `test_annotate_check_reports_remaining_slots_exit_3` | `--check` lists unfilled slots and exits 3 | No way to loop the fill to completion |
| `test_annotate_check_exits_0_when_fully_filled` | a fully filled candidate `--check`s clean | A false "still unfilled" on a done candidate |
| `test_from_json_array_merges` | `--from-json` applies a JSON array | The array input path broken |
| `test_json_output_shape` | a clean `--json` merge carries `ok`/`classified`/`findings_added`/`remaining_slots` | A driver breaking on the output shape |
| `test_apply_records_does_not_mutate_input` | the input candidate is unchanged after a merge | A merge with a side effect that defeats abort-on-violation |
| documented finding fields vs `FindingDict.__annotations__` | the man page's finding fields match the type | The fill contract drifting from the schema |

## Acceptance Criteria

- [ ] `annotate --stdin`/`--from-json` merges records into the candidate byte-identically to a `capture` dump of the same content (FR-10)
- [ ] `apply_records` is pure (deep-copies, never mutates) and returns violations as a list
- [ ] `malformed-record`, `annotate.unknown-comment`, `annotate.unrecognised-record`, and `annotate.finding-missing-field` each refuse; the file is untouched on any violation
- [ ] The CLI never infers a judgment — `reference_only` must be stated, severity/evidence are validated by `validate_filled` (FR-9, FR-15)
- [ ] `--check` lists the remaining slots (exit 3) or exits 0 when filled; `--replace-findings` clears findings first
- [ ] The `classification` vocabulary (incl. `bot`) and the `findings[]` field shape are documented, pinned against `FindingDict.__annotations__`
- [ ] `annotate --help` and `man/eval-harvest-annotate.md` teach the fill contract; all "How To Verify" commands pass; no regressions

## Out Of Scope

- The value-coherence rules for a finding's severity/evidence — owned by `validate_filled` (B-3); `annotate` runs it, never re-implements it.
- Selecting a kind, setting `expected_verdict`, or emitting — that is `emit` (D-3).
- Suggesting what a comment's classification should be — the agent's judgment (FR-9).

## Notes & Gotchas

- **Pure merge, single writer.** `apply_records` never touches the filesystem; only `handle_annotate` reads stdin and writes, through `Candidate.dump`, so bytes match `capture` (FR-10).
- **Never infer (FR-9).** A missing `reference_only` is a refusal, not a defaulted `false`; a missing rationale is the empty string, not a guess.
- **Order preservation.** Set existing slots in place (do not rebuild the dict) so `sort_keys=False` keeps the §8 field order and the file stays byte-stable.

## Dependencies

**Blocked by:** [B-3](B-3-build-candidate-py-and-wire-capture-verb.md) (`CandidateDict`/`FindingDict`/`Violation`/`validate_filled`), [B-4](B-4-build-diffspan-and-bind-comments-to-iterations.md) (the shared fill-contract constants), [F-2](F-2-man-pages-and-help-methodology-and-parity-test.md) (the man/help surface the fill contract is documented in)
**Blocks:** nothing
