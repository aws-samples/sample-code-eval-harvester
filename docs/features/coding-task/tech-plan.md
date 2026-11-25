# Technical Spec: coding-task — coding tasks from merged pull requests, graded in stages

**PRD:** [prd.md](./prd.md)
**Builds on:** [pr-eval-harvest tech plan](../pr-eval-harvest/tech-plan.md): the `GitCommandRunner`
chokepoint, `Harbor.validate_task_layout`, the answer-absence scan and its control token, the
temp-dir-then-promote write, and the FR-2 refusal shape are reused unchanged.
**Status:** Proposed (2026-11-25)

---

## 1. Modules

| Module | Role |
|--------|------|
| `codeprofile.py` | Reads `coding-profile.toml` (`[coding]`): image, install, test command with `{files}` and `{junit}`, run globs, test-side globs, workdir, timeout, env, agent setup, allowed hosts. Reports every problem at once; an unknown key is an error. Pure apart from reading the file. |
| `codetask.py` | `code`: finds the PR's integration commit, splits the change, and builds the task's files as bytes, then validates and writes them (§3). |
| `calibrate.py` | `calibrate`: nop and oracle trial results to `tests/expected.json`, and the fail-to-pass list in `instruction.md` (§5). Reads and writes files only. |
| `coding_tpl/` | Data the CLI writes into each task, never imports: `Dockerfile.tmpl`, `test.sh`, `grader.py`, `solve.sh`. |
| `harbor.py` | `validate_coding_task_config`: the task schema checks with the coding kind's verifier rule (ADR-1). |
| `gitcmd.py` | `git_bytes`: the chokepoint's binary-safe read, for blobs, the archive and the binary diff. |
| `cli.py` | The `code` and `calibrate` verbs. |

## 2. The task

`code --pr <n>` writes `tasks/<owner>__<name>__pr<n>-code/` (Harbor id `<owner>/<name>__pr<n>-code`):

| path | content |
|---|---|
| `environment/repo.tar.gz` | `git archive` of the merge's first parent, gzip with mtime 0 |
| `environment/Dockerfile` | the profile's image; the archive unpacked at `/work/repo`; the install; `git init` and one commit at a fixed date, tagged `harvest-base`; its SHA in `/var/lib/task_base_ref`; the agent setup |
| `tests/files/` | the merge's version of every changed test-side file |
| `tests/spec.json` | workdir, command, run files, test-side globs, deleted test files, timeout, env |
| `tests/test.sh`, `tests/grader.py` | the staged grader (§4) |
| `solution/gold.patch`, `solve.sh` | the source side of the change, `git diff --binary`, for the oracle |
| `instruction.md` | the PR title, the command, the hidden test files, the calibration markers |
| `task.toml` | the agent: `allowlist` of the profile's hosts (the workshop's Bedrock endpoints by default); the verifier: `no-network`, shared mode; `[metadata.origin]` with the base and merge commits |

The same pull request gives the same bytes (NFR-1): the archive, the commit date and the JSON are fixed.

## 3. ADR-1: a coding task is graded in the agent's container, in stages

**Decision.** A coding task's `task.toml` has no `environment_mode`, so Harbor runs the verifier in the
agent's container, and its `test.sh` implements the workshop's staged contract.
`validate_coding_task_config` refuses `environment_mode` on this kind; `validate_task_config` keeps
refusing `shared` for the review kind.

**Why.** FR-32's reason for `separate` holds: a verifier that shares the agent's container and reads
an agent-written file is agent-scoreable. A coding task has to be graded on the agent's tree, and a
separate verifier does not have it. The workshop's staged grader keeps the score out of the agent's
reach in the shared container instead:

1. As root: the agent's patch against the image's baseline, then `test.sh --stage untrusted` as the
   agent user with `OUT`, then every agent process is killed.
2. As root: the patch again. A change means no grade. The stage's regular files, never a `reward*`
   name, are copied into a root-owned `IN`.
3. `test.sh --stage trusted` as a grader user, with `IN` and a grader-owned `OUT`. Only
   `OUT/reward.json` reaches Harbor.

The repository must be at `/work/repo` with its `.git`: the workshop's image layer keeps a root-only
copy of it and records the tree as built, and that is the baseline of step 1.

## 4. The staged grader

| stage | runs as | reads | writes |
|---|---|---|---|
| `test.sh` (no stage) | Harbor's verifier user | the repository, `/tests` | `reward.txt` (0 first), `results.json`, `grade.json` |
| `--stage untrusted` | the agent user | a copy of the repository, `/tests` | `$OUT/junit.xml`, `run.json`, the test log; no reward |
| `--stage trusted` | the grader user | `/tests` (spec, `expected.json`, `files/`), `$IN/junit.xml`, `$IN/run.json`'s folder only | `$OUT/reward.json` (last), `grade.json`, `results.json` |

- **The untrusted stage runs on a copy** (`cp -a`) because step 2 compares the patch over every file
  in `/work/repo`. A test run writes caches and build output, and the grader restores test-side files
  before running, so an in-place run would make every trial ungraded.
- **The trusted stage takes nothing as a result from `IN`.** It resets `PATH`, picks `/usr/bin/python3`
  or `/usr/local/bin/python3` only if root owns it, no one else can write it, and it is outside `/app`,
  `/work`, `/home`, `/tmp`, `/run` and `/logs`, and runs it with `-I -S` from `/tests`. It maps report
  paths to test ids with the hidden test files in `/tests/files`, not the repository.
- **The report is bounded.** It must be under 20 MB and hold no DTD. A required test the report holds
  twice passes only if every copy passed, so a forged passing copy cannot hide a failing one.
- **Before calibration** (no `expected.json`) every stage records statuses and writes no passing
  reward: the trusted stage writes no `reward.json`, so the workshop reports the trial as ungraded.

## 5. ADR-2: calibration is a run of the task itself, read by an offline verb

**Decision.** `code` writes the task without `expected.json`. Harbor's nop and oracle agents run it on
the same environment the benchmark uses; `calibrate` reads their `verifier/results.json` and writes
`expected.json`.

**Why.** Finding fail-to-pass tests needs a container runtime and the repository's toolchain, which
`code` must not need (NFR-2). Running the task itself on the benchmark's own path means a task that
calibrates is a task that runs there, with no second executor to keep in step.

**Rule.** fail-to-pass: passed in no nop run and every oracle run. pass-to-pass: passed in every run.
Left out: a result that changed between runs of one agent. A run file with no test reported never
becomes required. No fail-to-pass test is the `no-fail-to-pass` refusal.

## 6. Failure containment

Every refusal is in the FR-2 shape. `code`: `coding-profile-invalid` (exit 2), `pr-not-found`,
`no-base-commit`, `repository-unknown`, `no-test-change`, `no-source-change`, `no-run-files`,
`task-invalid`, and the answer-absence findings (exit 3). `calibrate`: `not-a-coding-task`,
`no-calibration-runs`, `no-fail-to-pass` (exit 3). Nothing is promoted into `tasks/` until the layout,
the config and the scan pass.

## 7. Testing

- `tests/integration/test_coding_task.py`: a real repository with a merge, a docs-only merge and a
  tests-only squash (`tests/fixtures/coding_repo.py`). It checks the layout, the snapshot against the
  base tree, the gold patch, the instruction, the config, determinism and every refusal.
- `tests/integration/test_coding_grader.py`: the written task's `test.sh`, run in the three stages with
  local processes on an image-like checkout. It covers:
  - calibration runs and `calibrate` itself;
  - oracle 1 and nop 0;
  - planted reward and status files ignored;
  - a forged passing duplicate and a DTD scoring 0;
  - a hostile `python3` on `PATH` not run;
  - the one-stage path giving the same test ids.

  It skips on Windows (bash) and where no root-owned `python3` exists.
- Each guard was broken on purpose and its test watched go red: `PATH` and the interpreter choice, the
  duplicate rule, the copy, the DTD check, the shared-mode check.
- `tests/unit/test_codeprofile.py`, `tests/unit/test_calibrate.py`: the profile rules and the
  calibration rule.
