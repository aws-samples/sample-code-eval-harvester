---
id: G-2
title: "[Eval] Datapoint-quality LLM judge + alignment to human labels"
feature: pr-eval-harvest
workstream: Eval
status: todo
complexity: L
implements: []
user_story: US-1
blocked_by: [G-1]
blocks: [G-3]
---

# G-2: [Eval] Datapoint-quality LLM judge + alignment to human labels

## Context

G-1 runs the agent and captures, per trial, the eval datapoint it built. `eval-harvest verify` already
answers *"is this datapoint structurally valid and leak-free?"* (the US-7 checks). It does **not**
answer *"is this a **good** datapoint?"* — one that actually captures the PR's review, with an oracle
that reflects the real defects, a change that matches the reviewed state, and a verdict that fits.
That judgment is what tells us whether the agent used the CLI *well*, not just legally.

This ticket builds an **LLM judge** that rates datapoint quality, and — before we trust it — an
**alignment step**: hand-label a small set of datapoints (good and deliberately bad), run the judge,
measure how often it agrees with the human labels, and iterate its prompt until agreement clears a
bar. Only then does G-3 use the judge to score eval runs. An unaligned judge is just another opinion;
this is the step that makes it a measurement.

**North star:** prove the agent builds *good* datapoints from the CLI alone — with a judge we've
checked against human judgment first.
**Implements:** none (eval scoring; operationalizes the PRD §8 metric).  ·  **US-1.**

## Design References

- **Tech plan:** `../tech-plan.md` §12 Testing Plan — the recorded-judge pattern the in-task verifier
  uses; this judge is the same idea applied to *datapoint quality*, and it lives in `eval/`, outside
  the offline gate.
- **PRD:** `../prd.md` §6 US-1 acceptance criteria (what makes a datapoint valid — base precedes the
  change, instruction doesn't name the outcome, defect vs. nit) and §7 US-7 (the validity checks the
  structural side already covers) — the judge grades the *quality* those leave open.
- **G-1** (`G-1-eval-harness-harbor-task-microvm-runner-trajectory-capture.md`) — produces the
  per-trial datapoint + trajectory this judge reads.
- **`src/eval_harvest/verify.py`** (E-2) — the structural/leak checks. The judge is **layered on top**
  of `verify`, not a replacement: verify gates validity, the judge grades quality. `[TODO: verify path]`
  if the module name differs.

## What To Build

Everything here lives under `eval/` (may use a model client and network; not imported by `src/`).

1. **A quality rubric.** Write down what "good datapoint" means, as concrete criteria the judge scores:
   oracle findings reflect the PR's real review, the change matches the reviewed state, the verdict
   fits `--kind`, the instruction is outcome-neutral, nits aren't promoted to blocking. Keep it short
   and inspectable — this rubric is the judge's contract.
2. **The judge.** An `eval/`-side component that, given a built datapoint (instruction, change,
   oracle findings, verdict, provenance) plus the source PR context, renders a prompt, asks a model,
   and parses the reply into a structured verdict per criterion + an overall pass/score + notes. Put
   the model call behind a small **`ModelClient` seam** (a `prompt -> completion text` callable) so the
   prompt-building and reply-parsing are exercised offline: a **`RecordedModelClient`** replays a pinned
   reply in CI (the recorded-judge pattern, tech plan §12); a **`LiveModelClient`** makes the real call
   at eval time. The live client speaks the OpenAI-compatible chat-completions shape over `urllib` (no
   SDK import — keeps `eval/` light) against a **configurable endpoint and model** read from the
   environment (`EVAL_JUDGE_MODEL`, `EVAL_JUDGE_ENDPOINT`, `EVAL_JUDGE_API_KEY`), which is how Claude on
   Bedrock is reached in this project's setup; the model id is **never hardcoded**. Guard the transport:
   refuse any endpoint whose scheme is not `https://` before it reaches `urllib` (else a `file://`
   endpoint turns a model call into a local-file read whose contents come back as the verdict). Derive
   `overall_pass` (all criteria pass) and `overall_score` (their mean) from the per-criterion verdicts
   **in the harness — never read them from the model**, and fail parsing if any rubric criterion is
   missing (the judge cannot silently drop one). Distinct from the in-task verifier's judge (which
   scores an agent-under-review's findings against the oracle, and reaches Bedrock a different way — via
   the MicroVM's IAM execution role inside the sandbox, not an API key): say so in the module docstring
   so no one conflates them.
3. **A labeled alignment set.** Assemble a small set of datapoints with **human** labels — some good,
   some deliberately broken (wrong base, nit-as-blocking, leaked answer, verdict/findings mismatch).
   Reuse the test fixtures (S-2/S-6/S-8 shapes) and G-1's own outputs as raw material.
4. **The alignment measurement.** Run the judge over the labeled set and compute agreement with the
   human labels (e.g. accuracy / per-criterion agreement). Iterate the judge's rubric/prompt until
   agreement clears a **stated bar**; **record the bar and the achieved number** in `eval/`. Do not
   silently pick a threshold — write it down (memory: don't manufacture).
5. **A trusted entry point** G-3 calls: `judge(datapoint, pr_context) -> QualityVerdict`, gated on a
   recorded alignment result at or above the bar **for the current rubric version** — raise a distinct
   error (not a silent pass) when no result is recorded, the result is below its bar, or it was measured
   against a different rubric version. Tie the alignment record to a rubric/judge version so changing
   the rubric or prompt forces a re-run before the judge is trusted again.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `eval/judge/rubric.py` | Create | The quality rubric (the named criteria) + `JUDGE_VERSION` |
| `eval/judge/models.py` | Create | `Datapoint`, `PrContext`, `CriterionVerdict`, `QualityVerdict`, `AlignmentResult` |
| `eval/judge/client.py` | Create | The `ModelClient` seam: `RecordedModelClient` (offline) + `LiveModelClient` (https-only, `EVAL_JUDGE_*`) |
| `eval/judge/datapoint.py` | Create | Load a built task directory into a gradeable `Datapoint` |
| `eval/judge/judge.py` | Create | `QualityJudge`: build the prompt, call the model, parse the reply (overall derived) |
| `eval/judge/gate.py` | Create | The alignment gate + the trusted `judge(...)` entry point G-3 calls |
| `eval/alignment/…` | Create | Human-labeled set (`labeled/*.json`), the agreement measurement + bar, and the recorded `alignment_result.json` |
| `tests/test_quality_judge.py`, `tests/test_alignment.py` | Create | Offline: recorded-reply parsing, a known-bad datapoint fails, the gate refuses an unaligned judge |
| `eval/README.md` | Modify | Document the judge, the alignment bar, and how to re-run alignment |

## Schemas & Contracts

- **`QualityVerdict`** (Pydantic): `{per_criterion: (CriterionVerdict...), overall_pass: bool,
  overall_score: float, notes: str}`, where a `CriterionVerdict` is `{name, passed: bool, score: float,
  rationale: str}` — one per rubric criterion, in rubric order. `overall_pass`/`overall_score` are
  **derived** from `per_criterion` (all pass; mean score), never taken from the model.
- **Alignment result** (persisted): `{n_labeled, agreement, bar, passed: bool, judge_version, model}`
  — the record that licenses trusting the judge; `judge_version` ties it to the rubric it was measured
  against, so a stale record for an older rubric no longer licenses the judge.

## How To Verify

- The judge, run on the labeled alignment set, meets or exceeds the stated agreement bar, and the bar
  + achieved number are recorded under `eval/`.
- Given a **recorded** datapoint (fixture, no live model), the judge's parsing/scoring logic returns a
  well-formed `QualityVerdict` — the wiring is testable offline even though the live judgment is not.
- `mise run check` stays offline/green; S-18 import scan still passes (`eval/` may import a model client; `src/` may not).

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_quality_verdict_parses_from_recorded_response` | judge maps a recorded LLM response → a well-formed `QualityVerdict` | A parsing/schema drift that silently drops criteria |
| `test_judge_flags_known_bad_datapoint` (recorded) | on a fixture labeled "nit-as-blocking", the judge (recorded response) returns `overall_pass=False` | A judge that rubber-stamps everything — the failure alignment exists to catch |
| `test_alignment_gate_enforced` | the trusted entry point refuses to be used unless an alignment result at/above the bar is recorded | Trusting an unaligned judge |

Do **not** write a test asserting a live model's exact wording.

## Acceptance Criteria

- [ ] A written quality rubric defines "good datapoint" as concrete, inspectable criteria
- [ ] An LLM judge scores a built datapoint against the rubric and returns a structured `QualityVerdict`; its docstring distinguishes it from the in-task verifier's judge
- [ ] A human-labeled alignment set exists (good + deliberately broken datapoints)
- [ ] Judge↔human agreement is measured against a **stated bar**, and both the bar and the achieved number are recorded under `eval/`
- [ ] The trusted `judge(...)` entry point is gated on a recorded alignment result meeting the bar
- [ ] `mise run check` stays offline/green and S-18 passes

## Out Of Scope

- Running the eval and aggregating pass rates / writing the report → **G-3**.
- Tool-use / trajectory metrics → **G-3**.
- Changing `verify`'s structural checks — the judge sits on top of them, it doesn't modify them.

## Notes & Gotchas

- **Align before you trust — that's the whole point of this ticket.** A judge that hasn't been checked
  against human labels is an opinion, not a measurement.
- **Two different judges.** The in-task verifier's judge scores the *agent-under-review's* findings
  against the oracle (part of the emitted Harbor task). *This* judge scores whether the *datapoint the
  eval agent built* is good. Keep them clearly separate.
- **Don't manufacture the bar** (memory; PRD §8) — state it and record what you hit.
- **Keep the model out of the package and gate** — the judge lives in `eval/`; `src/eval_harvest/` and
  `mise run check` stay model-free (confirm S-18).

## Dependencies

**Blocked by:** [G-1](G-1-eval-harness-harbor-task-microvm-runner-trajectory-capture.md) (produces the datapoints the judge reads).
**Blocks:** [G-3](G-3-tool-use-trajectory-metrics-and-eval-report.md)

