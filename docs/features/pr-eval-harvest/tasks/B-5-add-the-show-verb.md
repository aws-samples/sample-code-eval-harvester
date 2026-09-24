---
id: B-5
title: "[Forge] Add the `show` verb: print the diff hunk around one comment"
feature: pr-eval-harvest
workstream: Forge capture
status: done
complexity: M
implements: [FR-1, FR-4, FR-9, FR-12]
user_story: US-3
blocked_by: [B-4]
blocks: [B-6]
---

# B-5: [Forge] Add the `show` verb: print the diff hunk around one comment

## Context

To classify a review comment the agent needs to see the few lines it points at — but the only place
those lines live is the materialized `patches/pr-<n>-iter<i>.patch` file, and nothing in the design
intends the agent to read whole patches (they exist for the container that grades the datapoint).
Reading a whole patch to see three lines is the largest avoidable cost in driving the CLI. `show`
closes the gap: given a candidate and a comment id, it renders only the hunk whose new-side span
covers that comment, with a little surrounding context, so the agent judges the comment without
paying for the file.

`show` is a read-only view over `capture`'s output (tech plan §5.3): it renders bytes the candidate
and its patches already hold and makes no judgment (§5.1), reads only, writes nothing (§6.2). Overlap
is decided by `diffspan` (B-4) — the one new-side span parser — so `show` and `verify` never disagree
about which hunk a line falls in.

**North star:** get Harbor review-eval datapoints from a repo's own PR history without hand-labelling.
**Implements:** FR-1 (a verb whose help teaches its own methodology), FR-4 (byte-identical for human and agent), FR-9 (renders facts, suggests no classification), FR-12 (the comment↔line facts it renders)  ·  **User story:** US-3

## Design References

- **Tech plan:** `../tech-plan.md` §5.3 — the per-verb contribution; `show` is a read-only view over the candidate
- **Tech plan:** §6.2 — `show` reads the candidate + the materialized patch, writes nothing
- **Tech plan:** §9 — the shared verb conventions (`--json`, refusal shape, exit codes)
- **B-4** — `diffspan` (the span parser `show` reuses) and `in_diff_iterations` (which picks the default iteration)
- **B-3** — the candidate schema and the materialized `patch_path` bytes `show` reads

## What To Build

1. Create `src/eval_harvest/show.py` with a stateless `Show` namespace class:
   - `hunks_for_location(diff, path, line_start, line_end) -> list[str]` — the hunk blocks whose
     new-side span overlaps `[line_start, line_end]` on `path`, each block its `@@ … @@` header plus
     body, verbatim and newline-terminated. Overlap is decided by `diffspan.location_in_spans` against
     the span `diffspan.new_side_spans` parses — the same geometry that put the iteration in the
     comment's `in_diff_iterations`. Segment on the two structural boundaries only (`+++ b/<path>` and
     `@@`); never re-parse the numeric span inside a `@@` header — that is diffspan's alone.
   - Context trimming: keep the `@@` header always (its trailing text is git's function context) and
     `--context` body lines either side of the comment's own lines; walk new-side line numbers from the
     header's `+start` (read via `diffspan`, not re-parsed). A removed (`-`) line carries no new-side
     number and is kept only inside the retained window.
   - `render(comment, iteration_index, hunks, context)` (human) and `as_json(...)` (`--json`), sharing
     one trimmed body. Byte-identical for a human and an agent (FR-4): no colour, no terminal detection.
2. Wire the `show` verb into the dispatcher: `show <candidate> --comment <id> [--iteration <n>]
   [--context <lines>] [--dataset <dir>]`; `--context` defaults to 5. The default iteration is the
   comment's **first** `in_diff_iterations` entry; an explicit `--iteration` wins so a line can be
   compared across rounds.
3. Refusals, each the FR-2 four fields (exit 3 unless noted):
   - `unknown-comment` — no comment with that id in the candidate.
   - `iteration-not-recoverable` — an explicit `--iteration` naming an unrecoverable index; `next`
     names the recoverable indices.
   - `comment-outside-every-diff` — a comment whose `in_diff_iterations` is empty and no `--iteration`
     was given; there is no hunk to show.
   - `missing-patch` — the selected iteration's `patch_path` is absent on disk.
   - A missing candidate file is a usage error (exit 2), not a refusal.
4. `show` writes nothing and makes no judgment (FR-9): no classification suggested, no candidate edit.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/show.py` | Create | The `Show` namespace class over `diffspan` |
| `src/eval_harvest/cli.py` | Modify | Register `show`, its flags, `handle_show`, the four refusals, `VERB_METHODOLOGY` |
| `man/eval-harvest-show.md` | Create | The `show` man page (SYNOPSIS, methodology) |
| `tests/test_show.py` | Create | The cases below |
| `tests/test_help_methodology.py` | Modify | Pin the `show` methodology phrases |

## Schemas & Contracts

**`--json` payload:** `{comment, iteration_index, path, line_start, line_end, body, hunk}` — the
comment's identity and location plus the context-trimmed covering hunk text, the same hunk the human
rendering shows. **Human rendering:** a header (`iteration N · comment <id> · <role> · <timestamp>`),
the `path:line_start-line_end` location, the quoted body, then the trimmed hunk(s).

**Migration:** none. **Backward compatibility:** new verb; reads the B-3/B-4 candidate only.

## How To Verify

```bash
mise run test -- tests/test_show.py tests/test_help_methodology.py
mise run lint
mise run typecheck
```

Then by hand: `capture` a multi-iteration PR, then `eval-harvest show <candidate> --comment <id>` and
confirm only the covering hunk prints (far smaller than the patch), that `--context` widens it, that
`--iteration` shows the same line in another round, and that an unknown id / an outside-every-diff
comment / a missing patch each refuse with the FR-2 four fields.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_show_prints_only_the_covering_hunk` | only the hunk whose span covers the comment prints | The agent paying for the whole patch to see three lines |
| `test_show_output_is_smaller_than_the_patch` | the rendered output is materially smaller than the source patch | A `show` that quietly dumps the file |
| `test_show_respects_context_lines` | `--context` widens the retained window | A fixed window the agent cannot widen when it needs more |
| `test_show_defaults_to_first_in_diff_iteration` | with no `--iteration`, the comment's first `in_diff_iterations` entry is shown | Showing a hunk from an iteration whose diff does not cover the line |
| `test_show_explicit_iteration_overrides_default` | `--iteration` shows the same line in another round | No way to compare a line across review rounds |
| `test_show_refuses_unknown_comment` | an unknown id refuses `unknown-comment` | A silent empty render on a typo'd id |
| `test_show_refuses_comment_outside_every_diff` | an out-of-diff comment with no `--iteration` refuses `comment-outside-every-diff` | An empty render read as "no defect here" |
| `test_show_refuses_unrecoverable_iteration` | an explicit unrecoverable `--iteration` refuses `iteration-not-recoverable` | Trying to render a hunk from an iteration with no patch |
| `test_show_refuses_missing_patch` | a selected iteration with no patch on disk refuses `missing-patch` | A traceback when the patch was not materialized |
| `test_show_missing_candidate_is_usage_error` | a missing candidate file is exit 2, not a refusal | A missing input mis-classed as a datapoint refusal |
| `test_show_json_payload_shape` | the `--json` payload carries exactly the documented keys | A driver breaking on a shape drift |
| `test_show_help_teaches_methodology` | `show --help` teaches when/why to use it | Help that lists flags but not the method (FR-1) |
| `test_show_writes_nothing` | no file is written | A read-only verb mutating the dataset |

## Acceptance Criteria

- [ ] `show <candidate> --comment <id>` prints only the covering hunk(s), trimmed to `--context`, far smaller than the patch
- [ ] The default iteration is the comment's first `in_diff_iterations` entry; `--iteration` overrides and must be recoverable
- [ ] `unknown-comment`, `comment-outside-every-diff`, `iteration-not-recoverable`, and `missing-patch` each refuse with the FR-2 four fields; a missing candidate is exit 2
- [ ] Overlap is decided by `diffspan`, not a second span parser
- [ ] `show` writes nothing and suggests no classification (FR-9)
- [ ] The `--json` payload matches the documented shape; human and agent output are byte-identical (FR-4)
- [ ] `show --help` and `man/eval-harvest-show.md` teach the methodology, pinned by the phrase test
- [ ] All commands in "How To Verify" pass; no regressions

## Out Of Scope

- The per-PR hand-off brief — [B-6](B-6-add-the-brief-verb.md); the append-only fill — [B-7](B-7-add-the-annotate-verb.md).
- Any change to `finding-line-absent` or the span parser itself — that is B-4/E-2.
- Suggesting a classification or severity — the agent's judgment, never the CLI (FR-9).

## Notes & Gotchas

- **Reuse `diffspan`; do not re-parse `@@` spans.** `show` detects only the `+++ b/` and `@@` boundaries structurally; the numeric spans are diffspan's, or `show` and `verify` drift.
- **A covering hunk always has lines in range**, so context trimming never empties it; guard the degenerate "nothing in range" case by returning the hunk whole rather than an empty string.
- **Byte-identical for human and agent (FR-4):** no colour, no width detection, no terminal branching.

## Dependencies

**Blocked by:** [B-4](B-4-build-diffspan-and-bind-comments-to-iterations.md) (`diffspan` + `in_diff_iterations`)
**Blocks:** [B-6](B-6-add-the-brief-verb.md) (the brief's per-comment next command points at `show`)
