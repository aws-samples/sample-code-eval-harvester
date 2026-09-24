---
id: B-3
title: "[Forge] Build candidate.py + wire the capture verb"
feature: pr-eval-harvest
workstream: Forge capture
status: done
complexity: M
implements: [FR-9, FR-12, FR-13, FR-15, FR-16, FR-19, FR-20, FR-10]
user_story: US-2
blocked_by: [B-2, C-2]
blocks: [D-3, B-4, B-7, B-8]
---

# B-3: [Forge] Build candidate.py + wire the capture verb

## Context

The candidate file is the hand-off artefact at the centre of the whole design (ADR-3): `capture` writes it full of *mechanical facts* with the *judgment slots blank*, the driving agent fills the slots, and `emit`/`verify` read both halves back. Without it there is nowhere for the agent's per-comment classifications, finding severities, and change-risk judgement to live as an inspectable, re-runnable record — the thing the PRD §4 human audit reads. This task builds `candidate.py` (the schema + scaffold-with-facts + read-back validation) and wires the `capture` verb that composes `forge.py` (B-2) and `riskmap.py` (C-2) into a written candidate.

**North star:** get Harbor review-eval datapoints from a repo's own PR history without hand-labelling.
**Implements:** FR-9, FR-12, FR-13, FR-15, FR-16, FR-19, FR-20, FR-10  ·  **User story:** US-2 (also serves US-3, US-4)

## Design References

- **Tech plan:** `../tech-plan.md` §8 (Data Model) — the candidate-file schema this task implements, field by field, and the `emit`/`verify` coherence constraints listed under it
- **Tech plan:** §9, the `eval-harvest capture` contract — the exact CLI surface, what it reads/writes, stdout, and determinism requirement
- **Tech plan:** §7.1 step 4 — where `capture` lands in the agent-driven sequence
- **Tech plan:** §6.2 (filesystem-as-bus table) — `capture` reads `gh`/`git` + `risk-map.toml`, writes `candidates/pr-<n>.json`
- **ADR-3** in §11 — why the candidate file exists and why the CLI writes only facts; the decision is settled, do not re-litigate
- **PRD:** `../prd.md` §6, US-2 and US-3 acceptance criteria — the mechanical-facts-not-labels rule and the six per-comment fields

## What To Build

1. Create `src/eval_harvest/candidate.py` with a module docstring stating the fact/judgment split (only `capture` writes facts; only the agent writes judgments — FR-9).
2. Define the candidate schema as the §8 shape. Use plain `dict`/`TypedDict` + explicit validation functions — **no pydantic, no third-party validator** (ADR-2, NFR-5). Fields exactly as §8: `repo`, `pr_number`, `pr_url`; `iterations[]` (`tip_sha`, `base_sha`, `patch_path`, `comment_ids[]`); `review_verdicts[]` (`state`, `author`, `author_role`, `submitted_at`); `comments[]` (`id`, `body`, `path`, `line_start`, `line_end`, `author_role`, `created_at`, `iteration_index`, + blank slots `classification`, `classification_rationale` — the classification the agent later fills is one of the five values `defect|nit|question|approval|bot`); `findings[]` (blank: `comment_ids[]`, `statement`, `severity`, `severity_evidence`, `severity_rationale`, `reference_only=false`); `change_risk` (`risk_structural`, `risk_structural_rule` filled; `risk_classified`, `risk_classified_rationale` blank); `rubric_version` blank.
3. Write `scaffold(facts, *, repo, pr_url, risk_structural, risk_structural_rule) -> CandidateDict`: takes the fact bundle `forge.py` produced (B-2), the PR identity (`repo`, `pr_url` — mechanical facts §8 requires that the bundle does not carry), and the structural-risk result `riskmap.py` computed (C-2), and returns the candidate dict with every judgment slot present-but-empty. Slots must be emitted (empty string / empty list / `false`), never omitted, so the agent sees what to fill.
4. Write `dump(candidate, path)` and `load(path)` using stdlib `json` with `indent=2`, `sort_keys=False`, `ensure_ascii=False`, and a trailing newline — byte-stable for FR-10 (see Notes for the exact determinism rules).
5. Write `validate_facts(candidate) -> list[Violation]`: the CLI-written half is well-formed (used right after scaffold). Write `validate_filled(candidate) -> list[Violation]`: the slot-coherence checks `emit`/`verify` call back — every `comments[].classification` non-empty; every non-`reference_only` `findings[].severity` ∈ {low,medium,high} with non-empty `severity_evidence` and `severity_rationale`; `change_risk.risk_classified` non-empty; `rubric_version` non-empty. Return a list of violations (do **not** raise) — mirror `harbor.py`'s `validate_task_config`, which returns `list[SchemaViolation]` rather than raising (D-2).
6. Wire the `capture` verb into the dispatcher (A-1): parse `<pr> --clone <dir> [--dataset <dir>]`; call `forge.py` (B-2) for the facts; call `riskmap.py` (C-2) to compute `risk_structural` from the dataset's `risk-map.toml`; materialize each iteration's diff to patch bytes on disk (referenced by `patch_path`) so `emit` needs no git; call `scaffold` + `dump`; print the candidate path and the list of slots the agent must fill.
7. **Do not** classify anything, assign any severity, or compute `risk_classified` — those are the agent's, left blank (FR-9). The CLI computes only `risk_structural` (mechanical, from path rules).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/candidate.py` | Create | Schema + scaffold + dump/load + validators |
| `src/eval_harvest/cli.py` | Modify | Register the `capture` subcommand (dispatcher created in A-1) |
| `tests/test_candidate.py` | Create | Cases from the test table below |
| `tests/fixtures/` | Modify | Reuse the recorded `gh` payload + fixture repo from B-2 |

`src/eval_harvest/forge.py` and `src/eval_harvest/riskmap.py` are created by B-2 and C-2 respectively — this task imports them, does not create them.

## Schemas & Contracts

This task defines the candidate-file contract — `capture`'s output *is* `emit`'s input (§9). There is no "before" (greenfield file).

**After** (`candidates/pr-<n>.json`, exactly §8; facts filled, slots blank on scaffold):
```json
{
  "repo": "our-org/our-repo",
  "pr_number": 1234,
  "pr_url": "https://github.com/our-org/our-repo/pull/1234",
  "iterations": [
    {"tip_sha": "…", "base_sha": "…", "patch_path": "patches/pr-1234-iter0.patch", "comment_ids": [101, 102, 103]}
  ],
  "review_verdicts": [
    {"state": "CHANGES_REQUESTED", "author": "reviewer", "author_role": "MEMBER", "submitted_at": "2026-08-02T10:00:00Z"}
  ],
  "comments": [
    {"id": 101, "body": "…", "path": "src/pay.py", "line_start": 40, "line_end": 42,
     "author_role": "MEMBER", "created_at": "2026-08-02T10:00:00Z", "iteration_index": 0,
     "classification": "", "classification_rationale": ""}
  ],
  "findings": [
    {"comment_ids": [], "statement": "", "severity": "", "severity_evidence": "",
     "severity_rationale": "", "reference_only": false}
  ],
  "change_risk": {
    "risk_structural": "high", "risk_structural_rule": "src/payments/ → high",
    "risk_classified": "", "risk_classified_rationale": ""
  },
  "rubric_version": ""
}
```

**Migration:** none — greenfield.
**Backward compatibility:** none to break. This file is consumed only by `emit`/`verify` (D-3, E-2), which are built after this task.

## How To Verify

Commands come from the `mise.toml` A-1 creates (`lint`/`format`/`test`/`typecheck` tasks). If the exact task names differ from A-1's final `mise.toml`, use those. `[TODO: verify task names against A-1's mise.toml once it lands]`

```bash
mise run test -- tests/test_candidate.py
mise run lint
mise run typecheck
```

Then by hand: run `capture` against the B-2 fixture (`eval-harvest capture 1234 --clone <fixture> --dataset <tmp-ds>`), open `candidates/pr-1234.json`, and confirm every judgment slot is present and empty, every fact is filled, and `risk_structural` matches the fixture's `risk-map.toml` rule. Run it twice and `diff` the two files — must be empty (FR-10).

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_scaffold_fills_facts_leaves_slots_empty` | scaffold output has every fact set and every judgment slot present-and-empty | The CLI silently writing a label, or omitting a slot the agent must fill (FR-9) |
| `test_capture_binds_comments_to_iterations` | each comment's `iteration_index` matches the iteration it was written against | A comment bound to the wrong before-state — corrupts the oracle (FR-12) |
| `test_comment_carries_all_six_fields` | body, path, line range, role, timestamp, iteration all present per inline comment | US-3's six-field capture silently dropping a field (FR-12) |
| `test_validate_filled_rejects_empty_classification` | `validate_filled` returns a violation when a comment classification is blank | An unclassified comment reaching `emit` (FR-13) |
| `test_validate_filled_rejects_finding_missing_evidence` | a finding with a severity but no `severity_evidence`/`severity_rationale` is a violation | A severity asserted with no recorded basis (FR-15) |
| `test_reference_only_finding_skips_severity_gate` | a `reference_only=true` finding is not forced through the severity-evidence gate | Over-strict validation blocking a legitimate late-comment reference (FR-16) |
| `test_capture_output_byte_identical_twice` | two `capture` runs on the fixed fixture + `--from-json` payload produce identical bytes | Nondeterministic dict/JSON ordering breaking FR-10 |
| `test_risk_structural_written_not_classified` | `risk_structural` is set from the path rule; `risk_classified` is left blank | The CLI leaking into the agent's judgment territory (FR-19, FR-20) |

No happy-path-only tests; each failure-path test constructs the exact violation and asserts the validator returns it (not that it raises).

## Acceptance Criteria

- [ ] `capture <pr> --clone <dir> --dataset <dir>` writes `candidates/pr-<n>.json` in the §8 shape with facts filled and slots blank
- [ ] The candidate carries all six per-comment fields bound to the correct iteration (FR-12)
- [ ] `risk_structural` + `risk_structural_rule` are CLI-computed from `risk-map.toml`; `risk_classified` is blank (FR-19, FR-20)
- [ ] `validate_filled` returns a specific violation for each unfilled/incoherent slot (FR-13, FR-15) and returns them as a list, never raising
- [ ] `capture` run twice on fixed inputs is byte-identical (FR-10)
- [ ] All commands in "How To Verify" pass
- [ ] No regressions in the existing suite

## Out Of Scope

- Setting `expected_verdict`, enforcing the reject/approve coherence invariants, or refusing `empty-oracle` — that is `emit` (D-3, FR-14/FR-18).
- The answer-absence / leak scan over the candidate or emitted task — that is `verify` (E-2, FR-35).
- Computing `risk_classified` or any classification — that is the agent's judgment step, never the CLI (FR-9).
- Parsing `risk-map.toml` itself — that is `riskmap.py` (C-2); this task only calls it.
- The `in_diff_iterations` comment field, the single diff-span parser, `Emittability`, the per-kind emittable report, and the `no-emittable-iteration` capture refusal — that is `diffspan.py` + the candidate augmentation in [B-4](B-4-build-diffspan-and-bind-comments-to-iterations.md). This task writes the base candidate; `capture`'s emittability reporting/refusal lands with B-4.
- Batch capture (`--all`/`--limit`/`--pr` from a `survey --json` payload) — that is [B-8](B-8-add-batch-mode-for-capture-and-emit.md). This task wires single-PR `capture` only.
- The `show`/`brief`/`annotate` read/fill verbs over this candidate — B-5/B-6/B-7.

## Notes & Gotchas

- **Determinism budget (§3, NFR-1):** no `datetime.now()`, no unseeded set/dict iteration in the written bytes. Every timestamp in the candidate comes from the forge/commit data, never the wall clock. Serialize with a fixed key order (define fields in a stable order and set `sort_keys=False`, or sort explicitly) — mirror `harbor_emitter`'s "candidate's timestamp, never the wall clock" rule.
- **Facts vs. judgments is the ADR-3 property and it is testable:** any field the CLI writes is mechanical; any judgment is the agent's. `test_scaffold_fills_facts_leaves_slots_empty` is the guard on it.
- **Validation shape:** return `list[Violation]`, do not raise — this matches `harbor.py`'s `validate_task_config` (which returns `list[SchemaViolation]`, D-2) so `emit`/`verify` can aggregate violations into the FR-2 four-field refusal rather than dying on the first.
- **`patch_path` materialization:** `capture` writes the diff bytes to disk so `emit` stays git-free (NFR-2 — `emit` makes no git call). The candidate stores the *path*, not the diff inline, keeping the JSON small and diffable.
- **`gh` field access:** reuse B-2's `forge.py` output; do not re-fetch. The `gh`/`git` calls all go through `gitcmd.py` (A-2), argv-only, prompts disabled, `GITHUB_TOKEN` stripped — never build a shell string from PR titles or review bodies (§3 security constraint).

## Dependencies

**Blocked by:** [B-2](B-2-build-forge-py-iterations-comments-verdicts.md) (the fact bundle `scaffold` consumes), [C-2](C-2-build-riskmap-py-parse-risk-map-compute-structural-risk.md) (the structural-risk computation `capture` calls)
**Blocks:** [D-3](D-3-build-emit-py-and-verifier-tpl-assemble-task-directory.md) (`emit` reads the candidate this produces), [B-4](B-4-build-diffspan-and-bind-comments-to-iterations.md) (augments this candidate with `in_diff_iterations`/`Emittability`), [B-7](B-7-add-the-annotate-verb.md) (`annotate` fills this candidate), [B-8](B-8-add-batch-mode-for-capture-and-emit.md) (batch `capture`/`emit`)
