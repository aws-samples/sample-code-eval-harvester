---
id: H-4
title: "[Env] Environment unit-test suite (microvms bindings stubbed)"
feature: pr-eval-harvest
workstream: Lambda MicroVMs execution environment
status: todo
complexity: L
implements: []
user_story: US-6
blocked_by: [H-2]
blocks: []
---

# H-4: [Env] Environment unit-test suite (microvms bindings stubbed)

## Context

`LambdaMicrovmsEnvironment` (H-2) has a lot of essential logic that never touches a live VM in CI: how it packs the build context, names images for reuse, maps network policy, bridges the daemon's exec to Harbor's bash contract, and rejects tasks that violate the platform's limits. All of it can and must be tested offline, with the `microvms` bindings and boto3 stubbed — because a defect here fails a real trial expensively and late. This task is that suite: build-it-then-assert, one scenario per behaviour that can silently go wrong.

**North star:** the substrate the eval runs on is trustworthy before a single credentialed trial is spent.
**Implements:** none (test coverage for H-2).  ·  **User story:** US-6.

## Design References

- **Tech plan:** `../tech-plan.md` §13 Workstream H (H-4 row).
- **H-2** — the class under test; **H-1** — the `microvms.pyi` surface the fake bindings must match.
- Harbor's existing environment unit tests as the shape to mirror (fixtures + stubbed provider).

## What To Build

A `pytest` suite exercising `LambdaMicrovmsEnvironment` with `microvms` and boto3 stubbed (no AWS, no VM):

1. **Build artifact.** The packed artifact contains the whole task build context **and** the appended agentd stanza; the Dockerfile handed to the core's guards matches; a stanza-less Dockerfile or a mismatched base is refused by the guards.
2. **Image reuse.** The content-addressed `image_name` is stable over the same (environment, daemon, stanza, base, size class) and changes when any of them changes; boto3 is used to check state, wait on a concurrent build, and delete before a forced/failed rebuild.
3. **Exec contract.** Commands run as `["bash","-c",command]` (not `sh -c`); `_to_exec_result` maps the daemon result to `ExecResult` (stdout/stderr/exit_code); streaming-with-resume acks correctly.
4. **Tar confinement.** `upload_dir`/`download_dir` refuse path-escaping entries.
5. **Constraint rejections** (each its own break-it test): a non-aarch64 agentd binary is rejected; a Docker Compose task environment is rejected; a network allowlist is rejected; the `public`/`no-network` policies map to INTERNET_EGRESS-on / -off.
6. **Packaging + import-path load** (H-3), in `tests/test_environment_packaging.py`: `harvest_env.lambda_microvms:LambdaMicrovmsEnvironment` resolves through Harbor's own `import_class` on an unmodified Harbor; the module imports against a stock Harbor whose `EnvironmentType` has no `LAMBDA_MICROVMS` member; the `eval` group provides `microvms`/`boto3`/`dockerfile-parse`; the import preflight names a missing dependency by name (before any AWS call). These need Harbor + the `eval` group, so they **skip visibly** (a named reason, not silently) when it is not synced.
7. **The gate stays green.** Packaging the environment and running trials must not turn `mise run check` red. `harvest_env/` imports Harbor internals only present in the `eval` group, and each trial copies the `eval_harvest` package into generated run output — so `harvest_env/` and the generated trees (`eval/runs/`, `work/`, `eval/reports/`) are excluded from mypy/ruff/bandit, kept in step with `.gitignore`. Pin the exclusions with a test that parses `pyproject.toml`.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `tests/test_lambda_microvms.py` | Create | the behaviour suite, `microvms`/boto3 stubbed. The project's own MIT-0 test, in-repo (not vendored, not shipped in a patch); the `async def` tests run under `anyio`. Gated behind the `eval` group; skips **visibly** when it is not synced |
| `tests/test_environment_packaging.py` | Create | Packaging + import-path suite for the `harvest-env` distribution: module imports from the real path, the import-path constant resolves via Harbor's `import_class`, the deps are present, the import preflight names a missing one, and the module imports against a stock (unpatched) Harbor. Gated behind the `eval` group; skips **visibly** when it is not synced |
| `pyproject.toml` | Modify | Exclude `harvest_env/` and generated run output (`eval/runs/`, `work/`, `eval/reports/`) from mypy/ruff/bandit, so packaging the env and running trials does not turn `mise run check` red |

## How To Verify

```bash
uv sync --group eval
uv run --group eval pytest tests/test_lambda_microvms.py -q   # behaviour suite (stubbed)
uv run --group eval pytest tests/test_environment_packaging.py -q               # packaging + import-path
mise run check                                                                  # must stay green
```

Then by hand: confirm no test reaches AWS or launches a VM (the provider is stubbed), and that each constraint-rejection test asserts the *refusal*, not just the happy path.

## Tests To Write

The whole task is tests. Each names the defect it catches:

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_artifact_packs_context_and_stanza` | packed zip = build context + agentd stanza | COPY steps failing at build because the context was dropped |
| `test_image_name_content_addressed` | name stable/changes with inputs | A stale image reused after the environment changed |
| `test_exec_uses_bash_not_sh` | argv is `["bash","-c",…]` | `pipefail` rejected by dash under `sh -c` |
| `test_tar_extraction_confined` | escaping tar entries refused | A task writing outside its tree via a crafted tar |
| `test_rejects_non_aarch64 / _compose / _allowlist` | each platform limit refused | A trial launched into an unsupported configuration |
| `test_import_path_constant_matches_a_real_module` | the import path resolves via Harbor's `import_class` on stock Harbor | A hidden dependency on the fork (H-3), or a constant that drifts from the package |
| `test_environment_imports_against_stock_harbor` | the module imports with no `LAMBDA_MICROVMS` enum member present | The module re-referencing a patched Harbor enum |
| `test_preflight_names_the_missing_package` | a missing dep is reported by name before any AWS call | A bare `ModuleNotFoundError` three frames deep inside Harbor |
| `test_generated_output_dirs_are_excluded_from_static_analysis` | `harvest_env/` + the generated trees are in every tool's exclude list | Packaging/running the env turning `mise run check` red |

## Acceptance Criteria

- [ ] The behaviour suite runs fully offline with `microvms`/boto3 stubbed — no AWS, no VM
- [ ] Build-artifact, image-reuse, bash-exec, tar-confinement, and every constraint rejection are each covered
- [ ] The packaging + import-path load (H-3) is covered in `tests/test_environment_packaging.py`, resolving via Harbor's `import_class` and importing against a stock Harbor; the Harbor-dependent tests skip **visibly** when the `eval` group is not synced
- [ ] `mise run check` stays green with the env packaged and trial output on disk — `harvest_env/` and the generated trees are excluded from mypy/ruff/bandit, pinned by a test
- [ ] `pytest tests/test_lambda_microvms.py` is green

## Out Of Scope

- Live MicroVM / AWS integration tests — gated on the credential `[TODO]`; they are not part of the offline suite.
- The eval's own scoring tests — Workstream G (G-1/G-2/G-3).

## Notes & Gotchas

- **Stub to the `microvms.pyi` surface (H-1)** — if the fake diverges from the real bindings, the suite passes while the environment breaks live. Keep the fake honest to the stub.
- **Break-it-then-fix-it for the constraint tests** — assert the rejection fires, the same discipline the CLI's US-7 guards use (CLAUDE.md).
- **Gitignored is not excluded.** `.gitignore` and each tool's exclude list are separate; a generated tree (`eval/runs/`, `work/`) or the packaged `harvest_env/` being gitignored/out-of-scope does not stop mypy/ruff/bandit walking it — two copies of `eval_harvest` under `eval/runs/` collide on module name and stop mypy dead. Add the exclusions explicitly, comment each to name `.gitignore`, and watch the collision return when reverted so the exclusion is proven to be the thing doing the work.

## Dependencies

**Blocked by:** [H-2](H-2-build-lambda-microvms-environment.md)
**Blocks:** nothing

