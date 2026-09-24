---
id: B-2
title: "[Forge] Build forge.py: iterations, inline comments, verdicts, timing"
feature: pr-eval-harvest
workstream: Forge capture
status: todo
complexity: L
implements: [FR-6, FR-7, FR-8, FR-12, FR-10, NFR-7]
user_story: US-2
blocked_by: [A-2]
blocks: [B-3, B-4]
---

# B-2: [Forge] Build forge.py: iterations, inline comments, verdicts, timing

## Context

`survey` (B-1) reports only the *count* of review rounds. To turn a PR into datapoints the agent needs the deep facts: each review iteration's tip/base SHA and diff, each inline comment bound to the iteration it landed against (path, line range, role, timestamp), and every verdict with its timing. This is the fetch `survey` (B-1) deliberately does not do — it needs the `gh api` REST endpoints (reviews/comments/timeline) plus `git fetch +refs/pull/<n>/head`, because `gh pr view --json reviews` returns verdict-level reviews only, with no per-line comments (TP-3). This module is the only source of the mechanical facts B-3's candidate file records.

**North star:** every reviewed PR yields the before-state, the diff, and the exact comments reviewers made — the raw material for both a reject and an approve datapoint.
**Implements:** FR-6, FR-7, FR-8, FR-12, FR-10, NFR-7  ·  **User story:** US-2 (serves US-3)

## Design References

- **Tech plan:** `../tech-plan.md` §9, `eval-harvest capture` contract — the exact reads (`gh api pulls/<n>/reviews`, `/comments`, `/timeline`; `git fetch +refs/pull/<n>/head`) and the fact set to produce
- **Tech plan:** §2.3 TP-3 — why the REST endpoints, not `gh pr view --json reviews` (confirmed by probing the field list)
- **Tech plan:** §8 candidate schema — the `iterations[]`, `review_verdicts[]`, `comments[]` field shapes this module must fill
- **Tech plan:** §4 Risks, "Force-pushed iterations unrecoverable" — report an unreachable iteration tip as unrecoverable, never fabricate it
- **Tech plan:** §2.2 NFR-7 — bound the `gh`/`git` round trips per PR `[TODO: set target — measure against a reference repo]`
- **The pull-head fetch pattern:** `git fetch '+refs/pull/<n>/head:refs/remotes/pr/<n>'` — a squash-merged, branch-deleted PR's before-states are recoverable only via the forge-retained pull head, since the branch is gone and the squash commit's first parent is the branch point

## What To Build

1. Create `src/eval_harvest/forge.py`. Every `gh`/`git` call goes through `gitcmd.py` (A-2).
2. Fetch the PR head ref: `git fetch origin +refs/pull/<n>/head:refs/remotes/pr/<n>` so squash-merged/rebased before-states are recoverable (FR-8).
3. Fetch reviews via `gh api repos/<owner>/<name>/pulls/<n>/reviews` — each with `state` (APPROVED / CHANGES_REQUESTED / COMMENTED), `user`, `author_association`, `submitted_at` (FR-7).
4. Fetch inline review comments via `gh api repos/<owner>/<name>/pulls/<n>/comments` — each with `id`, `body`, `path`, `original_line`/`line` (line range), `user`/`author_association`, `created_at`, and the `commit_id`/`original_commit_id` needed to bind it to an iteration (FR-12).
5. Fetch the timeline (`/issues/<n>/timeline` or the PR timeline) to order review rounds and locate the commits that constitute each iteration.
6. Reconstruct **iterations**: group commits into the states reviewers saw (round 1 = the pushed state that drew the first review, round 2 = after the fix, …). For each, compute `tip_sha`, `base_sha`, the unified diff against the base, and the `comment_ids` bound to it (FR-6). Bind each comment to an iteration via its `original_commit_id`/`commit_id` (FR-12).
7. When an iteration tip is unreachable (force-pushed away and GC'd on the forge, not at `refs/pull/<n>/head`), report it as **unrecoverable** — do not fabricate a diff (FR-8, §4 risk row). Recoverable states are the added-commit iterations and the final pre-merge tip.
8. Return a plain fact bundle (dataclasses or dicts) that B-3's `candidate.scaffold` consumes — no judgment, no label. Each recoverable iteration carries its unified diff; those diffs are the input B-4 parses (via `diffspan.py`) to bind each comment to the iterations whose diff covers its line range (`in_diff_iterations`). This module does **not** parse hunk spans itself — it produces the diffs; the single span parser lives in `diffspan.py` (B-4).
9. Determinism (FR-10): order everything by a stable key (comment id, submitted_at then id), no set iteration in output.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/forge.py` | Create | The deep-fetch module |
| `tests/test_forge.py` | Create | Cases from the test table, on a fixture repo + recorded payloads |
| `tests/fixtures/repo_builder.py` | Create | Fixture-repo builder (see New Infrastructure below) |
| `tests/fixtures/gh/` | Modify | Add recorded `reviews`/`comments`/`timeline` payloads for the squash-merge fixture |

## Schemas & Contracts

**Fact bundle this module returns (consumed by B-3, mirrors §8 facts):**
```
iterations:      [ { tip_sha, base_sha, diff: <unified diff bytes>, comment_ids: [int], recoverable: bool } ]
review_verdicts: [ { state, author, author_role, submitted_at } ]
comments:        [ { id, body, path, line_start, line_end, author_role, created_at, iteration_index } ]
```
`gh api …/reviews` gives `state`/`submitted_at`/`author_association`; `…/comments` gives `path`/`line`/`original_commit_id`. This is why TP-3 needs the REST endpoints — `gh pr view --json reviews` has none of the per-line fields.

**Migration:** none.
**Backward compatibility:** none — new module.

## How To Verify

```bash
mise run test -- tests/test_forge.py
mise run lint
mise run typecheck
```

Then by hand: against the squash-merge fixture (branch deleted), confirm `forge` recovers both iteration tips via `refs/pull/<n>/head`, binds the round-1 comments to iteration 0, and produces a unified diff for each recoverable iteration.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_squash_merged_before_state_recovered` | tip + diff recovered via `refs/pull/<n>/head` when the branch is deleted | The squash-recovery chain silently producing the wrong before-state (S-1, FR-8) |
| `test_comment_bound_to_correct_iteration` | a round-1 comment gets `iteration_index=0`, a round-2 comment `1` | A comment bound to the wrong iteration, corrupting the oracle (FR-12) |
| `test_comment_carries_six_fields` | each inline comment has body/path/line range/role/timestamp/iteration | US-3's six-field capture dropping a field (FR-12) |
| `test_verdicts_ordered_with_state_and_timing` | every review submission listed with `state` + `submitted_at`, ordered | An agent unable to tell a rejected state from an approved one (FR-7) |
| `test_unreachable_iteration_reported_not_fabricated` | a force-pushed, unfetchable tip is marked `recoverable=false`, no diff invented | A fabricated before-state passing as real (FR-8, §4 risk) |
| `test_forge_deterministic_twice` | two fetches on the same recorded payloads produce identical bundles | Nondeterministic ordering breaking FR-10 downstream |

## Acceptance Criteria

- [ ] For a PR with N review rounds, output enumerates N iterations each with its SHAs, a unified diff, and bound comment ids (FR-6)
- [ ] Every review submission listed with `state` and `submittedAt`, ordered (FR-7)
- [ ] A squash-merged PR whose branch was deleted still yields the change tip and diff via `refs/pull/<n>/head` (FR-8)
- [ ] Each inline comment carries all six fields bound to its iteration (FR-12)
- [ ] An unreachable iteration tip is reported unrecoverable, never fabricated (FR-8)
- [ ] Output is deterministic on fixed inputs (FR-10)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Writing the candidate file or any judgment slot — that is `candidate.py` (B-3). This module returns facts only.
- Computing `risk_structural` — that is `riskmap.py` (C-2), called by the `capture` verb (B-3).
- The mechanical triage `survey` already does (blockers, path_roots) — do not duplicate B-1.

## Notes & Gotchas

- **`gh pr view --json reviews` is a dead end (TP-3).** It has no per-line comments. Use `gh api …/pulls/<n>/reviews` and `…/pulls/<n>/comments`; the review *timing* is on the review object, the *inline location* is on the comment object, and binding needs `original_commit_id`.
- **`refs/pull/<n>/head` is a single tip.** A round-1 state later force-pushed away may be GC'd on the forge and unfetchable — this is expected and handled by marking it unrecoverable, not by failing (§4 risk row). Such a PR simply yields fewer datapoints.
- **NFR-7 cost:** count the `gh`/`git` round trips per PR in a test and record the number; the target is a PRD `[TODO]` — do not invent one, but do surface the measured count.
- **Recorded payloads, not live `gh`:** CI replays `gh` from saved JSON, because hitting GitHub is slow, authed, and non-deterministic (§12).

## Dependencies

**Blocked by:** [A-2](A-2-build-cross-platform-gitcmd-wrapper.md)
**Blocks:** [B-3](B-3-build-candidate-py-and-wire-capture-verb.md), [B-4](B-4-build-diffspan-and-bind-comments-to-iterations.md)
