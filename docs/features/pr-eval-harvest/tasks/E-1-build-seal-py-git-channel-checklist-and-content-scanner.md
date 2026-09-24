---
id: E-1
title: "[Verify] Build seal.py: git-channel checklist + new content scanner"
feature: pr-eval-harvest
workstream: Verification
complexity: L
implements: [TP-5, FR-35, FR-36, FR-40, NFR-6]
user_story: US-7
status: todo
blocked_by: [A-1]
blocks: [E-2]
---

# E-1: [Verify] Build seal.py: git-channel checklist + new content scanner

## Context

A leaked answer is the expensive silent failure: if the fix, review discussion, PR number, or follow-up commit is reachable from anything the evaluated agent can read, every score looks fine and measures nothing. This task builds the two-halves answer-absence scan: (a) the **git-channel checklist** — the list of places a repo leaks its own future while looking clean; and (b) a **new content scanner** over agent-visible files (grep `instruction.md`, `change.patch`, any agent-mounted tree for the PR number, review text, and answer tokens), guarded by a planted control token so an empty result proves the scan ran (FR-36/NFR-6). `verify` (E-2) drives it.

**North star:** an unsupervised run never produces a dataset that looks fine and measures nothing.
**Implements:** TP-5, FR-35, FR-36, FR-40, NFR-6  ·  **User story:** US-7

## Design References

- **Tech plan:** `../tech-plan.md` §2.3 TP-5 — the two halves: the git-channel checklist and the **new** content scanner
- **Tech plan:** §2.1 FR-35 (both scans), FR-36 (control token), FR-40 (`solution/` in the "must be absent from agent-visible content" corpus)
- **Tech plan:** §7.2 — the control-token flow; §7.3 — the runtime-absent degrade for the materialized-seal variant
- **The git-leak channels to check**: `objects/info/alternates`, `packed-refs`, commit-graph, `refs/replace`, reflog-outside-reachability, `include.path`/`includeif`/`core.hookspath`, grafts, shallow, FETCH_HEAD/ORIG_HEAD, bare `*.git/` in the tree

## What To Build

1. Create `src/eval_harvest/seal.py`. Build `check_repo` (the git-channel checklist over the channels listed above) — self-contained, stdlib-only. `seal.py` depends only on A-1, so it carries its own `Seal.git` subprocess wrapper (argv lists, never a shell string; prompts disabled) rather than reaching for `gitcmd.py`. Use the `Report`/`Finding` shape and its `{ok, findings:[{check,passed,detail,severity}]}` JSON (E-2 maps it to the FR-2 four-field refusal).
2. Build the **canary** mechanism: `check_repo(repo, canaries)` asserts each token appears nowhere under the path, and emits a `canary-absent:<token>` finding — this is the control-token half (FR-36/NFR-6).
3. Write the **new content scanner** `scan_agent_visible(task_dir, tokens, control_token) -> Report`: grep every agent-visible file (`instruction.md`, `change.patch`, any agent-mounted tree) for the PR number, review text, and answer tokens (FR-35). The scanner owns the honesty half: it is handed a `control_token` and, if that token is **not** present in the scanned corpus, refuses `scan-did-not-run` — an empty result and a broken scan are otherwise identical (FR-36/NFR-6). The caller (`verify`, E-2) plants the control token into the corpus before calling, which is what keeps the guard *exercisable*: E-3 removes it and watches `scan-did-not-run` fire. A self-planting scanner that always finds its own token could never be watched to fail.
4. Ensure the content scan's "must be absent from agent-visible content" corpus **includes** `solution/solve.sh`, `tests/oracle.json`, `judge.toml`, and review text (FR-40) — these are verifier-only and must never appear where the agent reads.
5. Make no network call — reads local files and the local `.git` only (NFR-2).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/seal.py` | Create | Git-channel checklist + new content scanner + control token |
| `tests/test_seal.py` | Create | Cases from the test table (fixture repos with planted leaks) |
| `tests/fixtures/repo_builder.py` | Create | A `RepoBuilder` that builds real single-commit fixture repos and plants a leak in a named channel (alternates, packed-refs, commit-graph, refs/replace, unreachable reflog entry, include.path, bare repo); B-2 extends it with PR-iteration support |
| `tests/fixtures/__init__.py` | Create | Makes the fixtures package importable |

## Schemas & Contracts

**API:**
```
check_repo(repo: Path, canaries: tuple[str, ...]) -> Report          # git-channel checklist
scan_agent_visible(task_dir: Path, tokens: list[str], control_token: str) -> Report  # NEW content scan
```
**`Report`/`Finding`:** `Finding(check, passed, detail, severity)`; `Report.failures`; JSON `{ok, repo, checks, findings:[{check,passed,detail,severity}]}`.

**Migration:** none.
**Backward compatibility:** none — new module; nothing under `src/` imports outside the package.

## How To Verify

```bash
mise run test -- tests/test_seal.py
mise run lint
mise run typecheck
```

Then by hand: build a sealed fixture tree, plant `objects/info/alternates` pointing at the source repo, run `check_repo` — it flags it even though `git log --all` shows one commit; remove it, clean. Plant "#1234" in `instruction.md`, run `scan_agent_visible` — it flags the file:line.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_alternates_channel_leak_caught` | `objects/info/alternates` pointing at the source is flagged | A tree that greps clean while `git show` prints the answer (S-3, FR-35) |
| `test_packed_refs_and_commit_graph_leaks_caught` | `packed-refs`, commit-graph, `refs/replace`, reflog-outside-reachability, `include.path` each flagged | Each non-obvious git channel the checklist covers (S-3, FR-35) |
| `test_bare_repo_in_tree_caught` | a bare `answerkey.git/` in the tree is flagged | A whole second repo smuggled into the task tree (S-3) |
| `test_content_scan_finds_pr_number_in_instruction` | "#1234" in `instruction.md` is flagged with file:line | The PR number leaking into agent-visible content (S-2, FR-35) |
| `test_solution_text_in_agent_tree_caught` | oracle/solution text present in the agent-visible tree is flagged | The reference answer leaking to the evaluated agent (FR-40) |
| `test_missing_control_token_refuses_scan_did_not_run` | removing the planted control token makes the scan refuse `scan-did-not-run` | An empty result mistaken for a clean one (S-4, FR-36/NFR-6) |
| `test_seal_makes_no_network_call` | `check_repo`/`scan_agent_visible` issue no `gh`/remote call | The sealing path reaching the forge (NFR-2) |

## Acceptance Criteria

- [ ] The git-channel checklist catches every forbidden git channel + the config/reflog/alternates cases (FR-35)
- [ ] The content scanner flags the PR number / review text / answer tokens in agent-visible files with file:line (FR-35)
- [ ] `solution/`, oracle, and judge text are in the "must be absent from agent-visible content" corpus (FR-40)
- [ ] Removing the control token makes the scan refuse `scan-did-not-run` (FR-36/NFR-6)
- [ ] No network call (NFR-2)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- The structural checks (base exists, patch applies, findings reference real lines, rubric coherent) — that is `verify.py` (E-2).
- The FR-2 refusal *formatting* — E-2 maps this module's `Report` into the four-field shape.
- The container-build materialized-seal variant — degrades to skipped-with-reason (exit 5); gated on the Lambda MicroVMs `[TODO]` (§7.3). Build the static-file scan here; leave a documented hook for the container variant.

## Notes & Gotchas

- **The git-channel half and the content-scan half are two different scans (TP-5).** Do not make the git-channel repo-scanner also do the content grep — they are different corpora (a `.git` tree vs. agent-visible files).
- **The control token is the honesty guard (NFR-6, CLAUDE.md "a scanner that finds nothing must prove it scanned something").** Require it, refuse without it (the caller plants it into the corpus). This is one of the FR-37 watched-to-fail guards (E-3 exercises it via S-4).
- **Object-id-shaped tokens (SHAs) must be caught in EDITMSG and other stray files** — a follow-up-commit SHA in a stray file is a real leak.
- **Reflog is about reachability, not volume:** flag reflog entries naming commits outside the reachable set, not the count.

## Dependencies

**Blocked by:** [A-1](A-1-scaffold-uv-package-cli-dispatch-refusal-helper.md)
**Blocks:** [E-2](E-2-build-verify-py-structural-checks-absence-scan-control-token.md)

