# PRD: coding-task — coding tasks from merged pull requests, graded in stages

> **Status: proposed.** A brownfield feature on top of [`pr-eval-harvest`](../pr-eval-harvest/prd.md).
> That PRD stays the authority for the product, the customer and the review datapoint; this one says
> what is added so the same repository history also yields coding tasks. Requirement ids (FR-*,
> NFR-*, US-*) and section numbers refer to the pr-eval-harvest PRD.

---

## 1. North Star

> An SDE who mines review datapoints from their repository can, from the same clone, build coding
> tasks: an agent makes a merged pull request's change, and the tests that pull request added or
> changed grade it, in the AgentCore + Harbor workshop's staged grader, so nothing the agent can write
> becomes its own score.

## 2. Problem Statement

The pr-eval-harvest PRD deferred "eval types other than PR review" and cut "coding-agent benchmarks
generally" (§10). A team choosing a coding agent for its repository has the same problem the review
kind solves for review agents: public benchmarks are not their code. Their merged pull requests
already hold the answer key, in the tests each one added or changed.

Two facts shape the feature:

- **Which tests the change makes pass is only known by running them.** A test file the PR touched can
  hold tests that already passed, tests that are flaky, and tests that fail for an unrelated reason.
- **A coding task is graded against the agent's own tree.** The review kind grades in a separate
  verifier because a shared one is agent-scoreable (FR-32). A coding task cannot: the verifier must run
  the tests on what the agent wrote. The AgentCore + Harbor workshop solves this with a staged grader
  in the agent's container: the tests run as the agent user, then a trusted stage, as another user and
  after the agent's processes are gone, computes the reward. Its contract is `test.sh --stage
  untrusted|trusted`.

## 3. Users and use

The same customer and the same driving agent (§3). The workflow:

1. Write `coding-profile.toml` once per repository: the image, the install command, the test command,
   the run globs.
2. `eval-harvest code --pr <n>` writes one task per merged pull request.
3. Run Harbor's nop and oracle agents on it, on the workshop path (`harbor run ... -e
   harbor_env.agentcore_env:AgentCoreEnvironment`).
4. `eval-harvest calibrate` turns those runs into the fail-to-pass and pass-to-pass tests.
5. Run nop and oracle again (0 and 1), then benchmark agents.

## 4. Requirements

- **CT-1** `code` is offline (NFR-2): the local clone and the profile only, no forge call. It finds the
  PR's merge or squash commit by GitHub's commit subjects, or takes `--merge-commit`.
- **CT-2** The task starts at the merge's first parent. The agent's image holds that commit's tree as
  one fresh commit at `/work/repo`, with no history and no file of the change.
- **CT-3** The change is split by path into a test side (hidden until grading, then restored over
  whatever the agent did to it) and a source side (the gold patch, which only the oracle applies).
  A PR with no test-side change, no source-side change, or no changed test file the profile runs is
  refused, with a reason.
- **CT-4** The instruction is the PR title, the hidden test files and how they run, and after
  calibration the fail-to-pass test names. Never the PR body, which describes the fix. The
  answer-absence scan (FR-40) runs on the written task, with the control token (FR-36).
- **CT-5** Grading follows the workshop's staged contract: `test.sh --stage untrusted` writes only
  evidence; `test.sh --stage trusted` computes `reward.json` from the task's own files and that
  evidence, and never takes a reward, status or exit code from it. `test.sh` without `--stage` stays
  Harbor's one-stage verifier, for environments that do not stage.
- **CT-6** `calibrate` reads files only. A test is fail-to-pass if it passed in no nop run and every
  oracle run, pass-to-pass if it passed in every run, and left out if its result changed between
  runs of one agent. No fail-to-pass test is a refusal.
- **CT-7** No runtime dependency (NFR-5). The grader that runs in the task is standard-library Python
  for any `python3` 3.7 or later.

## 5. Out of scope

- Running the tests inside `code`. Execution is Harbor's: calibration uses the same environment the
  benchmark does, so a task that calibrates is a task that runs.
- Sealing the test report itself. The tests run the agent's code, so code planted on the source side
  can still write a false report; the workshop says the same of its own grader. Staging keeps every
  reward file the agent can write out of the score.
- A separate-verifier coding task, LLM judges or rubrics for coding tasks, and the review kind's
  `dataset` aggregation of coding tasks.
- Forges other than GitHub's merge and squash commit subjects for finding the PR (`--merge-commit`
  covers the rest).
