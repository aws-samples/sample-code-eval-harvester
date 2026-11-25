# Decisions — coding-task (PRD)

A running log of the decisions that shaped this feature's requirements. One paragraph each: what was
decided, and why. The product-level decisions this feature builds on live in
[`../pr-eval-harvest/decisions-prd.md`](../pr-eval-harvest/decisions-prd.md).

## 2026-11-25 — coding tasks brought into scope

**Coding tasks are a second kind; the review kind is unchanged.** The pr-eval-harvest PRD deferred
"eval types other than PR review" and cut "coding-agent benchmarks generally" (§10). This reverses that
for one kind only: an agent makes a merged pull request's change and that pull request's tests grade
it. The review verbs, their candidate file and their tasks do not change, and `code` and `calibrate`
are separate verbs, so a review user meets nothing new.

**The tasks run on the AgentCore + Harbor workshop path, as its own coding tasks do.** A team that has
set up the workshop's environment runs these tasks with the same `harbor run -e
harbor_env.agentcore_env:AgentCoreEnvironment` command and the same staged grader. No new execution
environment ships with this feature, and nothing outside this repository is a runtime or onboarding
requirement.

**The instruction never quotes the PR body.** A pull request's description usually says how the fix
works, which is the answer. The title and the hidden test files say what to do, and calibration adds
the names of the tests that must pass.

**What staged grading does not stop is stated, not hidden.** The tests run the agent's code, so code
planted on the source side can still write a false test report. The workshop says the same of its own
grader. The requirement (CT-5) is narrower and checkable: no file the agent can write is ever read as
a reward, a status or an exit code.
