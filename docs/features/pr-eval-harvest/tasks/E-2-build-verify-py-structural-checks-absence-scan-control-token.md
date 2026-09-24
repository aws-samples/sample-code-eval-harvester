---
id: E-2
title: "[Verify] Build verify.py: structural checks + absence scan + control token"
feature: pr-eval-harvest
workstream: Verification
status: todo
complexity: L
implements: [FR-34, FR-35, FR-36, FR-39, FR-2, FR-15, NFR-6]
user_story: US-7
blocked_by: [E-1, D-3]
blocks: [E-3, E-4]
---

# E-2: [Verify] Build verify.py: structural checks + absence scan + control token

## Context

`verify` is the guarantee that replaces the absent human. Per datapoint it checks the well-formedness preconditions (base commit exists, patch applies at it, oracle findings reference real changed lines, rubric coherent) and drives the E-1 answer-absence scan (git channels + agent-visible content, control-token-guarded). Every failure is reported in the FR-2 four-field shape so the unsupervised agent can fix and retry. This is the verb `emit` calls before writing (wired in E-4) and the `verify <task-dir>` command the agent runs directly.

**North star:** the CLl tells the agent when a datapoint is broken, so an unsupervised run doesn't ship a worthless dataset.
**Implements:** FR-34, FR-35, FR-36, FR-39, FR-2, FR-15, NFR-6  ·  **User story:** US-7

## Design References

- **Tech plan:** `../tech-plan.md` §9, the `eval-harvest verify` contract — reads, checks, stdout, exit codes (0 pass / 4 fail / 5 unresolved)
- **Tech plan:** §2.1 FR-34 (structural checks), FR-35 (both scans), FR-36 (control token), FR-39 (actionable failures)
- **Tech plan:** §7.2 — verify inside emit; §7.3 — the runtime-absent degrade (exit 5, never a silent pass)
- **Contract (E-1):** `seal.check_repo` + `scan_agent_visible` return a `Report`/`Finding` with the `{ok, repo, checks, findings:[{check,passed,detail,severity}]}` JSON shape; map it into the FR-2 four-field refusal.
- Depends on **E-1** (`seal.check_repo` + `scan_agent_visible`) and **D-3** (the emitted task directory shape it validates)

## What To Build

1. Create `src/eval_harvest/verify.py` and wire the `verify` verb (A-1 dispatcher): `verify <task-dir> [--clone <dir>] [--json]`.
2. **Structural checks (FR-34), each producing its own specific failure:**
   - base commit exists in the local clone (`git cat-file -e` via `gitcmd.py`);
   - `change.patch` applies cleanly at the base — `git apply --check` against the sealed base, through a throwaway `GIT_INDEX_FILE` so the clone is never mutated. **Resolve the patch path to absolute** before handing it to `git -C <clone> apply`: git's working directory is the clone, so a caller-relative task path (the shape the README's `verify` step uses) would otherwise resolve against the clone, fail to open, and report a sound patch as `patch-does-not-apply`. Surface a "git could not open the patch file" read failure *distinctly* from a genuine non-apply — a file git cannot read is an internal error in the check, never a verdict about the diff. Apply the patch **exactly as shipped**: `change.patch` is stored newline-terminated at the write site, so `verify` must never normalize or rewrite it (e.g. copying it into a temp file with a trailing newline appended) to coax it into applying — a malformed or unterminated patch is a real defect and must surface as `patch-does-not-apply`, not be silently repaired by the checker;
   - every oracle finding references a file+line that exists in the change (parse `tests/oracle.json`, cross-check against the patch's changed hunks);
   - the rubric is coherent (present, versioned, non-empty, referenced by the datapoint).
   `verify` reads the emitted task directory, not the candidate (§9 lists its reads as the task files plus the clone), so finding-coherence is enforced at the artefact level over `tests/oracle.json`; the candidate's slot-coherence (FR-15 evidence present) is enforced earlier by `emit` before assembly.
3. **Absence scan (FR-35):** call `seal.check_repo` (git channels, over a materialized sealed tree when one is supplied) and `seal.scan_agent_visible` (content). Plant the control token into a copy of the scanned corpus first, and refuse `scan-did-not-run` if the scanner does not see it (FR-36/NFR-6).
   - **A token that predates the change is not a leak (base-tree suppression).** A `#605`-style token can appear in a source file *at the base commit*, months before the PR existed — it is prose any checkout carries, not the reviewer's conclusion. When a clone is available, build a base-tree oracle (`git grep --fixed-strings <token> <base_sha> -- <changed-paths>`, no worktree) and suppress a hit the oracle reports present at the base commit; record every suppression (token + base sha) so it is never silent, and report the scan arithmetic (scanned / hit / suppressed). Without a clone a hit still fails `answer-present`, but the refusal names the file and line and its `next` is a runnable `verify --clone …` (or `emit … --override answer-present`). The control token is **never** suppressible — special-case it before the oracle is consulted, so `scan-did-not-run` keeps firing regardless.
4. **Report (FR-39/FR-2):** map every failed `Finding` into the four-field shape — `check`, `datapoint`, `offending` (file:line where possible), `next` (what to do). Aggregate all failures (do not stop at the first). `--json` emits `{ok:false, failures:[...]}`.
5. **Exit codes (§9):** 0 pass · 4 a check failed · 5 a check was unresolved (runtime absent — the materialized-seal variant reports `skipped: no runtime …` and counts *unresolved*, never pass; §7.3). The standalone `verify` command never materializes a seal (the git-channel checklist runs against a sealing container built when the datapoint is *executed*, in the `eval/` harness), so `git-channel-absence` is unresolved through the CLI by design. Its unresolved message must be honest: it must **not** promise that setting `MICROVM_*` or providing Docker will make *this command* run the check — neither input is consumed here, and an operator who follows that advice gets the identical exit 5.
6. Make no network call (NFR-2) — reads the task dir + local clone only.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/verify.py` | Create | Structural checks + scan driver + FR-2 reporting |
| `src/eval_harvest/cli.py` | Modify | Register the `verify` subcommand |
| `tests/test_verify.py` | Create | Cases from the test table (S-12 four broken candidates + scan) |

Imports `seal.py` (E-1), `gitcmd.py` (A-2), and `diffspan.py` (B-4, the single new-side hunk-span parser the finding-line check reuses).

## Schemas & Contracts

**Refusal `check` names (FR-34/FR-35), one per failure:** `base-missing` · `patch-does-not-apply` · `finding-line-absent` · `rubric-incoherent` · `answer-present` (a content or git-channel leak) · `scan-did-not-run` (control token absent). `git-channel-absence` is reported unresolved through the CLI by design (§7.3).
**Canonical check names, exported (`CHECK_NAMES`):** the five check *groups* `verify` runs — `base-and-patch`, `finding-lines-present`, `rubric-coherent`, `content-absence`, `git-channel-absence`. `verify_task` returns the subset that ran with no refusal as the `passed` set, so `emit` can report which checks ran, passed, and were skipped without decoding the ad-hoc refusal names.
**Answer-absence arithmetic:** the content-scan report carries `scanned / hits / suppressed` counts and, per suppressed token, the base sha that cleared it.
**Output (`--json`):** `{ok, failures:[{check, datapoint, offending, next}], unresolved:[…same shape…], answer_absence:{scanned, hits, suppressed:[{token, location, base_commit}]}}` (the FR-2 refusal shape plus the scan arithmetic, §9).
**Exit:** `0` pass · `4` a check failed · `5` unresolved (runtime absent).

**Migration:** none. **Backward compatibility:** none — new verb; also called internally by `emit` (E-4 wiring).

## How To Verify

```bash
mise run test -- tests/test_verify.py
mise run lint
mise run typecheck
```

Then by hand: take an emitted task, break each precondition in turn (delete the base, corrupt the patch, point a finding at a missing line, blank the rubric), run `verify`, confirm each yields its own specific `check`/`offending`/`next` and exit 4; restore, confirm exit 0.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_missing_base_commit_specific_failure` | a datapoint whose base is absent fails `base-missing` with its own message | A datapoint that cannot be built passing as valid (S-12, FR-34) |
| `test_patch_does_not_apply_specific_failure` | a patch that won't apply at the base fails `patch-does-not-apply` | A patch/base mismatch shipped (S-12, FR-34) |
| `test_verify_applies_the_shipped_patch_unmodified` | `verify` applies `change.patch` exactly as stored (no normalizing copy); a malformed/unterminated patch surfaces as `patch-does-not-apply` | A checker that rewrites the patch to make it apply, greening a datapoint whose shipped bytes the image build then rejects (FR-34) |
| `test_finding_on_missing_line_specific_failure` | an oracle finding on a line not in the change fails `finding-line-absent` | An ungradeable oracle finding (S-12, FR-34) |
| `test_incoherent_rubric_specific_failure` | a blank/unversioned rubric fails `rubric-incoherent` | A datapoint graded against nothing (S-12, FR-34) |
| `test_leak_in_git_channel_fails_answer_present` | a planted alternates/packed-refs leak fails `answer-present` naming the channel | A leaked answer measuring nothing (S-3, FR-35) |
| `test_missing_control_token_refuses_scan_did_not_run` | control token removed ⇒ `scan-did-not-run`, not clean | An empty scan mistaken for a clean one (S-4, FR-36/NFR-6) |
| `test_every_failure_has_four_fields_and_distinct_exit` | each refusal carries check/datapoint/offending/next; exit 4 vs 5 distinct | An agent-unactionable error stalling the loop (S-17, FR-2/FR-39) |
| `test_runtime_absent_reports_unresolved_exit_5` | the container-seal variant with no runtime reports `skipped: no runtime` and exit 5 | "skipped" misread as "passed" (§7.3, S-10 gating) |
| `test_verify_relative_task_path_does_not_misfire_patch_applies` | a **relative** task path + absolute `--clone`, run from an arbitrary CWD, passes `patch-applies` on a sound datapoint; a genuinely non-applying patch still fails | A sound patch reported `patch-does-not-apply` because git resolved a relative path against the clone |
| `test_git_channel_unresolved_guidance_stays_honest` | the unresolved `git-channel-absence` message does not tell the operator to set `MICROVM_*`/Docker to make `verify` run it | Guidance promising an input the command never consumes |
| `test_answer_present_suppressed_when_token_exists_at_base` | a token present at the base commit does not fail `answer-present` when a clone is supplied; the suppression is reported with the base sha | A pre-existing token costing a sound datapoint |
| `test_answer_present_fires_when_token_only_in_the_change` | the same token introduced *by the change* still fails, clone or not | A blanket exemption silently disabling the leak check |
| `test_control_token_is_never_suppressed` | a base commit containing the control token still makes the scan prove it ran | Suppression disabling `scan-did-not-run` |

## Acceptance Criteria

- [ ] Each structural precondition fails with its own specific `check`/`offending`/`next` (FR-34, FR-39)
- [ ] The git-channel and content scans run; a leak in either fails `answer-present` naming file/line (FR-35)
- [ ] A missing control token refuses `scan-did-not-run` (FR-36/NFR-6)
- [ ] Every refusal carries all four fields; exit 4 (fail) vs 5 (unresolved) are distinct (FR-2, §7.3)
- [ ] The materialized-seal variant with no runtime reports unresolved (exit 5), never a silent pass
- [ ] `verify` with a relative task path and an absolute `--clone` passes `patch-applies` on a sound datapoint; a genuine non-apply still fails (patch path resolved absolute)
- [ ] The unresolved `git-channel-absence` guidance does not promise that `MICROVM_*`/Docker will let `verify` run the check
- [ ] A token present at the base commit is suppressed (with a clone) and reported with the base sha; a token introduced by the change still fails; the control token is never suppressible
- [ ] No network call (NFR-2)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- The refuse-before-write wiring in `emit` and the recorded `--override` — that is E-4 (FR-38).
- The guard-verification artefact listing each check→test — that is E-3 (FR-37).
- The container build itself — degrades to unresolved (exit 5); gated on the Lambda MicroVMs `[TODO]`.

## Notes & Gotchas

- **Aggregate failures, don't stop at the first (FR-39).** The agent fixes faster with the full list; `seal.Report` already collects findings — mirror that.
- **`offending` must be actionable:** file:line for a content leak, the channel name for a git leak, the finding index for a bad oracle line. "verification failed" is not actionable (the failure mode FR-2 exists to prevent).
- **Runtime-absent ≠ pass (§7.3, §4 risk row 6).** The single most dangerous misread is "skipped" as "passed" — exit 5 and the summary must count it *unresolved*.
- **`verify` is offline (NFR-2, ADR-4).** It reads the task dir and local clone; no `gh`, no `git fetch`, no remote op. E-4 adds the test that proves it with the network stubbed to fail.
- **Hand git an absolute patch path.** `git -C <clone> apply` resolves a relative patch path against the clone, not the caller's CWD — the one place a caller path is handed to git. Resolve it at the git call so every caller benefits, and keep a git-can't-open-the-file error separate from a real non-apply.
- **Base-tree suppression must be narrow.** "Exists somewhere at the base commit" is the rule; "looks like a `#\d+` reference" is not — a regex-shaped exemption would blow a hole in the check the first time a reviewer's conclusion cites an issue number. Use `git grep` against the base *object* (no worktree, cannot be confused by untracked files), scope it to the change's touched paths, and exempt the control token *before* the oracle is consulted so `scan-did-not-run` never stops firing.

## Dependencies

**Blocked by:** [E-1](E-1-build-seal-py-git-channel-checklist-and-content-scanner.md), [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md)
**Blocks:** [E-3](E-3-guard-verification-suite.md), [E-4](E-4-wire-emit-verify-refuse-before-write-and-nfr2-test.md)

