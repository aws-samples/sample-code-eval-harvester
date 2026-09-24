---
id: C-2
title: "[Artefacts] Build riskmap.py: parse risk-map.toml, compute structural risk"
feature: pr-eval-harvest
workstream: Artefacts (rubric & risk map)
status: todo
complexity: S
implements: [FR-20, FR-22]
user_story: US-4
blocked_by: [A-1, D-1]
blocks: [B-3, D-3]
---

# C-2: [Artefacts] Build riskmap.py: parse risk-map.toml, compute structural risk

## Context

The customer only wants to auto-approve *low-risk* changes, so every datapoint must carry a change-risk classification — and one half of it is free: teams already segregate risky code by directory, so path rules give a cheap, auditable structural signal. This task builds the versioned `risk-map.toml` artefact and the function that applies its path rules to a change's paths to produce `risk_structural`. `capture` (B-3) calls this to pre-compute the structural half; the agent supplies the classified half separately (FR-20). It needs the TOML writer (D-1) to emit the versioned template deterministically.

**North star:** the customer can measure the agent on the low-risk slice they would actually let it auto-approve.
**Implements:** FR-20, FR-22  ·  **User story:** US-4

## Design References

- **Tech plan:** `../tech-plan.md` §8 "Dataset-root artefacts" — `risk-map.toml`, the path rules, versioned, e.g. `[[rule]] prefix = "src/payments/" risk = "high"`
- **Tech plan:** §6.1 module table, `riskmap.py` row — "parse `risk-map.toml`; apply path rules to compute structural risk"
- **Tech plan:** §2.1 FR-20/FR-22 — both risk values recorded; path rules a first-class versioned artefact
- **PRD:** `../prd.md` §6, US-4 acceptance criteria — structural + classified, both recorded, disagreement preserved
- **Literal-prefix matching, shared with the `survey` verb's `path_roots`** (task B-1, in parallel) — use the same `path.startswith` model here; a monorepo whose code lives at `cdk/src/` matches none of the root-anchored prefixes, which is exactly what the literal model surfaces

## What To Build

1. Create `src/eval_harvest/riskmap.py`.
2. Parse `risk-map.toml` with stdlib `tomllib` (`RiskMap.parse`): a versioned document with a list of rules, each `{prefix, risk}` where `risk ∈ {low, medium, high}`, plus a `version` and a `default` risk for unmatched paths. Split parse from validation: `RiskMap.validate(risk_map) -> list[str]` returns every violation (a `risk`/`default` outside the taxonomy, a missing/empty `version`, a malformed rule) as a human-readable string and never raises — so a caller reports all problems at once (`[]` means well-formed).
3. Write `RiskMap.structural_risk(changed_paths, risk_map) -> (risk, matched_rule)`: apply the rules by literal path prefix (`path.startswith` — the same literal-prefix model the `survey` verb's `path_roots` also uses, task B-1), taking the **highest** matching risk across all changed paths (a change touching any high-risk path is high). Return the resulting level and the matched rule rendered as `"<prefix> → <risk>"` (for `risk_structural_rule`, FR-20); when no rule matches any path, fall to the map's `default` and return `"default → <default>"` so the audit trail is never blank.
4. Write the `risk-map.template.toml` content (the template `init` writes via C-1) and a deterministic emitter for it using `tomlw.py` (D-1) so it is byte-stable (FR-22, NFR-1).
5. Version the artefact: the `version` field is read back so `emit` can reference it and a team can correct their risk map without rebuilding datapoints (FR-22).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/riskmap.py` | Create | Parse + `structural_risk()` + template emitter |
| `tests/test_riskmap.py` | Create | Cases from the test table |

`src/eval_harvest/tomlw.py` is created by D-1 — this task imports it to emit the versioned template.

## Schemas & Contracts

**`risk-map.toml` (dataset-root artefact, §8):**
```toml
version = "v1"
default = "medium"

[[rule]]
prefix = "src/payments/"
risk = "high"

[[rule]]
prefix = "docs/"
risk = "low"
```
**`riskmap.py` public surface** (methods on a stateless `RiskMap` namespace, per CODING_STANDARDS; module constants `RISK_MAP_FILENAME = "risk-map.toml"`, `RISK_MAP_TEMPLATE_FILENAME = "risk-map.template.toml"`, and the taxonomy `RISK_LEVELS = ("low", "medium", "high")` in ascending severity):
```
RiskMap.structural_risk(changed_paths, risk_map) -> tuple[str, str]
    # -> ("high", "src/payments/ → high")   highest matching risk + its matched rule
    # matched-rule string is "<prefix> → <risk>"; on no match, ("<default>", "default → <default>")
RiskMap.parse(text: str) -> dict            # stdlib tomllib; the raw value tree
RiskMap.validate(risk_map) -> list[str]     # every violation as a string, never raises ([] == well-formed)
RiskMap.template_document() -> dict         # the value tree behind risk-map.template.toml
RiskMap.emit_template() -> bytes            # deterministic bytes via tomlw (D-1)
```

**Migration:** none.
**Backward compatibility:** none — new artefact + module.

## How To Verify

```bash
mise run test -- tests/test_riskmap.py
mise run lint
mise run typecheck
```

Then by hand: with a rule `src/payments/ → high` and a change touching `src/payments/charge.py` and `docs/x.md`, confirm `structural_risk` returns `("high", "src/payments/ → high")`; a change touching only `docs/` returns `("low", …)`; an unmatched path falls to `default`.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_highest_matching_risk_wins` | a change touching a high-risk and a low-risk path resolves to high | A risky change under-classified because a low-risk path matched first |
| `test_unmatched_path_falls_to_default` | a path matching no rule uses `default` | A monorepo layout matching no rule silently getting no risk (`path_roots` docstring) |
| `test_matched_rule_recorded` | the returned matched-rule string names the prefix + level | `risk_structural_rule` blank, losing the audit trail (FR-20) |
| `test_template_emitted_byte_stable` | the `risk-map.template.toml` bytes are identical across two emits | Nondeterministic template breaking the artefact's versioning (FR-22, NFR-1) |
| `test_invalid_risk_level_rejected` | a rule with `risk="critical"` is rejected with a violation | An out-of-taxonomy risk level silently accepted |

## Acceptance Criteria

- [ ] `risk-map.toml` parses to versioned rules; `structural_risk` returns the highest matching level + the matched rule (FR-20)
- [ ] An unmatched path falls to `default`
- [ ] The template is emitted byte-stably via `tomlw.py` (FR-22, NFR-1)
- [ ] `risk` values outside {low, medium, high} are rejected
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- The agent's `risk_classified` judgment — supplied by the agent, never computed here (FR-20 second half).
- Recording the structural/classified disagreement into `task.toml` — that is `emit` (D-3, FR-21).
- The `dataset` risk-distribution report — that is F-1 (FR-23).

## Notes & Gotchas

- **Literal prefix matching (`path.startswith`) — the same literal-prefix model the `survey` verb's `path_roots` also uses (B-1).** Do not add glob/regex semantics — the artefact is meant to be inspectable and correctable by an SDE, and the literal model is what the visibility rules use.
- **Highest-risk-wins is the safe direction:** a change touching any high-risk path is high. Under-classifying a risky change is the expensive error (the whole point of US-4 is the low-risk slice).
- **Depends on D-1** only for the deterministic template emit — the parse side is pure `tomllib` (stdlib). If D-1 is not yet ready, the parse + `structural_risk` logic can be built and tested first, template emit last.

## Dependencies

**Blocked by:** [A-1](A-1-scaffold-uv-package-cli-dispatch-refusal-helper.md), [D-1](D-1-build-tomlw-py-deterministic-toml-writer.md)
**Blocks:** [B-3](B-3-build-candidate-py-and-wire-capture-verb.md), [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md)

