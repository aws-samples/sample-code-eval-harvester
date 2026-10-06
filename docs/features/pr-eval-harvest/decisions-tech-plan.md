# Decisions — pr-eval-harvest (technical plan)

A running log of the decisions that shaped this feature's technical plan. One paragraph each: what
was decided, and why it was decided that way. This exists so a later session does not re-argue
something already settled, and so the reasoning survives when the plan gets rewritten. Where a
decision reverses an earlier one, the reversal says so. Requirements-level decisions live in
`decisions-prd.md`.

## 2026-09-04 — technical plan

**The CLI is one uv-installable package with verb subcommands, not per-verb single-file scripts.**
The customer wants it installable "like it's from PyPI," a uv project with a `mise.toml` for dev
tasks (lint/format via ruff, test, typecheck) and identical Windows/macOS behaviour. Several verbs
(`init`, `survey`, `capture`, `emit`, `verify`, `dataset`) share substantial logic — the Harbor
emitter, the TOML writer, the seal checklist — so per-verb single-file scripts would copy that
shared logic across verbs, a maintenance hazard one package avoids. Python for now; the CLI boundary
and candidate-file format are kept language-agnostic so a hot path could move to Rust/PyO3 later
without a rewrite — the customer's stated contingency (ADR-1).

**Zero third-party runtime dependencies; the TOML writer is hand-rolled.** The customer asked for
"as few deps as possible … small and simple", naming the previous over-complicated version as the
thing to simplify away from. `tomllib` reads TOML but stdlib writes none, and emission must be
byte-stable (determinism AC and Harbor's content hash), so a small deterministic writer is written
rather than importing `tomli-w`. Validation is plain functions returning violation lists rather than
pydantic models, so pydantic is not needed on the emit path. Dev-only deps: ruff, pytest, a type checker. This makes the "no LLM client, minimal tree" claim
trivially auditable — the runtime dependency list is empty (ADR-2).

**A per-PR JSON candidate file is where the agent's recorded judgment lives.** `capture` scaffolds
it with mechanical facts filled and judgment slots empty; the agent fills per-comment
classifications, finding severities (with evidence), and its change-risk classification; `emit` and
`verify` read it back and enforce that the slots are filled and coherent. The alternative — passing
judgments as command args or piped stdin with no persistent artefact — was rejected because the
recorded-evidence trail would then live only in the emitted task, re-running would mean re-deciding,
and a bad classification rule would be diffuse rather than findable in one inspectable file. The
candidate file is also the surface the PRD §4 human audit reads and the re-runnable record that
makes regeneration byte-identical. JSON, not TOML, because stdlib `json` round-trips both directions
at zero cost and is trivially agent-editable; TOML is reserved for the emitted `task.toml` where
Harbor requires it (ADR-3).

**The whole CLI owns `gh`+`git` directly; the sealing path stays offline by test, not by a wire.**
The customer chose this over a hard online/offline process seam. Mining verbs (`survey`, `capture`,
`init`'s convention scan) touch the network; `emit`/`verify`/`dataset` operate only on captured
local state and make no network call, so the answer-sealing path can never reach the forge. That
guarantee is enforced by a test (run with `gh`/`git remote` stubbed to fail) rather than a
two-process boundary — more machinery than one product needs. The consequence, recorded because it
matters: that offline test carries the whole guarantee and is treated as one of the watched-to-fail guards. The
old CLAUDE.md network-isolation of `tools/` was about a pipeline that no longer exists (ADR-4).

**v1 ships the reward-object *shape* inside the task, not the full matching/scoring machinery.**
US-6 requires the CLI to write the judge and rubric into `tests/`, and PRD §4 requires the reward
object to keep coverage and precision separate and treat unmatched agent findings as *uncredited*.
The reference `finding_matching.py` + `review_scoring.py` implement that fully (~2,700 lines,
pydantic, Kuhn maximum-cardinality assignment, versioned taxonomy equivalence, judge-sampling
disagreement policy). v1 reproduces the *shape* at reduced sophistication — deterministic path+line
narrowing before any judge call, an LLM judge for semantic equivalence with `REWARDKIT_*` scrubbed,
separate coverage/precision keys with unmatched counted uncredited, per-severity counts — but a
*greedy* one-to-one match rather than Kuhn's, and no cross-class equivalence table. Severity
*aggregation* into an automation-gate score and the taste judge are not emitted at all, consistent
with the earlier deferral. Porting it in full was rejected for v1: it is the most complex piece,
drags in pydantic (against the zero-dep decision), and its elaborate parts buy precision the first
datasets do not need. The greedy match can, in rare multi-claim cases, credit fewer findings than
the maximum — it understates the agent, which is the safe direction. The reference files remain the
specification for the deferred full matcher, and the reward-key seam is kept so it can replace the
greedy one without changing the dataset format. This is the one place v1 knowingly
under-satisfies a story's spirit (ADR-5).

**`gh pr view --json reviews` does not carry per-line review comments — a new capture path is
required, not an extension of `survey`.** `gh`'s `reviews` field is verdict-level only (author,
state, timing); the inline comments US-3 needs (body, file path, line range) come from the
`pulls/{n}/comments` and timeline REST endpoints. So the verdict fetch is insufficient for the
oracle, and `capture` fetches the comments separately. This constraint shaped the forge-capture
task (TP-3).

**The Lambda MicroVMs substrate is driven through the published `microvms` bindings, not a
hand-rolled daemon.** Workstream H's environment talks to each Firecracker-isolated Lambda MicroVM
through the `microvms` PyO3 bindings (`microvms==0.6.0`), built upstream from the
[`microvms-agentd`](https://github.com/theagenticguy/microvms-agentd) project over its sibling
`microvms-core` crate. Those bindings already own the parts that are easy to get subtly and
dangerously wrong: the per-VM agent token delivered out-of-band (never baked into the shared
snapshot), proxy-token minting and refresh on every request, idempotent detached exec with
caller-minted ids, SSE output streaming with resume, and confined tar extraction. Rejected:
hand-rolling a daemon-plus-client against Firecracker for one consumer, which would re-implement
exactly that security-sensitive surface with none of the upstream's mileage. The project consumes
the published wheel from PyPI and points at the upstream repo for the source and the typed stub
(`microvms.pyi`) rather than vendoring or rebuilding the Rust — consistent with the minimal-tree
stance (ADR-2). This is the client H-1 pins and H-2 builds `LambdaMicrovmsEnvironment` against.

**No Harbor fork is needed; the environment loads by import path.** Harbor's environment factory
honours a configured import path, so the Lambda MicroVMs environment loads into an unmodified
Harbor; proving it with one dispatched trial stays a PRD `[TODO]`, carried forward into the spec
unresolved rather than answered.

**The kill-criterion experiment runs before the mining logic is trusted.** Per PRD §9, a stub CLI
with hand-written help is put in front of a coding agent end-to-end first; if an unsupervised agent
cannot produce valid datapoints from help alone, more verbs will not fix it. It is scheduled as an
early task depending only on the authored help text, and its result is a Definition-of-Done gate on
whether v1 is worth continuing.

**The emitted datapoint is a standard Harbor task, and the oracle follows aws-bench's
"introspection" pattern.** The emitted task uses `schema_version = "1.4"`, the standard
`[task]/[agent]/[verifier]/[environment]` sections, `verifier.environment_mode = "separate"` (in
which Harbor builds the verifier image from `tests/` and does not upload it at runtime, so the image
self-provides `/tests/test.sh`), and a reward file at `/logs/verifier/reward.json` (multi-key, read
first) or `reward.txt` — Harbor ignores the exit code. Our provenance and classifications go under
`[metadata.origin]` and `[metadata.harvest]`, the placements Harbor preserves. The review oracle is
modelled the way aws-bench grades its read-only "introspection" tasks — a reference answer
(`oracle.json`, the curated findings + severities) compared against the agent's output by an LLM
judge shipped in `tests/` — rather than the code-authoring shape where `solution/` is the answer.
This made §8 of the tech plan concrete rather than schematic.

**The tech plan is written as a fresh, standalone build.** The customer's instruction was that the
CLI be built from scratch, so the plan does not diagram existing scripts as an as-is system or frame
the work as extending them — it specifies what to build. The Harbor-source facts and the git-leak
checklist it relies on are stated as design knowledge to reproduce, not as code to port. The sequence
diagrams were also
required to show the coding agent driving the CLI (agent as an actor running commands and editing the
candidate file), and the CLI verbs to be specified as input/output contracts — both are
documentation-quality corrections from the customer, recorded so a later session keeps that framing.

**`solution/` is a first-class, verifier-only artefact (FR-40), not an orphan.** The emitted
`solution/solve.sh` (the known-good submission that proves the reward stack responds) was in the
data model but traced by no requirement and not in the leak scan. It is now covered by FR-40, listed
in US-6's acceptance criteria and the S-10 validity test, and required to be absent from the
agent-visible environment (included in the content-leak scan's corpus). Kept rather than cut because
proving a reward mechanism responds to a known-good and a known-bad submission is a habit both the
PRD and this log already value.

**The answer-absence scan is two halves — a git-channel port plus a new content scanner.**
the git-channel checklist covers channels only, not file content. The leak the design dramatizes
(a PR number in `instruction.md`) also needs a content scan of agent-visible files. TP-5, FR-35, the
`seal.py` module note, and task E-1 now say so.

**The content-leak scan runs against the sealed clone, not the emitted files alone, so it does not
flag pre-existing tokens as introduced leaks.** The content half of the answer-absence scan (a PR
number or an outcome string surfacing in `instruction.md` or another agent-visible file) has to tell
a token the datapoint *introduced* apart from one that already lived in the repository's base tree —
a string that was in the code before the change is not a leak this datapoint created, and refusing on
it would produce false failures the driving agent cannot fix. So the scan takes the clone as its
baseline and fires only on tokens absent from the base tree, which is why `verify`'s content scan
needs `--clone` and not just the emitted task directory. This extends the two-halves answer-absence
decision and the `--clone` verb contract, which recorded the clone for the base-commit/patch-apply
checks but not for the content scan's false-positive baseline.

**`emit`/`verify` may read the local clone; the offline guarantee is about the network, not git.**
The diff is materialized by `capture` as patch bytes (referenced by `patch_path`) so `emit` assembles
`change.patch` with no git call; `verify` takes `--clone` for the base-commit/patch-apply checks.
Reading a local `.git` with plain `git` is not a network call, so this does not weaken NFR-2, whose
guarantee is no forge contact (no `gh`, no `git fetch`/remote op) — clarified in §6.2 and the verb
contracts.

**Force-pushed intermediate iterations are a stated limitation.** `refs/pull/<n>/head` is a single
tip; a round-1 state later force-pushed away can be GC'd on the forge and unfetchable. `capture`
recovers added-commit iterations and the final pre-merge tip, and reports an unreachable tip as
unrecoverable rather than fabricating it. Added as a risk row and an FR-6 acceptance clause.

**Smaller tech-plan fixes from review.** The reward-object math got its own test scenario (S-19) and
a Definition-of-Done line. The "clone first" precondition is now taught in the help methodology
(FR-1) and noted in §7.1. The banned phrase "load-bearing" was removed from ADR-4.

**The eval that measures the core bet is a full end-to-end run, not the earlier stub experiment —
this reverses the kill-criterion decision.** The discovery-era plan was to test the kill criterion
first against a stub CLI with hand-written help, before any mining logic. That is dropped. The
finished CLI is instead run in the Lambda MicroVMs environment against a live agent — Claude Code on
Bedrock — over a public repo pinned at a commit SHA, 2 objectives × k=3 trials, and each built
datapoint is graded by `verify` plus a human-aligned LLM quality judge and tool-use/trajectory
metrics. It is a re-runnable, extensible eval (Workstream G), not a one-shot writeup; it lives under
`eval/`, runs via `mise run eval`, is excluded from `mise run check`, and skips cleanly when Bedrock
credentials are absent. The stub-first experiment lost because the real signal needs the finished
mining path and a real agent scored end to end, not authored help alone — and because the eval is
worth keeping and extending, not throwing away after one measurement.

**The Dockerfile sealing sequence is fixed and binding (FR-30).** Sealing a clone against the
repository's own future is six steps in this order: shallow single-branch clone with no tags,
`git reset --hard` to the base commit, remove the remote, delete remote refs, expire the reflog,
prune unreachable objects. This is established practice — it is how a real Senior SWE-Bench task's
Dockerfile seals — reproduced as design knowledge, not ported code.

**Binding Harbor runtime facts beyond the format basics.** In `separate` mode Harbor builds the
verifier image from `tests/` and does not upload `tests/` at runtime, so the image self-provides
`/tests/test.sh`. Only `/logs/artifacts` is transferred from the agent environment into the separate
verifier — a submission under `/logs/agent` never reaches the scorer — so the agent's output must
land in `/logs/artifacts` and `test.sh` re-materializes it before scoring. Harbor's
local/registry/repo consumption paths ignore a dataset `metric.py` and fall back to `Mean()`, so
every reward key must be meaningful when averaged. Unknown `task.toml` keys load then vanish; the
only keys Harbor preserves are `[metadata.*]` and a top-level `source` (unused here), so provenance
and classifications go under `[metadata.origin]`/`[metadata.harvest]`. The environment loads into an
unmodified Harbor because `EnvironmentFactory.create_environment_from_config` honours
`config.import_path`, resolved via Harbor's own `import_class`.

**The emitted reward object keeps two real precision tiers, and the CLI has distinct exit codes.**
`precision_strict` divides credit by the judge-free location-overlap match set; `precision_adjudicated`
by the judge-gated subset — so `precision_strict ≥ precision_adjudicated` always and the gap is
exactly the credit the judge withheld. An empty submission scores `0.0` precision, not `1.0` over an
empty denominator, while coverage scores `1.0` over an empty oracle. The CLI's exit codes are distinct
per class: 0 success, 2 usage error, 3 refusal (input cannot become a datapoint), 4 verification
failure (a datapoint is broken), 5 runtime-unavailable (a check could not run). `GITHUB_TOKEN` is
stripped from the `gh` child so an invalid ambient token cannot shadow the working keyring credential.

**The reward object splits coverage into two tiers as well as precision.** Alongside
`precision_strict`/`precision_adjudicated`, `reward.json` reports `coverage_required` — the fraction
of the datapoint's blocking (high-severity, must-catch) oracle findings the agent matched — and
`coverage_all` — the same fraction over every oracle finding, non-blocking ones included. Keeping
both mirrors the two-precision-tier reasoning: `coverage_required` measures whether the agent caught
what actually had to block, which is the automation-gate question, while `coverage_all` measures
thoroughness against the full human oracle, which by PRD assumption 2 is only a lower bound.
Collapsing them into one coverage number was rejected for the same reason the precision split was
kept — it would blur the must-catch signal that decides auto-approval into the softer thoroughness
signal — and since Harbor averages whatever keys the reward file carries, both tiers survive
aggregation.

**Workstream H platform limits and contracts.** The Lambda MicroVMs environment drives each trial
through one Firecracker-isolated VM via the `microvms` bindings plus in-VM `agentd`: a server-side
Dockerfile→snapshot build against an S3 bucket and a build role, content-addressed image reuse via
boto3, the per-VM token delivered via `runHookPayload` (never baked into the shared snapshot), exec
as `["bash","-c",cmd]` to honour Harbor's pipefail contract (not the daemon's `sh -c`), streaming
exec with resume, and confined tar transfer. It enforces the platform limits: ARM64-only images,
≤8h VM life, a fixed `public`/`no-network` policy set at launch, and single-container only — Docker
Compose is rejected.

**Packaging: no stored Harbor source or patch; stock PyPI pins.** The runtime uses stock
`harbor==0.22.0` from PyPI. The environment ships as the `harvest-env` distribution (`harvest_env/`,
module `harvest_env.lambda_microvms`) under MIT-0 — the project's own code, never imported by
`eval-harvest`. The change to Harbor is not upstreamed as a stored patch; a Harbor PR, if ever made,
is a `git diff` generated at that time. The `microvms` bindings and the environment's real deps
(`dockerfile-parse`, `boto3`) are plain PyPI dependencies, not vendored, and they replace a fake
`harbor[lambda-microvms]` extra.

**Workstream I infrastructure, with Bedrock scoped to ARNs.** The live eval run needs three AWS
resources as Terraform under `infrastructure/terraform/`: the MicroVM image bucket (`MICROVM_BUCKET`),
the server-side build role (`MICROVM_BUILD_ROLE_ARN`), and the caller IAM permissions — with Bedrock
access scoped to the inference-profile ARN plus the foundation-model ARN and never `Resource="*"`,
behind a checkov gate. The in-task judge is Bedrock reached via the MicroVM's *execution* IAM role,
not an OpenAI endpoint needing a key and not the caller's role. Bedrock model-access enablement is a
console step, not Terraform.

**Context is the scarce resource, so the CLI vends facts and never makes the agent re-derive them
(ADR-6).** Building ten datapoints in one agent context by reading raw patch bytes and rewriting
whole candidates costs on the order of a million tokens, most of it re-deriving facts the CLI already
holds; and a hand-written ten-PR shell loop is where a quoting bug produces junk invocations the CLI
gets blamed for. So the CLI holds the facts and vends exactly what a judgment needs. One shared
unified-diff span parser (`diffspan.py`) — called by both the capture-time comment↔iteration binding
and `verify`'s finding-line check, so there is exactly one and they can never disagree — records
which iterations' diffs cover each comment. `show` renders only the covering hunk on demand; `brief`
prints one self-contained per-PR hand-off with no diff bytes, so a fresh agent context can fill one
candidate; `annotate` merges small per-comment fill records so a correction is one record, not a
whole-file rewrite; and `--batch` moves the ten-PR loop into the CLI with per-item refusal/exception
containment. None of these makes a judgment — they are views and merges over facts `capture` already
wrote.

**Dev tooling beyond the lint/format/test/typecheck baseline.** The dev toolchain adds four choices
the baseline (ruff, pytest, a type checker) does not name, each for a stated reason. The build
backend is `hatchling`. `pytest-xdist` runs the suite with `-n auto` to fan it across cores, and the
suite is serial-by-default where a test needs it. The pre-merge security gate runs as a pinned
`mise run security` task that `check` depends on — its scanners are set out in the security-gate
decision below — and ruff and every scanner version are pinned (not `latest`) so the gate is
reproducible; a floating upgrade silently turned a green `check` red twice before. `pydantic` is a
**dev-only** dependency used solely by the eval harness under `eval/` (Workstream G); it is *not* a
runtime dependency — `[project].dependencies` stays empty (ADR-2, NFR-5), so the "no third-party
runtime deps" claim is untouched. This is deliberately the one place pydantic appears, and only
off the CLI's own execution path. Harbor-dependent and live-eval tests (`test-harbor`, the one-real-
datapoint probe) run under their own tasks and are excluded from `check`, which must stay green
without network or AWS.

**The security gate is one rolled-up `mise run security` task over per-tool scanners.** Static
analysis is not a single tool, so the gate runs a set of pinned scanners, each as its own namespaced
sub-task (so a failure names the tool that raised it) rolled up under `mise run security`, which
`check` depends on. `security:bandit` is the Python SAST named above. `security:secrets` runs gitleaks
over the git *history*, not just the worktree — a secret that was committed and later deleted still
ships in the history a public mirror would carry, so scanning only the working tree would miss it.
`security:sast` runs semgrep against its published registry rulesets for this stack (Python, Terraform,
secrets, and the OWASP top ten); the tool version is pinned but the rulesets are fetched at scan time,
so this is the one sub-task that needs network. `infra-check` runs checkov over the Terraform — it
joins the rollup with Workstream I, since there is nothing to scan before that infrastructure exists
(I-1). Two reproducibility rules the gate lives or dies by. First, **pin every tool version** — gitleaks
and semgrep in `mise.toml` `[tools]`, bandit and checkov via a pinned `uv tool run` — because a floated
scanner silently turns a green gate red on a machine that resolved a newer release. Second, **never let
a scanner run silent**: semgrep prints its scan summary (no `--quiet`) so an empty result is provably a
scan that ran rather than one that matched no files — the CLAUDE.md scanner-honesty rule (NFR-6) applied
to the gate itself — and it disables its exit-time metrics and version-check calls
(`--metrics=off --disable-version-check`), which otherwise hang after the scan finishes behind a
restrictive network.

**`emit` selects the oracle iteration explicitly, and refuses when a finding does not belong to it.**
By default `emit` builds the datapoint from the iteration `capture` marked as the substantive review,
but it accepts `--iteration <n>` to override when the review that matters landed in a later round.
Whichever iteration is chosen, every oracle finding must reference a comment whose covering-iteration
set (the `in_diff_iterations` binding from `diffspan.py`) includes that iteration — otherwise the
finding describes a defect the emitted change does not contain, and `emit` refuses with
`finding-iteration-mismatch` (override-able with `--override finding-iteration-mismatch` for a
deliberately cross-iteration finding). This adds a verb flag and a refusal class the §9 `emit`
contract did not name — it enumerated only `empty-oracle` and the verify-failure classes — but it
follows directly from the comment↔iteration binding: once findings are bound to iterations, emitting
an iteration a finding does not cover is incoherent, and catching it at `emit` is cheaper than
surfacing a broken datapoint downstream.

**Each emitted task carries a self-recorded `content_digest`, and the dataset manifest is its
consumer.** `emit` computes a deterministic `sha256:`-prefixed digest over the task's canonical
content and records it in `task.toml` under `[metadata.harvest].content_digest`; `dataset` (F-1)
reads those digests into the manifest's `task_digests` map. This is a cross-task contract — one verb
writes the field, another reads it — that the §8 `[metadata.harvest]` example and the FR-29 manifest
did not spell out. It exists so the byte-stability NFR is recorded per datapoint rather than only
asserted: a manifest that pins each task's content digest makes a silent regeneration drift
detectable at the dataset level, and gives the PRD §4 audit a stable handle on exactly which bytes it
signed off. It lives in `[metadata.harvest]` because that is one of the few placements Harbor
preserves.

**`survey` reports why each PR is unharvestable and refuses when none are — a blocker taxonomy the §9
contract only gestured at.** FR-5 says `survey` emits blockers but never enumerates them; the verb
assigns each unharvestable PR a specific blocker from a fixed set — `no-integration-commit`,
`no-pull-head-ref`, `no-changed-paths`, `no-test-change`, `squash-without-change-tip` — reports the
histogram across the repository, and refuses with `no-harvestable-pr` (exit 3, online path only) when
every enumerated PR carries at least one blocker, naming `next` as the remedy. `survey` also performs
the bulk `+refs/pull/*/head:refs/remotes/pr/*` fetch itself (default-on, suppressible with
`--no-fetch`, with its own `pull-head-fetch-failed` refusal) rather than leaving the pull-head fetch
to `capture` as §7.1 implied. The named taxonomy and the all-blocked refusal are the survey-side peer
of `emit`'s `finding-iteration-mismatch`: the tech-plan gave `survey` only `no-review-iteration`, and
an implementer working from the plan alone would build the coarser version. Naming the blockers is
what lets the driving agent act on a refusal instead of guessing why a repository yielded nothing.

**Tests are organized by kind into `tests/unit/` and `tests/integration/`, with gating left
orthogonal to the split.** `tests/unit/` is the pure, deterministic, single-module tier (the
`tomlw`/`diffspan`/`harbor`/`riskmap` module tests, the no-model-client and static-analysis-exclude
repo guards, and the eval-logic units); `tests/integration/` is everything that crosses modules,
drives the CLI via `Cli.main`/subprocess, shells to `git`/`gh` (real or stubbed), or needs the
Harbor/eval/AWS groups. The split is by *kind*, not by whether `check` runs a test: the Harbor-,
eval-, and AWS-dependent integration tests keep their existing mise-task gating and
skip-when-absent behaviour, so `mise run check` stays green offline without those tests leaving
`tests/integration/`. Chosen over the single flat `tests/` the suite first grew into, because the
unit/integration boundary here is real and roughly one-third/two-thirds — a genuine deterministic
core against a majority that shells out or drives the CLI — so making the boundary a directory fact
rather than a naming convention lets a reader (or a rebuild) tell at a glance which tests are fast
and hermetic. §12 carries the criteria and the per-file assignment.

**The CLI's black-box I/O contract is fixed, so anything downstream binds to a stable surface
rather than to the implementation.** Every verb is invocable as the `eval-harvest` console script;
`--json` is a top-level flag selecting a machine-readable object, the default being a human
rendering of the same data. Exit codes are distinct per class and are part of the contract: 0
success, 2 usage error, 3 refusal (input cannot become a datapoint), 4 verification failure (a
datapoint is broken), 5 runtime-unavailable (a check could not run and is left unresolved). Every
refusal carries the same four fields — `check`, `datapoint`, `offending`, `next` — on stderr and,
under `--json`, as `{"ok": false, "failures": [{check, datapoint, offending, next}, …]}`. This is
pinned here, not only in §9, because it is the surface every consumer and every black-box check
depends on: a regenerated plan must land on the same exit-code classes and refusal shape or the
interface silently diverges.

**Each read/scaffold verb's observable contract — the files it writes and the object it prints — is
fixed.** `init <clone> --dataset <dir>` scaffolds the dataset skeleton (`candidates/`, `tasks/`) and
writes `rubric.template.md` + `risk-map.template.toml`. `survey --clone <dir> (--repo <org/name> |
--from-json <file>) [--json]` — neither flag is a usage error (exit 2) — prints an object with a
`prs` array whose entries carry at least `number` (int), `state`, and `blockers` (list), and refuses
`no-review-iteration` / `no-harvestable-pr` at exit 3. `capture <pr> --clone <dir> --dataset <dir>` —
a bare `<pr>` and `--from-survey` are mutually exclusive (exit 2) — writes `candidates/pr-<n>.json`
carrying `repo`, `pr_number`, `pr_url`, `iterations`, `review_verdicts`, `comments`, `findings`,
`change_risk`, where each comment carries
`id`/`body`/`path`/`line_start`/`line_end`/`author_role`/`created_at`/`iteration_index`/`in_diff_iterations`,
the judgment slots are present-and-empty (`classification == ""`, top-level `findings == []`,
`change_risk.risk_classified == ""`), and `change_risk.risk_structural` is one of
`low`/`medium`/`high`. `annotate <candidate> (--stdin | --from-json)` merges judgment records into
the candidate. Pinning these keeps a rebuild's candidate schema field-for-field compatible with what
the fill step and the emitter read.

**The emission/verification verbs' contract is fixed too, because the emitted task shape is the
product.** `emit <candidate> --kind reject|approve [--iteration <n>] [--override <check>] [--clone
<dir>]` — neither `<candidate>` nor `--all` is a usage error (exit 2) — writes a task directory
containing `task.toml`, `instruction.md`, `environment/Dockerfile`, `environment/change.patch`,
`tests/`, and `solution/`; the `task.toml` is `verifier.environment_mode = "separate"`, carries a
`schema_version`, `[metadata.origin]` with `repo`/`pr_numbers`/`base_commit`, and `[metadata.harvest]`
with `kind`, `expected_verdict`, and a `sha256:`-prefixed `content_digest`; `expected_verdict`
follows `--kind` (reject⇒`block`, approve⇒`approve`); an approve candidate carrying a high-severity
finding is refused at exit 3. `verify <task-dir> [--clone <dir>] [--json]` returns 0 (all checks
passed), 4 (a check failed — with the four-field failures emitted), or 5 (a check such as
`git-channel-absence` could not resolve under a CLI-only run). `dataset --dataset <dir> [--json]`
writes `dataset.toml` + `registry.json` and prints the risk/severity distribution object, refusing
`no-datapoints` at exit 3 on an empty dataset. §9 holds the full per-verb spec; these entries pin the
load-bearing, externally observable parts so the interface survives a regeneration.
