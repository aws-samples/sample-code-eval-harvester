# eval-harvest

![Stability: Experimental](https://img.shields.io/badge/stability-experimental-orange.svg)

## What is eval-harvest

eval-harvest is a sample of how to turn a repository's own pull-request review history into a
rigorous [Harbor](https://github.com/harbor-framework/harbor) evaluation — **without hand-labelling a
single PR or learning eval methodology**. Point it at a clone, and it mines merged review iterations
into sealed eval datapoints that rank a PR-review agent on approve/deny accuracy and review
thoroughness.

The CLI vends facts and instructions (the way `gh` does) and never calls a model; a coding agent
supplies the judgment. Its job ends when a self-contained, verified Harbor task directory exists — it
does not run evals and it does not score agents. That is what Harbor and the `eval/` harness do.

## Why it matters

- **No hand-labelling** — a PR that went through review iteration *is* a labelled pair: the rejected
  push, the reviewer comments (the defects), and the approved state. Findings are quoted, not inferred.
- **No eval expertise required** — every `--help` teaches the methodology, not just its flags. The
  driving agent learns the whole workflow from the CLI itself.
- **Answer-absence enforced** — a datapoint that leaks its own answer measures nothing; `verify`
  refuses to seal one that does, so scores mean what they claim.
- **Deterministic and cheap where it can be** — the CLI is pure-stdlib, zero runtime deps, and never
  spends on a model. LLM cost is confined to the optional eval run.
- **Isolated execution** — trials run one Firecracker-isolated Lambda MicroVM per datapoint, in an
  unmodified Harbor loaded by import path (no fork).

## The use case

An SDE ships a PR-review agent into their SDLC and does not trust it. They cannot tell whether it
misses real defects or cries wolf, or whether last week's prompt change helped — because they have no
eval. eval-harvester builds that eval from the review
history already sitting in their repo.

Key characteristics:

- **Repository-scoped** — each dataset is mined from one local clone you already have.
- **Agent-driven, model-free CLI** — the CLI vends facts; a coding agent judges. No LLM inside it.
- **Documentation is the product** — if an unsupervised agent produces garbage, the docs failed;
  more CLI verbs will not fix it.
- **Sealed datapoints** — a task must not contain its own answer, and `verify` proves it before use.
- **Outcome-measurable** — the finished dataset scores an agent on real accepted/rejected states.

## How it works

Each dataset is built through a six-verb pipeline —
`init → survey → capture → (you fill the candidate) → emit → verify → dataset`. **Step zero is your
own local clone**: every verb reads it and none fetches it for you.

1. **`init`** — scaffold the dataset and list the repo's convention files so you can author `rubric.md`
   and `risk-map.toml` first (the overlay method — see `init --help`).
2. **`survey`** — find which PRs are harvestable (went through review iteration) and why the rest are
   not.
3. **`capture`** — record one PR's mechanical facts into a candidate (SHAs, diffs, comments, verdicts),
   leaving the judgment slots blank for you to fill (defect vs nit is the whole game).
4. **`emit`** — build a self-contained Harbor task from the filled candidate. `--kind reject` builds
   from a rejected iteration (expected verdict: block); `--kind approve` builds from the approved state
   (expected: approve). `emit` runs `verify` for you.
5. **`verify`** — re-check a task and run its answer-absence scan standalone (plants a control token
   and refuses `scan-did-not-run` if the scan returns without it).
6. **`dataset`** — assemble the Harbor manifest and report the risk/severity distribution.

Add `--json` to any verb for a machine-readable payload. Verbs communicate failure as a four-field
**refusal** (`check` / `datapoint` / `offending` / `next`) with a distinct exit code per outcome class
(`0` ok · `2` usage · `3` refusal · `4` verification failed · `5` runtime unavailable).

## Current status

**Experimental sample for local evaluation.** Not intended for production workloads or unattended
processing of third-party datasets. Use public or synthetic data and a disposable working environment.
Evaluation results are advisory and require review before they are used to authorize changes.

## Getting started

> **These steps show what happens under the hood — you are not meant to run them by hand.**
> eval-harvest is built to be driven by a coding agent (Kiro, Claude Code, etc.): the agent reads the
> `--help` output, decides which verb to run next, and fills the candidate's judgment slots. The
> commands below are the workflow it follows, laid out so you can see the moving parts.

### Install

The package is pure-stdlib and pinned to **Python 3.12** — zero runtime dependencies. Run it straight
from a clone with `uv`, or install the `eval-harvest` console script:

```bash
# run in place (no install)
uv run eval-harvest --help

# or install the console script onto your PATH
uv tool install .        # then: eval-harvest --help
```

Every verb's `--help` teaches the methodology, and the same prose lives in [`man/`](man/). The CLI is
the only place the review-eval methodology is written down — start there:

```bash
uv run eval-harvest --help                 # the workflow, end to end
uv run eval-harvest <verb> --help          # one verb's methodology + flags
```

### Build a dataset

```bash
# 0. clone the repo you want to mine
git clone https://github.com/OWNER/REPO /path/to/clone

# 1. scaffold the dataset (author rubric.md + risk-map.toml first — see init --help)
uv run eval-harvest init /path/to/clone --dataset ./my-dataset

# 2. find harvestable PRs
uv run eval-harvest survey --clone /path/to/clone --repo OWNER/REPO

# 3. capture one PR's mechanical facts into a candidate
uv run eval-harvest capture 13611 --clone /path/to/clone --dataset ./my-dataset
#    ... fill only the judgment slots in ./my-dataset/candidates/pr-13611.json ...
#    Keep captured paths, revisions, and other mechanical facts unchanged; prefer annotate.

# 4. build a self-contained, verified Harbor task
uv run eval-harvest emit ./my-dataset/candidates/pr-13611.json --kind reject --dataset ./my-dataset

# 5. re-check a task and its answer-absence scan standalone
uv run eval-harvest verify ./my-dataset/tasks/pr-13611 --clone /path/to/clone

# 6. assemble the manifest and report the risk/severity distribution
uv run eval-harvest dataset --dataset ./my-dataset
```

This workflow assumes trusted candidate inputs — read the [Security](#security) section and
[`SECURITY.md`](SECURITY.md) before processing any input.

### Run the eval (optional)

`mise run eval` runs the core-bet eval: does an unsupervised agent build good datapoints from the
CLI's help alone? It drives a live model against the finished CLI on the Lambda MicroVMs environment,
scores each datapoint with an LLM judge aligned to human labels, and prints per-objective pass rates
plus the go/no-go. It is **deliberately excluded from `check`/CI** — it needs AWS Bedrock credentials,
a network, and the running environment; without those it skips cleanly (exit 0). See
[`eval/README.md`](eval/README.md), including its required dedicated-account and runner-role setup.

## Documentation

| Path | What it is |
| --- | --- |
| [`man/`](man/) | Man pages for every verb — the same methodology prose the `--help` vends, pinned in sync by a test. |
| `src/eval_harvest/` | The CLI package — zero runtime deps. `cli.py` dispatches the six verbs; `survey`/`forge`/`candidate` mine PRs; `rubric`/`riskmap` build artefacts; `emit`/`harbor`/`tomlw`/`verifier_tpl/` assemble the task; `seal`/`verify` enforce answer-absence; `dataset` aggregates the manifest. |
| [`eval/`](eval/README.md) | The core-bet **eval harness** (Workstream G): runs a real agent against the finished CLI, scores datapoint quality with an aligned LLM judge, and reports pass rates + the go/no-go. Nothing in `src/` imports it. |
| `harvest_env/` | The packaged `harvest-env` distribution — the Lambda MicroVMs execution environment for Harbor (one Firecracker-isolated microVM per trial). The project's own code (MIT-0), loaded into an unmodified `harbor==0.22.0` by import path (no fork). |
| [`docs/features/pr-eval-harvest/`](docs/features/pr-eval-harvest/) | The design: [`prd.md`](docs/features/pr-eval-harvest/prd.md) (scope), [`tech-plan.md`](docs/features/pr-eval-harvest/tech-plan.md) (design), the `decisions-*.md` logs (why), and [`tasks/`](docs/features/pr-eval-harvest/tasks/) (the board). |

For contributor setup, coding standards, and the spec-driven agent-burndown workflow this repo is
built and extended with, see [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`CODING_STANDARDS.md`](CODING_STANDARDS.md). In short: always go through `uv` or `mise run <task>`
(never bare `python`/`pip`/`ruff`/`mypy`/`pytest`), and **`mise run check` must pass before every
commit — do not commit red.**

## Security

This is an experimental sample; the runtime does not enforce account isolation or confine candidate
patch paths. Two known limitations are documented rather than enforced in code:

- **AWS credential exposure (S1)** — the optional AWS runner forwards exported AWS credentials to the
  evaluated agent. Run live evaluations only in a **dedicated, disposable AWS account** with a separate
  runner role scoped to the sample.
- **Trusted candidate inputs only (S2)** — a crafted candidate can cause local files outside the
  dataset to be copied into generated tasks. Use candidates captured locally, keep their mechanical
  facts unchanged, and review generated output before sharing it.

See [`SECURITY.md`](SECURITY.md) for the full policy, posture, and the suppressed-finding rationale. To
report a potential security issue, use the AWS
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting) — **do not** open
a public GitHub issue.

**Generative AI notice.** This tool builds and runs evaluations with the help of generative AI, and the
optional AWS runner can incur Amazon Bedrock and other AWS charges. Generative AI can make mistakes —
review generated output **and costs** before acting on them. See the
[AWS Responsible AI Policy](https://aws.amazon.com/ai/responsible-ai/policy/).

## Disclaimer

The example provided in this repository is for experimental and educational purposes only. It
demonstrates concepts and techniques but is not intended for direct use in production environments.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
