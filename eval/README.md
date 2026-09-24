# eval/ — the core-bet eval (Workstream G)

This directory holds the eval that measures the product's core bet (PRD §4/§8/§9): **an
unsupervised coding agent, given only the repo name and the CLI's `--help`, builds valid PR-review
eval datapoints** — no methodology in its prompt, no human correction.

The eval is itself a **Harbor task**, run in the **Lambda MicroVMs** environment (Workstream H)
against a live agent (**Claude Code on Bedrock**), over **2 objectives × k=3 trials**. It dogfoods
the whole stack: the emitted-task shape (`eval_harvest.harbor`), the execution environment, and the
CLI the agent drives.

`eval/` is **not part of the `eval_harvest` package**. Nothing under `src/eval_harvest/` imports it
(S-18), so — unlike the zero-dependency CLI (NFR-5) — it may depend on Pydantic, a network, a model
client, and the Workstream H environment. The dependency direction is one-way: `eval/` imports
`eval_harvest`; the package never imports `eval/`.

## Layout

| Path | What it is |
|------|-----------|
| `models.py` | Pydantic models: `Objective`, `ExpectedProperties`, `TrialTrajectory`, `RunManifest`, … |
| `objectives/*.toml` | One declarative record per objective. **Adding a third objective is dropping a third file here.** |
| `catalog.py` | Loads + validates the objective records. |
| `agent.py` | The pluggable agent-under-test interface (`AgentUnderTest`), `ClaudeCodeAgent`, and the offline `RecordedAgent`. |
| `harbor_task.py` | Authors the eval as a Harbor task, validated with the shipped `Harbor` validators. |
| `runner.py` | Runs objectives × trials, normalizes each trial into a `TrialTrajectory`, writes the run manifest. |
| `run.py` | The `mise run eval` entry point; skips cleanly without credentials. |
| `judge/` | The **datapoint-quality judge** (G-2): the rubric, the `QualityVerdict`, the model-call seam, and the alignment-gated `judge()` entry point G-3 calls. |
| `alignment/` | The human-labeled alignment set, the agreement measurement, and the recorded `alignment_result.json` that licenses the judge. |
| `metrics/` | **Tool-use metrics** (G-3) over a captured trajectory: verb usage/order, redundant calls, refusals + whether they were acted on, efficiency. |
| `report.py` | **The report** (G-3): combines `verify` + the quality judge + tool-use metrics into per-objective pass rates and writes the go/no-go report. |

## The datapoint-quality judge (G-2)

`eval-harvest verify` answers *is this datapoint structurally valid and leak-free?* (the US-7
checks). It does **not** answer *is this a good datapoint?* — one whose oracle reflects the PR's real
review, whose change matches the reviewed state, whose verdict fits the kind, whose instruction leaks
nothing, and which did not promote a nit to a blocker. The **quality judge** (`judge/`) grades that,
**layered on top of `verify`** — it never modifies or replaces the structural checks.

Do not confuse it with the *in-task verifier's* judge (`src/eval_harvest/verifier_tpl/score.py`),
which scores an agent-under-review's findings against the emitted oracle and ships inside every
datapoint. This judge scores whether the *datapoint the eval agent built* is itself good, and lives
only under `eval/`.

The rubric is five criteria (`judge/rubric.py`): `oracle-reflects-review`,
`change-matches-reviewed-state`, `verdict-fits-kind`, `instruction-outcome-neutral`,
`nits-not-promoted`. The judge renders them into a prompt, asks a model (`ModelClient` seam — a
`RecordedModelClient` replays a pinned reply in CI, a `LiveModelClient` calls a configurable model at
eval time), and parses the reply into a `QualityVerdict`. The overall pass/score is **derived** from
the per-criterion verdicts (all must pass), never read from the model, and every criterion must
appear or parsing fails — the judge cannot silently drop one.

### Alignment — align before you trust

An unaligned judge is an opinion, not a measurement. So the trusted entry point `eval.judge.judge()`
refuses (`UnalignedJudgeError`) unless a recorded alignment result at or above the bar exists for the
current rubric version (`JUDGE_VERSION`). Alignment (`alignment/`) hand-labels a small mixed set of
datapoints — good ones and ones deliberately broken along each criterion — runs the judge over them,
and records agreement with the human labels.

- **The bar is stated, not manufactured:** **0.80** overall agreement (`alignment/measure.py`,
  `ALIGNMENT_BAR`), the reason recorded there — the judge is a coarse quality gate, so it must agree
  with an independent human on at least four of every five datapoints or its verdicts are noise.
- **The achieved number is recorded:** `alignment/alignment_result.json` (the record the gate reads).
  The committed run scores **0.875** over **8** labeled datapoints — the set carries one genuine
  judge↔human disagreement (`08-disagree-judge-misses`), so agreement is a real number below 1.0,
  proving the measurement discriminates rather than rubber-stamping.
- The recorded run replays pinned model replies (the recorded-judge pattern, tech plan §12), so it is
  reproducible and green in the offline gate. **Refreshing the number against a live model** is the
  human-gated step — the same posture as the live eval dispatch and H-3's proving trial:

  ```bash
  uv run python -m eval.alignment.run_alignment              # offline: replay the pinned replies
  uv run python -m eval.alignment.run_alignment -m <model>   # human-gated: measure against a live model
  ```

  Re-run it whenever the rubric or prompt changes (bump `JUDGE_VERSION`); a stale alignment for an
  older rubric no longer licenses the judge, so the gate forces the re-run.

## The report: tool-use metrics, the pass rule, and the §8 bar (G-3)

G-1 captures *what happened*; G-2 grades *whether the datapoint is good*; G-3 turns both into **the
answer** — a pass rate and a go/no-go read (`report.py`).

### The pass rule — a trial is solved only when all three hold

A trial's `task_solved` is `True` **only** when it:

1. **produced** a datapoint,
2. that datapoint **passes `eval-harvest verify`** (structural validity + the leak scan, E-2), and
3. the **quality judge** passes it (G-2).

The three are ANDed on purpose: a structurally-valid datapoint about the *wrong* PR is a fail, not a
pass — that "valid but wrong" trap is exactly what the quality judge exists to catch. When a trial
fails, the report records *which* gate failed (`no-datapoint` / `failed-verify` / `failed-quality`),
so it is diagnostic, not just a number. The headline is the **pass rate over k=3** per objective and
overall — a single stochastic run is a weak signal, so the report shows the spread, never one run.

### Tool-use metrics (`metrics/`)

Alongside the pass rule, each trial gets a *tool-accuracy* read computed from its captured trajectory
(pure functions, no model): which `eval-harvest` verbs were run and in what order, how many calls
were redundant (byte-identical repeats), how many CLI calls were **refusals** (a non-zero exit), and
— the core-bet signal — how many of those refusals the agent **acted on** (a refusal followed by a
later `eval-harvest` call, i.e. a corrected retry rather than a giveup), plus a coarse
tool-calls-per-turn efficiency. These are reported, not part of the hard pass gate: the gate is
produced + valid + good.

### The PRD §8 pass bar — set or open, never invented

The report states the §8 success bar as **open** with the straw man (80% first-attempt, 100% after
the agent acts on the CLI's feedback), not a manufactured number (`PASS_BAR` in `report.py`; PRD §8).
The go/no-go threshold is a number a human sets *after* reading a real run — until then the report
records the achieved rate and marks the go/no-go **DEFERRED**. A failed bar is a legitimate v1
outcome (ship a working builder, document that its findings are not yet trusted).

### Live prerequisites (human-gated, mirrors the open item below)

The scoring is exercised offline in the gate with recorded trajectories and a recorded judge (no
model, no VM). The *live* report stage additionally needs the quality judge's model
(`EVAL_JUDGE_MODEL`, the alignment-gated `judge()`), and — for `verify`'s base-exists/patch-applies
checks to run rather than report unresolved — a local clone of the source repo. Supplying that clone
from the trial is a live-run wiring item, the same posture as the richer-trace extraction note below.

## Objectives (v1)

Both are real, well-reviewed `pydantic/pydantic` pull requests, pinned at a commit SHA:

- **`pydantic-13611-reject`** — PR #13611 went through a real review iteration (changes requested →
  fix → approved); the reject datapoint is built from the change-requested state (expected verdict
  `block`).
- **`pydantic-13731-approve`** — PR #13731 was reviewed and approved with no changes requested; the
  approve datapoint is built from the approved state (expected verdict `approve`).

## Running it

### AWS credentials and isolation (S1)

**The evaluated agent receives the runner's exported AWS credentials.** With the current Harbor
Bedrock integration, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` are
forwarded into agent commands. An agent processing malicious repository content can read and use
them. The MicroVM boundary and `MICROVM_EXECUTION_ROLE_ARN` do not restrict permissions granted by
these forwarded credentials. Choosing an `AWS_PROFILE` is not a credential-isolation fix.

The supported setup for this sample requires:

- A **dedicated disposable AWS account** containing only sample resources and public or synthetic
  data, with no access to production or other accounts. The caller policy spans MicroVM resources
  in its account and region.
- A **separate runner role** with only the reviewed sample caller policy and no administrative,
  unrelated, or cross-account grants. Use another identity to provision infrastructure. Attaching
  the sample policy to an existing administrator role does not reduce that role's permissions.
- **Short-lived role credentials** for live runs. Do not use root credentials, administrative
  sessions, long-lived IAM user keys, or your normal development account. Keep provisioning
  credentials out of the runtime shell.
- Review of inputs and generated artifacts under the [S2 restrictions](../README.md#trusted-candidate-inputs-s2).
  Treat task content and agent output as untrusted, and review artifacts before uploading or sharing.

The runtime does not verify these conditions. An agent can still use every permission granted to
the sample runner role, expose sample artifacts, and incur charges while its credentials are valid.
Set spending alerts and appropriate service quotas; alerts are not a hard spending cap. These are
conditions for accepting the residual risk of a sample run, not a fix for credential forwarding.
Separating control-plane credentials from agent/model credentials remains open.

### Setup (one-time)

Run the commands below from the repository root.

**1. Install the locked evaluation dependencies.**

```bash
uv sync --frozen --group eval
uv run --frozen --group eval harbor --version
```

This installs Harbor, `microvms`, and the local `harvest-env` package selected by `uv.lock`.
The environment is loaded as `harvest_env.lambda_microvms:LambdaMicrovmsEnvironment`.

**2. Provision the isolated sample infrastructure.**

Follow the [Terraform setup](../infrastructure/terraform/README.md). Create a dedicated runner role
first and set `eval_caller_principal_arns` to that role explicitly. The historical empty-list default
attaches the policy to the Terraform caller; do not use that default for this setup.

Use a separate provisioning identity, inspect the Terraform plan, and confirm the sample account
before applying. Close that provisioning session before running an agent. `mise run eval` reads
the artifact bucket, build role, and execution role from the local Terraform outputs.

**3. Configure a profile for the restricted runner role.**

Using AWS CLI v2 and your approved sign-in method, configure a profile named `eval-harvest-runner`
that obtains a short-lived session for the role from step 2. Check it before exporting credentials:

```bash
aws --profile eval-harvest-runner sts get-caller-identity
```

The account must be the disposable sample account and the ARN must identify the dedicated runner
role session. This command confirms identity, not effective permissions; review all policies
attached to that role separately. Confirm access to the selected Bedrock models. Diagnose any
permission error for its specific action and resource instead of switching to an administrator.

### Run

The following Bash example exports only the selected runner profile into a subshell. Start in a
clean terminal without shell tracing or credential logging. `aws configure export-credentials`
must be available in your AWS CLI v2 installation. Review the identity above before running:

```bash
(
  set -e
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
  unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_BEARER_TOKEN_BEDROCK
  runner_exports="$(aws configure export-credentials --profile eval-harvest-runner --format env)"
  eval "$runner_exports"
  unset runner_exports
  : "${AWS_SESSION_TOKEN:?A short-lived runner role session is required}"
  export AWS_REGION=us-east-1
  mise run eval -- -m us.anthropic.claude-sonnet-4-5-20251001
)
```

The subshell keeps these exports out of the parent shell. It does not revoke credentials or hide
them from the evaluated agent. Do not save the exported values in files, logs, examples, or Git.
Use the same restricted credential setup for all live examples below.

The skip messages tell you exactly which prerequisite is absent — fix them one at a time:

| Message | What to fix |
|---------|------------|
| `agent 'claude-code' is missing required environment: AWS_REGION` | Set the sample region in the restricted runner session |
| `the 'harbor' runner is not on PATH` | Sync the locked evaluation group (step 1 above) |
| `MICROVM_BUCKET is not set` | Use `mise run eval` after the isolated Terraform setup |

The eval is **excluded from `mise run check`** and offline CI — it drives a live model and a network.

### Choosing the agent-under-test (`-a`)

Two agents ship in `eval.run._AGENTS`:

- `claude-code` — Harbor's built-in Claude Code driver, unmodified.
- `claude-code-reviewer` — the **starter PR-review agent**: Claude Code with a review-oriented system
  prompt (`eval/agents/claude_code_reviewer.py`, the `REVIEW_SYSTEM_PROMPT` knob). Use this as the
  template for a real agent to compare against a harvested gold standard.

The harness never runs the agent itself — it hands the agent to `harbor run -a <agent>`. Harbor's
`AgentFactory` loads any `-a` value containing `:` as a custom import path (`module:Class`), exactly
like the environment's `-e harvest_env.lambda_microvms:LambdaMicrovmsEnvironment` — **no Harbor fork
needed**. So `claude-code-reviewer` resolves through the harness to the import path
`eval.agents.claude_code_reviewer:ClaudeCodeReviewer`. The only contract the agent must meet is the
one `instruction.md` states: read `change.patch`, write findings to `/logs/artifacts/findings.json`.

Run the starter against a dataset harvested with the `eval-harvest` CLI, either through the harness:

```bash
# Replace the mise command inside the restricted-credential subshell above with:
mise run eval -- -a claude-code-reviewer -m us.anthropic.claude-sonnet-4-5-20251001
```

…or one task straight through Harbor (the path the smoke harness has actually validated):

```bash
# Inside the same restricted-credential subshell, load the local infrastructure outputs:
export MICROVM_BUCKET="$(terraform -chdir=infrastructure/terraform output -raw microvm_bucket)"
export MICROVM_BUILD_ROLE_ARN="$(terraform -chdir=infrastructure/terraform output -raw microvm_build_role_arn)"
export MICROVM_EXECUTION_ROLE_ARN="$(terraform -chdir=infrastructure/terraform output -raw microvm_execution_role_arn)"
export CLAUDE_CODE_USE_BEDROCK=1
uv run --frozen --group eval harbor run -p ./my-dataset/tasks/TASK_NAME \
  -a eval.agents.claude_code_reviewer:ClaudeCodeReviewer \
  -m us.anthropic.claude-sonnet-4-5-20251001 \
  -e harvest_env.lambda_microvms:LambdaMicrovmsEnvironment -o ./out -y
```

`tests/score.py` in the task grades the agent's `/logs/artifacts/findings.json` against
`tests/oracle.json` (the harvested gold standard). Compare against `-a oracle` (ceiling) and `-a nop`
(floor) on the same task to confirm the wiring discriminates.

**Adding your own agent (or Kiro):** copy `eval/agents/claude_code_reviewer.py`, change the
prompt/behaviour (or subclass a different Harbor `BaseAgent`), then register a matching
`AgentUnderTest` in `eval/agent.py` and add one line to `eval.run._AGENTS`.

## Artifacts

Each run writes under `eval/runs/<timestamp>/`:

- `run.json` — the `RunManifest`: agent, model, k, and every trial's result.
- `<objective-id>/trial-<k>/trajectory.json` — the normalized `TrialTrajectory` (turns, tool calls,
  produced task dir, wall time, exit code) that G-2 (quality judge) and G-3 (metrics + report)
  consume.
- `<objective-id>/trial-<k>/task/` — the datapoint directory the agent produced, when the trial
  produced one.

The G-3 report stage then writes, under `eval/reports/`:

- `<timestamp>.md` — the human-readable run report: pass rates, tool-use findings, quality notes,
  the §8 bar, and the go/no-go read.
- `<timestamp>.json` — the same `EvalReport` as JSON, so a run can be re-scored or diffed.

Both `eval/runs/` and `eval/reports/` are git-ignored — the harness, judge, metrics, and report
*code* is committed; each run's *output* is not. Git ignore rules do not make artifacts safe to share:
review task directories, logs, and build contexts for unexpected files and credential material
before copying them elsewhere. Retain only the sample outputs you need, then clean up the sample
resources using the [Terraform cleanup instructions](../infrastructure/terraform/README.md#cleanup).

## Open item (live confirmation)

The harness consumes what Harbor records rather than building a bespoke capture layer. The live
executor (`HarborCliExecutor`) records the `harbor run` invocation's combined log as a coarse
single-turn trajectory; extracting a richer per-tool trace from Harbor's native run output — and
confirming the exact field mapping and the `harbor run` flags against a real run — is the first
thing to nail down on the live run. This mirrors H-3's honest posture: the wiring is built and
tested offline; the one live dispatched proving trial remains the PRD `[TODO]` that a human or a
credentialed CI job closes.
