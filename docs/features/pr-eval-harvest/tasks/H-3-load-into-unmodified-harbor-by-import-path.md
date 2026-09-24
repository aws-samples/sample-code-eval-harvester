---
id: H-3
title: "[Env] Load the environment into an unmodified Harbor by import path (no fork)"
feature: pr-eval-harvest
workstream: Lambda MicroVMs execution environment
status: done
complexity: S
implements: []
user_story: US-6
blocked_by: [H-2]
blocks: [G-1]
---

# H-3: [Env] Load the environment into an unmodified Harbor by import path (no fork)

## Context

`LambdaMicrovmsEnvironment` (H-2) has to reach Harbor's environment registry so `harbor run` can dispatch trials to it. The obvious way is to fork Harbor and edit its `EnvironmentType` enum + `_ENVIRONMENT_REGISTRY` — but a fork is a standing maintenance cost this project does not want to carry. This task establishes the **no-fork** path: `EnvironmentFactory.create_environment_from_config` honours `config.import_path`, so the environment loads into an **unmodified** Harbor by import path (`'module.path:ClassName'`). The enum/registry edits are kept only as the documented *alternative* that buys the `-e lambda-microvms` CLI shorthand and nothing else. This is the wiring Workstream G's harness (G-1) uses to run the eval without shipping a patched Harbor.

**North star:** run the emitted datapoints on the MicroVMs substrate without maintaining a Harbor fork.
**Implements:** none (wiring; resolves tech plan §3 "No fork of Harbor is required").  ·  **User story:** US-6.

## Design References

- **Tech plan:** `../tech-plan.md` §3 Constraints — "No fork of Harbor is required. `EnvironmentFactory.create_environment_from_config` honours `config.import_path` … The design depends on this being run, not just read — the one dispatched proving trial stays a PRD `[TODO]`."
- **PRD:** `../prd.md` §4 evidence table ("An out-of-tree execution environment registers by import path without forking Harbor") and §9 ("The environment-by-import-path finding … `[TODO: prove it with one real dispatched trial]`").
- **H-2** — the class being loaded.
- Harbor's `environments/factory.py`: `create_environment_from_config` branches on `config.import_path` vs. `config.type`; `create_environment_from_import_path('module:ClassName')` is the loader.

## What To Build

1. **The import-path load path.** Confirm and document that setting `config.import_path = 'harvest_env.lambda_microvms:LambdaMicrovmsEnvironment'` (the packaged distribution H-2 builds) makes `create_environment_from_config` construct the environment with no fork — it branches to `create_environment_from_import_path`, which resolves the class through Harbor's own `harbor.utils.import_path.import_class`; `run_preflight` and `resource_capabilities` honour `import_path` alike (via `import_symbol`), so the whole trial lifecycle works out-of-tree. The module must live at `harvest_env.*`, not `harbor.environments.*`: the `harbor.*` namespace belongs to the installed distribution and cannot be overlaid.
2. **The `-e lambda-microvms` shorthand as the fork-only alternative.** Record that the enum value `LAMBDA_MICROVMS = "lambda-microvms"` in `models/environment_type.py` and the `_ENVIRONMENT_REGISTRY` entry in `factory.py` are the *only* thing a fork buys — the CLI shorthand — and are not required when loading by import path.
3. **Do NOT** add anything to the `eval-harvest` package for this. The wiring lives in the environment/Harbor config, and G-1 (in `eval/`) selects it; `src/eval_harvest/` never imports Harbor or the environment (S-18).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| Harbor `environments/factory.py` | Modify (fork alternative only) | registers `-e lambda-microvms`; **not needed** on the import-path path |
| Harbor `models/environment_type.py` | Modify (fork alternative only) | adds the enum value; **not needed** on the import-path path |
| (import-path path) | — | No Harbor edit; set `config.import_path = 'harvest_env.lambda_microvms:LambdaMicrovmsEnvironment'` and load an unmodified Harbor |

## How To Verify

```bash
# Import-path path (no fork), against an UNMODIFIED Harbor:
harbor run -d "<org/name>" -m "<model>" -a "<agent>" \
  --environment-import-path "harvest_env.lambda_microvms:LambdaMicrovmsEnvironment" -n 1
# or the fork shorthand:
harbor run -d "<org/name>" -m "<model>" -a "<agent>" -e lambda-microvms -n 1
```

Then by hand: the factory constructs `LambdaMicrovmsEnvironment` from `import_path` on a stock Harbor with no enum/registry edits present.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_import_path_constant_matches_a_real_module` | the configured import path resolves through Harbor's own `import_class` (the same call the factory makes) to `LambdaMicrovmsEnvironment`, on an unmodified Harbor | A silent dependency on the fork's enum/registry edits, or a constant that drifts from the packaged module |
| `test_environment_imports_against_stock_harbor` | the packaged module imports with a Harbor whose `EnvironmentType` has no `LAMBDA_MICROVMS` member (i.e. stock Harbor) | The module re-acquiring a reference to a patched enum member |

(These live in `tests/test_environment_packaging.py`, gated behind the `eval` group; see H-4.)

## Acceptance Criteria

- [ ] The environment loads into an **unmodified** Harbor via `config.import_path` — no fork required
- [ ] The enum + registry edits are documented as the shorthand-only alternative, not a requirement
- [ ] Nothing under `src/eval_harvest/` imports Harbor or the environment (S-18 still green)
- [ ] **One live dispatched trial** proves the import-path load end-to-end on real MicroVMs — remains the PRD `[TODO]` (needs the AWS creds/bucket/build-role that PRD §9 also flags)

## Out Of Scope

- Building the environment itself — **H-2**.
- The eval that dispatches through it — **G-1**.
- Provisioning the AWS infra to run the live proving trial — a separate, later task (the credential `[TODO]`).

## Notes & Gotchas

- **Read, then run.** The import-path finding is validated *by reading* `factory.py` (PRD §4); the design still owes one real dispatched trial before it fully rests on it (PRD §9). Keep that box unchecked until a trial runs — do not mark the `[TODO]` closed on the strength of the code read alone.
- **The fork would buy exactly one thing, and the packaged module needs none of it.** A `_ENVIRONMENT_REGISTRY` entry only adds the `-e lambda-microvms` CLI shorthand; the import-path path already registers the environment. A separate `EnvironmentType.LAMBDA_MICROVMS` enum member is a different thing — the module must not *reference* it, or it would fail to import on a stock Harbor (an `AttributeError`, not a missing shorthand). Keep `harvest_env.lambda_microvms` free of that reference so it needs neither the enum member nor the registry entry, and loads by import path alone.

## Dependencies

**Blocked by:** [H-2](H-2-build-lambda-microvms-environment.md)
**Blocks:** [G-1](G-1-eval-harness-harbor-task-microvm-runner-trajectory-capture.md)

