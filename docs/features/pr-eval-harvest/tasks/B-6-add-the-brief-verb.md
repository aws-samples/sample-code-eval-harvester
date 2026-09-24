---
id: B-6
title: "[Forge] Add the `brief` verb: one self-contained per-PR brief"
feature: pr-eval-harvest
workstream: Forge capture
status: done
complexity: M
implements: [FR-1, FR-4, FR-9]
user_story: US-3
blocked_by: [B-4, B-5]
blocks: []
---

# B-6: [Forge] Add the `brief` verb: one self-contained per-PR brief

## Context

Ten datapoints are ten *independent* units of judgment — nothing about one PR informs another — so a
driver should be able to hand one PR's whole context to a fresh subagent rather than accumulate every
PR's comments and re-reads in one long context. `brief` makes that possible: one command that prints
everything needed to fill one candidate and nothing else — identity, the change's structural risk, the
recoverable iterations, which kinds are buildable, every comment with its in-diff markers, the fill
contract, and the exact next commands — so the fill work stops accumulating (tech plan §5.1, §7.1).

`brief` is a *view* of the candidate, never a second source of truth (§5.2): it renders facts the
candidate holds and the shared fill contract (`FILL_CONTRACT_GUIDE`), and suggests no classification
(FR-9). It prints **no diff bytes** — that is `show`'s job, per comment, on demand; ten briefs must
stay small, which a patch dump would defeat. Pure rendering: data in, string out, no filesystem or
forge access (the rubric version is read by the CLI and passed in).

**North star:** get Harbor review-eval datapoints from a repo's own PR history without hand-labelling.
**Implements:** FR-1 (help carries the methodology), FR-4 (byte-identical for human and agent), FR-9 (renders facts, suggests no verdict)  ·  **User story:** US-3

## Design References

- **Tech plan:** `../tech-plan.md` §5.1, §5.2 — the agent drives; the brief is a view of the candidate, not a source of truth
- **Tech plan:** §7.1 — where the fill step sits; the brief is the hand-off into a fresh context
- **Tech plan:** §9 — the shared verb conventions
- **B-4** — `Emittability` (which kinds are buildable, per-kind in-diff comment ids) and `in_diff_iterations`
- **B-5** — `show`, which the brief's per-comment/next-command lines point at for the diff bytes it omits

## What To Build

1. Create `src/eval_harvest/brief.py` with a stateless `Brief` namespace class:
   - `render(candidate, emittability, *, rubric_version, candidate_display_path, body_chars)` — the
     human hand-off, as sections: identity (repo/PR/url, rubric version, structural risk + rule),
     `iterations` (index, short tip, recoverability, how many comments its diff covers), `emittable`
     (the reject/approve rows from `Candidate.emittable_report`-style data, with a note when both kinds
     resolve to the same single recoverable round), `comments (N)` (one numbered block each: identity,
     the in-diff markers `written on iteration … · in diffs [...] · [in reject diff]/[not in reject diff]`,
     then the body), `you fill` (the shared `FILL_CONTRACT_GUIDE`), and `next` (the runnable commands).
   - `as_json(...)` — the same facts under documented keys, bodies always full (a machine consumer
     wants the exact text, so `body_chars` does not apply).
   - `body_chars` truncates each comment body when positive (with a pointer to `show` for the rest);
     `0` prints every body in full — a truncated comment cannot be classified honestly, so full is the
     default.
2. Wire `brief <candidate> [--dataset <dir>] [--body-chars <n>]`. The CLI reads the dataset's
   `rubric.md` version (or `(unset — author rubric.md and pin its version)`) and passes it in; computes
   `Candidate.emittability`; renders.
3. Refuse `no-emittable-kind` (exit 3, the FR-2 four fields) when no iteration is recoverable — a PR
   with nothing buildable has no brief to give. A missing candidate file is a usage error (exit 2).
4. The `next` commands are the exact, runnable commands for *this* candidate: `show` (a concrete
   comment id), `annotate --stdin`, then one `emit` per buildable kind — offering both kinds rather
   than picking one keeps the CLI out of the verdict (FR-9).
5. `brief` writes nothing and suggests no classification (FR-9).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/brief.py` | Create | The `Brief` namespace class; pure rendering |
| `src/eval_harvest/cli.py` | Modify | Register `brief`, `--body-chars`, `handle_brief`, the `no-emittable-kind` refusal, reading the rubric version |
| `man/eval-harvest-brief.md` | Create | The `brief` man page |
| `tests/test_brief.py` | Create | The cases below |
| `tests/test_help_methodology.py` | Modify | Pin the `brief` methodology phrases |

## Schemas & Contracts

**`--json` payload:** `{repo, pr_number, pr_url, rubric_version, change_risk, iterations:[{index, tip_sha,
recoverable, in_diff_comment_count}], emittable, comments:[{id, path, line_start, line_end, author_role,
created_at, iteration_index, in_diff_iterations, in_reject_diff, body}], fill_contract, next_commands}`.
The `fill_contract` is the shared `FILL_CONTRACT_GUIDE` — the same text `capture`'s slot summary and
`annotate` (B-7) print, so the fill is described one way.

**Migration:** none. **Backward compatibility:** new verb; reads the B-3/B-4 candidate only.

## How To Verify

```bash
mise run test -- tests/test_brief.py tests/test_help_methodology.py
mise run lint
mise run typecheck
```

Then by hand: `capture` a PR, then `eval-harvest brief <candidate>` and confirm it prints identity,
risk, iterations, emittable kinds, every comment with its in-diff markers, the fill contract, and
runnable next commands — and no diff bytes. Confirm `--body-chars` truncates bodies and points at
`show`, and that a PR with no recoverable iteration refuses `no-emittable-kind`.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_brief_lists_every_comment_with_in_diff_marker` | each comment shows its `in_diff_iterations` and reject-diff marker | The agent unable to tell which comments a kind can anchor a finding for |
| `test_brief_states_both_emittable_kinds` | both reject and approve rows appear with their iteration and count | The brief silently picking a kind |
| `test_brief_reports_kind_unavailable_with_reason` | a kind that cannot build is named with its reason | A missing kind read as an error rather than a fact |
| `test_brief_suggests_no_classification` | no field suggests defect/nit or a verdict | The CLI leaking a judgment (FR-9) |
| `test_brief_body_full_by_default` | bodies print in full with no `--body-chars` | A silently truncated comment classified on partial text |
| `test_brief_body_chars_truncates_and_points_at_show` | `--body-chars` truncates and names `show` for the rest | A truncation with no way to recover the full body |
| `test_brief_next_commands_use_the_real_candidate_path` | the next commands carry this candidate's real path, no placeholders | Commands the agent must edit before running |
| `test_brief_prints_no_diff_bytes` | no hunk/patch text appears | A brief that dumps patches, defeating the small-context goal |
| `test_brief_json_payload_shape` | the `--json` payload carries exactly the documented keys | A driver breaking on a shape drift |
| `test_fill_contract_text_shared_with_slot_summary` | the brief's fill contract is the same constant `capture` prints | The fill described two different ways |
| `test_brief_refuses_no_emittable_kind` | a PR with no recoverable iteration refuses `no-emittable-kind` | A brief for a PR nothing can be built from |
| `test_brief_missing_candidate_is_usage_error` | a missing candidate is exit 2 | A missing input mis-classed as a datapoint refusal |
| `test_brief_writes_nothing` | no file is written | A read-only verb mutating the dataset |
| `test_brief_help_teaches_methodology` | `brief --help` teaches when/why to use it | Help that lists flags but not the method (FR-1) |

## Acceptance Criteria

- [ ] `brief <candidate>` prints identity, risk, iterations, emittable kinds, every comment with in-diff markers, the fill contract, and runnable next commands
- [ ] No diff bytes appear; `--body-chars` truncates bodies and points at `show`
- [ ] The fill contract is the shared `FILL_CONTRACT_GUIDE`, identical to `capture`'s slot summary and `annotate`'s
- [ ] The next commands carry this candidate's real path (`show`/`annotate`/one `emit` per buildable kind)
- [ ] A PR with no recoverable iteration refuses `no-emittable-kind`; a missing candidate is exit 2
- [ ] `brief` writes nothing and suggests no classification (FR-9); human and `--json` carry the same facts (FR-4)
- [ ] `brief --help` and `man/eval-harvest-brief.md` teach the methodology, pinned by the phrase test
- [ ] All commands in "How To Verify" pass; no regressions

## Out Of Scope

- Printing diff bytes — that is `show` (B-5); the brief points at it and prints none.
- Writing the fill back — that is `annotate` (B-7).
- Suggesting any classification, severity, or verdict — the agent's judgment (FR-9).

## Notes & Gotchas

- **Pure rendering, no I/O.** `Brief` takes data and returns a string; the CLI does the reads (candidate, rubric version) and the writes (none). This keeps it trivially testable and byte-stable.
- **Bodies full by default (FR-4).** A truncated review comment cannot be classified honestly; `body_chars == 0` means full, and `--json` is always full.
- **One recoverable round** collapses reject and approve onto the same state — state it plainly rather than let two identical rows read as "both kinds fine".

## Dependencies

**Blocked by:** [B-4](B-4-build-diffspan-and-bind-comments-to-iterations.md) (`Emittability`, `in_diff_iterations`, the fill-contract constants), [B-5](B-5-add-the-show-verb.md) (the brief's per-comment/next-command lines point at `show`)
**Blocks:** nothing
