---
id: B-4
title: "[Forge] Extract diffspan.py + bind each comment to the iterations whose diff covers it"
feature: pr-eval-harvest
workstream: Forge capture
status: todo
complexity: M
implements: [FR-9, FR-12, FR-34, FR-2, FR-6]
user_story: US-2
blocked_by: [B-2, B-3]
blocks: [B-5, B-6, B-7]
---

# B-4: [Forge] Extract diffspan.py + bind each comment to the iterations whose diff covers it

## Context

Two questions in this pipeline are the same geometric question: *which iterations' diffs show the
line a review comment points at* (a fact `capture` should record), and *does an oracle finding point
at a line the change actually touches* (`verify`'s `finding-line-absent`, FR-34). Both reduce to
"parse a unified diff's new-side hunk spans, then test a location against them". Left un-centralised,
that parser gets written more than once — `verify.py`'s `finding-line-absent` (E-2) needs it, and so
does whatever a driver reaches for to work out,
before it fills a candidate, which comments a given iteration can carry a finding for. Copies of a
geometric rule drift, and when they drift the two questions get different answers for the same diff —
which reads as "the agent placed the finding wrong" rather than as a parser bug.

This task makes the membership a **fact `capture` records**. It extracts the single new-side span
parser into `diffspan.py`, records `in_diff_iterations` on every comment (the list of iteration
indices whose diff touches that comment's file and line range), and — because those lists plus each
iteration's recoverability are exactly the "can this PR become a datapoint?" question — computes an
`Emittability` view that `capture` reports and refuses on (`no-emittable-iteration`). Mechanical,
derived from diffs `capture` already holds, so it stays on the CLI's side of the FR-9 fact/judgment
line. It is the foundation the `show`, `brief`, and `annotate` verbs (B-5/B-6/B-7) all build on.

**North star:** get Harbor review-eval datapoints from a repo's own PR history without hand-labelling.
**Implements:** FR-9 (facts only), FR-12 (comment↔iteration binding), FR-34 (the same membership `finding-line-absent` tests), FR-2 (the emittability refusal), FR-6 (`capture` reports which iterations are buildable)  ·  **User story:** US-2

## Design References

- **Tech plan:** `../tech-plan.md` §8 (Data Model) — the candidate `comments[]` table this extends with one fact field
- **Tech plan:** §5.1 — the agent drives and the CLI knows the rules; a fact the CLI can compute is the CLI's job, not the agent's
- **Tech plan:** §6.2 (filesystem-as-bus) — `capture` writes `candidates/pr-<n>.json`; nothing else needs to change to carry this
- **ADR-3** in §11 — the CLI writes facts, the agent writes labels; `in_diff_iterations` is a fact
- **B-2** — the recoverable iterations and their unified diffs this parses
- **B-3** — the base `candidate.py` this augments; **E-2** — `verify.py`, whose `finding-line-absent` this shares the parser with

## What To Build

1. Create `src/eval_harvest/diffspan.py` — one module owning unified-diff new-side span parsing,
   stdlib only (`re`), no project imports so both callers depend on it without a cycle. Two functions:
   - `new_side_spans(diff: str) -> dict[str, list[tuple[int, int]]]` — the inclusive new-side line
     spans each file's hunks touch, keyed by the `+++ b/<path>` path. A `@@ … +start,count @@` header
     contributes `[start, start + count - 1]`. Keep the `count > 0` guard (a `count == 0` pure-deletion
     hunk has no new-side line and its inverted end would overlap the preceding line) and the
     omitted-count default of `1` (an omitted count means exactly one line).
   - `location_in_spans(path, line_start, line_end, spans) -> bool` — True when `[line_start, line_end]`
     overlaps a span for `path`. Both guards do real work: a path the diff never mentions has no
     spans (outside), and `line_start <= 0` means the location is not on the new side at all (a
     file-level or outdated comment the forge could not place on a line).
2. `diffspan` is the single owner of new-side span parsing: `verify.py`'s `finding-line-absent`
   (E-2) must call it too rather than carry a private copy, so `verify` and `capture` can never
   disagree about which hunk a line falls in. The two guards below (`count > 0`, `line_start <= 0`)
   are exactly the semantics `finding-line-absent` needs — get them right here and both callers
   agree by construction.
3. Add `in_diff_iterations: list[int]` to `CommentDict` immediately after `iteration_index` (before
   the `classification` slots), so the fact half stays contiguous and the §8 field order is stable.
4. In the candidate scaffold, compute the binding once for the whole PR (`_spans_by_iteration` over
   each **recoverable** iteration's diff — skip `recoverable=False`, whose diff is empty by
   construction) and hand each comment's list to the per-comment builder (`_in_diff_iterations`);
   `_comment_dict` takes the computed list as a parameter and recomputes nothing, reads no files.
   Build the list sorted (never from set-iteration order — FR-10).
5. Extend `validate_facts` with `_check_in_diff_iterations`: the field is present (the same
   `candidate.facts-missing` code B-3 uses for its other required-field checks) and every entry indexes a real
   iteration (`0 <= index < len(iterations)`, else `candidate.facts-invalid`). Return violations,
   never raise (ADR-2).
6. Add an `Emittability` view and `Candidate.emittability(candidate) -> Emittability`, reading only
   recorded facts: `recoverable_indices` (iterations with a `patch_path`), `reject_index`/`approve_index`
   (the first/last recoverable iteration — the reject/approve build states), and
   `reject_in_diff_comment_ids`/`approve_in_diff_comment_ids` (`_comment_ids_in_diff` — the comments
   whose `in_diff_iterations` names that iteration, in candidate order). `Emittability.as_json()` is
   the shape `capture`/`brief` reuse.
7. Add `Candidate.emittable_report(candidate, emittability)` — one padded line per kind naming the
   iteration it builds from and how many comments can anchor a gradeable finding. A kind whose in-diff
   comment count is **zero** gets a second, indented warning line: that kind is structurally emittable
   but a finding built on any of its comments points at a line the emitted diff never touches, so it
   fails `verify`'s `finding-line-absent`. A warning, not a refusal — the *other* kind may still be
   buildable. Fold the fill contract that `capture` prints into shared constants (`FILL_CONTRACT_GUIDE`,
   `CLASSIFICATION_GUIDE`, `FINDING_FIELD_GUIDE`) so `capture`, `brief`, and `annotate` describe the fill
   one way.
8. In the `capture` handler: after writing the candidate (it is a truthful record of what the forge
   returned and is kept for the audit), refuse `no-emittable-iteration` (exit 3, the FR-2 four fields
   via `_no_emittable_refusal_fields`) when `not emittability.recoverable_indices` — a PR with no
   recoverable iteration cannot become a datapoint of any kind, and saying so at `capture` beats a
   later `emit` refusal after the fill is spent. The four fields:
   - `check`: `no-emittable-iteration`
   - `datapoint`: `pr-<n>`
   - `offending`: "no iteration has a recoverable tip (all N reviewed states were force-pushed away); the candidate was written for the record but cannot be emitted"
   - `next`: "pick another PR from `survey`; this one's reviewed states are unreachable from refs/pull/<n>/head"
   Otherwise print the candidate path, the slots to fill, and the per-kind emittable report; carry the
   same facts under `emittable` in the `--json` payload.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/diffspan.py` | Create | The single new-side span parser; imports only `re` |
| `src/eval_harvest/verify.py` | Modify | point `finding-line-absent` (E-2) at `diffspan` so it and `capture` share one span parser |
| `src/eval_harvest/candidate.py` | Modify | `CommentDict.in_diff_iterations`; `_spans_by_iteration`/`_in_diff_iterations`; `Emittability`; `emittability`/`_comment_ids_in_diff`/`emittable_report`; the fill-contract constants; `validate_facts` |
| `src/eval_harvest/cli.py` | Modify | `handle_capture` refuses `no-emittable-iteration` and prints the emittable report; `_report_capture`/`_capture_payload` |
| `tests/test_diffspan.py` | Create | The span-parser cases below |
| `tests/test_candidate.py` | Modify | The `in_diff_iterations`, emittability, and emittable-report cases |

## Schemas & Contracts

This adds one fact field to the candidate `comments[]` entry (§8), between `iteration_index` and the
judgment slots:

```json
{ "id": 101, "…": "…", "iteration_index": 0,
  "in_diff_iterations": [0, 2],
  "classification": "", "classification_rationale": "" }
```

`in_diff_iterations: list[int]` — sorted, may be empty (a comment on a line no recoverable iteration
touches), entries index `iterations[]`. Distinct from `iteration_index` (which says *when the comment
was written*); this says *which diffs can show the line it points at*. The `Emittability` `--json`
shape (reused by `capture` and `brief`): `{recoverable_indices, reject:{iteration, in_diff_comment_ids},
approve:{…}}`, each kind object `null` when no iteration is recoverable.

**Migration:** none. A candidate captured before the field is missing it; `validate_facts` reports it
`candidate.facts-missing` and the remedy is to re-run `capture` (cheap, deterministic) — do not write a back-fill.
**Backward compatibility:** the consumers are in this repo only (`emit`, `verify`, `show`, `brief`).

## How To Verify

```bash
mise run test -- tests/test_diffspan.py tests/test_candidate.py tests/test_verify.py
mise run lint
mise run typecheck
mise run check
```

Then by hand: run `capture` on a PR with more than one iteration and open the candidate. Confirm every
comment carries `in_diff_iterations`, that a comment on a line the first iteration does not touch
excludes `0`, and that `capture`'s stdout names the per-kind counts. Run `capture` on a PR with no
recoverable iteration and confirm it refuses `no-emittable-iteration` (exit 3). Run `capture` twice and
`diff` the two candidates — must be empty (FR-10).

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_new_side_spans_reads_multi_file_diff` | a two-file diff yields the right spans per `+++ b/` path | Spans attributed to the previous file, so membership is answered against the wrong file |
| `test_new_side_spans_ignores_pure_deletion_hunk` | a `+0,0` hunk contributes no span | A deletion-only hunk claiming line 0, making every comment "in diff" |
| `test_new_side_spans_reads_a_single_line_hunk` | an omitted count means one line, not zero | An off-by-one that drops single-line hunks entirely |
| `test_location_in_spans_boundary_lines` | a range touching only the first/last line of a span is inside; one past is outside | An off-by-one that silently widens or narrows `finding-line-absent` |
| `test_location_in_spans_rejects_unknown_path_and_zero_line` | an unknown path and a `line_start <= 0` location are both outside | A file-level comment or an unmentioned path being treated as in-diff |
| `test_in_diff_iterations_excludes_unrecoverable` | an unrecoverable iteration (empty diff) never appears in any comment's list | An unrecoverable iteration claiming to contain lines, which `emit` cannot build a patch from |
| `test_in_diff_iterations_differs_from_iteration_index` | a comment written on iteration 2 whose line iteration 0 also touches lists both | The two fields conflated — the mis-binding that causes spurious `finding-line-absent` refusals |
| `test_comment_outside_every_diff_gets_empty_list` | a comment on an untouched line gets `[]`, a fact not a violation | Treating a legitimately out-of-diff comment as malformed, or omitting the field |
| `test_validate_facts_rejects_out_of_range_iteration_index` | an index past the end of `iterations[]` is a `candidate.facts-invalid` violation | A hand-edited candidate pointing at an iteration that does not exist |
| `test_capture_refuses_no_emittable_iteration` | a PR with no recoverable iteration refuses `no-emittable-iteration` (exit 3) | An unbuildable PR passing capture, wasting the fill before `emit` refuses |
| `test_emittable_report_names_per_kind_comment_counts` | the printed report names the iteration each kind builds from and how many comments can anchor a finding | The agent filling findings blind and learning the answer only from `emit` |
| `test_emittable_report_warns_when_a_kind_has_no_in_diff_comments` | a kind whose selected iteration covers zero comments gets the indented `finding-line-absent` warning line (capture still exits 0) | Refusing — or silently accepting — a PR whose *other* kind is perfectly buildable |
| `test_capture_output_byte_identical_twice` (extend) | the new field introduces no set-iteration order | Nondeterministic ordering breaking FR-10 |

## Acceptance Criteria

- [ ] `diffspan.py` is the only implementation of new-side span parsing in `src/`; `verify.py`'s `finding-line-absent` (E-2) imports it, not a private copy
- [ ] `capture`'s `in_diff_iterations` and `verify`'s `finding-line-absent` give the same answer for the same diff (they call one parser)
- [ ] Every comment in a freshly captured candidate carries `in_diff_iterations`, listing only recoverable iterations whose diff covers its line range
- [ ] `validate_facts` returns a violation (never raises) for a missing or out-of-range entry
- [ ] `capture` refuses `no-emittable-iteration` (exit 3, the FR-2 four fields, candidate written for the record) when no iteration is recoverable, and otherwise prints the per-kind emittable report — with the `finding-line-absent` warning under any kind whose in-diff comment count is zero (exit stays 0)
- [ ] Two `capture` runs on fixed inputs remain byte-identical (FR-10)
- [ ] All commands in "How To Verify" pass; no regressions in the existing suite

## Out Of Scope

- Printing diff hunks to the agent — [B-5](B-5-add-the-show-verb.md) (`show`).
- The per-PR hand-off brief — [B-6](B-6-add-the-brief-verb.md); the append-only fill — [B-7](B-7-add-the-annotate-verb.md).
- Which iteration `emit` selects, or a finding/iteration coherence refusal — `emit` (D-3).
- Exempting `reference_only` findings from `finding-line-absent` — deliberately not done: `score.py` matches reference-only findings into coverage by location overlap, so an unreachable location would silently deflate coverage. `reference_only` exempts a finding from the severity-evidence gate, not from being locatable.

## Notes & Gotchas

- **Determinism (FR-10, NFR-1):** `in_diff_iterations` must be built sorted, not from set iteration order — the same way the candidate's other list fields are built for byte-stability.
- **`recoverable=False` iterations carry an empty diff and empty `patch_path`.** Skip them explicitly rather than relying on an empty diff yielding no spans; the explicit skip is what the test pins.
- **The two "which iteration" fields are genuinely different** — `iteration_index` is forge-assigned from when the comment was written, `in_diff_iterations` is geometric. Do not "simplify" one into the other.
- **Both guards in `location_in_spans` matter.** It returns False when `line_start <= 0` *or* the path has no spans — a location not on the new side at all, and a path the diff never mentions. Implement both branches; dropping either silently widens what counts as in-diff (and what `finding-line-absent` accepts).
- **`comments[].path` can be empty** for a file-level or outdated comment; an empty path must yield `[]`, not a crash.
- Line length is 130 and mypy runs strict; `spans_by_iteration` needs a written-out annotation.

## Dependencies

**Blocked by:** [B-2](B-2-build-forge-py-iterations-comments-verdicts.md) (the recoverable iterations + their diffs), [B-3](B-3-build-candidate-py-and-wire-capture-verb.md) (the base `candidate.py` this augments)
**Blocks:** [B-5](B-5-add-the-show-verb.md), [B-6](B-6-add-the-brief-verb.md), [B-7](B-7-add-the-annotate-verb.md)
