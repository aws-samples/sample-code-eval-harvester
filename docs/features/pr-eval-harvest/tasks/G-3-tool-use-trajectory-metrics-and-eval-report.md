---
id: G-3
title: "[Eval] Tool-use + trajectory metrics, eval report, and the §8 pass bar"
feature: pr-eval-harvest
workstream: Eval
status: todo
complexity: L
implements: []
user_story: US-1
blocked_by: [G-1, G-2]
blocks: []
---

# G-3: [Eval] Tool-use + trajectory metrics, eval report, and the §8 pass bar

## Context

G-1 produces per-trial trajectories and built datapoints; G-2 scores each datapoint's quality. This
ticket turns those into the **answer**: for each of the 2 objectives × k=3 trials, did the agent
solve the task, and *how well did it use the tool getting there?* Three things matter (from the eval
design): (1) datapoint quality — G-2's verdict; (2) **tool-use accuracy** — did the agent use the
right commands, loop or repeat needlessly, act on the CLI's refusals; (3) **trajectory read** —
does the trace actually show the tool solving the asked task. This ticket computes (2) and (3),
combines all three into per-objective **pass rates**, and writes the go/no-go report — and sets (or
explicitly leaves open) the PRD §8 target that decides whether the product's core bet holds.

**North star:** turn the eval runs into a defensible go/no-go on "the agent drives the CLI from its
help alone" — with a number, not a vibe.
**Implements:** none (eval scoring/report; operationalizes the PRD §8 metric).  ·  **US-1.**

## Design References

- **PRD:** `../prd.md` §8 (the success metric + its `[TODO: set target]` — straw man 80% first-attempt,
  100% after the agent acts on CLI feedback), §4/§9 (this is the go/no-go the bet turns on).
- **Tech plan:** `../tech-plan.md` §4 Risks row 1, §15 Definition of Done (the eval result must be
  recorded).
- **G-1** — the trajectory artifact shape (turns, tool calls + args + results + exit codes, produced
  task dir) this ticket reads.
- **G-2** — the trusted `judge(...)` entry point and `QualityVerdict` this ticket consumes.
- **`src/eval_harvest/verify.py`** (E-2) — run per built datapoint for the structural/leak gate that
  feeds the pass rule. `[TODO: verify path]` if the module name differs.

## What To Build

All under `eval/` (network + model allowed; not imported by `src/`).

1. **Tool-use metrics from the trajectory.** From G-1's captured trace, compute per-trial:
   which `eval-harvest` verbs were used and in what order, redundant/repeated calls, error→retry
   loops, whether the agent **acted on refusals** (a refusal followed by a corrected call, not a
   giveup), and a rough efficiency measure (tool calls / turns to a built datapoint). Define each
   metric precisely — this is the "tool accuracy" signal.
2. **A trajectory read of task completion.** Decide, per trial, whether the trace shows the task
   solved: a datapoint was produced, it passes `verify`, and G-2's judge passes it. Record *why* a
   trial failed — the first gate that failed, one of `no-datapoint` / `failed-verify` / `failed-quality`
   — so the report is diagnostic. (Tool-use metrics are reported alongside, but the pass gate is
   produced + valid + good *only*; tool misuse is a reported signal, not itself a failure reason.)
3. **Per-objective pass rates.** Combine over k=3: pass fraction per objective and overall, plus the
   tool-use summary. This is the eval's headline number.
4. **The report.** Write a run report under `eval/` (e.g. `eval/reports/<ts>.md`): objectives, pass
   rates, tool-use findings (did it build a rubric first? mislabel a nit? get base/change order right?
   act on refusals?), the judge's quality notes, and the **go/no-go read**. Persist the same
   `EvalReport` as `eval/reports/<ts>.json` beside the markdown, so a run can be re-scored or diffed.
5. **Set the PRD §8 target.** Either set the pass bar deliberately or record it as **still open** with
   the straw man — do **not** silently invent a number (memory; PRD §8). If the eval fails the bar,
   record that as a real outcome: v1 can ship as a working builder with a documented reason not to
   trust findings yet.
6. **Wire it into `mise run eval`.** After G-1 runs the trials and G-2 scores them, this stage emits
   the report. Still **outside** `mise run check`; skips cleanly without creds.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `eval/metrics/tool_use.py` | Create | `ToolUseMetrics` + `ToolUseAnalyzer` — pure tool-use metrics over the captured trace |
| `eval/report.py` | Create | The report models (`PerTrialResult`, `EvalReport`, `PassBar`), the pass rule behind injectable seams, the renderer + persistence |
| `eval/reports/…` | Create (output) | The written run reports — `<ts>.md` + `<ts>.json` (git-ignored, per `eval/README`) |
| `tests/test_eval_report.py` | Create | Offline: metrics from a recorded trajectory, the three-gate pass rule, pass-rate math, the open-target record |
| `eval/README.md` | Modify | Document the metrics, the pass rule, and where reports land |
| `mise.toml` | Modify | `eval` task runs G-1 → G-2 → G-3 report; still out of `check` |

## Schemas & Contracts

- **Per-trial result** (Pydantic): `{objective_id, trial_index, produced_datapoint: bool,
  verify_passed: bool, quality: QualityVerdict | None, tool_use: ToolUseMetrics, task_solved: bool,
  failure_reason: ("no-datapoint" | "failed-verify" | "failed-quality") | None}`. `quality` is `None`
  when the judge did not run (no datapoint, or `verify` failed) — scoring short-circuits before spending
  a model call.
- **Eval report** (persisted): per-objective + overall `pass_rate`, tool-use summary, the §8 bar
  (set or open), and the go/no-go read.

## How To Verify

- `mise run eval` (with creds) runs the full pipeline and writes a report with per-objective pass
  rates over k=3, a tool-use summary, and a go/no-go read.
- Given **recorded** trajectories + a **recorded** judge (no live model, no VM), the metrics and pass
  rule produce the expected per-trial results and pass rates — the scoring is testable offline.
- The report states the §8 bar as set-or-open with the straw man, never a silently-invented number.
- `mise run check` stays offline/green; S-18 passes.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_tool_use_metrics_from_recorded_trajectory` | metrics computed from a fixture trace match expected counts (repeats, refusal-then-fix) | Miscounting tool use — a wrong headline signal |
| `test_pass_rule_requires_all_three` | a trial with a produced datapoint that fails `verify` **or** fails quality is scored `task_solved=False` | A valid-but-wrong (or invalid) datapoint counted as a pass |
| `test_pass_rate_over_k_trials` | 2-of-3 passing trials → pass_rate 0.67 for that objective | Off-by-one / averaging errors in the headline number |
| `test_report_records_open_target_when_unset` | when the §8 bar isn't set, the report records it as open with the straw man | Silently inventing the go/no-go threshold |

## Acceptance Criteria

- [ ] Tool-use metrics (verb usage/order, repeats, retry loops, acted-on-refusals, efficiency) are computed per trial from G-1's trajectory
- [ ] Each trial gets a `task_solved` verdict requiring a produced datapoint that passes **both** `verify` and G-2's quality judge, with a recorded failure reason otherwise
- [ ] Per-objective and overall **pass rates** over k=3 are computed
- [ ] A run report records objectives, pass rates, tool-use findings, quality notes, and a go/no-go read
- [ ] The PRD §8 target is set, or recorded as still open with the straw man (not invented)
- [ ] `mise run eval` runs G-1 → G-2 → G-3 and writes the report; `mise run check` stays offline/green and S-18 passes

## Out Of Scope

- The harness / running the agent → **G-1**. The quality judge + its alignment → **G-2**.
- A full Harbor leaderboard scoring how well agents *review* PRs (PRD §10) — this scores whether the
  agent can *build a datapoint*, not review quality.
- Adding Kiro as a second agent-under-test (deferred).

## Notes & Gotchas

- **A pass needs all three: produced + valid (`verify`) + good (judge).** A structurally-valid
  datapoint about the wrong PR must not count — that's the "valid but wrong" trap the quality judge exists for.
- **Report the rate, not one run.** k=3 exists because a single stochastic run is a weak signal; the
  headline is the fraction, and the report should show the spread, not hide it.
- **A failed bar is a legitimate v1 outcome** (PRD §8) — record it honestly.
- **Don't manufacture the §8 target** (memory) — set it or leave it open with the straw man.
- **Keep the model/network out of the package and gate** — all of this is `eval/`-side (confirm S-18).

## Dependencies

**Blocked by:** [G-1](G-1-eval-harness-harbor-task-microvm-runner-trajectory-capture.md) (trajectories),
[G-2](G-2-datapoint-quality-llm-judge-and-alignment.md) (the trusted quality judge).
**Blocks:** nothing

