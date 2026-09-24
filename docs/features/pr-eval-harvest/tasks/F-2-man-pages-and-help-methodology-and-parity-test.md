---
id: F-2
title: "[Docs] man/ + --help methodology for every verb + human-parity test"
feature: pr-eval-harvest
workstream: Dataset & docs
status: done
complexity: M
implements: [FR-1, FR-4, FR-13, FR-15, FR-16]
user_story: US-1
blocked_by: [A-1]
blocks: [G-1, B-7]
---

# F-2: [Docs] man/ + --help methodology for every verb + human-parity test

## Context

The CLI's documentation *is* the product: the customer does not know what an eval is, cannot recognise a broken datapoint, and drives the tool through a coding agent that learns the whole workflow from `--help` and the man pages alone. So the help text must carry the *methodology* — what makes a review datapoint valid, why the base commit precedes the change, why the instruction must not name the outcome, what a defect is versus a nit, and how to get the local clone — not just flags. It must also write down the **candidate-file schema** the agent fills, because the agent doing the fill is the one that most needs the shape, and a rule that lives only in the code is not a rule the agent can follow. This is the surface the eval exercises (Workstream G tests it). It can be authored as soon as the CLI skeleton exists (A-1); the prose does not wait on the mining logic.

**North star:** the agent learns the methodology from the tool, so the customer does not have to.
**Implements:** FR-1, FR-4, FR-13, FR-15, FR-16  ·  **User story:** US-1

## Design References

- **Tech plan:** `../tech-plan.md` §2.1 FR-1 (methodology in help) and FR-4 (human-usable parity)
- **Tech plan:** §5.1 — the CLI's stdout is the only place the methodology lives; §7.1 step 1 — the agent learns from `--help`/man before anything else
- **Tech plan:** §8 (Data Model) — the authoritative `findings[]` and `comments[]` shapes the CANDIDATE FILE section must match, field for field
- **Tech plan:** §12, scenario S-17 (refusal shape) and the FR-4 parity check
- **ADR-3** in §11 — the fact/judgment split, which is *why* the candidate slots are the agent's to write
- **PRD:** `../prd.md` §6, US-1/US-2 acceptance criteria — the exact methodology items help must teach; FR-13's five classification values, FR-15's severity-evidence requirement, FR-16's reference-only rule; and §9 (the core-bet eval)

## What To Build

1. Author a **methodology section** in each verb's `--help` (via argparse description/epilog) and a corresponding `man/` page. There is a page per verb plus the top-level command — `man/eval-harvest.md` and `man/eval-harvest-{init,survey,capture,emit,verify,dataset,show,brief,annotate}.md`. Each must teach the relevant methodology, not just flags:
   - **top-level / `init`:** what a review datapoint is; that you build a rubric + risk map first; how to obtain the local clone (step zero, §7.1); the overlay method.
   - **`survey`:** what makes a PR harvestable; why a repo with no review iteration cannot be mined.
   - **`capture`:** why the base commit must precede the change; what facts are captured vs. what the agent must judge; defect vs. nit vs. question vs. approval vs. **bot**.
   - **`show`:** how to read the exact diff hunk a comment points at without paying for the whole patch — the context-economy verb the fill step uses instead of reading `change.patch`.
   - **`brief`:** what a single self-contained per-PR brief contains, so a fresh agent can be handed one PR's whole context at once.
   - **`annotate`:** the append-only per-comment fill contract — how the agent writes its classification/severity/rationale back into the candidate one comment at a time without rewriting the file.
   - **`emit`:** why the instruction must not name the outcome; why the verdict follows the human review, not a severity formula; what `--kind` means; which severities make a datapoint emittable (a `reject` needs at least one non-`reference_only` medium/high finding); `separate` mode + sealing, in a sentence; the `--batch` mode.
   - **`verify`:** what "the answer must be absent" means and the channels it covers; the control token; that a failure is an instruction to act on.
   - **`dataset`:** why the risk distribution matters for the automation decision.
2. Add a **CANDIDATE FILE** section to `man/eval-harvest-capture.md`, after DESCRIPTION and before METHODOLOGY, documenting the file `capture` writes so an agent can fill it from the documentation alone:
   - a top-level key table (`repo`, `pr_number`, `pr_url`, `iterations[]`, `review_verdicts[]`, `comments[]`, `findings[]`, `change_risk`, `rubric_version`), each marked **fact** (written by `capture`) or **slot** (written by the agent);
   - `comments[]` field by field, marking the facts and the two slots (`classification`, `classification_rationale`);
   - `findings[]` field by field: `comment_ids`, `statement`, `severity` (`low|medium|high`), `severity_evidence`, `severity_rationale`, `reference_only`;
   - `change_risk`, distinguishing `risk_structural`/`risk_structural_rule` (facts) from `risk_classified`/`risk_classified_rationale` (slots).
   Take field order from §8 and from the `CommentDict`/`FindingDict` TypedDicts so the document and the file agree. Render the field lists as markdown **tables** (a machine-readable first column) so the drift test in step 6 can parse them.
3. State the three rules that otherwise live only in the validator, each in one sentence with its consequence:
   - **All five classification values**, `defect|nit|question|approval|bot`, with a sentence on what each is for.
   - **`severity_evidence` is required for a non-`reference_only` finding** and must point at concrete evidence (path:line, output).
   - **`reference_only` marks a finding kept for coverage but not required to block.** Say plainly what it exempts (the severity-evidence gate, FR-16; being a required finding) and what it does **not** (needing a location inside the emitted diff — the scorer matches these findings into `coverage_all` by location overlap).
4. Make `capture`'s stdout name the `findings[]` field list inline (via `Candidate.slot_summary`), not just "findings[] — one per substantive review point", because the agent reads stdout before it reads a man page. Keep the classification and finding-field prose in shared module constants next to the TypedDicts they describe, so the prose and the schema are edited together and `brief`/`annotate` (their own tasks) can reuse them.
5. Cross-reference rather than restate: `man/eval-harvest-emit.md` (which severities make a datapoint emittable) and `man/eval-harvest-verify.md` (that oracle finding locations must fall inside the emitted diff) each point at the CANDIDATE FILE section and name it in SEE ALSO.
6. Ensure every verb's `--help` is self-contained enough that an agent given only the repo name + help output can build valid datapoints (the FR-1 / core-bet bar).
7. Add the tests: the **FR-4 human-parity test** (each verb run from a plain shell produces identical bytes to an agent invocation with the same args — one code path, no agent-only branch, help width pinned by the formatter so output is environment-independent); a doc test that each verb's help carries a methodology section; and a **schema-drift guard** that compares the documented `findings[]`/`comments[]` field set against `FindingDict.__annotations__`/`CommentDict.__annotations__` in both directions, so a new field with no row (or a row for a field that no longer exists) fails immediately.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/cli.py` | Modify | Per-verb `description`/`epilog` methodology text; `MethodologyHelpFormatter` (raw + fixed width); `VERB_SUMMARIES`/`VERB_METHODOLOGY`/`TOP_LEVEL_METHODOLOGY`; `bot` + the findings field list in the `capture` entry |
| `src/eval_harvest/candidate.py` | Modify | `slot_summary` names the `findings[]` fields; the shared classification/finding-field guide constants live next to the TypedDicts |
| `man/eval-harvest.md`, `man/eval-harvest-{init,survey,capture,emit,verify,dataset,show,brief,annotate}.md` | Create | One man page per verb + top-level; `capture` gains the CANDIDATE FILE section; `emit`/`verify` cross-reference it |
| `tests/test_help_methodology.py` | Create | Asserts each help/man page carries the methodology items, the five classifications incl. `bot`, the `reference_only`/`severity_evidence` rules, and the field-set-matches-TypedDict drift guard |
| `tests/test_human_parity.py` | Create | FR-4: shell invocation == agent invocation bytes; help identical across environments |

## Schemas & Contracts

This task changes documentation and adds tests. It does **not** change any schema, field name, or validation rule — it writes down what is already true; if the documentation and the code disagree, the code is right. The verb surfaces are defined by their own tasks; F-2 populates their help text and pins the candidate-file shape to the code.

`capture` stdout after this task names the fields inline:
```
you fill (N comment(s) to classify):
comments[].classification — classify each comment (defect|nit|question|approval|bot):
    defect: a substantive problem a reviewer would block on   nit: style or preference, does not block
    question: asks for information rather than asserting      approval: a sign-off, carries no finding
    bot: an automated comment (linter, coverage, dep bot) — not a human review point, never a finding
findings[] — one per substantive review point:
    comment_ids: [int]        which comment(s) this finding is drawn from
    statement: str            what is wrong, in the reviewer's terms
    severity: low|medium|high
    severity_evidence: str    concrete evidence (path:line, output) — required unless reference_only
    severity_rationale: str   which rubric rule assigns this severity
    reference_only: bool      kept for coverage, not required to block; still needs a location in the diff
change_risk.risk_classified — classify the change's risk (structural half already computed)
rubric_version — pin the rubric version this datapoint was built against
```

**Migration:** none. **Backward compatibility:** nothing exists to break.

## How To Verify

```bash
mise run test -- tests/test_help_methodology.py tests/test_human_parity.py
mise run lint
uv run eval-harvest --help
uv run eval-harvest capture --help
```

Then the reading test that matters: open `man/eval-harvest-capture.md` and `capture`'s stdout **only** — no source files — and write one complete finding by hand into a captured candidate. Run `emit`. If it refuses on a schema or severity-evidence rule, the documentation is still incomplete.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_each_verb_help_has_methodology` | every verb's help contains a methodology section (not just flags) | An agent that can't learn the workflow from the tool (FR-1, the core bet) |
| `test_man_page_exists_for_every_verb` | a `man/` page exists for the top-level command and each verb | A verb the agent can't learn from the manual |
| `test_man_pages_carry_methodology` | every man page carries the same methodology phrases its verb's help teaches | Help and man drifting into two different methodologies (the help↔man parity contract) |
| `test_emit_and_verify_cross_reference_the_section` | `emit`/`verify` man pages name the CANDIDATE FILE section and `eval-harvest-capture(1)` in SEE ALSO | The contract restated in two places and drifting |
| `test_help_teaches_base_precedes_change` | `capture` help explains why the base commit precedes the change | The agent building a datapoint with the wrong before-state |
| `test_help_teaches_defect_vs_nit` | `capture`/`emit` help distinguishes defect from nit | The agent labelling a nit as a blocking finding (§9 nits risk) |
| `test_help_teaches_instruction_hides_outcome` | `emit` help explains the instruction must not name the outcome | A verdict inferable from the instruction (FR-33) |
| `test_all_five_classifications_documented` | `defect`, `nit`, `question`, `approval`, `bot` all appear in help and man | `bot` learnable only from a validation error |
| `test_reference_only_rule_documented` | the man page states what `reference_only` exempts and what it does not | An agent assuming it exempts the location requirement |
| `test_severity_evidence_requirement_documented` | the requirement and its `reference_only` exemption appear | A refusal that is easier to avoid than to read |
| `test_slot_summary_names_finding_fields` | `capture`'s stdout lists the finding field names inline | Having to open `candidate.py` to fill a candidate |
| `test_finding_field_docs_match_typeddict` | the documented field set equals `FindingDict`/`CommentDict.__annotations__` (both directions) | Documentation and code diverging silently — the guard that keeps the schema documented |
| `test_human_parity_identical_bytes` | a shell invocation produces identical bytes to an agent invocation | An agent-only code path breaking FR-4 |
| `test_help_identical_across_environments` | help is a pure function of its args regardless of terminal | An environment-dependent help width breaking parity |

## Acceptance Criteria

- [ ] Every verb's `--help` contains a methodology section covering its FR-1 items, and `man/` pages exist for the top-level command and every verb (`init`, `survey`, `capture`, `show`, `brief`, `annotate`, `emit`, `verify`, `dataset`)
- [ ] A reviewer can point to the sentence teaching each of: valid datapoint, base-precedes-change, instruction-hides-outcome, defect-vs-nit, how-to-clone (FR-1)
- [ ] `man/eval-harvest-capture.md` has a CANDIDATE FILE section documenting every `comments[]` and `findings[]` field, each marked fact or slot
- [ ] All five classification values, including `bot`, appear in the man page, the help text, and the phrase contract
- [ ] The `severity_evidence` requirement and the `reference_only` rule are both stated, including that `reference_only` does *not* exempt the location requirement
- [ ] `capture`'s stdout names the `findings[]` fields inline
- [ ] A test compares the documented field set against `FindingDict.__annotations__`/`CommentDict.__annotations__` so the two cannot drift
- [ ] `emit` and `verify` cross-reference the CANDIDATE FILE section rather than restating it
- [ ] A finding written from the documentation alone passes `emit`
- [ ] No schema, field name, or validation rule changed
- [ ] The FR-4 parity test passes (shell == agent bytes)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Running the eval — that is Workstream G (G-1 provides the runner; this task provides the help text the agent-under-test reads).
- Any verb's logic — F-2 is prose + doc/parity tests only; the per-verb handlers remain each verb's own task (`show`/`brief`/`annotate` in B-5/B-6/B-7).
- Changing the schema or any validation rule.
- A `docs/spec/` directory, requirement ids in the man pages, or a conformance level (CLAUDE.md).

## Notes & Gotchas

- **This is the product, not packaging (§5.1).** The methodology has to be *in the tool*, because it is the only place it lives — the customer does not carry it and the agent's prompt does not either.
- **Author it early (§14).** The prose depends only on the CLI skeleton (A-1), not the mining logic — and Workstream G (the eval, the project's top risk) reads it as the agent-under-test's only guidance.
- **FR-4 parity is a real constraint, not a formality:** if any verb branches on "am I being run by an agent" or emits different bytes, it fails. Keep one code path, and pin help width in the formatter — branching on `COLUMNS` would itself be an environment-dependent path FR-4 forbids.
- **`test_finding_field_docs_match_typeddict` is the schema-drift guard.** Without it the candidate-file documentation decays the first time a field is added. Render the field lists as tables so the test can parse a machine-readable first column; add the phrase-contract entries first and watch them go red before writing the prose.
- **`reference_only` is the field most likely to be documented wrongly.** It exempts a finding from the severity-evidence gate and from being required to block — not from needing a location in the emitted diff. Get this sentence right.

## Dependencies

**Blocked by:** [A-1](A-1-scaffold-uv-package-cli-dispatch-refusal-helper.md)
**Blocks:** [G-1](G-1-eval-harness-harbor-task-microvm-runner-trajectory-capture.md), [B-7](B-7-add-the-annotate-verb.md)
