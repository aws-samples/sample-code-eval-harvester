---
id: D-2
title: "[Emission] Build harbor.py: constants + task-config/layout validators"
feature: pr-eval-harvest
workstream: Emission
status: todo
complexity: L
implements: [TP-4, FR-28, FR-32]
user_story: US-6
blocked_by: [D-1]
blocks: [D-3, F-1]
---

# D-2: [Emission] Build harbor.py: constants + task-config/layout validators

## Context

US-6 correctness rests on Harbor behavioural facts that Harbor's docs get wrong. This task builds the correct constants and validation functions into a stdlib-only `harbor.py`: the `task.toml` document builder, the schema validators (`validate_task_config`, `validate_task_layout`), the dataset-manifest builder, and the mirrored-Harbor-version constant. It uses `tomlw.py` (D-1) to serialize. Everything `emit` (D-3) and `dataset` (F-1) write goes through here, so it is the correctness chokepoint for the emitted format.

**North star:** the emitted task actually runs under Harbor — not a near-miss format that fails at `harbor run`.
**Implements:** TP-4, FR-28, FR-32  ·  **User story:** US-6

## Design References

- **Tech plan:** `../tech-plan.md` §2.3 TP-4 — "a stdlib-only Harbor serializer carrying the correct `task.toml` constants and schema-validation functions"
- **Tech plan:** §3 Constraints — the binding Harbor-source facts (exit code ignored / reward file only; `separate` mode mandatory; unknown keys drop so provenance goes under `[metadata.*]`; local path falls back to `Mean()`)
- **Tech plan:** §8 — the exact `task.toml` shape to emit and validate (schema_version 1.4, sections, `[metadata.origin]`/`[metadata.harvest]`)
- **Tech plan:** §4 Risks, "Harbor behaviour drifts from the mirrored constants" — record the mirrored version in a constant; validate before writing
- **Harbor's source** is the source of truth for the constants and validators this builds: `SCHEMA_MIRROR_SOURCE = "harbor 0.22.0"`, `task_config_document`, `validate_task_layout`, `validate_task_config`, the `_check_task_section`/`_check_verifier_section`/`_check_environment_section` helpers, `dataset_manifest_document`, `validate_dataset_manifest`, `dataset_content_hash`, `local_registry_document`. Read the binding Harbor-source facts in §3 Constraints (they cite Harbor's `config.py`/`verifier.py`/`trial.py`/`job.py`)

## What To Build

1. Create `src/eval_harvest/harbor.py`, stdlib-only: `tomlw.py` (D-1) for serialization, a plain `SchemaViolation` dataclass, and the plain value dataclasses the builders/validators need — `TaskOrigin` and `HarvestFields` (the `[metadata.origin]`/`[metadata.harvest]` inputs to `task_config_document`), plus `CompiledTask`/`EmittedFile` for layout validation — no pydantic (ADR-2).
2. Carry `SCHEMA_MIRROR_SOURCE = "harbor 0.22.0"` (or the current mirrored version) as a constant, so drift is traceable (§4 risk).
3. Build `task_config_document(...)` to produce the §8 `task.toml` dict (schema_version 1.4; `[task]`/`[agent]`/`[verifier]`/`[verifier.environment]`/`[environment]`; provenance under `[metadata.origin]`; harvest fields under `[metadata.harvest]`). It returns a `TomlValue` dict for `tomlw.emit_document`.
4. Build `validate_task_config(document)` and its `_check_*` section helpers, and `validate_task_layout(task)` — returning `list[SchemaViolation]` (do not raise). These enforce FR-28 (self-contained, all parts present) and FR-32 (`verifier.environment_mode == "separate"`; refuse `shared`).
5. Build `dataset_manifest_document`, `validate_dataset_manifest`, `dataset_content_hash`, and `local_registry_document` for F-1 to build on.
6. Keep the validators as plain functions returning violation lists (ADR-2) — do not reach for pydantic models.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/harbor.py` | Create | Constants + document builders + validators, built against Harbor's source |
| `tests/test_harbor.py` | Create | Cases from the test table |
| `tests/test_harbor_loader_contract.py` | Create | The one test module that imports real Harbor and asserts an emitted task *loads* |

Imports `src/eval_harvest/tomlw.py` (D-1) for serialization.

## Schemas & Contracts

**`task.toml` document (§8, what `task_config_document` builds):** `schema_version = "1.4"`, `[task]`, `[agent].timeout_sec`, `[verifier].environment_mode = "separate"`, `[verifier.environment]`/`[environment]` with `network_mode`/`os = "linux"`, `[metadata.origin]` (repo, pr_numbers, base_commit, base_commit_date, merged_at), `[metadata.harvest]` (kind, expected_verdict, blocking_severity, finding_severities, change_risk_*, rubric_version, overrides).

**Validator contract (matches Harbor's schema, `validate_task_config`/`validate_task_layout`):**
```
validate_task_config(document: Mapping[str, TomlValue]) -> list[SchemaViolation]
validate_task_layout(task: CompiledTask) -> list[SchemaViolation]
```

**Migration:** none.
**Backward compatibility:** none — new module. Behaviour must match the mirrored Harbor version recorded in `SCHEMA_MIRROR_SOURCE`.

## How To Verify

```bash
mise run test -- tests/test_harbor.py
mise run lint
mise run typecheck
```

Then by hand: build a task-config document, emit with `tomlw`, re-parse with `tomllib`, run `validate_task_config` — zero violations; flip `environment_mode` to `shared` and confirm a violation names it.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_valid_config_has_no_violations` | a correct §8 document validates clean | A false-positive validator blocking good tasks |
| `test_shared_mode_rejected` | `environment_mode = "shared"` produces a violation | A `shared`-mode task the evaluated agent could pre-plant a reward into (FR-32, §3) |
| `test_layout_requires_all_parts` | a task missing `tests/`/`environment/`/`instruction.md` fails layout validation | An incomplete task that only fails at `harbor run` (FR-28) |
| `test_metadata_preserved_under_metadata_namespace` | provenance sits under `[metadata.origin]`, not a top-level key Harbor drops | Provenance silently vanishing because Harbor drops unknown keys (§3) |
| `test_timeout_is_float` | `timeout_sec` serializes as `1800.0` | Harbor schema rejecting an int timeout (§8, D-1 interplay) |
| `test_schema_mirror_source_recorded` | `SCHEMA_MIRROR_SOURCE` is present and non-empty | Untraceable drift when Harbor changes (§4 risk) |
| `test_harbor_loads_an_emitted_task` (loader contract) | real Harbor's `Task.is_valid_dir` accepts an emitted task, and rejects one missing `task.toml`/`instruction.md`/`environment/` | The port drifting from Harbor's real loader — the emitter and its own tests both wrong, green meaning nothing |

The loader-contract test is the **only** module in the suite that imports `harbor` itself. Because a
port built by reading a third party's source is exactly what drifts when that source moves, it is run
by hand against a real Harbor checkout via `mise run test-harbor` (which syncs the `eval` dependency
group) and is **not** part of `mise run check` — `check` stays green without that group, with the
Harbor-dependent tests skipped visibly. Its measured limit is that Harbor "loads" is not Harbor
"grades": `Task.is_valid_dir` accepts a `task.toml` with an unknown verifier mode and a task with
`tests/` deleted, so this test proves a required file has not been renamed and the manifest schema has
not changed — the end-to-end grading guard is the smoke datapoint (E-3).

## Acceptance Criteria

- [ ] `task_config_document` builds the §8 shape; it round-trips through `tomlw` + `tomllib`
- [ ] `validate_task_config` returns violations for `shared` mode, missing sections, and bad types; clean for a correct document (FR-28, FR-32)
- [ ] `validate_task_layout` catches a missing required part (FR-28)
- [ ] Provenance lives under `[metadata.*]`; `SCHEMA_MIRROR_SOURCE` is recorded
- [ ] Validators return `list[SchemaViolation]`, never raise
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Assembling the actual task directory (Dockerfile, patch, instruction, verifier files) — that is `emit` (D-3), which calls these builders/validators.
- The dataset distribution *report* — that is F-1 (this task ports the manifest/registry document builders F-1 uses).
- Any *dispatched* `harbor run` smoke test — gated on the Lambda MicroVMs `[TODO]` (S-10), not this task. The loader-contract test above imports Harbor and asserts a task *loads*; it dispatches nothing.

## Notes & Gotchas

- **The three Harbor-source facts (§3 Constraints)** are why this file is long, not why to shorten it: (1) exit code ignored, reward file only; (2) `shared` mode is agent-scoreable — `separate` is mandatory; (3) local path collapses to `Mean()` so every reward key must be meaningful averaged. Build the validators that enforce these against Harbor's source, do not re-derive from docs.
- **No pydantic (ADR-2).** Values are plain dataclasses; the validators are plain functions returning violation lists — that is the shape to keep.
- **`SCHEMA_MIRROR_SOURCE`** is the drift anchor. If the mirrored Harbor version differs from `0.22.0`, update the constant and note it; the `[TODO]` drift check against a real Harbor checkout is deferred (§4).

## Dependencies

**Blocked by:** [D-1](D-1-build-tomlw-py-deterministic-toml-writer.md)
**Blocks:** [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md), [F-1](F-1-build-dataset-py-manifest-and-distribution-report.md)

