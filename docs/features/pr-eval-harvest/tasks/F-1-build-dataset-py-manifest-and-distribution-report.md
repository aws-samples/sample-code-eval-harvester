---
id: F-1
title: "[Dataset] Build dataset.py: manifest + risk/severity distribution report"
feature: pr-eval-harvest
workstream: Dataset & docs
complexity: M
implements: [FR-23, FR-29]
user_story: US-4
status: done
blocked_by: [D-2, D-3]
blocks: []
---

# F-1: [Dataset] Build dataset.py: manifest + risk/severity distribution report

## Context

Once datapoints exist, the customer needs two things: a Harbor dataset manifest so `harbor run` can consume the whole set, and a report of the risk/severity distribution so they can see whether they have enough *low-risk* datapoints to justify switching automation on — the decision they actually care about (US-4). Trusting a number computed over a dataset that is mostly high-risk changes would answer the wrong question. This task assembles `dataset.toml` + `registry.json` from the emitted tasks and prints the distributions.

**North star:** the customer learns whether the low-risk slice is large enough to act on — before they trust any score.
**Implements:** FR-23, FR-29  ·  **User story:** US-4 (serves US-6)

## Design References

- **Tech plan:** `../tech-plan.md` §9, the `eval-harvest dataset` contract — reads every `tasks/*` + its `task.toml`; writes `dataset.toml` + `registry.json`; stdout is the risk + severity distribution; refuses `no-datapoints` on empty
- **Tech plan:** §8 — the `[metadata.harvest]` fields to aggregate (`change_risk_*`, `finding_severities`) and the Harbor manifest shape (FR-29)
- **Tech plan:** §3 — Harbor's local/registry path ignores a dataset `metric.py` and falls back to `Mean()`, so ship a `metric.py` to state the aggregation but keep reward keys meaningful when averaged
- **`harbor.py` builders (D-2's workstream):** `Harbor.dataset_manifest_document`, `Harbor.validate_dataset_manifest`, `Harbor.dataset_content_hash`, `Harbor.local_registry_document` — the manifest/registry/hash builders this task calls once D-2 lands them. `harbor.py` also carries `Harbor.metric_script`, the `metric.py` builder F-1 hands the reward keys to.
- **From `emit.py` (D-3's workstream):** the reward-key set (`REWARD_KEYS`) `metric.py` aggregates, and the **per-task content-digest scheme** `emit` records on each task under `[metadata.harvest].content_digest`. F-1 *consumes* both; it defines neither.

## What To Build

1. Create `src/eval_harvest/dataset.py` and wire the `dataset` verb (A-1 dispatcher): `dataset --dataset <dir> [--json]`.
2. Read every `tasks/*/task.toml` (via `tomllib`), collecting `[metadata.harvest].change_risk_structural`/`change_risk_classified` and `finding_severities`, and each task's **content digest** as recorded by `emit` (D-3). F-1 does not define the digest scheme — it reads what D-3 recorded (see D-3 §What To Build); it must fail loudly if a task lacks a digest rather than inventing one.
3. Build `dataset.toml` (the Harbor manifest, FR-29 shape) via `harbor.dataset_manifest_document` (passing the `task_digests` map from step 2) + `tomlw`, and a local `registry.json` via `harbor.local_registry_document`. Ship a `metric.py` via `harbor.metric_script`, passing it the reward-key set `emit` defines (`REWARD_KEYS`), so the aggregation is stated even though the local path uses `Mean()`.
4. Compute and print the **risk distribution** and the **severity distribution** (count per finding-severity level) (FR-23). Bucket the risk distribution by the **classified** change risk — the agent's holistic judgment — and surface the structural/classified **disagreement count** separately, honouring the rule to record the disagreement rather than pick a winner.
5. Derive the dataset's Harbor name from the datapoints' shared origin repo (`[metadata.origin].repo`, already `org/name`), since v1 mines one repository at a time. Refuse `mixed-origin-repos` (exit 3) rather than silently pick one if the tasks span more than one origin repo.
6. Refuse `no-datapoints` (exit 3) on an empty dataset, and `missing-content-digest` (exit 3) — loudly — for any task whose `task.toml` lacks the `content_digest` `emit` records, rather than inventing one.
7. Determinism: manifest + report are byte-stable functions of the tasks present (NFR-1).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/dataset.py` | Create | Manifest + registry + distribution report |
| `src/eval_harvest/cli.py` | Modify | Register the `dataset` subcommand |
| `tests/test_dataset.py` | Create | Cases from the test table |

Imports `harbor.py` (manifest/registry/metric builders), `tomlw.py` (TOML writer), `REWARD_KEYS` from `emit.py`, and `RISK_LEVELS` from `riskmap.py`.

## Schemas & Contracts

**Writes:** `dataset.toml` (Harbor manifest, FR-29), `registry.json`, `metric.py`.
**Stdout (`--json`):**
```json
{ "risk_distribution": {"low": 12, "medium": 5, "high": 3, "disagreements": 2},
  "severity_distribution": {"low": 20, "medium": 8, "high": 4},
  "datapoints": 20 }
```
**Refusals (exit 3):** `no-datapoints` on an empty dataset; `mixed-origin-repos` when the tasks span more than one origin repo; `missing-content-digest` when a task lacks the digest `emit` records.

**Migration:** none. **Backward compatibility:** none — new verb.

## How To Verify

```bash
mise run test -- tests/test_dataset.py
mise run lint
mise run typecheck
```

Then by hand: with a handful of emitted tasks under `tasks/`, run `dataset --dataset <dir>`; confirm `dataset.toml` validates (`harbor.validate_dataset_manifest` returns no violations) and the printed counts match the tasks' `[metadata.harvest]` fields; run against an empty dir and confirm `no-datapoints` (exit 3).

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_risk_distribution_counts_correct` | counts per change-risk level match the tasks' metadata | A wrong distribution misleading the automation decision (FR-23) |
| `test_severity_distribution_counts_correct` | counts per finding-severity level are correct | The severity picture the customer reads being wrong (FR-23) |
| `test_disagreement_count_surfaced` | tasks with `change_risk_disagreement=true` are counted | The two-way risk signal invisible in the summary (US-4) |
| `test_manifest_validates` | emitted `dataset.toml` passes `validate_dataset_manifest` | A manifest `harbor run` rejects (FR-29) |
| `test_empty_dataset_refuses_no_datapoints` | an empty dir refuses `no-datapoints` (exit 3) | A meaningless empty manifest emitted |
| `test_mixed_origin_repos_refuses` | tasks spanning two origin repos refuse `mixed-origin-repos` | A dataset silently naming itself after one of several repos |
| `test_missing_content_digest_refuses` | a task without a `content_digest` refuses `missing-content-digest` | A manifest whose content hash is invented rather than read |
| `test_dataset_deterministic` | manifest + report are byte-stable for the same tasks | Nondeterministic manifest breaking Harbor's content hash (NFR-1) |

## Acceptance Criteria

- [ ] `dataset` writes `dataset.toml` (validating) + `registry.json` + `metric.py` (FR-29)
- [ ] Stdout reports the risk distribution bucketed by classified risk (+ the structural/classified disagreement count) and the severity distribution (FR-23)
- [ ] Empty dataset refuses `no-datapoints`; a mixed-origin dataset refuses `mixed-origin-repos`; a task without a digest refuses `missing-content-digest` (all exit 3)
- [ ] Manifest + report are byte-stable (NFR-1)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Running the eval, scoring, ranking, or aggregating severity into an automation-gate score — out of scope entirely (PRD §10).
- Emitting individual tasks — that is `emit` (D-3); `dataset` only reads what `emit` wrote.
- Defining the per-task content-digest scheme or the reward-key set `metric.py` aggregates — both belong to `emit` (D-3), which owns the task layout and reward-key shape. Building `harbor.metric_script` itself belongs to `harbor.py` (D-2). `dataset` consumes all three.

## Notes & Gotchas

- **Every reward key must be meaningful under `Mean()` (§3).** The local path ignores `metric.py` and averages — ship `metric.py` to state intent, but do not rely on it changing the local aggregation.
- **Report the disagreement count (US-4).** A distribution that hides where structural and classified risk disagree loses the thing US-4 exists to surface.
- **This is a read-only aggregator over `tasks/`** — it makes no forge/network call and no judgment.

## Dependencies

**Blocked by:** [D-2](D-2-port-harbor-py-constants-and-validators.md) (manifest/registry/hash + `metric_script` builders), [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md) (`REWARD_KEYS` + the per-task content-digest scheme this task consumes)
**Blocks:** nothing

