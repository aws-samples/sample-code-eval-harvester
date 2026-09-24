---
name: execute-local-task-harvest
description: Pick up a task from a checked-in task board under docs/features/<feature>/tasks/, claim it, implement the work, verify it, merge it, and mark it done. Given a feature name instead of a task ID, automatically selects the best unblocked task. The local counterpart to execute-linear-task-harvest.
argument-hint: <task-id or feature-name> [--dry-run]
---

# Execute Local Task

The user provides a task ID or a feature name: $ARGUMENTS

You are a software engineer picking up a task from the board. The board is checked into the repository as markdown files — the ones `create-tasks-locally-harvest` writes. Your job is to read the task, claim it, implement it in a worktree, verify it works, squash it into one clean commit, fast-forward-merge and push it to `main`, and mark it done.

Task state lives in two places that must never disagree: the `status` field in a task file's frontmatter, and that task's row in the board index. Every state change updates both.

## Phase 1: Parse Arguments

Extract from `$ARGUMENTS`:
- **identifier** (required): Either a task ID (e.g. `A-1`, `B-2` — a letter-number pair) or a feature slug (e.g. `user-onboarding`).
- **--dry-run** (optional): Do everything up to the point of changing state or writing code. Report the task you would pick up and the plan you would follow, then stop.

If the identifier is ambiguous or missing, list the directories under `docs/features/` that contain a `tasks/` subdirectory and ask which feature to work on.

## Phase 2: Resolve the Task

### If the identifier is a task ID (e.g. `A-1`):

1. Find the file: `docs/features/*/tasks/<task-id>-*.md`. If more than one feature has a task with that ID, list the matches and ask which one.
2. Read the file's frontmatter. Check `status`:
   - `done` — tell the user it is already complete and stop.
   - `in-progress` — someone or something else claimed it. Run `git log -1 --format='%an, %ar' -- <task-file>` to see who and when, report that, and ask the user whether to continue anyway.
   - `todo` — proceed.
3. Check `blocked_by`. For each blocker, read that task file's `status`. If any blocker is not `done`, tell the user: "`A-2` is blocked by `A-1` (status: todo). Pick a different task or finish the blocker first." Then stop.
4. Proceed to Phase 3.

### If the identifier is a feature name:

1. Read `docs/features/<feature-name>/tasks/README.md` for the board overview and dependency graph.
2. Read the frontmatter of every task file in that directory. Do not trust the index table alone — the files are the source of truth, and a stale index is exactly the failure this check catches. If the index and a task file disagree, say so and trust the file.
3. Filter to candidates: `status: todo` with every entry in `blocked_by` at `status: done`.
4. If no candidates exist:
   - If tasks remain but all are blocked, the dependency graph has a cycle or an unfinished root. Report which tasks are blocked by what and stop — this is a graph problem, not something to work around.
   - If every task is `done`, say the feature is complete.
   - If tasks are `in-progress`, list them and note the board is already being worked.
5. From the candidates, pick the best one:
   - **Most unblocking first:** prefer the task whose `blocks` list transitively unlocks the most downstream work.
   - **Smaller first:** among equals, prefer lower `complexity` (S before M before L) — finishing something and merging it reduces the chance of conflicts.
   - **ID order third:** among equals, take the lowest ID.
6. Present the choice: "I'm picking up **A-1: [Data Layer] Add status column to todos table** (complexity S, unblocks 3 downstream tasks). Proceed?"
7. Wait for confirmation before proceeding.

## Phase 3: Understand the Task

1. **Read the task file in full.** It is written to be self-sufficient: What To Build, Files Affected, Schemas & Contracts, How To Verify, Tests To Write, Acceptance Criteria, Out Of Scope, Notes & Gotchas. Read all of it before touching code — the Notes & Gotchas section exists because someone already hit those traps.

2. **Read the linked design sections.** The Design References section cites specific sections of `../tech-plan.md` and `../prd.md`. Read the cited sections, not the whole documents. If an ADR is cited, read it and treat its decision as settled — do not re-litigate it in the implementation.

3. **Read `CLAUDE.md`** at the project root (and `AGENTS.md`, if the repo has one). Understand the project's conventions, dependency rules, and commands. Where a nested directory has its own `CLAUDE.md`, read that too if the task touches it.

4. **Explore the codebase.** Read every file listed in Files Affected, plus the adjacent code and the existing tests for it. Confirm the task's claims: do the paths exist, does the current schema match what the task says it is, is the pattern it tells you to follow actually there?

5. **If the task's claims do not hold** — a path has moved, a signature has changed, the described current state is wrong — stop. Report the discrepancy to the user. Do not silently reinterpret the task; a task file that is wrong about the present is probably wrong about the target too.

6. **If the task is underspecified**, do not guess. Report what is missing and stop. The task file was supposed to make the decisions; if it did not, that gap goes back to the user, not into the code.

If `--dry-run` was passed, stop here and report the task and your implementation plan.

## Phase 4: Claim the Task

The claim has to be visible on `main` before you start, so a parallel agent does not pick up the same task.

1. **Check the working tree is clean.** Run `git status --short`. If it is dirty, stop and tell the user — do not claim a task on top of uncommitted work.

2. **Set `status: in-progress`** in the task file's frontmatter, and update that task's row in `tasks/README.md` to match.

3. **Commit the claim directly to main:**
   ```bash
   git add docs/features/<feature>/tasks/
   git commit -m "chore(tasks): claim <task-id>"
   ```

4. **Create an isolated worktree** from main, so the claim is already present in it:
   ```bash
   git worktree add ../<slug> -b <slug> main
   ```

   Generate `<slug>` from the task ID and title: strip the `[Workstream]` prefix, lowercase, hyphens for spaces, drop filler words (implement, create, add, the, for) if it runs long, and append the lowercased task ID. Keep it under 50 characters.
   - `[Data Layer] Add status column to todos table` (A-1) → `status-column-todos-a-1`
   - `[Agent] Wire status into the list_todos tool` (C-1) → `wire-status-list-todos-c-1`

   If creation fails because the branch exists, try `git worktree add ../<slug> <slug>`. If that also fails, stop and ask.

5. **All subsequent work happens in the worktree.** Use its absolute path for every read, edit, write, and command for the rest of this task.

6. Tell the user: "Claimed `<task-id>` on main. Created worktree at `../<slug>/` on branch `<slug>`. Working there."

## Phase 5: Implement

Work through What To Build in order, inside the worktree.

- **Match existing patterns.** Where the task names a pattern to follow, follow it exactly. Where it does not, find the nearest equivalent in the codebase and match that.
- **Respect the constraints.** The task's Notes & Gotchas and the tech plan's §3 are limits, not suggestions.
- **Implement the schema exactly as specified.** The Schemas & Contracts section gives the target shape field by field. Do not improve on it — if it is wrong, that is a Phase 3 discrepancy to report, not something to fix in passing.
- **Write the tests from Tests To Write.** Each one should catch the defect its row names. Verify that by making it fail first where you reasonably can: for a guard, break the invariant deliberately, watch the test go red, then revert. A test you have only ever seen pass has not been shown to test anything.
- **Stay inside the task.** Out Of Scope is binding. Do not refactor adjacent code, do not fix unrelated things you notice — note them for the user instead.
- **Run the task's verification commands as you go**, not just at the end.

## Phase 6: Verify

Run the commands from the task's **How To Verify** section, in the order given. These came from `CLAUDE.md` when the task was written; if a command no longer exists or the project's commands have changed, use the current ones from `CLAUDE.md` and note the drift in your report.

Then work through this pipeline **in order** — tests first, then the built CLI, then end-to-end. Each stage only earns trust once the one before it is green.

1. **Run the test suite** — `mise run test` (or the current command from `CLAUDE.md`). Run the whole suite, not just the tests you added, to catch regressions. This is the fast inner loop; get it green before touching the CLI.

2. **Build the CLI** — *once the project has a CLI to build.* The design doc decides when that exists; until it does, this stage and the next do not apply, and you say so in your report rather than inventing a build. When it does exist, build/install it the way `CLAUDE.md` documents (e.g. `mise run build`, or `uv build` / `uv tool install .`). A build that does not complete cleanly blocks the task — a green unit suite over code that will not build is not "done".

3. **Invoke it end-to-end, where E2E makes sense for this task.** Run the *built* CLI as a user would — real argv, real input fixtures, real exit codes — and assert on what comes back, not on internal state. E2E makes sense when the task changes a command, its flags, its output shape, or a flow a user drives; it does not for a pure internal-layer change with no user-visible surface, and forcing one there is noise. When the task's Tests To Write names E2E cases, they are the spec for this stage. Prefer an E2E test that runs under `mise run test` over a one-off manual invocation, so the check survives; where a manual invocation is the only practical option, record the exact command and its output in your report. When you skip E2E, say which of the two reasons above applies.

4. **Re-read the Acceptance Criteria** and confirm each one. For each, name the passing test, the E2E run, or the concrete observation that satisfies it. Check the boxes in the task file as you confirm them — an unchecked box means unverified.

5. **Run the linter, security scan, and type checker** — `mise run lint` and `mise run typecheck`, or `mise run check` to run lint + format-check + the bandit security scan + typecheck + tests together as the pre-commit gate `CLAUDE.md` requires. Never invoke `python`, `pip`, `ruff`, `mypy`, or `pytest` directly — go through `uv` or `mise` (a project hook will stop you otherwise). This is a dry run of the gate; Phase 7 step 5 runs it again on the final squashed, rebased state as the merge gate.

If a command in the task file or above does not exist in this project — no test suite, no CLI yet, no linter — say so plainly in your report. Do not claim a check passed that you did not run, do not substitute a weaker check silently, and do not fabricate a build or E2E stage for infrastructure that is not there yet.

If anything fails and you cannot fix it after reasonable effort: revert the claim (`status` back to `todo` in both the task file and the index, committed to main), append a note to the task file's Completion Log explaining what is stuck, remove the worktree, and tell the user. Do not leave a task claimed and abandoned.

## Phase 7: Rebase, Squash, Gate, Merge, and Push

The goal is a clean, linear `main`: **one** well-described implementation commit per task on top of its claim commit, fast-forwarded onto `main` and pushed — never a merge bubble, never a trail of "wip"/"fix typo" commits.

1. **Finish the work as commits on the branch.** Checkpoint commits during implementation are fine — they get squashed below, so don't agonise over their messages.

2. **Set `status: done`** in the task file's frontmatter and update that task's row in `tasks/README.md`. Then append a Completion Log to the task file:

   ```markdown
   ## Completion Log

   **Completed:** [date]  ·  **Branch:** `<slug>`  ·  **Commit:** `<sha>`

   **What was implemented:** [two or three sentences]

   **Files changed:**
   - `path/to/file.py` — [what changed]

   **Acceptance criteria:** [how each was met, or which are unverifiable and why]

   **Deviations from the task:** [anything done differently, and why — or "none"]

   **Follow-ups noticed but not done:** [things deliberately left, or "none"]
   ```

   Commit it (this commit is squashed with the rest below).

3. **Rebase onto the latest `main`:**
   ```bash
   cd <worktree-path>
   git fetch origin main 2>/dev/null || true
   git rebase main            # rebase onto origin/main instead if a remote exists and is ahead
   ```
   Resolve conflicts if they are straightforward. If another task changed the index table in a way that conflicts, keep both rows. If conflicts are complex, stop and tell the user.

4. **Squash the branch into one commit (fixup).** `git rebase -i` is not available in this environment, so collapse non-interactively: a soft reset to `main` keeps every change staged, then make a single commit.
   ```bash
   git reset --soft main
   git commit -F <message-file>          # one commit = the whole task
   ```
   **Write a good commit message.** A conventional-commit subject `type(scope): summary [<task-id>]` (e.g. `feat(cli): scaffold eval-harvest package, dispatch, and refusal helper [A-1]`), then a body that says *what* changed and *why* — the acceptance criteria it satisfies and any deviation — not a bare file list. This is the only implementation commit that lands on `main`, so it carries the task's whole story.

5. **Run the full gate on the squashed, rebased state — a green gate is what authorises the merge.** In the worktree:
   ```bash
   mise run check     # ruff lint + format-check + bandit security scan + typecheck + tests
   ```
   plus the CLI build and any E2E case if the task has one. **Only a green gate earns the merge.** If lint, the security scan, the type check, or the tests fail, do **not** merge or push — follow the failure protocol in Phase 6 (revert the claim, note what is stuck, remove the worktree, tell the user).

6. **Fast-forward merge into `main`:**
   ```bash
   cd <original-repo-path>
   git checkout main
   git merge --ff-only <slug>
   ```
   `--ff-only` guarantees a linear history with no merge commit. If it fails because `main` moved, rebase again in the worktree (step 3) and retry.

7. **Push `main` to origin:**
   ```bash
   git push origin main
   ```
   This is a fast-forward push of `main` (not a force-push of a protected branch), so it should be accepted. If there is no `origin` remote, say so and skip the push — do not invent one. If the push is rejected because `main` advanced on the remote, `git fetch` and re-rebase (step 3), then retry — **never force-push `main`**.

8. **Clean up:**
   ```bash
   git worktree remove ../<slug>
   git branch -d <slug>
   ```

9. **Re-confirm `main` is green** (`mise run check`, plus the CLI build and any E2E case if the task had one) to prove the merge did not break anything.

## Phase 8: Report

Only after the merge has landed and tests pass on main:

```
Completed **A-1: [Data Layer] Add status column to todos table**

Changes:
- `src/models/todo.py` — added the status column with a default
- `migrations/0004_add_status.py` — new migration
- `tests/test_todo_model.py` — 3 tests covering default, constraint, and rejection of invalid values

Verified: [commands run and their result, incl. the `mise run check` merge gate. Name anything you could not run.]
Squashed to one commit, ff-merged and pushed to main. Task marked done in docs/features/<feature>/tasks/.

Now unblocked and ready to start:
- A-2: [Data Layer] Backfill status for existing rows
- B-1: [API] Add status filter to GET /todos
```

Compute the newly unblocked list from the finished task's `blocks` field: for each, check whether all of its own blockers are now `done`. Report only the ones that are fully unblocked. If finishing this task unblocked nothing, say so.

## Important Rules

- **The task files are the source of truth, not the index.** When they disagree, trust the files, fix the index, and tell the user the index was stale.
- **Never pick up a blocked task.** If every remaining task is blocked, the dependency graph is wrong — report it rather than picking one anyway.
- **Never pick up a task already `in-progress`** without asking. Check `git log` on the file first to see who claimed it and when.
- **Claim on main before starting.** The claim commit is what stops two agents from doing the same work. Skipping it to save a commit defeats the point of a checked-in board.
- **Never mark a task `done` with failing tests**, and never mark it `done` before the branch is merged to `main` and pushed.
- **Keep `main`'s history linear and clean.** Squash the branch's implementation into one well-described commit (soft-reset to `main`, then commit — `git rebase -i` is unavailable here), then fast-forward-merge it. No merge bubbles, no "wip" commits on `main`.
- **The gate authorises the merge.** Lint, the bandit security scan, the type check, and the tests must all pass (`mise run check`) on the rebased, squashed state before you ff-merge. A red gate means revert, not merge.
- **Push `main` after merging** (`git push origin main`, a fast-forward). Never force-push `main`; if the push is rejected, re-fetch and re-rebase, then retry.
- **Never mark a task `done` with unchecked acceptance criteria.** If a criterion cannot be verified, say so in the Completion Log and ask the user rather than checking the box.
- **Verify in order: tests, then the built CLI, then E2E.** Get the unit suite green first, then build the CLI (once one exists) and drive it end-to-end where the task has a user-visible surface. Everything runs through `uv`/`mise`, never bare `python`/`ruff`/`mypy`/`pytest`.
- **Report what you did not run.** If the project has no test suite, no CLI yet, or no linter, name that gap explicitly — and never fabricate a build or E2E stage for infrastructure that does not exist. Silence reads as success.
- **Do not fix the task file's design.** If the task is wrong, stop and report it. Reinterpreting a task quietly produces work nobody reviewed the plan for.
- **Out Of Scope is binding.** Note adjacent problems for the user; do not fix them.
- **One task at a time.** Finish and merge the current task before suggesting the next.
