---
name: create-tasks-locally-harvest
description: Read a feature's tech plan (and optionally PRD), break it into fully specified task files under docs/features/<feature>/tasks/, one markdown file per task with the title in the filename, plus an index carrying the dependency graph. Use instead of create-tasks-in-linear-harvest when you want the task board checked into the repo and no Linear dependency.
argument-hint: <feature-name> [--dry-run]
---

# Create Tasks Locally

The user provides a feature name and optional flags: $ARGUMENTS

You are a technical project manager. Your job is to read the feature's tech plan and codebase, then write a flat set of task files into the repository — one markdown file per task, plus an index that carries the dependency graph. No Linear, no external service: the task board is checked in alongside the code it describes.

This produces the same tickets as `create-tasks-in-linear-harvest`, in files instead of issues.

## Phase 1: Parse Arguments

Extract from `$ARGUMENTS`:
- **feature-name** (required): The kebab-case feature slug, e.g. `user-onboarding`. If omitted, list the directories under `docs/features/` and ask which one.
- **--dry-run** (optional): Print the task list and one full task file to the terminal without writing anything to disk.

## Phase 2: Read the Feature

1. **Read the tech plan.** Look for `docs/features/<feature-name>/tech-plan.md`. This is the primary input — it contains the task breakdown, workstreams, dependencies, and requirements.
2. **Read the PRD.** Look for `docs/features/<feature-name>/prd.md`. Use this for context on the north star, user stories, and acceptance criteria.
3. **If no tech plan exists**, tell the user: "No tech plan found at `docs/features/<feature-name>/tech-plan.md`. Run `/create-tech-plan-harvest` first to generate one, or I can read the PRD and codebase to build a task breakdown from scratch." If the user wants you to proceed without one, move to Phase 2b.
4. **If neither file exists**, stop and tell the user you need at least a PRD or tech plan to work from.

Read the tech plan fully, including the sections you will cite in task files: requirements (§2), constraints (§3), sequence diagrams (§7), schemas (§8–10), and the ADRs (§11). Task files quote these by section, so you need to know what is in each one.

### Phase 2b: Build Task Breakdown from Scratch (only if no tech plan)

If proceeding without a tech plan:
1. Read the PRD thoroughly.
2. Read `CLAUDE.md` at the project root to understand the codebase structure, conventions, and commands.
3. Explore the codebase areas relevant to the feature.
4. Build a task breakdown yourself:
   - Group tasks into workstreams (e.g., Data Layer, API, Agent Tools, Testing)
   - Identify dependencies between tasks
   - Keep each task small enough for a single focused session
5. Present the breakdown to the user and get approval before writing any files.

## Phase 2c: Ground Every Task in the Codebase

The tech plan's task breakdown is a skeleton. Before writing task files, open the code each task touches. You cannot write a task a junior developer can pick up without knowing:

- The **real file paths** the task modifies — not `the user service` but `src/services/user.py`
- The **current shape** of anything being changed: the existing schema, the current function signature, the current response body. A task that says "add a field" without showing what the field is being added to sends the reader on a hunt.
- The **existing pattern** the task should follow, named as a concrete example: "follow the shape of `list_todos` in `src/tools/todos.py:88`"
- The **commands** that build, test, and lint the affected area (from CLAUDE.md)

If a task's target file does not exist yet because an earlier task creates it, say so in the task file and name the task that creates it.

Do NOT reference a file, function, table, or symbol you have not confirmed exists. If you are inferring one, mark it `[TODO: verify]` so the assignee knows it is unverified.

## Phase 3: Build the Task List

From the tech plan's "Task Breakdown" and "Parallel Workstreams" sections (or your own breakdown from Phase 2b), build a flat list of tasks.

**Task ID:** Carry the tech plan's workstream IDs (`A-1`, `A-2`, `B-1`, …). These become filename prefixes, so the directory sorts by workstream and the IDs match the plan the tasks came from.

**Title:** `[Workstream] Imperative description of the change` — e.g. `[Data Layer] Add status column to todos table`. Start with a verb. The title says what changes, not just what area it is in.

**Filename:** `<task-id>-<kebab-case-title>.md`, where the slug comes from the title with the workstream prefix dropped. The title must be readable from the filename alone — that is how the board gets browsed without a tool.

```
docs/features/user-onboarding/tasks/
  README.md
  A-1-add-status-column-to-todos-table.md
  A-2-backfill-status-for-existing-rows.md
  B-1-add-status-filter-to-get-todos.md
  C-1-wire-status-into-the-list-todos-tool.md
```

**Blocked by / blocks:** Derived from the tech plan's dependency column and workstream diagram. Reference other tasks by their ID and filename so the links are followable in a plain editor.

**Status:** Every task starts at `todo`. The lifecycle is `todo` → `in-progress` → `done`, recorded in the task file's frontmatter and mirrored in the index.

## Phase 4: Confirm with the User

Before writing anything, show two things.

First, the full task list:

```
| ID | Title | Blocked By | Complexity |
|----|-------|------------|-----------|
| A-1 | [Data Layer] Add status column to todos table | — | S |
| A-2 | [Data Layer] Backfill status for existing rows | A-1 | M |
| B-1 | [API] Add status filter to GET /todos | A-1 | M |
| C-1 | [Agent] Wire status into the list_todos tool | B-1 | S |
```

Second, **one fully written task file** — pick the most representative task and render it completely. The user should approve the depth before you generate fifteen files at the wrong level of detail.

Ask: "Does this look right? A-1 above is a full example of the task depth. I'll write these to `docs/features/<feature-name>/tasks/`. Want me to proceed, adjust the breakdown, or change the depth?"

Do NOT write files until the user confirms. If `--dry-run` was passed, stop here.

## Phase 5: Write the Task Files

Create `docs/features/<feature-name>/tasks/` if it does not exist. Write one file per task using the template below, then write the index.

### Task File Template

Write the body so that someone who has never opened this feature's tech plan can finish the task correctly. That is the bar: **a junior developer, new to the codebase, picks this up and ships it without asking a follow-up question.** Every section is required; where one genuinely does not apply, write "None" rather than deleting it.

> The body of this template is shared with `create-tasks-in-linear-harvest/SKILL.md`, which produces the same tickets as Linear issues. If you improve it here, port the change there — the two skills are expected to stay in agreement.

`````markdown
---
id: A-1
title: "[Data Layer] Add status column to todos table"
feature: <feature-name>
workstream: Data Layer
status: todo
complexity: S
implements: [FR-1, NFR-2]
user_story: US-1
blocked_by: []
blocks: [A-2, B-1]
---

# A-1: [Data Layer] Add status column to todos table

## Context

Two to four sentences: why this task exists, what it unblocks, and what breaks or stays broken without it. Name the user-visible problem, not just the mechanical change.

**North star:** [one line, carried from the PRD]
**Implements:** FR-2, NFR-1  ·  **User story:** US-1

## Design References

Point at the exact place to read, not the whole document. The assignee should not have to read a 400-line plan to find their three paragraphs.

- **Tech plan:** `../tech-plan.md` §8 (Data Model) — the column definitions this task implements
- **Tech plan:** §7.2 — the sequence diagram showing where this call lands
- **ADR-2** in §11 — why we chose this approach over [alternative]; do not re-litigate it here
- **PRD:** `../prd.md` §6, US-1 — the acceptance criteria this rolls up to

## What To Build

Numbered, ordered, concrete steps. Each step is an action on a named thing. If a step needs a decision, the decision is already made here — the task file decides, the assignee implements.

1. [Action, naming the file and function]
2. [Action]
3. [Action]

State explicitly what NOT to change if there is an adjacent thing that looks like it should also change.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/module/file.py` | Modify | Add the new branch to `handler()` at ~line 40 |
| `src/module/new_thing.py` | Create | New file; mirror the layout of `sibling.py` |
| `tests/test_file.py` | Modify | Add cases from the test table below |

## Schemas & Contracts

Required whenever this task changes stored data, an API surface, a tool signature, or any structure another component reads. Show **before and after** — a diff the assignee can check their work against. Delete this section only if the task changes no contract at all.

**Before:**
```
[current schema / signature / response shape, copied from the actual code]
```

**After:**
```
[the target shape, fully specified: field names, types, nullability, defaults, constraints, indexes]
```

**Migration:** [how existing data gets to the new shape, or "none — additive with a default"]
**Backward compatibility:** [what existing callers see; whether this is a breaking change]

## How To Verify

The exact commands, in order, that prove the task is done. Copy them from CLAUDE.md — do not invent commands.

```bash
[setup command, if any]
[test command scoped to this change]
[lint / typecheck command]
```

Then, what to look at by hand: [the observable behavior to check, and what correct looks like]

## Tests To Write

Each test names the defect it catches. Do not write tests that assert a constructor returns non-null, that a getter returns what you just set, or that a mock was called — those verify the test's own wiring, not the system.

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_[name]` | [Behavior, as input → observable output] | [The bug that makes this go red] |

Include the failure paths, not just the happy path. If this task adds a guard or validity check, one test must break the invariant deliberately and confirm the guard fires.

## Acceptance Criteria

Observable, checkable conditions. Someone other than the author must be able to confirm each one without reading the diff.

- [ ] [Condition stated as an observable outcome]
- [ ] [Condition]
- [ ] All commands in "How To Verify" pass
- [ ] No regressions in the existing suite

## Out Of Scope

What a reasonable person might do while in here, and should not. Name the task that covers it instead, if there is one.

- [Thing deliberately deferred] — covered by [task ID]

## Notes & Gotchas

The traps you found while reading the code. This section is where the task file earns its keep.

- [Constraint from the tech plan's §3 that applies here]
- [Existing pattern to follow, with a concrete example: "match the error shape returned by `foo()` in `bar.py:120`"]
- [Anything surprising in the current code that the assignee will otherwise trip over]

## Dependencies

**Blocked by:** nothing — ready to start
**Blocks:** [A-2](A-2-backfill-status-for-existing-rows.md), [B-1](B-1-add-status-filter-to-get-todos.md)
`````

### Index File

The index is where the dependency graph lives, since there is no issue tracker holding it. Write `docs/features/<feature-name>/tasks/README.md`:

`````markdown
# Tasks: [Feature Name]

**Tech plan:** [../tech-plan.md](../tech-plan.md)  ·  **PRD:** [../prd.md](../prd.md)
**North star:** [one line, carried from the PRD]

## Status

| ID | Title | Status | Blocked By | Complexity |
|----|-------|--------|-----------|-----------|
| [A-1](A-1-add-status-column-to-todos-table.md) | [Data Layer] Add status column to todos table | todo | — | S |
| [A-2](A-2-backfill-status-for-existing-rows.md) | [Data Layer] Backfill status for existing rows | todo | A-1 | M |
| [B-1](B-1-add-status-filter-to-get-todos.md) | [API] Add status filter to GET /todos | todo | A-1 | M |
| [C-1](C-1-wire-status-into-the-list-todos-tool.md) | [Agent] Wire status into the list_todos tool | todo | B-1 | S |

## Dependency Graph

```
A-1 ──┬──► A-2
      └──► B-1 ──► C-1
```

## Ready To Start

Tasks with no unmet blockers:
- **A-1** — [Data Layer] Add status column to todos table

## Parallel Workstreams

- **Fully parallel:** [workstreams with no cross-dependencies]
- **Integration point:** [where parallel work converges and needs joint testing]

## Requirement Coverage

Every requirement in the tech plan maps to at least one task. Gaps are listed explicitly.

| Requirement | Tasks |
|------------|-------|
| FR-1 | A-1, B-1 |
| NFR-1 | B-1 |
| [Any requirement with no task] | **UNCOVERED — [why]** |

## Conventions

- Update a task's `status` in its frontmatter and in the table above when you pick it up or finish it.
- `todo` → `in-progress` → `done`.
- A task is done when every box in its Acceptance Criteria is checked.
`````

## Phase 6: Verify and Report

Before reporting success, check your own output:

1. **Every task file exists** and its filename matches its `id` and `title`.
2. **The dependency graph has no cycles.** Walk it. A cycle means the breakdown is wrong, not that the graph needs editing.
3. **Every `blocked_by` and `blocks` entry names a task that exists.** Dangling references make the board unusable.
4. **Every requirement in the tech plan's §2 appears in the coverage table.** If a requirement has no task, say so out loud — silently dropping a requirement is the failure mode this step exists to catch.
5. **At least one task is unblocked.** If nothing can start, the graph is wrong.

Then report:

```
Wrote [N] tasks to docs/features/<feature-name>/tasks/

| ID | Title | Blocked By |
|----|-------|------------|
| A-1 | [Data Layer] Add status column | — |
| A-2 | [Data Layer] Backfill status | A-1 |

Ready to start now: A-1
Uncovered requirements: none
```

State the requirement-coverage result explicitly, including when it is clean. A coverage check that reports nothing is indistinguishable from a coverage check that did not run.

## Important Rules

- **Flat structure only.** One file per task, all in the same directory. No subdirectories per workstream — the ID prefix already groups them, and nesting breaks the relative links.
- **The filename carries the title.** That is the point of this skill: the board is browsable with `ls`.
- **Every task must be actionable by someone new to the codebase.** The test is not "could an expert figure this out" — it is "could a junior developer ship this without a follow-up question." Real file paths, current-and-target schemas, the commands to run, and the traps you found while reading the code.
- **Never reference a symbol you have not confirmed.** File paths, function names, tables, and columns must come from actually reading the code, not from the tech plan's prose or from inference. Mark anything unverified `[TODO: verify]`.
- **The task file makes the decisions.** If a task involves a choice, resolve it in the file — do not leave the assignee to pick. If you cannot resolve it, that is an open question for the user in Phase 4, not a gap to ship.
- **Cite the tech plan by section, and only the relevant section.** "See the tech plan" is not a reference. `../tech-plan.md §8` is.
- **Do not restate an ADR's argument.** Link it and say the decision is settled. Tasks that re-open decided questions get re-litigated in review.
- **Do NOT create tasks for "review" or "QA".** Those are handled by the merge process.
- **Do NOT create a separate "write tests" task** unless the tech plan explicitly breaks tests into their own workstream. Tests belong in each task's Tests To Write and Acceptance Criteria.
- **Respect the tech plan's dependency graph exactly.** Do not add or remove dependencies unless you identify a clear error, in which case flag it to the user rather than silently fixing it.
- **Never overwrite an existing task file without asking.** If `docs/features/<feature-name>/tasks/` already has files, list what is there, say which would be overwritten and which task IDs are new, and let the user decide before writing.
