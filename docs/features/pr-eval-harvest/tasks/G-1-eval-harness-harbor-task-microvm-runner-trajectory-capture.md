---
id: G-1
title: "[Eval] Eval harness: Harbor task + MicroVMs runner + trajectory capture"
feature: pr-eval-harvest
workstream: Eval
status: done
complexity: L
implements: []
user_story: US-1
blocked_by: [F-2, D-3, E-4, H-3]
blocks: [G-2, G-3]
---

# G-1: [Eval] Eval harness — Harbor task + MicroVMs runner + trajectory capture

## Context

The product's core bet (US-1, PRD §4/§8/§9): an **unsupervised coding agent, given only the repo name
and the CLI's help, builds valid PR-review eval datapoints** — no methodology in its prompt, no human
correction. This bet was originally going to be checked by a one-shot "kill-criterion experiment."
That is, mechanically, **an eval** — run an agent on a task, score its output — so we build it as a
proper, re-runnable one, and we **dogfood our own stack**: the eval *is a Harbor task*, executed in
the **Lambda MicroVMs** environment (Workstream H), with the agent-under-test driving the real
`eval-harvest` CLI inside the sandbox.

This ticket builds the **harness**: author the Harbor task, run it in MicroVMs against **Claude Code
on Bedrock**, over **2 objectives × k=3 trials**, and **capture the full agent trajectory** of each
trial. The scoring (LLM datapoint-quality judge → G-2; tool-use + trajectory metrics + the go/no-go
report → G-3) is layered on top of the trajectories this ticket produces.

**North star:** prove the agent can drive the CLI from its help alone — with a re-runnable eval that
dogfoods Harbor + MicroVMs, not a one-off writeup.
**Implements:** none (gating eval infra; operationalizes the PRD §8 metric).  ·  **US-1.**

## Design References

- **PRD:** `../prd.md` §4, §8 (the success metric + its `[TODO: set target]`), §9 (this bet is the top risk).
- **Tech plan:** `../tech-plan.md` §4 Risks row 1, §12 Testing Plan (the eval runs **outside** the
  offline `mise run check` gate — it drives a live model and a network), §13 Workstream G.
- **Workstream H — the Lambda MicroVMs environment (G-1's blocking dependency, H-1…H-4).** H delivers
  the out-of-tree Harbor environment G-1 dispatches trials to; code against its interface, do not
  rebuild VM provisioning. It lands in parallel/ahead — coordinate on the boundary rather than assuming
  finished code to read:
  - `LambdaMicrovmsEnvironment(BaseEnvironment)` (H-2) — an **unmodified** Harbor loads it by import
    path via `EnvironmentFactory.create_environment_from_import_path('module.path:ClassName')` (no fork
    — H-3); the packaged path is `harvest_env.lambda_microvms:LambdaMicrovmsEnvironment`. It builds the
    image, launches the microVM, uploads build context, and execs the agent — G-1 **uses** it, it does
    not re-implement VM provisioning.
  - The `microvms` bindings (v0.6.0, H-1): `Sandbox.run(…) → Session`, `Session.run_sync(cmd, …) →
    ExecResult` (stdout/stderr/exit_code), `upload_file`/`download_tar`. Only needed if you drop below
    Harbor; prefer driving Harbor.
  - **`harbor run` is Harbor's own pre-existing CLI** — `harbor run -p "<task-dir>" -m "<model>"
    -a "<agent>" -e "<import-path>"`. This *is* the harness at the CLI level: Harbor runs the agent in
    the microVM env for one task and emits the run trace. G-1 wraps it (via CLI or the equivalent Harbor
    Python API), **one dispatch per trial**; it does not build a bespoke runner. Use `-p`/`--path` for a
    local task directory (**not** `-d`/`--dataset`, which is `name@version` and routes a local path into
    registry resolution — a "no job was started" abort, not a run), and `-e`/`--env` with the import
    path (not the deprecated `--environment-import-path`). `-n` is Harbor's `--n-concurrent`, **not** a
    trial count — do not pass it expecting k trials; the harness loops k times itself.
  - **Standing up the AWS infra** (the MicroVMs Terraform stack, the built image) is a **separate task,
    added later** — G-1 assumes the environment is runnable and codes against it.
- **Out-of-package isolation** — `eval/` is **not** part of the shipped `eval_harvest` package and
  nothing under `src/eval_harvest/` may import it, so it is free to use a network, a model client, and
  the Workstream H env. Confirm the S-18 import scan still passes after this lands.
- The emitter/`harbor.py` (D-2/D-3, a blocking dependency) — author the eval task in the same Harbor
  task shape the emitter produces, validated with the same shipped `Harbor` validators.

## What To Build

Create a top-level **`eval/`** directory (sibling to `src/`, `tests/`; not in the package).

1. **Parameterized objectives.** Each objective is one declarative record: a **public** repo URL +
   a pinned **commit SHA** (public ⇒ no `gh` credentials; pinned ⇒ the history the agent sees can't
   drift), the PR(s) to work from, the datapoint `--kind`, and the expected properties later tickets
   grade against. Ship **2 objectives** (e.g. one `--kind reject` and one `--kind approve`, chosen
   from a well-reviewed OSS repo with real change-requested→fixed→approved iterations). Adding a
   third is appending a record.
2. **The eval as a Harbor task.** Author the objective as a Harbor task (dogfooding `emit`/`harbor.py`
   shape): the task's environment editable-installs `eval_harvest` (`uv pip install -e .`) and holds
   the repo cloned at the pinned SHA; the instruction tells the agent to **build PR-review eval
   datapoints with the `eval-harvest` CLI, reading its `--help`/man pages** — and **nothing else**
   (no methodology in the prompt; coaching it tests the prompt, not the CLI's docs — PRD §4).
3. **Run it via the Workstream H Harbor environment (assume it works).** Materialize the objective's
   eval task to a local directory, then wrap `harbor run -p "<task-dir>" -m "<bedrock-model>"
   -a "<agent>" -e "<import-path>"` (or the equivalent Harbor Python API —
   `create_environment_from_import_path` for the Workstream H `LambdaMicrovmsEnvironment`) to execute
   it, **one dispatch per trial**. Resolve the task path to absolute (the runner may shell out with a
   different `cwd`). The agent-under-test is **Claude Code on Bedrock** (`CLAUDE_CODE_USE_BEDROCK=1`,
   AWS region + creds from the environment, model id via `ANTHROPIC_MODEL` / Harbor's `-m` —
   **configurable, not hardcoded**). Public repo ⇒ the CLI's `gh`/`git` calls need no auth (note the
   unauthenticated `gh` rate limit; pin/replay if it bites). A **non-zero `harbor run` exit must be
   recorded distinguishably** — a failed launch (e.g. Harbor refusing to start) must not be normalized
   into a zero-scoring trajectory that reads as "the agent produced nothing"; mark it so the normalizer
   and any reader can tell the two apart. Do **not** provision VMs or reimplement the runner — that
   plumbing is the Workstream H environment's job.
4. **A pluggable agent-under-test interface, with a runnable starter agent.** Model the agent-under-test
   behind a small interface (it maps to Harbor's `-a <agent>`) so a **second agent (Kiro) can be added
   later** via its own API key. Ship **two**: `claude-code` (Harbor's built-in driver) and
   `claude-code-reviewer` — a *starter* PR-review agent (a `ClaudeCode` subclass with a review-oriented
   system prompt exposed as one editable constant, injected via `--append-system-prompt` and
   `shlex.quote`d so a multi-word prompt survives as a single shell token, not shattered argv). Resolve
   the starter through Harbor's custom-import-path mechanism (any `-a` value containing `:` loads as
   `module:Class` — `eval.agents.claude_code_reviewer:ClaudeCodeReviewer` — **no Harbor fork**, the same
   way the environment is loaded by import path). Keep the harness-side agent descriptor **import-free
   of `harbor`** (the offline suite imports it): it names the import path as a *string*; the `harbor`
   import lives only under `eval/agents/`, behind the `eval` group. The agent's only output contract is
   the one `instruction.md` states: read `change.patch`, write findings as a JSON array
   (`{path, line, statement, severity}`) to **`/logs/artifacts/findings.json`** — the artifacts-convention
   dir, the one path Harbor carries from the agent environment into a separate-mode verifier (a
   submission written under `/logs/agent/` never reaches the scorer and grades as empty).
5. **2 objectives × k=3 trials.** Run each objective **k=3** times by **looping the harness** (one
   `harbor run` dispatch per trial); LLM agents are nondeterministic, so a single run is a weak signal —
   later tickets report a pass *rate*. (k is the harness's own `-n`/`--trials`; do **not** map it onto
   Harbor's `-n`, which is `--n-concurrent`.)
6. **Persist the run trajectory** of every trial from **Harbor's run output** — the agent's turns,
   each tool call + arguments + result, the CLI's refusals/exit codes, and the **task directory the
   agent produced**. Normalize Harbor's emitted trace into structured artifacts under `eval/`
   (e.g. `eval/runs/<ts>/<objective>/<trial>/…`); these are the input G-2 (judge) and G-3
   (metrics/report) consume. Consume what Harbor already records — do not build a bespoke capture layer.
7. **Invocation outside the gate.** Add `mise run eval` (or documented `uv run eval/run.py …`) that
   runs the harness. It must be **excluded from `mise run check`** and offline CI (needs Bedrock +
   network + MicroVMs), and **skip with a clear message** when Bedrock creds / the MicroVMs env are absent.
8. **An oracle/nop gradeability guard — a runnable acceptance gate, not a unit test.** The emitter
   suite tests the emitter against its own port of Harbor's rules and never *executes* a task; a
   datapoint that passes every offline check can still fail to grade end to end (a patch that will not
   apply, a solution that cannot run, a `score.py` that miscredits, a submission that never reaches the
   verifier). Add a `mise run smoke-datapoint -- <task-dir>` task (a small module plus a thin
   Terraform-output wrapper script) that dispatches Harbor's two model-free agents against **one**
   emitted task and asserts the datapoint actually grades:
   - `--agent oracle` copies the reference solution in and submits the reference findings — it must
     score **`coverage_all > 0`** and its agent must exit cleanly (read the trial's exit code /
     `exception_info`, not only the reward: a crashing agent that scores non-zero by accident must fail).
   - `--agent nop` does nothing — it must **credit nothing**: every reward key zero *except* the
     `oracle_*` reference-set sizes (which count oracle findings and are non-zero for any agent, `nop`
     included) and `coverage_required` when the task has no required findings. In particular
     **`precision_strict` and `precision_adjudicated` must be `0.0` for a silent agent** — an empty
     submission has demonstrated no precision and must not score a perfect `1.0` (else silence dominates
     under Harbor's `Mean()` aggregation). The two are real, distinct tiers: `precision_strict` is
     judge-free (location match only), `precision_adjudicated` is the judge-gated number.
   - the two reward objects must **differ** (the cheapest strong assertion — a perfect reviewer and
     silence scoring identically means the eval measures nothing).
   Split the pure reward-contract logic (drive it with synthetic reward objects — offline, no AWS) from
   the subprocess/filesystem edge that dispatches Harbor and reads what it wrote. On failure, print the
   diagnosis (the oracle traceback + the build-log tail), not just expected/actual. When credentials /
   Terraform outputs / the environment are missing, the guard must **exit non-zero with a named reason —
   never skip** (a runtime check that "did not run" must never be read as "passed"). Keep it out of
   `mise run check`. Cost: one image build plus two short trials, minutes of wall time — the *agents*
   need no model, but the oracle's verifier judge calls Bedrock to adjudicate its non-empty submission,
   a small model spend, so the guard is not entirely model-free.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `eval/` | Create | Top-level; not in the package, not imported by `src/` |
| `eval/models.py` | Create | Pydantic models: `Objective`, `ExpectedProperties`, `TrialTrajectory`, `RunManifest`, … |
| `eval/objectives/*.toml` | Create | 2 declarative objective records (public repo + pinned SHA + PR + kind + expected props); a third is a third file |
| `eval/catalog.py` | Create | Loads + validates the objective records |
| `eval/harbor_task.py` | Create | The eval authored as a Harbor task (env clones the pinned SHA + editable-installs `eval_harvest`), validated with the shipped `Harbor` validators |
| `eval/agent.py` | Create | The pluggable agent-under-test interface + `ClaudeCodeAgent`, `ClaudeCodeReviewerAgent` (import-path string), offline `RecordedAgent`; **harbor-free** |
| `eval/agents/claude_code_reviewer.py` | Create | Starter PR-review agent: a `ClaudeCode` subclass + `REVIEW_SYSTEM_PROMPT`, `shlex.quote`d `--append-system-prompt`. New pkg `eval/agents/` behind the `eval` group |
| `eval/runner.py` | Create | `HarborCliExecutor` (`harbor run -p … -e <import-path>`; records a failed launch distinguishably) + trajectory normalizer + k-trial loop + run manifest |
| `eval/run.py` | Create | The `mise run eval` entry point; agent choices; skips cleanly without creds |
| `eval/smoke.py` | Create | The oracle/nop gradeability guard: pure reward-contract logic + the Harbor dispatch/read edge |
| `scripts/smoke-datapoint.sh` | Create | Thin wrapper: loads Terraform outputs, runs the guard; exits non-zero (never skips) when prerequisites are absent |
| `eval/README.md` | Create | How to run, required env (Bedrock creds/region/model, the Workstream H env), the smoke guard, what artifacts land where |
| `tests/test_eval_harness.py`, `tests/test_eval_runner.py`, `tests/test_smoke_datapoint.py` | Create | Offline: models, agent registry/argv, and the smoke reward-contract logic |
| `mise.toml` | Modify | Add `eval` and `smoke-datapoint` tasks; keep both **out of `check`** |

## Schemas & Contracts

- **Objective spec** (Pydantic per CODING_STANDARDS): `id`, `repo_url`, `commit_sha`, `pr`, `kind`,
  `expected_properties`.
- **Trajectory / run artifact**: per trial `{objective_id, trial_index, agent, model, turns[] (role,
  text), tool_calls[] (name, args, result, exit_code), produced_task_dir, wall_time_sec, exit_code}` —
  the analysis input for G-2/G-3. A run manifest (`run.json`) indexes every trial. Persisted, not a
  shipped API.

## How To Verify

- `mise run eval` (Bedrock creds + MicroVMs available) runs both objectives × k=3, Claude Code builds
  datapoints inside the MicroVM against the editable-installed CLI, and a trajectory + produced task
  dir is captured for each of the 6 trials.
- With no creds / no MicroVMs, `mise run eval` **skips cleanly**; `mise run check` stays green and
  offline; the S-18 import scan still finds no model client under `src/eval_harvest/`.
- `mise run smoke-datapoint -- <task-dir>` (creds + MicroVMs) dispatches `oracle` + `nop` and passes
  only when the datapoint grades; with prerequisites absent it exits non-zero with a named reason, not a
  skip. Its reward-contract assertions are exercised offline with synthetic rewards.
- Adding a third objective is a one-record append.

## Tests To Write

- Offline: the objective-spec model and the trajectory-artifact (de)serialization, and the agent-under-
  test interface with a **recorded/fake agent** (no live Bedrock, no MicroVM) — assert the harness
  wires objective → run → captured artifact correctly.
- The `harbor run` **argv**, with a stubbed `subprocess.run` (no Harbor, no AWS): pin the full argv,
  asserting positively (`-p`, `-e` present) *and* negatively (`-d` and `--environment-import-path`
  absent), and that a non-zero `harbor run` exit is recorded as a distinguishable failure rather than
  consumed as a zero-scoring trial.
- The starter agent's registration: `claude-code-reviewer` is selectable via `-a` and its
  `harbor_agent` resolves to the import path `eval.agents.claude_code_reviewer:ClaudeCodeReviewer`
  (a bare name Harbor's factory would not load to the custom class is the defect to catch); and — behind
  the `eval` group — that the prompt renders as one shell token (dropping `shlex.quote` shatters it).
- The smoke guard's **pure reward-contract logic**, driven with synthetic reward objects (no AWS):
  a zero-coverage oracle fails, a `precision_strict = 1.0` nop fails, byte-identical oracle/nop rewards
  fail, a non-zero oracle exit fails, and a missing prerequisite yields a **non-zero exit with a named
  reason, not a skip**.
- The S-18 import-graph guard still passes after `eval/` lands (`eval/` imports freely; the package may not).
- Do **not** fabricate a test that asserts a live agent's behaviour or spins a real MicroVM in CI.

## Acceptance Criteria

- [ ] `eval/` exists as a parameterized harness with **2** objectives (public repo pinned at a commit SHA) and a one-record path to add more
- [ ] The eval is authored as a **Harbor task** whose environment editable-installs `eval_harvest`; the agent's only guidance is the repo name + the CLI `--help` (no methodology in the prompt)
- [ ] The harness runs the task through the Workstream H Lambda MicroVMs environment against **Claude Code on Bedrock** (configurable model), **2 objectives × k=3 trials** — *wiring built and verified offline (correct `harbor run` argv: `-p <abs task-dir>`, `-e <import-path>`, configurable `-m`, no `-d`/`--environment-import-path`; a failed launch recorded distinguishably; k=3 by looping the harness); the actual live dispatch is human-gated and unrun, like H-3's live proving trial (needs Bedrock creds + the MicroVMs env)*
- [ ] Agent-under-test sits behind a pluggable interface (Kiro can be added later); v1 ships **two** — `claude-code` (Harbor's built-in driver) and the `claude-code-reviewer` starter agent (loaded by `:`-import-path, no Harbor fork), whose findings go to `/logs/artifacts/findings.json`
- [ ] The **full trajectory** (turns, tool calls/args/results/exit codes, produced task dir) is captured per trial as structured artifacts under `eval/`
- [ ] `mise run smoke-datapoint -- <task-dir>` dispatches `oracle` + `nop` on one emitted task and asserts it grades (oracle `coverage_all > 0` + clean exit; nop credits nothing incl. `precision_strict`/`precision_adjudicated == 0`; the two rewards differ), prints a diagnosis on failure, and exits non-zero (never skips) when prerequisites are absent — wired + reward-contract logic tested offline; the live dispatch is human-gated
- [ ] `eval/` is outside the package and gate: `mise run check` stays offline/green, S-18 passes, `mise run eval` and `mise run smoke-datapoint` stay out of `check` (the eval skips cleanly without creds/MicroVMs)

## Out Of Scope

- The datapoint-quality **LLM judge** and its alignment → **G-2**.
- **Tool-use / trajectory metrics**, the aggregate report, and the PRD §8 bar → **G-3**.
- Kiro as a second agent-under-test (deferred; API key added later).
- Building the mining logic (Workstreams B–E) or a full Harbor leaderboard of PR-review agents (PRD §10).

## Notes & Gotchas

- **Dogfood, don't reinvent.** The sandbox is the Workstream H Lambda MicroVMs environment and the task
  is in Harbor format — this exercises `emit`/`harbor.py`/the execution env for real. Assume the
  environment works; don't re-implement VM provisioning or the runner.
- **Harbor's flags are a third-party contract — enumerate them, don't guess.** `-p`/`--path` is for a
  local task directory; `-d`/`--dataset` is `name@version` and sends a local path down registry
  resolution, which aborts without starting a job. `-e`/`--env` takes the import path; the older
  `--environment-import-path` still parses but is deprecated and prints a notice every run. `-n` is
  `--n-concurrent`, not attempts-per-trial (`-k`) and not the harness's k. Pin the argv in a test so a
  flag drift cannot silently return.
- **A failed launch must not look like an empty trial.** Record a non-zero `harbor run` exit
  distinguishably (a marked failure turn / an exit-code flag in the trace), or the next flag mistake is
  again consumed as a zero-scoring trajectory nobody reads.
- **The submission path is critical.** The graded agent writes findings to
  `/logs/artifacts/findings.json` — Harbor only carries the artifacts dir into a separate-mode verifier;
  `/logs/agent/` never crosses that boundary, so a submission written there grades as empty.
- **Public + pinned SHA** keeps it credential-free and non-drifting; mind the unauthenticated `gh` rate limit.
- **Keep the model out of the package and gate** — `eval/` lives outside the package and nothing in `src/eval_harvest/` imports it.
- **Requires AWS Bedrock creds + network + the MicroVMs env** — not autonomously runnable by an offline
  loop. A human or a credentialed CI job starts it; an autonomous agent without creds treats it as human-gated.

## Dependencies

**Blocked by:** [F-2](F-2-man-pages-and-help-methodology-and-parity-test.md) (the help the agent reads),
[D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md) (the real `emit` the agent runs),
[E-4](E-4-wire-emit-verify-refuse-before-write-and-nfr2-test.md) (pulls in E-2, the `verify` the agent runs),
[H-3](H-3-load-into-unmodified-harbor-by-import-path.md) (the Lambda MicroVMs environment the trials run on).
**Blocks:** [G-2](G-2-datapoint-quality-llm-judge-and-alignment.md), [G-3](G-3-tool-use-trajectory-metrics-and-eval-report.md)

