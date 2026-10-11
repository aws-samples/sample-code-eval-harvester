# Harbor Lambda MicroVM provider

`harvest-env` loads into stock Harbor `0.22.0` as
`harvest_env.lambda_microvms:LambdaMicrovmsEnvironment`. It uses the published
`microvms==0.12.0` wheel and its matching daemon. The CLI package keeps zero runtime
dependencies; only the `eval` group installs this provider.

```bash
uv sync --frozen --group eval
mise run eval-preflight
```

## What the SDK owns

The provider translates Harbor's lifecycle, exec, and transfer methods into SDK calls.
Each call that reaches AWS or the VM is the SDK's `_async` twin, awaited on Harbor's event
loop; the network work runs on the SDK's own runtime, so no SDK call holds a thread:

- `provision_agentd_async` obtains the matching daemon, verifies downloaded release
  artifacts, and checks ARM64 ELF architecture. It shares the SDK/CLI cache.
  `agentd_binary` and `HARBOR_AGENTD_BINARY` remain explicit overrides; their owners must
  supply a daemon compatible with SDK `0.12.0`.
- `wrap_dockerfile` and `BaseImage.from_dockerfile` prepare the daemon harness while
  preserving the task's base-image digest. `SizeClass.from_request` selects the baseline.
- `Sandbox.ensure_image_async` hashes the complete build inputs, packs the context, honors
  `Dockerfile.dockerignore` or `.dockerignore`, uploads only when needed, and handles
  image polling, failed/forced rebuilds, and concurrent creators. Prebuilt-image tasks
  receive their environment directory through Harbor's post-start upload instead.
- `Sandbox.run_async` waits for the platform and daemon. One `keep_awake(while_busy=False)`
  covers idle gaps between Harbor phases and stops during suspension or termination.
- `Session.run_to_completion_async` handles bash execution, guest user names, inherited image
  environment, streaming, acknowledgement, timeout cleanup, and POSIX exit codes.
  Harbor's environment overrides and working-directory precedence are preserved. A
  small numeric-UID lookup preserves the user's passwd primary group because the SDK
  intentionally retains the daemon's group for numeric UIDs without an explicit group.
- Harbor's tar helpers remain in charge of host-side directory packing and safe
  extraction; SDK workspace-sync helpers have different path and exclusion semantics.

The SDK lets a cancelled build or launch run to completion, so a cancelled launch still
allocates its VM. The provider keeps each build and launch as a task and awaits it through
`asyncio.shield`; if Harbor cancels `start`, `stop` waits for that task and then terminates
the VM it allocated. The provider records the VM id when the launch returns, because a
`Sandbox` property waits for the sandbox's lock, which a running exec holds, and teardown
must not wait on the event loop. Cancelling an exec stops the SDK's drive but leaves the
command running in the VM, and a one-shot kill can miss an exec not yet registered, so a
cancelled exec terminates the trial's VM. Streamed output reaches Harbor's coroutine
callback in order through a queue; a callback that fails, or takes more than 30 seconds
on one chunk, stops delivery and fails the exec once the SDK has acknowledged it.
`stop(delete=True)` waits for observed termination and raises on reported cleanup
failure, retaining the handle for a retry. `stop(delete=False)` suspends the VM.
Reusable images, S3 artifacts, and CloudWatch logs remain for reuse and diagnosis.

## Settings and migration

Required settings are `s3_bucket`, `build_role_arn`, and `region`, or the corresponding
`MICROVM_BUCKET`, `MICROVM_BUILD_ROLE_ARN`, and `AWS_REGION` environment variables.
`AWS_DEFAULT_REGION` is also accepted. `execution_role_arn` or
`MICROVM_EXECUTION_ROLE_ARN` sets the guest role. Existing duration, idle, readiness,
S3-prefix, and managed-base options retain their meanings.

Use `--ek image_name_prefix=my-eval` to name the image cache. The SDK appends a
12-character content hash. `image_name` is a deprecated alias for the prefix and logs
a warning; it does not select an exact image name. The SDK's hashing scheme produces
different names from the previous provider, and SDK `0.12.0` moved that hash to version 2
so the name also covers the fields the service fixes at image creation. Expect one rebuild
after upgrading from either, and retain or remove old images through your normal
resource-management process.

Network isolation is not available: `egress=False` omits `INTERNET_EGRESS` but does
not block outbound traffic, and the SDK cannot verify VPC routing. The provider
rejects `no-network` and allowlist requests. Default emitted tasks and the eval
verifier require `no-network` and cannot run here until that requirement can be
enforced. This integration does not weaken those task policies.

Other limits are ARM64 Linux, one container, no Compose, no strict CPU/memory
enforcement, and an eight-hour maximum lifetime. Guest execution-role credentials
remain accessible to tasks. See the [eval setup](../eval/README.md) for the separate
Harbor Bedrock credential-forwarding constraints.

## Verification

```bash
mise run check
mise run test-harbor
mise run eval-preflight
```

The provider tests use the published SDK's pure helpers and validate fake AWS/session
calls against the installed native method signatures. The fakes define only the `_async`
methods, so a blocking SDK call fails the suite. The tests exercise Harbor behavior,
timeout/result mapping, ordered output delivery and its failure and stall paths, safe
transfers, cancellation of a launch the SDK finishes anyway, and cleanup failure/retry
without AWS. These checks do not constitute a live VM trial.

Sources checked October 11, 2026:

- [PyPI release metadata](https://pypi.org/pypi/microvms/json), latest stable `0.12.0`.
- [SDK v0.12.0 binding guide, "Use it from asyncio"](https://github.com/laithalsaadoon/microvms-agentd/blob/v0.12.0/bindings/microvms-py/README.md)
  and the published wheel's `microvms/__init__.pyi`.
- [SDK v0.12.0 embedding guide](https://github.com/laithalsaadoon/microvms-agentd/blob/v0.12.0/docs/EMBEDDING.md).
- [SDK v0.12.0 networking contract](https://github.com/laithalsaadoon/microvms-agentd/blob/v0.12.0/docs/NETWORKING.md).
- [Harbor custom-sandbox contract](https://docs.harborframework.com/sandboxes/custom-sandboxes.md)
  and the installed Harbor `0.22.0` `BaseEnvironment` source.
