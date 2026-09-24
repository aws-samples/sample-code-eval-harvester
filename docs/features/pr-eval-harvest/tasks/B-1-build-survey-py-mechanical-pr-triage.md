---
id: B-1
title: "[Forge] Build survey.py — mechanical PR triage — with a golden-file test"
feature: pr-eval-harvest
workstream: Forge capture
status: done
complexity: M
implements: [FR-5, FR-10, FR-11, FR-2, FR-8]
user_story: US-2
blocked_by: [A-2]
blocks: []
---

# B-1: [Forge] Build survey.py — mechanical PR triage — with a golden-file test

## Context

`survey` is the triage verb: it reports, per PR, the mechanical facts that decide whether it is
harvestable — SHAs, changed paths, review rounds, blockers — and never a quality label, and it
refuses outright if the repo does not review its PRs (FR-11). This task builds that logic as a
package module from the spec below; the output is pinned to a committed golden payload so any drift
in a key, a range, or a field is caught by a test.

**North star:** the agent can see which of the repo's PRs are worth mining, fast, without a label
being invented for it.
**Implements:** FR-5, FR-10, FR-11  ·  **User story:** US-2

## Design References

- **Tech plan:** `../tech-plan.md` §9, the `eval-harvest survey` contract — flags, stdout shape
  `{repo, mainline_commits, prs:[…]}`, the `no-review-iteration` refusal, determinism
- **Tech plan:** §6.1 module table, `survey.py` row
- **A-2** (`gitcmd.py`) — the cross-platform `git`/`gh` chokepoint every call here routes through

## What To Build

Build `src/eval_harvest/survey.py` as a stateless `Survey` namespace class. Every `git`/`gh` call
routes through `GitCommandRunner` (A-2) — there is no inline subprocess here.

**The one bulk `gh` query.** Fetch PR metadata with a single `gh pr list --json <fields>` call
asking for: `number, title, body, state, createdAt, closedAt, mergedAt, mergeCommit, headRefName,
headRefOid, baseRefName, baseRefOid, additions, deletions, changedFiles, labels, reviews, url,
author`. One query per page, never one per PR.

**The mainline pass.** One `git log --first-parent --format=<RS>%H<FS>%at<FS>%s<FS>%b<FS>
--name-only <branch>` call parses into a list of `Commit(sha, at, subject, body, paths)`. Use ASCII
unit/record separators (`\x1f`/`\x1e`) as field/record delimiters — a commit body can contain any
newline or tab but not these, so a body cannot forge a record boundary. Skip blank or truncated
(<5-field) records. One pass, not one per PR: the follow-up scan is an in-memory join between path
sets.

**The revert/repair index (`RepairIndex`).** Built once from the mainline commits and queried per
PR. Index every revert by the two independent ways it names its target, because either can be
absent: the subject form ``Revert "<subject>"`` (what the GitHub revert button and `git revert`
write) and the body form `This reverts commit <sha>` (survives a subject rewrite). `reverts_of`
returns the sorted-unique set of mainline commits reverting a given subject or sha (prefix-matched,
7–40 hex).

**Per-PR facts (`survey_one`).** For each PR emit a dict in a **fixed key order** (the golden-file
contract) with these fields, all mechanical:
- Identity/timing: `number, title, url, state, author, created_at, merged_at, closed_at`.
- Range/seal: `integration_revision` (the `mergeCommit` oid), `integration_parents`,
  `integration_is_true_merge` (≥2 parents), `change_tip` (the fetched `pr/<n>` head if present,
  else `headRefOid`), `change_tip_ref` (`pr/<n>` or ``""``), `change_base` (the merge-base if a tip
  was recovered, else the recorded base), `recorded_base` (`baseRefOid`),
  `tip_is_ancestor_of_integration`, `branch_commits`.
- Size/shape: `changed_paths`, `n_changed` (the integrated path count, falling back to the forge's
  `changedFiles`), `branch_changed_paths`, `forge_changed_files`, `path_count_disagrees`,
  `additions`, `deletions`, `test_paths`, `has_test_change`, `path_roots`.
- Intent/review: `branch`, `issue_candidates`, `labels`, `review_rounds` (the count of `reviews`),
  `reviewers` (sorted-unique review authors).
- Signals: `reverted_by`, `followup_fixes`, `closed_unmerged`.
- The one derived field: `blockers`.

**`blockers` (derived from facts, never taste):** `no-integration-commit` (merged but no merge
commit), `no-pull-head-ref` (no recoverable tip), `no-changed-paths` (merged but empty integrated
diff), `no-test-change` (no path matches a test marker), `squash-without-change-tip` (single-parent
integration and no recoverable tip).

**Supporting scans:**
- `TEST_MARKERS` — a deliberately broad set of path fragments (`/test/`, `/tests/`, `/spec/`,
  `/__tests__/`, `test_`, `_test.`, `.test.`, `.spec.`, `Test.java`, `conftest.py`). Over-reporting
  a test costs one glance; under-reporting hides the only PRs that can carry hidden tests.
- `path_roots(paths, depth=2)` — the sorted distinct leading path segments (literal, no glob), so a
  curator can see a monorepo's real roots (`cdk/src/`) rather than assume root-anchored rules.
- `issue_candidates(title, body, branch)` — `#<n>` in title/body plus a leading number in a branch
  like `fix/789-…`, sorted numerically.
- `followups(...)` — repair-shaped (`^(fix|hotfix|revert|patch)…`) mainline commits touching a
  changed path within a window after the integration time, excluding the change's own commit. A
  signal, not a verdict.

**Payload assembly.** `{repo, mainline_commits, prs:[…]}` with rows sorted by PR number descending;
`repo` echoed verbatim (`args.repo or ""`, so `--from-json` with no `--repo` yields the empty
string). Render `--json` as `json.dumps(payload, indent=2)` plus a trailing newline.

**Wire the `survey` verb into the dispatcher (A-1):** `--clone <dir> (--repo <org/name> |
--from-json <file>) [--state ...] [--limit N] [--branch main] [--followup-days 14] [--summary]
[--cache <file>]`. The top-level `--json` selects the machine payload; `--summary` names the human
table.

**The pull-head fetch (online path).** A plain `git clone` does not create `refs/remotes/pr/*`, and
every per-PR fact that needs the recovered tip (`change_tip`, the `no-pull-head-ref` blocker) reads
that namespace — so on the online path (`--repo`, a clone available) `survey` fetches
`PULL_HEAD_REFSPEC = "+refs/pull/*/head:refs/remotes/pr/*"` once, before enumerating, and reports how
many `refs/remotes/pr/*` refs resulted (silence on a slow bulk fetch reads as a hang). Route it
through `Forge.fetch_refs(clone, refspec, remote=…)` (a shared primitive `forge.py` also uses for the
single-PR fetch — one fetch, one owner) and pick the remote via `Survey.default_remote` (`origin` if
present, else the first) so any printed remedy names the remote that actually exists. Add `--no-fetch`
for an already-fetched clone or a mirror; **the default is to fetch — the default must be the one that
works.** Never fetch on the offline `--from-json` path (NFR-2): it is a pure re-render and must not
touch the network. A fetch that cannot reach the remote refuses `pull-head-fetch-failed` (exit 3) with
git's own stderr and the `--no-fetch` escape, never a traceback.

**The two refusals.** (a) FR-11 `no-review-iteration` (exit 3): when the payload has PRs but **0**
went through review (all `review_rounds == 0`), `offending` is the reviewed-vs-merged count (e.g.
"0 of 214 merged PRs had review rounds"), `next` = "pick a repo whose PRs go through review".
(b) `no-harvestable-pr` (exit 3, **online path only**): when every enumerated PR carries at least one
blocker, `offending` names the dominant blocker and its count ("all N pull request(s) are blocked; the
most common blocker is <name> (M of N)"), `next` is the remedy for that blocker (`Survey.dominant_blocker_remedy`).
A survey with at least one clean PR keeps exiting 0. The offline `--from-json` path stays exit 0 — its
histogram and `--json` `blockers` object carry the same remedy information, so a saved payload can
always be re-rendered.

**The blocker histogram + remedies.** Whenever any PR is blocked, print a `blocked (N)` section
beneath the human table — one line per blocker name, its count, and a one-line remedy
(`_STATIC_BLOCKER_REMEDIES` for the fixed ones; the fetch form with the live remote for
`no-pull-head-ref`/`squash-without-change-tip`). Do **not** print it when nothing is blocked.
`render_summary`'s table lists only the clean (unblocked) rows; the histogram is a separate section
beneath it, not a replacement.

**Methodology.** Document `--no-fetch` and the fetch in `man/eval-harvest-survey.md` with one
methodology sentence (a plain clone has no `refs/pull/*/head`, so `survey` fetches it; harvesting is
impossible without it); mirror it in `VERB_METHODOLOGY` and pin it via `METHODOLOGY_PHRASES` in
`test_help_methodology.py`.

Keep every output a mechanical fact; do not add a "good"/"bad" field (FR-5).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/survey.py` | Create | The `Survey` namespace class, calling `gitcmd.py`; the fetch, histogram, `render_summary`/`render_json` |
| `src/eval_harvest/forge.py` | Modify | A shared `fetch_refs(clone, refspec, remote=)` primitive the bulk pull-head fetch and the single-PR fetch (B-2) both use |
| `src/eval_harvest/gitcmd.py` | Modify | `git_with_stderr` (3-tuple) so a failed fetch's reason survives to the refusal; `git` delegates to it |
| `src/eval_harvest/cli.py` | Modify | Register `survey`; `--no-fetch`; the fetch, `no-review-iteration`/`no-harvestable-pr`/`pull-head-fetch-failed` refusals; `VERB_METHODOLOGY` |
| `man/eval-harvest-survey.md` | Modify | `--no-fetch` in SYNOPSIS, the fetch + `no-harvestable-pr` methodology, EXIT STATUS |
| `tests/test_survey.py` | Create | Golden-file, determinism, refusal, no-label, fetch, histogram tests (S-13) |
| `tests/test_help_methodology.py` | Modify | Pin `refs/pull/*/head`, `--no-fetch`, `no-harvestable-pr` for `survey` |
| `tests/fixtures/gh/survey/` | Create | Recorded `gh pr list` payload templates + the frozen `expected.json` golden (carries the `blockers` object) |

## Schemas & Contracts

**Stdout (`--json`):**
```json
{ "repo": "org/name", "mainline_commits": 512,
  "prs": [ { "number": 1234, "state": "MERGED", "integration_revision": "…", "change_tip": "…",
             "change_base": "…", "changed_paths": ["…"], "review_rounds": 2, "blockers": [], "…": "…" } ] }
```
The per-PR object is exactly the fixed-key-order dict specified above. The `--json` payload also
carries a top-level `blockers` object — `{"<name>": {"count": n, "remedy": "…"}}` — so a driver can
act on an all-blocked survey. **Refusals (exit 3):** `no-review-iteration` (FR-11), and, on the
online path, `no-harvestable-pr` (every PR blocked) and `pull-head-fetch-failed` (the fetch could not
reach the remote).

**Migration:** none.
**Backward compatibility:** nothing exists to break; the golden-file test is the contract that keeps
the payload byte-stable.

## How To Verify

```bash
mise run test -- tests/test_survey.py
mise run lint
mise run typecheck
# the golden comparison, by hand:
uv run eval-harvest survey --clone <fixture-clone> --from-json tests/fixtures/gh/survey/reviewed.json --json > /tmp/b.json
diff tests/fixtures/gh/survey/expected.json /tmp/b.json   # must be empty (deterministic fixture SHAs)
```

Then by hand: run `survey` against the never-reviewed payload and confirm it refuses
`no-review-iteration` with the count and exits `3`. Against a fresh online clone, confirm `survey`
fetches the pull refs, reports the ref count, and lists harvestable PRs; run `survey --no-fetch` on a
fresh clone and confirm exit 3 `no-harvestable-pr` whose `next:` command, pasted verbatim, makes the
next `--no-fetch` run succeed.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_survey_matches_golden` | `survey --json` output == the committed `expected.json` on the reviewed fixture | Triage logic drifting — a reordered key, a different range, a lost field (S-13, FR-5) |
| `test_survey_deterministic_twice` | two runs on fixed inputs are byte-identical | Nondeterministic dict/set ordering (FR-10) |
| `test_refuse_no_review_iteration` | a payload with 0 reviewed PRs refuses `no-review-iteration` with the count, exit 3 | A worthless empty dataset shipped instead of an honest refusal (S-5, FR-11) |
| `test_survey_reports_no_quality_label` | no output field asserts good/bad | Judgment smuggled into a script where no provenance attaches (FR-5) |
| `test_survey_fetches_pull_head_refs_on_online_path` | the bulk refspec is invoked once before enumeration | A fresh clone reporting nothing harvestable and exiting 0 |
| `test_survey_from_json_never_fetches` | the offline path issues no `git fetch`/network call | Breaking NFR-2 and making re-rendering require the network |
| `test_survey_no_fetch_flag_skips_the_fetch` | `--no-fetch` suppresses the fetch | An unavoidable slow fetch against a mirror |
| `test_survey_refuses_when_every_pr_is_blocked` | online, exit 3 `no-harvestable-pr` with the dominant blocker and a runnable `next:` | Exit 0 on an empty table — silence read as "no review history" |
| `test_survey_exits_zero_with_one_clean_pr` | one harvestable PR keeps exit 0 | Over-refusal on a mostly-blocked repo |
| `test_survey_prints_blocker_histogram_with_remedies` | counts and remedies appear when any PR is blocked | Blockers computed per PR and never shown in aggregate |
| `test_survey_refuses_when_the_fetch_fails` | a failing fetch refuses `pull-head-fetch-failed` with git's stderr, not a traceback | A crash on a repo with no remote |
| `test_survey_json_carries_blocker_counts` | the documented `blockers` object rides `--json` | A driver unable to diagnose an empty survey |

## Acceptance Criteria

- [ ] `survey --json` output is byte-identical to the committed golden on the reviewed fixture
- [ ] `survey` run twice on fixed inputs is byte-identical (FR-10)
- [ ] A never-reviewed repo refuses `no-review-iteration` with the reviewed-vs-merged count (exit 3, FR-11)
- [ ] `survey --repo …` fetches `+refs/pull/*/head:refs/remotes/pr/*` by default, reports the ref count, and `--no-fetch` skips it
- [ ] `--from-json` performs no `git fetch`/network call (NFR-2)
- [ ] An online survey where every PR is blocked exits 3 `no-harvestable-pr` with a runnable `next:`; one clean PR keeps exit 0
- [ ] A blocker histogram with per-blocker remedies prints whenever any PR is blocked; the `--json` payload carries the `blockers` object
- [ ] A failed fetch refuses `pull-head-fetch-failed` with git's stderr rather than raising
- [ ] The methodology sentence appears in both `survey --help` and `man/eval-harvest-survey.md`, pinned by the phrase test
- [ ] No output field is a quality label (FR-5)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Deep per-PR capture (iterations, inline comments, verdicts) — that is `forge.py` (B-2). `survey`
  reports only the `review_rounds` count.

## Notes & Gotchas

- **The first-parent diff trap:** the integrated effect is `parents[0]..integration`, never
  `merge-base..integration` — on a squash-merging repo the mainline is linear, so `merge-base..`
  also contains every unrelated PR merged in between, inflating the changed-file count wildly. Diff
  against the first parent; it agrees with the forge's own
  `changedFiles` count.
- **The golden is byte-stable because the fixture SHAs are deterministic:** `RepoBuilder` pins the
  author/committer identity *and* dates, so the squash-merged fixture hashes to the same SHAs every
  run. That is what lets a raw-bytes golden stand in for an external comparison. Regenerate it by
  running the verb over the reviewed fixture with `--json` and writing stdout to `expected.json`.
- **Byte-stability is strict:** JSON `indent=2`, the fixed key order, sort-by-number-descending, and
  the trailing newline must all hold or `test_survey_matches_golden` fails.

## Dependencies

**Blocked by:** [A-2](A-2-build-cross-platform-gitcmd-wrapper.md)
**Blocks:** nothing
