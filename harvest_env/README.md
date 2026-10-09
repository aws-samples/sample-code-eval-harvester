# Harbor Lambda MicroVM provider

`harvest-env` loads into stock Harbor `0.22.0` as
`harvest_env.lambda_microvms:LambdaMicrovmsEnvironment`. It uses the published
`microvms==0.11.0` wheel and its matching daemon. The CLI package keeps zero runtime
dependencies; only the `eval` group installs this provider.

```bash
uv sync --frozen --group eval
mise run eval-preflight
```

## What the SDK owns

The provider translates Harbor's lifecycle, exec, and transfer methods into SDK calls:

- `provision_agentd` obtains the matching daemon, verifies downloaded release artifacts,
  and checks ARM64 ELF architecture. It shares the SDK/CLI cache. `agentd_binary` and
  `HARBOR_AGENTD_BINARY` remain explicit overrides; their owners must supply a daemon
  compatible with SDK `0.11.0`.
- `wrap_dockerfile` and `BaseImage.from_dockerfile` prepare the daemon harness while
  preserving the task's base-image digest. `SizeClass.from_request` selects the baseline.
- `Sandbox.ensure_image` hashes the complete build inputs, packs the context, honors
  `Dockerfile.dockerignore` or `.dockerignore`, uploads only when needed, and handles
  image polling, failed/forced rebuilds, and concurrent creators. Prebuilt-image tasks
  receive their environment directory through Harbor's post-start upload instead.
- `Sandbox.run` waits for the platform and daemon. One `keep_awake(while_busy=False)`
  covers idle gaps between Harbor phases and stops during suspension or termination.
- `Session.run_to_completion` handles bash execution, guest user names, inherited image
  environment, streaming, acknowledgement, timeout cleanup, and POSIX exit codes.
  Harbor's environment overrides and working-directory precedence are preserved. A
  small numeric-UID lookup preserves the user's passwd primary group because the SDK
  intentionally retains the daemon's group for numeric UIDs without an explicit group.
- Harbor's tar helpers remain in charge of host-side directory packing and safe
  extraction; SDK workspace-sync helpers have different path and exclusion semantics.

SDK lifecycle calls and native property reads run on one worker. If Harbor cancels a
build or launch, teardown queues after that worker. A canceled exec terminates the
trial's VM because a one-shot exec kill can miss registration by a delayed worker.
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
different names from the previous provider, so expect an initial rebuild and retain
or remove old images through your normal resource-management process.

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
calls against the installed native method signatures. They exercise Harbor behavior,
timeout/result mapping, output callbacks, safe transfers, delayed cancellation, and
cleanup failure/retry without AWS. These checks do not constitute a live VM trial.

Sources checked October 9, 2026:

- [PyPI release metadata](https://pypi.org/pypi/microvms/json), latest stable `0.11.0`.
- [SDK v0.11.0 embedding guide](https://github.com/laithalsaadoon/microvms-agentd/blob/v0.11.0/docs/EMBEDDING.md)
  and the published wheel's `microvms/__init__.pyi`.
- [SDK v0.11.0 networking contract](https://github.com/laithalsaadoon/microvms-agentd/blob/v0.11.0/docs/NETWORKING.md).
- [Harbor custom-sandbox contract](https://docs.harborframework.com/sandboxes/custom-sandboxes.md)
  and the installed Harbor `0.22.0` `BaseEnvironment` source.
