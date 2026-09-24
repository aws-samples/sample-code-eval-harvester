---
id: E-4
title: "[Verify] Wire emit→verify refuse-before-write + NFR-2 offline test"
feature: pr-eval-harvest
workstream: Verification
status: todo
complexity: S
implements: [FR-38, NFR-2]
user_story: US-7
blocked_by: [E-2]
blocks: []
---

# E-4: [Verify] Wire emit→verify refuse-before-write + NFR-2 offline test

## Context

`emit` (D-3) assembles the task directory behind an injectable `verify` hook seam; this task wires that seam's default to the real `verify` (E-2) so a datapoint failing any check is not written — the loop that makes an unsupervised agent safe (§7.2). Overriding must be possible, explicit, and recorded in the datapoint (`--override <check>` → `[metadata.harvest].overrides`). It also lands the NFR-2 test that proves the answer-sealing path never reaches the forge — the guarantee ADR-4 rests on a test rather than a process boundary, which makes that test critical.

**North star:** a broken datapoint comes back as an instruction, not a silent artefact.
**Implements:** FR-38, NFR-2  ·  **User story:** US-7

## Design References

- **Tech plan:** `../tech-plan.md` §2.1 FR-38 — "`emit` runs `verify` and refuses on failure unless `--override <check>` is passed, which records the override in `task.toml`"
- **Tech plan:** §7.2 — emit assembles in temp, runs verify, promotes only on pass or with a recorded override
- **Tech plan:** §2.2 NFR-2 and ADR-4 — the sealing path makes no forge call; the offline-ness is enforced by a test, not a wire, so that test is critical (and an FR-37 watched-to-fail guard)
- **Tech plan:** §12, scenario S-15 — `emit`/`verify` succeed with `gh` and `git remote`/`git fetch` stubbed to fail

## What To Build

1. In `emit.py` (D-3), wire the default of the injectable `verify` hook seam to a real call to `Verify.verify_task(temp_task_dir, clone=clone)` (E-2): if it reports hard failures (exit 4), do **not** promote the temp dir — surface the FR-2 refusal and exit with `verify`'s code. Unresolved checks (exit 5) do **not** block promotion (see the note below the acceptance criteria).
2. Implement `--override <check>` (repeatable): if a named check fails and is overridden, promote anyway but record the override in `[metadata.harvest].overrides` (FR-38). An un-overridden failure still refuses. A real leak (`answer-present`) override must be loud — record it verbatim so the audit can find it (the `next` message already warns against overriding a real leak, §7.2).
3. Add the **NFR-2 offline test** (S-15): run `emit` and `verify` end-to-end with `gh` and `git remote`/`git fetch` stubbed to fail (e.g. a fake `gitcmd` that raises on any network op), and assert both succeed — proving neither touches the forge.
4. Make this NFR-2 test one of the FR-37 watched-to-fail guards: temporarily add a network call to the sealing path, confirm the test goes red, revert (record in E-3's guard report if practical).

## Files Affected

| Path | Change | Notes |
|------|--------|-------|
| `src/eval_harvest/emit.py` | Modify | Real `verify` call + `--override` recording |
| `tests/test_emit_verify_wiring.py` | Create | Refuse-before-write + override cases |
| `tests/test_offline_seal.py` | Create | NFR-2: emit/verify succeed with network stubbed to fail (S-15) |

Imports `verify.py` (E-2), `emit.py` (D-3).

## Schemas & Contracts

**`[metadata.harvest].overrides` (only non-empty when `--override` used):**
```toml
[metadata.harvest]
overrides = ["finding-line-absent"]   # each overridden check recorded verbatim
```
**Behaviour:** failing check + no override ⇒ refuse (exit 4/5), nothing written. Failing check + `--override <check>` ⇒ promote + record.

**Migration:** none. **Backward compatibility:** none — completes D-3's stub.

## How To Verify

```bash
mise run test -- tests/test_emit_verify_wiring.py tests/test_offline_seal.py
mise run lint
mise run typecheck
```

Then by hand: emit a candidate with a deliberately non-applying patch — confirm nothing is written and the refusal names `patch-does-not-apply`; re-run with `--override patch-does-not-apply` and confirm the task is written with `overrides = ["patch-does-not-apply"]` in `task.toml`.

## Tests To Write

| Test | What it verifies | Defect it catches |
|------|-----------------|------------------|
| `test_failing_verify_writes_nothing` | a datapoint that fails `verify` is not promoted; temp dir cleaned | A broken datapoint silently shipped (FR-38) |
| `test_override_records_and_promotes` | `--override <check>` promotes and records the check in `overrides` | An override that is silent or not auditable (FR-38) |
| `test_unoverridden_failure_still_refuses` | overriding check A does not suppress a different failing check B | An override widening past the one check it named |
| `test_emit_verify_succeed_offline` | emit + verify pass with `gh`/`git fetch`/remote stubbed to fail | The sealing path reaching the forge — the ADR-4 guarantee (S-15, NFR-2) |
| `test_offline_guard_watched_to_fail` | adding a network call to the sealing path makes the offline test go red | A regression that lets sealing touch the network going unnoticed (FR-37) |

## Acceptance Criteria

- [ ] `emit` runs `verify` before writing and refuses (exit 4/5), writing nothing, on failure (FR-38)
- [ ] `--override <check>` promotes and records the check in `[metadata.harvest].overrides`; an un-overridden failure still refuses (FR-38)
- [ ] `emit` and `verify` succeed with the network stubbed to fail (NFR-2, S-15)
- [ ] The offline guard has been watched to fail and reverted (FR-37)
- [ ] All commands in "How To Verify" pass

> **On "exit 4/5":** `emit` refuses on `verify`'s hard failures (exit 4 — a broken datapoint). It does
> **not** refuse on *unresolved* checks (exit 5): the git-channel checklist needs a materialized
> sealing container (deferred) and the base/patch checks need a clone, and tech-plan §7.3 says a
> runtime-absent check "contributes neither pass nor fail" — so a datapoint is still promoted once
> every check that *could* run passes. The standalone `eval-harvest verify` command still reports
> those as exit 5. This matches the emit-without-clone path D-3 exercises: an emit with no clone or
> seal must still succeed, promoting once every check that *can* run has passed.

## Out Of Scope

- The `verify` checks themselves (E-2) and the emit assembly (D-3).
- The full guard-report artefact — E-3; this task contributes the NFR-2 offline guard row.

## Notes & Gotchas

- **The offline guarantee rests on a test, not a wire (ADR-4).** That makes `test_emit_verify_succeed_offline` the whole guarantee — it must stub *every* network path (`gh`, `git fetch`, `git remote`), not just one. Route the stub through `gitcmd.py` so a new network call anywhere is caught.
- **Overriding a real leak must be loud (§7.2).** The `answer-present` refusal's `next` says "do not `--override` a real leak"; still allow it (FR-38 requires override to be possible) but record it verbatim so the §4 audit finds it.
- **Clean up the temp dir on refusal** — a failed emit must leave no partial task behind (§7.2 "a broken datapoint comes back as an instruction, not a silent artefact").

## Dependencies

**Blocked by:** [E-2](E-2-build-verify-py-structural-checks-absence-scan-control-token.md)
**Blocks:** nothing

