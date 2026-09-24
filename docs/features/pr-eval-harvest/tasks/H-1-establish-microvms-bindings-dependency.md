---
id: H-1
title: "[Env] Establish the microvms bindings dependency and its typed surface"
feature: pr-eval-harvest
workstream: Lambda MicroVMs execution environment
status: done
complexity: S
implements: []
user_story: US-6
blocked_by: []
blocks: [H-2]
---

# H-1: [Env] Establish the microvms bindings dependency and its typed surface

## Context

The CLI emits Harbor task directories (US-6); Workstream G's eval has to *run* them, and the PRD fixes the execution substrate as **Lambda MicroVMs** (PRD §7 Constraints, "Execution is Lambda MicroVMs"). That environment (H-2) is a thin client over the published `microvms` Python bindings — a PyO3 extension over `microvms-core` from [microvms-agentd](https://github.com/theagenticguy/microvms-agentd) that owns everything a hand-rolled daemon/client used to: the per-VM agent token (delivered via `runHookPayload`, never baked into the shared snapshot), proxy-token minting and refresh inside every request, idempotent detached exec with caller-minted ids, SSE output streaming with resume, and confined tar extraction. This task pins that dependency and captures its typed surface so H-2 has a stable, type-checkable client to build against. Nothing else in Workstream H builds until it exists.

**North star:** an SDE gets Harbor datapoints they can hand to `harbor run` — and the eval that proves the agent can build them runs those tasks on this environment.
**Implements:** none (execution-substrate infrastructure; PRD §7 constraint).  ·  **User story:** US-6.

## Design References

- **PRD:** `../prd.md` §7 Constraints — "Execution is Lambda MicroVMs" (the environment is built as part of this project; the CLI depends on none of it).
- **Tech plan:** `../tech-plan.md` §13 Workstream H (the workstream this task opens), §3 Constraints (the environment is a separate component the `eval-harvest` package never imports).
- Upstream: `microvms-agentd` `origin/main` at v0.6.0; PyPI `microvms==0.6.0`. The bindings depend on the sibling `microvms-core` crate, so they do **not** build standalone.

## What To Build

1. Pin `microvms==0.6.0` as the bindings dependency the Lambda MicroVMs environment imports (H-2 does `import microvms`). The normal install path is `pip install microvms==0.6.0`. Published Harbor exposes **no** `[lambda-microvms]` extra, so this dependency is declared in the packaged environment's *own* manifest (`harvest_env/pyproject.toml`, created in H-2), with a `microvms>=0.5.0` floor — not in any Harbor extra.
2. Do **not** vendor a copy of the bindings' source or its `microvms.pyi` stub. The bindings ship no `py.typed`, so H-2 type-checks against them as `Any` (mypy's `ignore_missing_imports` for `microvms` in `pyproject.toml`); the typed surface H-2 codes against — `Sandbox.run(...) -> Session`; `Session.run_sync(cmd, ...) -> ExecResult` (with `stdout`/`stderr`/`exit_code`); `Session.upload_file(...)` / `download_tar(...)`; the region/cost/hooks helpers — is documented upstream (the `microvms.pyi` in the `microvms-agentd` repo), not copied here.
3. Record provenance in prose only: the bindings are built upstream (`maturin build --release` inside a `microvms-agentd` checkout, against the sibling `microvms-core` crate). **This project does not rebuild the Rust and does not vendor its source** — it consumes the published wheel from PyPI (fetched on demand by the install), and points at the upstream repo for the source and stub.

State explicitly what NOT to do: do **not** vendor `microvms-core`, the bindings' source, or the `.pyi` stub, and do not attempt a standalone build of the bindings from this repo — the crate dependency makes that fail, and it is upstream's concern.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| Packaged environment manifest (`harvest_env/pyproject.toml`) | (created in H-2) | Declares `microvms` (floor `>=0.5.0`) alongside `boto3`/`dockerfile-parse`/`harbor` — published Harbor has no `[lambda-microvms]` extra to add it to |
| `pyproject.toml` (`[[tool.mypy.overrides]]`) | (already present) | `microvms` set to `ignore_missing_imports` — the bindings ship no `py.typed`, so no vendored stub is needed |

The bindings' source and `microvms.pyi` are **not** vendored; they live upstream at
[`microvms-agentd`](https://github.com/theagenticguy/microvms-agentd) (v0.6.0) and the wheel comes
from PyPI.

## How To Verify

```bash
# The pin resolves and the wheel imports (in an env with the eval group synced):
uv run --group eval python -c "import microvms; print(microvms.__version__)"   # 0.6.0
```

Then by hand: confirm the upstream `microvms.pyi` declares `Sandbox.run`, `Session.run_sync`
(returning something with `stdout`/`stderr`/`exit_code`), and the tar up/download methods H-2 calls —
that is the surface H-4's fake `microvms` must match.

## Tests To Write

Bindings are third-party (their own suite lives upstream). The only thing this task owns is that the pin resolves and the stub matches the surface H-2 uses — exercised transitively by H-4's environment tests, which stub `microvms` against this surface.

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| (covered by H-4) | H-4's fake `microvms` matches `microvms.pyi` | The environment coding against a method the real bindings do not expose |

## Acceptance Criteria

- [ ] `microvms==0.6.0` is the pinned bindings dependency the environment imports
- [ ] The surface H-2 uses (`Sandbox.run`, `Session.run_sync` → `ExecResult`, tar up/download) is documented upstream and mypy treats `microvms` as `ignore_missing_imports` (no vendored stub)
- [ ] Provenance recorded in prose: the Rust bindings are built upstream, not rebuilt in this repo
- [ ] `microvms-core`, the bindings' source, and the `.pyi` stub are **not** vendored or built here

## Out Of Scope

- Building the `LambdaMicrovmsEnvironment` that drives the bindings — that is **H-2**.
- Standing up AWS infra (S3 bucket, build role) — deferred; tracked with H-3's live-trial `[TODO]`.

## Notes & Gotchas

- **The bindings own the security-sensitive plumbing.** Per-VM token delivery, proxy-token minting, exec idempotency, streaming-with-resume, and confined tar extraction all live in the bindings — H-2 must not re-implement them, only call them.
- **Not standalone-buildable.** The bindings depend on the sibling `microvms-core` crate; `maturin build` only works inside a full `microvms-agentd` checkout. Do not vendor their source — consume the published wheel and point at the upstream repo for reference.

## Dependencies

**Blocked by:** nothing — ready to start (parallel with A-1)
**Blocks:** [H-2](H-2-build-lambda-microvms-environment.md)

