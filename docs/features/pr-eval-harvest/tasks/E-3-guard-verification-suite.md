---
id: E-3
title: "[Verify] Guard-verification suite: break every US-7 invariant, watch it go red"
feature: pr-eval-harvest
workstream: Verification
status: done
complexity: M
implements: [FR-37, NFR-6]
user_story: US-7
blocked_by: [E-2]
blocks: []
---

# E-3: [Verify] Guard-verification suite: break every US-7 invariant, watch it go red

## Context

"A guard nobody has watched fail is decorative" (CLAUDE.md). Every US-7 check must have been watched to go red: the invariant broken deliberately, the check turning red, the break reverted, and that recorded. This task makes that discipline a shipped artefact — for each US-7 check it constructs the exact violation, asserts the check fails, asserts a corrected input passes, and generates a `guard-report` listing each check and the test that exercised it. It is not a new test *type*; it is why the S-2/S-3/S-4/S-8/S-9/S-12 scenarios are framed break-it-then-fix-it.

**North star:** the customer cannot tell a valid datapoint from an invalid one — so the guards that can, must be proven to actually fire.
**Implements:** FR-37, NFR-6  ·  **User story:** US-7

## Design References

- **Tech plan:** `../tech-plan.md` §2.1 FR-37 — "a committed test per check breaks the invariant and asserts red; a recorded artefact lists each check + the test that exercised it"
- **Tech plan:** §12, "Guard verification (FR-37)" paragraph — the discipline made a shipped artefact; the scenarios S-2/S-3/S-4/S-8/S-9/S-12 are the guard verifications
- **CLAUDE.md** — "Break the invariant deliberately, watch the check go red, revert. This applies to every validity check the PRD's US-7 describes."

## What To Build

1. Create `tests/test_guards.py` (or organise as a marked suite) covering every US-7 check with a break-then-fix pair:
   - **S-2 / answer-present (content leak):** emit with the PR number in the instruction slot → `verify` fails `answer-present`; remove → passes.
   - **S-3 / answer-present (git channel):** plant `objects/info/alternates` (and the variants: `packed-refs`, commit-graph, `refs/replace`, reflog-outside-reachability, `include.path`, bare `*.git/`) → fails; clean → passes.
   - **S-4 / scan-did-not-run:** remove the planted control token → `verify` refuses `scan-did-not-run`; restore → passes.
   - **S-8 / verdict-coherence:** `--kind approve` + high finding → `emit` refuses; drop the high finding → emits.
   - **S-9 / risk-disagreement:** structural≠classified → both recorded + `change_risk_disagreement=true`; align → single value.
   - **S-12 / preconditions:** four broken candidates (missing base, non-applying patch, finding on a missing line, incoherent rubric) → each its own specific failure; fix → passes.
2. Generate a **guard report** artefact (`docs/features/pr-eval-harvest/guard-report.md`): a table of each US-7 check, the scenario, the invariant it protects, and the test id that watched it fail. Drive it from a declared registry (`tests/guard_registry.py`) shared by the suite and a small renderer, so the report is a pure function of the registry and cannot drift — `test_guards.py` byte-compares the committed file against the renderer and fails with a pointer to re-generate.
3. Assert the "watched to fail" property structurally: each guard test must contain both the red assertion (violation ⇒ failure) and the green assertion (corrected ⇒ pass) — a test that only asserts green does not satisfy FR-37.
4. **The runtime arm of the same discipline — the gradeability guard (US-6, FR-40).** The offline guards above watch the *build-time* invariants fail; one guard can only be watched at *run time*, because it needs a datapoint that actually executes. An emitted datapoint must grade **differently** for a good submission than for silence, or it measures nothing. Two model-free agents Harbor ships prove it with no model spend: `--agent oracle` copies `solution/` in and runs `solve.sh` (it submits the reference findings, so it must score `coverage_all > 0` and exit 0), and `--agent nop` does nothing (it must credit nothing on every reward key, and the two reward objects must differ). This runs on demand via `mise run smoke-datapoint -- <task-dir>` against the Lambda MicroVMs environment — **not** part of `mise run check`, because it needs AWS and minutes. Missing credentials/Terraform must cause a **non-zero exit with a named reason**, never a skip — a silent pass here is the exact §4-risk-row-6 failure mode. The pure reward-contract logic is unit-tested offline (needs no AWS); the dispatch itself is proven by a live run. The harness that dispatches it belongs to the eval workstream (G); E-3 records this guard as the runtime member of the US-6/US-7 guard family.

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `tests/guard_registry.py` | Create | The single source of truth: a `GUARDS` tuple (one `GuardSpec` per US-7 invariant) plus a pure `render_guard_report()`. Kept free of pytest/git so the renderer imports the registry alone |
| `tests/test_guards.py` | Create | Break-then-fix pair per US-7 check; asserts the registry stays complete and the committed report matches the renderer |
| `scripts/gen_guard_report.py` | Create | Renders `guard-report.md` from `guard_registry.py` (run via `uv run`) |
| `docs/features/pr-eval-harvest/guard-report.md` | Create | Generated from the registry: check → scenario → invariant → test id |

Imports/exercises `emit.py` (D-3), `verify.py` (E-2), `seal.py` (E-1), the fixture builder.

## Schemas & Contracts

**Guard report row:**
```
| Check            | Scenario | Invariant it protects                     | Watched-to-fail test             |
|------------------|----------|-------------------------------------------|----------------------------------|
| answer-present   | S-2      | PR number absent from agent-visible files | test_guard_content_leak          |
| scan-did-not-run | S-4      | control token present in scan corpus      | test_guard_missing_control_token |
```

**Migration:** none. **Backward compatibility:** none.

## How To Verify

```bash
mise run test -- tests/test_guards.py
uv run scripts/gen_guard_report.py   # regenerates guard-report.md from the registry
mise run lint
```

Then by hand: open `guard-report.md` and confirm every US-7 check has a row naming a real test; temporarily weaken one guard in the code and confirm its guard test goes red (then revert).

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_guard_content_leak` | PR number in the instruction ⇒ `answer-present`; removed ⇒ pass | A leak guard that never actually fires (S-2, FR-37) |
| `test_guard_git_channel_leak` | each seal channel leak ⇒ fail; clean ⇒ pass | A git-channel guard that is decorative (S-3, FR-37) |
| `test_guard_missing_control_token` | control token removed ⇒ `scan-did-not-run`; restored ⇒ pass | A scan that can't prove it ran (S-4, FR-36/NFR-6) |
| `test_guard_verdict_coherence` | approve+high ⇒ refuse; corrected ⇒ emit | The coherence invariant not enforced (S-8) |
| `test_guard_risk_disagreement` | structural≠classified ⇒ both + marker; aligned ⇒ single | The disagreement silently resolved (S-9) |
| `test_guard_preconditions` | each of the four broken candidates ⇒ its own failure; fixed ⇒ pass | A precondition guard that passes broken input (S-12) |
| `test_guard_report_covers_every_us7_check` | the generated report has a row for every US-7 check | A check shipped without a watched-to-fail record (FR-37) |

## Acceptance Criteria

- [ ] Every US-7 check has a break-then-fix test that asserts both red (on violation) and green (on correction)
- [ ] `guard-report.md` is generated from the suite and lists every check → invariant → test id (FR-37)
- [ ] The report cannot silently omit a check (a test asserts coverage)
- [ ] All commands in "How To Verify" pass

## Out Of Scope

- Building the checks themselves — E-1/E-2/D-3 own them; this task exercises and records them.
- The NFR-2 offline test and `--override` recording — that is E-4.
- Running the eval or scoring — out of scope entirely (PRD §10).

## Notes & Gotchas

- **Red *and* green, both required (FR-37).** A guard test that only asserts the corrected input passes has not "watched it go red" — the whole point. Structure each as: assert failure on the violation, then assert pass on the fix.
- **Generate the report, don't hand-write it.** A hand-written list drifts from the tests; generate it so "the report says X is covered" is true by construction.
- **This is the discipline CLAUDE.md names twice** (the guard rule and the scanner-honesty rule). NFR-6's control token (S-4) is a guard here too.
- **The gradeability guard's honest contract is "`nop` credits nothing", not "`nop` is zero on every key".** `score.py` sets each `oracle_<sev>` key to the *count* of reference findings of that severity, so a correct `nop` reward still carries e.g. `oracle_high = 2.0`; `coverage_required` is `1.0` only when there is a high-severity finding to miss. Assert `oracle` scored coverage and exited cleanly, `nop` credited nothing (every key zero except the `oracle_*` reference sizes), and `oracle ≠ nop`. Comparing the two reward files for equality is the cheapest strong assertion — a whole failure class in one line. The reward-contract logic is a pure function so its unit tests need no AWS; the shipped form is `eval/smoke.py` (`SmokeContract` + `SmokeDatapoint`), `scripts/smoke-datapoint.sh`, the `mise run smoke-datapoint` task, and `tests/test_smoke_datapoint.py`.

## Dependencies

**Blocked by:** [E-2](E-2-build-verify-py-structural-checks-absence-scan-control-token.md)
**Blocks:** nothing

