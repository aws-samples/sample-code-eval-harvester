---
id: H-2
title: "[Env] Build the LambdaMicrovmsEnvironment on microvms-agentd"
feature: pr-eval-harvest
workstream: Lambda MicroVMs execution environment
status: todo
complexity: L
implements: []
user_story: US-6
blocked_by: [H-1]
blocks: [H-3, H-4]
---

# H-2: [Env] Build the LambdaMicrovmsEnvironment on microvms-agentd

## Context

The eval (Workstream G) dispatches each trial into a Firecracker-isolated AWS Lambda MicroVM, but Harbor ships no such environment — it has to be built. This task builds `LambdaMicrovmsEnvironment`, a Harbor `BaseEnvironment` subclass that is a thin client over the `microvms` bindings (H-1) plus the in-VM `agentd` daemon: the platform builds the task's Dockerfile server-side into a snapshot-backed image and launches one VM per trial, exposing a per-instance HTTPS endpoint but *no* exec or file API, so the image carries `agentd` to supply both. Without this, Workstream G has nothing to run the emitted Harbor tasks on. It shares no code with the `eval-harvest` package (tech plan §3, §6) — it is upstream-of-Harbor infrastructure that only Workstream G consumes.

**North star:** the emitted Harbor datapoints have to actually run somewhere; this is that somewhere for the eval.
**Implements:** none (execution substrate; PRD §7 constraint).  ·  **User story:** US-6.

## Design References

- **PRD:** `../prd.md` §7 Constraints — "Execution is Lambda MicroVMs."
- **Tech plan:** `../tech-plan.md` §13 Workstream H (this task's row), §3 Constraints (separate component; the CLI never imports it).
- **H-1** — the `microvms` bindings surface (`Sandbox.run → Session`, `Session.run_sync → ExecResult`, tar up/download) this env drives; do not re-implement token/exec/tar plumbing the bindings own.
- Harbor's `BaseEnvironment` contract: `preflight`, `capabilities`, `resource_capabilities`, `start`/`stop`, `exec`, `upload_file`/`upload_dir`/`download_file`/`download_dir`, and the environment-type value it returns from `type()`.

## What To Build

Build `LambdaMicrovmsEnvironment(BaseEnvironment)`. The bindings own the daemon protocol; this class adds the four things Harbor needs on top of them:

1. **The build artifact.** microvms-core zips only Dockerfile + agentd, but the S3 upload is the caller's — so pack the task's **whole build context** alongside the appended daemon stanza (COPY instructions depend on it) and hand the core the same Dockerfile text for its local guards. Load `agentd`, append its stanza, pack the whole build context, and upload the artifact to S3.
2. **Content-addressed image reuse.** Name the image over the environment, the daemon binary, the stanza, the base, and the size class. Use boto3 (declared in `harvest-env`'s manifest, `>=1.43.35`) to check image state, wait on a concurrent trial's build, and delete before a forced rebuild or after a failed one — the bindings expose no image lookup at this version.
3. **Daemon semantics bridged to Harbor's exec contract.** The child environment starts empty, so capture the image ENV once from the daemon's `/proc`, resolve the exec identity/user, and run exec streaming with resume. **Exec as `["bash","-c",command]`** — Harbor's contract is bash semantics (`set -o pipefail`), and the daemon's `shell=True` is `/bin/sh -c` (dash on Debian-family images, which rejects `pipefail`). Do not exec through the daemon's `sh -c`.
4. **The lifecycle + capability surface.** Implement `start(force_build)` (ensure image → launch VM → capture env), `stop(delete)`, `capabilities`/`resource_capabilities`/`size_class`, `preflight` (creds/bucket/role present), and definition validation enforcing the platform limits below.

**Enforce the platform constraints** (they are hard limits, from the Lambda MicroVMs platform):
- **ARM64 only** — task images must build for `linux/arm64`; validate the agentd binary is aarch64 ELF.
- **One VM lives at most 8 hours.**
- **Network policy is fixed at launch:** `public` requests the managed INTERNET_EGRESS connector, `no-network` omits it. **Allowlists are not supported** — reject them.
- **Single container** — reject Docker Compose task environments.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `harvest_env/src/harvest_env/lambda_microvms.py` | Create | `LambdaMicrovmsEnvironment(BaseEnvironment)` — the importable module |
| `harvest_env/src/harvest_env/__init__.py` | Create | Makes `harvest_env` a real, importable package |
| `harvest_env/pyproject.toml` | Create | The `harvest-env` distribution manifest: declares `harbor`, `microvms>=0.5.0` (H-1), `boto3>=1.43.35`, `dockerfile-parse>=2.0.1`, plus whatever the client implementation needs |

Package the environment as its **own installable distribution** (`harvest-env`), not inside the `harbor.*` namespace — a regular installed package cannot be overlaid, so the module must live at `harvest_env.lambda_microvms` to be importable at all. Published Harbor ships **no** `[lambda-microvms]` extra, so the environment's dependencies go in `harvest-env`'s own manifest, not a Harbor extra. Develop against a Harbor checkout, but do **not** vendor Harbor's source or a patch into this repo: the environment is the project's own code (MIT-0), shipped as `harvest_env/` and loaded into stock `harbor==0.22.0` by import path. The change was not upstreamed as a stored patch; a Harbor PR, if ever made, is a `git diff` generated at that time.

## Schemas & Contracts

**`BaseEnvironment` methods this class overrides** (the contract Harbor calls):
```
type() -> _LambdaMicrovmsEnvType   # a module-local str subclass, value "lambda-microvms"
preflight() -> None
capabilities() -> EnvironmentCapabilities
resource_capabilities() -> EnvironmentResourceCapabilities
async start(force_build: bool) -> None
async stop(delete: bool) -> None
async exec(command, *, user, timeout_sec, output_callback, ...) -> ExecResult
async upload_file / upload_dir / download_file / download_dir
```
**Environment type — no dependency on a patched Harbor enum.** `type()` returns a **module-local** `str` subclass, `_LambdaMicrovmsEnvType("lambda-microvms")` (a `.value` of `"lambda-microvms"`). Do **not** import or reference `harbor.models.environment_type.EnvironmentType`, and do not add a member to it: published Harbor's enum has no `LAMBDA_MICROVMS` member, so any reference to it makes the module fail to import against a stock Harbor (an `AttributeError`, not a missing feature). The `str` subclass shape matters — Harbor's own code calls `.value` on the returned type, so a bare string would fail at that call site.

**Environment kwargs / env vars:** `s3_bucket` (or `MICROVM_BUCKET`), `build_role_arn` (or `MICROVM_BUILD_ROLE_ARN`) — the same variables the `microvm` CLI reads.

**Backward compatibility:** additive — a new environment implementation; touches no existing environment.

## How To Verify

```bash
# With the eval dependency group synced (installs harbor + harvest-env + microvms/boto3/dockerfile-parse):
uv sync --group eval
uv run --group eval python -c "from harvest_env.lambda_microvms import LambdaMicrovmsEnvironment"
uv run --group eval pytest tests/unit/environments/test_lambda_microvms.py -q   # H-4's behaviour suite
```

Then by hand: with AWS creds + `MICROVM_BUCKET` + `MICROVM_BUILD_ROLE_ARN`, a `harbor run … -e lambda-microvms -n 1` on a hello-world task should build an image, launch a VM, exec the agent, and score.

## Tests To Write

The suite is **H-4** (build-it-then-assert, bindings stubbed). This task's code must make those tests pass: build-artifact packing (context + stanza), image reuse/rebuild, the `["bash","-c",…]` exec contract, tar confinement, network-policy mapping, and the ARM64/8h/single-container/no-allowlist rejections.

## Acceptance Criteria

- [ ] `LambdaMicrovmsEnvironment(BaseEnvironment)` implements the full lifecycle + exec + file-transfer contract
- [ ] Build artifact packs the whole build context alongside the appended agentd stanza
- [ ] Images are content-addressed and reused; boto3 checks state, waits on concurrent builds, deletes before a forced/failed rebuild
- [ ] Exec runs `["bash","-c",command]` (bash/pipefail), never the daemon's `sh -c`
- [ ] ARM64-only, ≤8h, fixed `public`/`no-network` (no allowlists), single-container are all enforced and their violations rejected
- [ ] Packaged as an installable `harvest-env` distribution: `harvest_env.lambda_microvms:LambdaMicrovmsEnvironment` imports from a real module path (not `harbor.*`), with `microvms`/`boto3>=1.43.35`/`dockerfile-parse` (plus any client-implementation deps) declared in its own manifest
- [ ] `type()` returns a module-local `str` subclass valued `"lambda-microvms"`; the module references no `harbor` `EnvironmentType`, so it imports against a stock (unpatched) Harbor
- [ ] H-4's unit suite is green

## Out Of Scope

- Loading it into Harbor without a fork (import path vs. the `-e` shorthand) — that is **H-3**.
- The unit suite — that is **H-4**.
- Provisioning the AWS infra (Terraform stack, S3 bucket, build role) — deferred; tracked with H-3's live-trial `[TODO]`.

## Notes & Gotchas

- **Don't re-implement what the bindings own** (H-1): per-VM token via `runHookPayload`, proxy-token minting/refresh, idempotent detached exec, streaming-with-resume, confined tar. This class only adds the artifact, image reuse, and the daemon→Harbor bridging.
- **The `sh -c` → `bash -c` fix is not theoretical** — dash (the default `/bin/sh` on Debian-family images like `ubuntu:24.04`) rejects `set -o pipefail`, so an agent-setup step that relies on it fails under the daemon's `sh -c`. Keep exec on an argv array with no shell in between.
- **`boto3` is declared explicitly in `harvest-env`'s manifest with a `>=1.43.35` floor** — the `lambda-microvms` API model first shipped in boto3 1.43.35, so the floor is not incidental. Use the one boto3 client for image state; do not add a second AWS client.
- **Package it so it actually imports.** The environment is only useful if an unmodified Harbor can `import` it: ship `harvest_env/__init__.py` + `harvest_env/pyproject.toml`, declare the deps there (not in a non-existent Harbor extra), and make its imports fail *loudly and by name* before any AWS call rather than three frames deep inside Harbor. Loading it by import path is H-3.

## Dependencies

**Blocked by:** [H-1](H-1-establish-microvms-bindings-dependency.md)
**Blocks:** [H-3](H-3-load-into-unmodified-harbor-by-import-path.md), [H-4](H-4-environment-unit-test-suite.md)

