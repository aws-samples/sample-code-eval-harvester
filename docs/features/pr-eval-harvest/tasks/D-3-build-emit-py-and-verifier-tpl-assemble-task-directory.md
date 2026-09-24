---
id: D-3
title: "[Emission] Build emit.py + verifier_tpl/: assemble the task directory"
feature: pr-eval-harvest
workstream: Emission
status: done
complexity: L
implements: [FR-14, FR-16, FR-17, FR-18, FR-19, FR-21, FR-27, FR-28, FR-29, FR-30, FR-31, FR-32, FR-33, FR-40, NFR-1, NFR-8]
user_story: US-6
blocked_by: [D-2, B-3, C-1, C-2]
blocks: [E-2, F-1]
---

# D-3: [Emission] Build emit.py + verifier_tpl/: assemble the task directory

## Context

This is the integration point: `emit` turns a filled candidate into a self-contained Harbor task directory — a reject datapoint from a rejected iteration, an approve datapoint from the approved state. It sets the expected verdict from `--kind` (never a severity formula), enforces the coherence invariants (a reject needs ≥1 substantive finding; an approve carries no high finding), assembles the sealed Dockerfile, the change patch, the uniform `instruction.md`, `tests/` (judge + oracle + rubric), the verifier-only `solution/`, and `task.toml`. It also materializes the reward-object *shape* (ADR-5) into the in-task verifier. `emit` runs `verify` (E-2) before writing and refuses on failure — but E-2 depends on this, so this task builds `emit` against a stub `verify` hook that E-4 wires to the real one.

**North star:** the customer's output is the thing they needed — a task that runs under `harbor run` — not an intermediate format.
**Implements:** FR-14, FR-16, FR-17, FR-18, FR-19, FR-21, FR-27, FR-28–FR-33, FR-40, NFR-1, NFR-8  ·  **User story:** US-6

## Design References

- **Tech plan:** `../tech-plan.md` §8 — the emitted task directory layout and the `task.toml` shape (every field, `[metadata.harvest]`), and "How Harbor runs it" (reward.json keys)
- **Tech plan:** §9, the `eval-harvest emit` contract — reads/behaviour/writes/refusals/determinism, `--kind`, `--override`
- **Tech plan:** §7.2 — emit assembles in a temp dir, runs verify, promotes only on pass
- **Tech plan:** §5.4 and ADR-5 — the reward-object shape v1 ships (greedy match, coverage/precision keys, uncredited handling) and what it simplifies
- **The emit half — build against Harbor's source:** the verifier wrapper (`test.sh`), the separate-verifier `tests/Dockerfile`, the oracle `solution/solve.sh`, the set of files packaged into each task, and the dataset `metric.py` builder (`Harbor.metric_script`) land in `emit.py`/`harbor.py`/`verifier_tpl/`. The sealing Dockerfile must leave the agent no path to the fix, the review discussion, or any commit later than the base — the forbidden paths the seal check (a parallel Workstream E task) later enforces; see the tech plan's §7.2 sealing design.
- **The reward-object shape (ADR-5):** reproduce it at reduced sophistication — separate `coverage_*`/`precision_*` keys, `JUDGE_SCRUBBED_VARIABLES` scrubbing, a recorded-judge test hook — but **only the shape**, a greedy match, not a full one-to-one maximum-cardinality matcher. See §5.4/ADR-5 for what v1 simplifies and defers.

## What To Build

1. Create `src/eval_harvest/emit.py` and the `src/eval_harvest/verifier_tpl/` template directory.
2. Wire the `emit` verb (A-1 dispatcher): `emit <candidate> --kind reject|approve [--iteration <n>] [--clone <dir>] [--override <check>] [--json]`.
3. Read the filled candidate (B-3), `rubric.md` (C-1 version helper), `risk-map.toml` (C-2). Call `candidate.validate_filled` first; refuse with the FR-2 shape if slots are unfilled/incoherent.
4. **Select the iteration the datapoint is built from, and set the expected verdict from `--kind`** (reject⇒`block`, approve⇒`approve`) — never from a severity formula (FR-14). `emit` chooses the iteration `--kind` names (the first recoverable rejected iteration for a reject, the approved tip for an approve), reports which it chose and why (a human-readable `selection` note plus the `iteration_index`), and accepts `--iteration <n>` to override when the substantive review happened in a later round. Every oracle finding must have a comment whose `in_diff_iterations` includes the emitted iteration, or `emit` refuses `finding-iteration-mismatch` before writing (override-able with `--override finding-iteration-mismatch` for a deliberately reference-only-heavy datapoint) — a finding pointing at a line outside the emitted diff cannot be graded, so this refusal moves that discovery to the earliest verb that holds the fact. Enforce coherence: refuse `--kind reject` on a candidate with no substantive (non-nit, non-`reference_only`) finding → `empty-oracle` (exit 3, FR-18); refuse `--kind approve` on a candidate carrying any high-severity finding (FR-14). A nits-only PR (all findings low) emits an approve datapoint with the nits recorded non-blocking in `tests/` (FR-17).
5. Assemble the task directory in a **temp dir** (§7.2): `task.toml` (via `harbor.task_config_document` + `tomlw`, D-2/D-1), `instruction.md` (uniform text modulo declared slots, FR-33), `change.patch` (from the candidate's materialized `patch_path` — no git call — written **inside the environment build context** so the sealing Dockerfile's `COPY change.patch` resolves; the stored patch bytes are newline-terminated so `git apply` accepts them), `environment/Dockerfile` (the FR-30 sealing steps), `tests/` (`test.sh`, `score.py` from `verifier_tpl/`, `oracle.json`, `rubric.md`, `judge.toml` with `REWARDKIT_*` scrubbed) plus the separate-verifier `tests/Dockerfile`, and `solution/solve.sh` (verifier-only, FR-40).
6. Materialize the reward-shape verifier into `tests/score.py` from `verifier_tpl/` (ADR-5): deterministic path+line narrowing → a **greedy** one-to-one match → an LLM judge call for semantic equivalence (pinned config, `JUDGE_SCRUBBED_VARIABLES` scrubbed) → `reward.json` with `coverage_required`, `coverage_all`, `precision_strict`, `precision_adjudicated`, `credited_*`, per-severity counts. The empty denominator is answered differently per family, on purpose: **coverage** scores `1.0` over an empty oracle (nothing to miss), **precision** scores `0.0` on an empty submission (declining to review demonstrates no precision — silence must not score perfect). The two precision tiers are genuinely different measurements: `precision_adjudicated` divides by the judge-gated credited set, `precision_strict` divides by the deterministic location-overlap set (no judge), so `precision_strict ≥ precision_adjudicated` always and the gap is the credit the judge withheld — one shared matching loop drives both. Unmatched agent findings are counted **uncredited**, never false. The agent's submission is read from `/logs/artifacts/findings.json` — the one channel Harbor carries from the agent environment into the *separate* verifier — never `/logs/agent`, which never reaches `score.py`. The judge calls **Bedrock** signed with the MicroVM's IAM execution role (no API key): `judge.toml` pins `provider = "bedrock"`, a Haiku model, `temperature = 0`, and records `seed` without sending it (Bedrock's Converse API has no seed parameter). Build `Harbor.metric_script` in `harbor.py` (the dataset `metric.py` builder — F-1 calls it too) and ship a `metric.py` so aggregation is stated (§3/§8).
6a. The verifier-only `solution/solve.sh` carries the reference findings **inline** (substituted at emit time), not read from `tests/oracle.json`: in `separate` mode Harbor builds the verifier image from `tests/` and uploads nothing at runtime, so `/tests/` does not exist in the agent environment where the oracle solution runs. It writes its submission to the same `/logs/artifacts/findings.json` path `score.py` reads, so the reward stack is proven to respond to a known-good submission (a non-zero `coverage_all`, FR-40).
7. Record `[metadata.origin]` provenance (FR-29) and `[metadata.harvest]` fields including `change_risk_structural`, `change_risk_classified`, and `change_risk_disagreement=true` when they differ (FR-19, FR-21), `rubric_version` (FR-27), and `overrides` (non-empty only when `--override` used).
8. **Define and record the per-task content digest.** After the directory is assembled, compute a deterministic `sha256:`-prefixed digest over the task's canonical content (a stable, sorted enumeration of the emitted files and their bytes — the same set the byte-identical golden covers) and record it in `task.toml` `[metadata.harvest].content_digest`. This is the digest F-1 reads for the dataset manifest's `task_digests`; no other task defines it. Must match `harbor.py`'s `^sha256:[a-f0-9]{64}$` and be a pure function of the emitted bytes (NFR-1).
9. Set `environment.os = "linux"` and `verifier.environment.os = "linux"` regardless of host (NFR-8).
10. Call the `verify` hook on the temp dir, passing the local `--clone` through so the base/patch checks can run; promote to `tasks/<name>__<kind>/` only when nothing *failed*, or record the `--override <check>` in `task.toml` (FR-38 is wired fully in E-4 — build against a hook here). Report honestly which checks **passed**, which **failed** (a broken datapoint — exit 4, blocks the write), and which went **unresolved** for want of a clone or a container runtime (exit 5 — does not block, per §7.3). The success line must not claim "all checks passed" when a check went unresolved: a non-empty unresolved set means the datapoint is not yet verified, and the report names each unresolved check and the short reason (`base-and-patch: no clone provided`, `git-channel-absence: no container runtime`) so the state is diagnosable rather than silent.
11. Determinism (NFR-1): two emits from one unchanged candidate produce a byte-identical directory (golden file, S-14) — including a byte-stable `content_digest`.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/emit.py` | Create | Assemble + kind/verdict + coherence + promote |
| `src/eval_harvest/verifier_tpl/score.py` | Create | Reward-shape verifier template (data, not imported by the CLI) |
| `src/eval_harvest/verifier_tpl/test.sh` | Create | Entrypoint writing `/logs/verifier/reward.json` |
| `src/eval_harvest/verifier_tpl/instruction.md` | Create | Uniform instruction, declared slots |
| `src/eval_harvest/verifier_tpl/Dockerfile.tmpl` | Create | Sealing environment Dockerfile |
| `src/eval_harvest/verifier_tpl/solve.sh` | Create | Verifier-only oracle solution; reference findings inlined at emit time (FR-40) |
| `src/eval_harvest/verifier_tpl/judge.toml` | Create | Pinned Bedrock judge config (`REWARDKIT_*` scrubbed at run time) |
| `src/eval_harvest/cli.py` | Modify | Register the `emit` subcommand + `--iteration`/`--clone`/`--override` flags |
| `tests/test_emit.py` | Create | Cases from the test table |

Imports `harbor.py` (D-2), `tomlw.py` (D-1), `candidate.py` (B-3), `rubric.py` (C-1), `riskmap.py` (C-2).

## Schemas & Contracts

**Emitted directory (§8):**
```
<name>__<kind>/
  task.toml            # [metadata.harvest] carries kind, expected_verdict, blocking_severity="high",
  instruction.md       #   finding_severities, change_risk_structural/classified/disagreement, rubric_version, overrides
  environment/
    Dockerfile
    change.patch       # newline-terminated; lives inside the build context (environment/), and only there, so the Dockerfile's `COPY change.patch` resolves
  tests/{test.sh, score.py, oracle.json, rubric.md, judge.toml, Dockerfile}   # Dockerfile: the separate-verifier image
  solution/solve.sh    # verifier-only, absent from agent-visible env (FR-40)
```
**`reward.json` keys (ADR-5, §8):** `coverage_required`, `coverage_all`, `precision_strict`, `precision_adjudicated`, `credited_*`, per-severity counts. Unmatched agent findings → uncredited, not false. Coverage is `1.0` over an empty oracle; precision is `0.0` on an empty submission; `precision_strict` (location-overlap, judge-free) ≥ `precision_adjudicated` (judge-gated subset).

**Refusals:** `empty-oracle` (exit 3, FR-18); `--kind approve` + high finding (FR-14); `finding-iteration-mismatch` (exit 3, an oracle finding has no comment inside the emitted iteration's diff) unless `--override finding-iteration-mismatch`; a `verify` failure (exit 4) unless `--override`.

**Migration:** none. **Backward compatibility:** none — new verb.

## How To Verify

```bash
mise run test -- tests/test_emit.py
mise run lint
mise run typecheck
```

Then by hand: emit reject + approve from the S-1 squash-merge candidate; confirm the reject task is the round-1 state with the 3 findings as its oracle and the approve task is the round-2 state; `task.toml` is `separate` mode with `[metadata.origin]` and `os="linux"`; `solution/` is present but excluded from the agent-visible env; emit twice and `diff -r` the directories (empty).

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_verdict_follows_kind_not_severity` | `--kind reject`⇒`block`, `--kind approve`⇒`approve` in `task.toml` | A severity formula overriding the human verdict (FR-14) |
| `test_approve_with_high_finding_refused` | `--kind approve` + a high finding refuses | "Should this block?" and "what was wrong?" disagreeing in one datapoint (S-8, FR-14) |
| `test_reject_with_no_finding_refuses_empty_oracle` | `--kind reject` on a comment-less PR refuses `empty-oracle` (exit 3) | An empty-oracle reject datapoint shipped (S-6, FR-18) |
| `test_nits_only_emits_approve_nonblocking` | all-low findings → approve datapoint, nits non-blocking in `tests/` | A nit promoted to a blocking oracle (S-6, FR-17) |
| `test_risk_disagreement_recorded` | structural≠classified ⇒ both + `change_risk_disagreement=true` | The two-way risk signal collapsing to one (S-9, FR-19/FR-21) |
| `test_instruction_uniform_modulo_slots` | two datapoints' `instruction.md` differ only in declared slots | A verdict inferable from phrasing (S-11, FR-33) |
| `test_task_toml_separate_mode_linux` | `environment_mode="separate"`, `os="linux"`, `[metadata.origin]` present | A shared-mode/host-OS task failing at `harbor run` (S-10, FR-32/NFR-8) |
| `test_solution_excluded_from_agent_env` | `solution/solve.sh` present but outside the agent-visible tree | The oracle leaking to the evaluated agent (FR-40) |
| `test_emit_byte_identical_twice` | two emits from one candidate are byte-identical (golden dir) | Nondeterministic emission breaking Harbor's content hash (S-14, NFR-1) |
| `test_content_digest_recorded_and_stable` | `task.toml` carries a `sha256:`-form `content_digest` that is byte-stable across emits and changes when content changes | F-1's manifest missing/mismatching a `task_digests` entry (NFR-1) |
| `test_reward_shape_uncredited_not_false` | `score.py` counts an unmatched agent finding as uncredited (recorded judge) | Teaching the eval that a human-missed finding is a false positive (S-19, ADR-5) |
| `test_empty_submission_scores_zero_precision` | an empty agent submission scores `precision_* = 0.0`, coverage still `1.0` over an empty oracle | Silence scoring perfect precision — `nop` and a good submission producing identical rewards |
| `test_precision_tiers_are_distinct` | `precision_strict` (location-overlap) ≥ `precision_adjudicated` (judge-gated); they can differ | Two precision columns that always agree, implying a judge-robustness check never performed |
| `test_finding_iteration_mismatch_refused` | an oracle finding outside the emitted iteration's diff refuses `finding-iteration-mismatch` (override-able) | An ungradeable finding (points at a line not in the diff) shipping, discovered only at grade time |
| `test_iteration_override_selects_named_round` | `--iteration <n>` builds from the named round and records the `selection`/`iteration_index` | The wrong before-state built silently when the substantive review was a later round |
| `test_solve_sh_inlines_reference_findings` | `solution/solve.sh` carries the findings inline and writes to `/logs/artifacts/findings.json` | The oracle reading a `/tests/oracle.json` that does not exist in the agent environment (FR-40) |
| `test_emit_report_names_unresolved_checks` | the emit report distinguishes passed/failed/unresolved and never says "all passed" with an unresolved check | A false "all checks passed" on a machine with no clone or runtime |

## Acceptance Criteria

- [ ] `expected_verdict` follows `--kind`; approve+high-finding and reject+no-finding both refuse (FR-14, FR-18)
- [ ] Nits-only PR emits an approve datapoint with non-blocking nits (FR-17)
- [ ] Emitted dir has all parts incl. verifier-only `solution/`; `separate` mode; `os="linux"`; `[metadata.origin]`; sealing Dockerfile (FR-28–FR-32, FR-40, NFR-8)
- [ ] `instruction.md` uniform modulo declared slots (FR-33)
- [ ] Risk disagreement recorded, both values kept (FR-21); `rubric_version` stamped (FR-27)
- [ ] `reward.json` shape: coverage/precision keys + per-severity, unmatched=uncredited (ADR-5); coverage `1.0` over an empty oracle, precision `0.0` on an empty submission; `precision_strict ≥ precision_adjudicated`
- [ ] `emit` selects the iteration `--kind` names (with `--iteration <n>` override) and refuses `finding-iteration-mismatch` when a finding is outside the emitted diff
- [ ] `solution/solve.sh` inlines the reference findings and writes to `/logs/artifacts/findings.json` (the channel that reaches the separate verifier); the judge is pinned to Bedrock via the MicroVM IAM role (no API key)
- [ ] the emit report distinguishes passed/failed/unresolved checks and never claims "all passed" with an unresolved check
- [ ] Two emits from one candidate are byte-identical (NFR-1)
- [ ] `harbor.metric_script` built in `harbor.py`; a `metric.py` shipped (§3/§8)
- [ ] `task.toml` records a `sha256:`-form `content_digest` that is a byte-stable function of the emitted content (for F-1's manifest, NFR-1)
- [ ] `emit` runs the `verify` hook and refuses on failure unless `--override` (records it); full wiring in E-4
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- The actual leak/structural checks inside `verify` — that is E-2; here call a hook, wired to the real thing in E-4.
- Porting the full maximum-cardinality matcher, taxonomy-equivalence table, or severity aggregation/taste judge — explicitly deferred (ADR-5, PRD §10). Greedy match only.
- Running the emitted judge (no model in the tree; the judge runs under Harbor at eval time).
- The sealing *checklist* verification — E-1/E-2 verify the seal; this task *writes* the sealing Dockerfile.

## Notes & Gotchas

- **`solution/` and the answer must never reach the agent-visible tree (FR-40).** `solve.sh`, `oracle.json`, `judge.toml`, review text, PR number — all verifier-only. E-2's content scan treats these as "must be absent from agent-visible content"; if `emit` leaks the PR number into `instruction.md`, S-2 will (correctly) refuse.
- **Greedy match understates, which is the safe direction (ADR-5).** In rare multi-claim cases greedy credits fewer findings than the maximum matching — that under-credits the agent, never over-credits. Note this in `score.py`'s comments.
- **`JUDGE_SCRUBBED_VARIABLES`** must be scrubbed from `judge.toml` — a config that leaks reward-kit env vars is a hole. `test.sh` unsets `REWARDKIT_JUDGE`/`REWARDKIT_MODEL`/`REWARDKIT_TEMPERATURE` (and the `OPENAI_*` overrides) before running `score.py`, so the model pinned in `judge.toml` is the one that runs.
- **The submission channel is `/logs/artifacts` in `separate` mode.** Harbor uploads only the artifacts dir from the agent environment into the separate verifier; a submission under `/logs/agent` never reaches `score.py`, which would then grade an empty submission. `score.py`'s default agent-findings path, `solve.sh`, and `instruction.md` all name `/logs/artifacts/findings.json`.
- **The judge calls Bedrock, not OpenAI.** The verifier has no API key; it signs Bedrock Converse calls with the MicroVM's IAM execution role (region from `AWS_REGION`). `judge.toml` pins `provider = "bedrock"`, a Haiku model and `temperature = 0`; `seed` is recorded but not sent (Converse has no seed parameter).
- **The oracle solution is self-contained.** `solve.sh` inlines the reference findings at emit time rather than reading `tests/oracle.json`, because in `separate` mode `/tests/` is not present in the agent environment where the oracle runs.
- **Every reward key must be meaningful when averaged (§3):** Harbor's local path collapses to `Mean()`. Do not emit a key that is nonsense under a mean.
- **The verifier template files are data, not CLI code** — keep them under `verifier_tpl/` and ensure A-3's import scan treats them as data (they may contain "anthropic"/"judge" strings), not as a model import.
- **`emit` makes no git/network call (NFR-2):** it reads the candidate's materialized `change.patch` bytes, not the clone. E-4 adds the offline test.

## Dependencies

**Blocked by:** [D-2](D-2-port-harbor-py-constants-and-validators.md), [B-3](B-3-build-candidate-py-and-wire-capture-verb.md), [C-1](C-1-build-rubric-py-init-scaffold-convention-surfacing.md), [C-2](C-2-build-riskmap-py-parse-risk-map-compute-structural-risk.md)
**Blocks:** [E-2](E-2-build-verify-py-structural-checks-absence-scan-control-token.md), [F-1](F-1-build-dataset-py-manifest-and-distribution-report.md) (F-1 consumes the `content_digest` this task records and the `harbor.metric_script` it ports)

